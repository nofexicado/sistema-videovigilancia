#!/usr/bin/env python3
"""
Sistema de Videovigilancia · sonda de inventario de camaras

Descubre camaras IP, prueba los juegos de credenciales y reporta que soporta
realmente cada una: fabricante, modelo, perfiles ONVIF, codec, resolucion,
fps, substream, PTZ y eventos. Para las camaras viejas sin ONVIF prueba
patrones RTSP conocidos por fabricante.

Requisitos: Python 3.11+ (solo biblioteca estandar) y ffprobe (paquete ffmpeg).
No instala nada ni escribe en las camaras: todas las llamadas son de lectura.

Uso:
    python3 probe.py --inventory ../inventory/cameras.csv -c credentials.json
    python3 probe.py --inventory ../inventory/cameras.csv --site "Sitio Norte" -c credentials.json
    python3 probe.py --range 192.0.2.0/24 -c credentials.json -o inventario.json
    python3 probe.py --discover
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import getpass
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constantes

ONVIF_DEVICE_PATHS = ["/onvif/device_service", "/onvif/services", "/onvif/Device"]
PROBE_PORTS = [554, 80, 8000, 8080]
HTTP_TIMEOUT = 6
FFPROBE_TIMEOUT = 18

NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"

# Patrones RTSP por fabricante, para equipos que no hablan ONVIF.
# El primero de cada lista suele ser el stream principal.
RTSP_PATTERNS: dict[str, list[str]] = {
    "axis": [
        "/axis-media/media.amp",
        "/axis-media/media.amp?videocodec=h264",
        "/axis-media/media.amp?camera=1&videocodec=h264&resolution=640x360",
        "/mpeg4/media.amp",
        "/mpeg4/1/media.amp",
    ],
    "sony": [
        "/media/video1",
        "/media/video2",
        "/video1",
    ],
    "bosch": [
        "/rtsp_tunnel",
        "/?h26x=4&line=1&inst=1",
        "/?h26x=4&line=1&inst=2",
        "/?inst=1",
    ],
    "genetec": [
        "/stream1",
        "/h264",
        "/media/video1",
    ],
    "generic": [
        "/stream1",
        "/live",
        "/video1",
        "/onvif1",
        "/media/video1",
        "/Streaming/Channels/101",
    ],
}


# ---------------------------------------------------------------------------
# Utilidades XML

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter_named(root: ET.Element, name: str):
    for el in root.iter():
        if _local(el.tag) == name:
            yield el


def _first_text(root: ET.Element, name: str, default: str | None = None) -> str | None:
    for el in _iter_named(root, name):
        if el.text:
            return el.text.strip()
    return default


# ---------------------------------------------------------------------------
# Cliente ONVIF minimo (SOAP crudo, sin dependencias)

class OnvifError(Exception):
    pass


class Onvif:
    """Cliente ONVIF de solo lectura.

    Usa WS-UsernameToken con digest. Toma la hora del propio equipo para
    calcular el token: si el reloj de la camara esta corrido -- el motivo mas
    comun de un 401 en ONVIF -- la autenticacion igual funciona.
    """

    def __init__(self, host: str, user: str, password: str):
        self.host = host
        self.user = user
        self.password = password
        self.device_url: str | None = None
        self.media_url: str | None = None
        self.ptz_url: str | None = None
        self.events_url: str | None = None
        self.time_offset = 0.0

    # -- transporte ---------------------------------------------------------

    def _security_header(self) -> str:
        nonce = secrets.token_bytes(16)
        created = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + self.time_offset, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        return (
            '<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/'
            '2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken>'
            f"<Username>{_esc(self.user)}</Username>"
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
            f"{base64.b64encode(digest).decode()}</Password>"
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
            f"{base64.b64encode(nonce).decode()}</Nonce>"
            '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
            "</UsernameToken></Security></s:Header>"
        )

    def _call(self, url: str, body: str, authenticated: bool = True) -> ET.Element:
        header = self._security_header() if authenticated else ""
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            f"{header}<s:Body>{body}</s:Body></s:Envelope>"
        ).encode()

        req = urllib.request.Request(
            url,
            data=envelope,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return ET.fromstring(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and authenticated:
                # Algunos equipos (varios Axis) piden HTTP Digest en vez de
                # WS-Security. Reintentamos por esa via.
                return self._call_http_auth(url, envelope)
            detail = ""
            try:
                detail = _first_text(ET.fromstring(exc.read()), "Text") or ""
            except Exception:
                pass
            raise OnvifError(f"HTTP {exc.code} {detail}".strip()) from exc
        except ET.ParseError as exc:
            raise OnvifError(f"respuesta no es XML: {exc}") from exc
        except Exception as exc:
            raise OnvifError(str(exc)) from exc

    def _call_http_auth(self, url: str, envelope: bytes) -> ET.Element:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, self.user, self.password)
        opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(mgr),
            urllib.request.HTTPBasicAuthHandler(mgr),
        )
        req = urllib.request.Request(
            url,
            data=envelope,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            method="POST",
        )
        try:
            with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                return ET.fromstring(resp.read())
        except Exception as exc:
            raise OnvifError(f"auth HTTP fallo: {exc}") from exc

    # -- llamadas -----------------------------------------------------------

    def connect(self) -> None:
        """Encuentra el endpoint del servicio de dispositivo y sincroniza el reloj."""
        last: Exception | None = None
        for path in ONVIF_DEVICE_PATHS:
            url = f"http://{self.host}{path}"
            try:
                # GetSystemDateAndTime no requiere autenticacion en la norma:
                # sirve para saber si hay ONVIF y para corregir el desfase horario.
                root = self._call(
                    url, f'<GetSystemDateAndTime xmlns="{NS_DEVICE}"/>', authenticated=False
                )
            except OnvifError as exc:
                last = exc
                continue

            self.device_url = url
            self._read_device_time(root)
            return
        raise OnvifError(f"sin servicio ONVIF ({last})")

    def _read_device_time(self, root: ET.Element) -> None:
        utc = None
        for el in _iter_named(root, "UTCDateTime"):
            utc = el
            break
        if utc is None:
            return
        try:
            fields = {}
            for name in ("Year", "Month", "Day", "Hour", "Minute", "Second"):
                value = _first_text(utc, name)
                if value is None:
                    return
                fields[name] = int(value)
            cam = datetime(
                fields["Year"], fields["Month"], fields["Day"],
                fields["Hour"], fields["Minute"], fields["Second"],
                tzinfo=timezone.utc,
            )
            self.time_offset = cam.timestamp() - datetime.now(timezone.utc).timestamp()
        except (TypeError, ValueError):
            self.time_offset = 0.0

    def device_information(self) -> dict[str, str | None]:
        root = self._call(self.device_url, f'<GetDeviceInformation xmlns="{NS_DEVICE}"/>')
        return {
            "manufacturer": _first_text(root, "Manufacturer"),
            "model": _first_text(root, "Model"),
            "firmware": _first_text(root, "FirmwareVersion"),
            "serial": _first_text(root, "SerialNumber"),
            "hardware": _first_text(root, "HardwareId"),
        }

    def capabilities(self) -> None:
        root = self._call(
            self.device_url,
            f'<GetCapabilities xmlns="{NS_DEVICE}"><Category>All</Category></GetCapabilities>',
        )
        for el in root.iter():
            name = _local(el.tag)
            if name not in ("Media", "PTZ", "Events"):
                continue
            xaddr = _first_text(el, "XAddr")
            if not xaddr:
                continue
            xaddr = self._rewrite_host(xaddr)
            if name == "Media":
                self.media_url = xaddr
            elif name == "PTZ":
                self.ptz_url = xaddr
            else:
                self.events_url = xaddr
        if not self.media_url:
            self.media_url = self.device_url

    def _rewrite_host(self, xaddr: str) -> str:
        """Algunos equipos publican su IP interna o 0.0.0.0 en el XAddr."""
        try:
            parsed = urllib.parse.urlsplit(xaddr)
        except Exception:
            return xaddr
        if parsed.hostname in (None, "0.0.0.0", "127.0.0.1"):
            return urllib.parse.urlunsplit(
                (parsed.scheme or "http", self.host, parsed.path, parsed.query, "")
            )
        return xaddr

    def profiles(self) -> list[dict]:
        root = self._call(self.media_url, f'<GetProfiles xmlns="{NS_MEDIA}"/>')
        out: list[dict] = []
        for node in _iter_named(root, "Profiles"):
            enc = None
            for el in _iter_named(node, "VideoEncoderConfiguration"):
                enc = el
                break
            width = height = fps = bitrate = None
            encoding = None
            if enc is not None:
                encoding = _first_text(enc, "Encoding")
                width = _int(_first_text(enc, "Width"))
                height = _int(_first_text(enc, "Height"))
                fps = _int(_first_text(enc, "FrameRateLimit"))
                bitrate = _int(_first_text(enc, "BitrateLimit"))
            out.append({
                "token": node.get("token"),
                "name": _first_text(node, "Name"),
                "encoding": encoding,
                "width": width,
                "height": height,
                "fps": fps,
                "bitrate_kbps": bitrate,
                "ptz": any(True for _ in _iter_named(node, "PTZConfiguration")),
            })
        return out

    def stream_uri(self, token: str) -> str | None:
        body = (
            f'<GetStreamUri xmlns="{NS_MEDIA}"><StreamSetup>'
            '<Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
            '<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport>'
            f"</StreamSetup><ProfileToken>{_esc(token)}</ProfileToken></GetStreamUri>"
        )
        root = self._call(self.media_url, body)
        return _first_text(root, "Uri")


def _int(value: str | None) -> int | None:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Descubrimiento

def ws_discover(timeout: float = 4.0) -> set[str]:
    """WS-Discovery por multicast. Solo alcanza la subred local del servidor."""
    message = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f"<e:Header><w:MessageID>uuid:{secrets.token_hex(16)}</w:MessageID>"
        "<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
        "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
        "</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>"
        "</d:Probe></e:Body></e:Envelope>"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(1.0)

    hosts: set[str] = set()
    try:
        sock.sendto(message, ("239.255.255.250", 3702))
        deadline = datetime.now().timestamp() + timeout
        while datetime.now().timestamp() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            hosts.add(addr[0])
            try:
                root = ET.fromstring(data)
                for el in _iter_named(root, "XAddrs"):
                    for xaddr in (el.text or "").split():
                        host = urllib.parse.urlsplit(xaddr).hostname
                        if host:
                            hosts.add(host)
            except ET.ParseError:
                pass
    finally:
        sock.close()
    return hosts


def tcp_open(host: str, port: int, timeout: float = 1.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def alive(host: str) -> list[int]:
    return [p for p in PROBE_PORTS if tcp_open(host, p)]


# ---------------------------------------------------------------------------
# ffprobe

_FFPROBE: str | None = None


def find_ffprobe() -> str | None:
    """Ubica ffprobe. En Windows winget lo instala fuera del PATH de la sesion
    actual, asi que ademas buscamos en las rutas habituales."""
    global _FFPROBE
    if _FFPROBE is not None:
        return _FFPROBE or None

    found = shutil.which("ffprobe")
    if not found and sys.platform == "win32":
        roots = [
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
            r"C:\ffmpeg\bin",
            os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin"),
        ]
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                if "ffprobe.exe" in files:
                    found = os.path.join(dirpath, "ffprobe.exe")
                    break
            if found:
                break

    _FFPROBE = found or ""
    return found


def find_ffmpeg() -> str | None:
    """ffmpeg vive junto a ffprobe."""
    probe = find_ffprobe()
    if not probe:
        return None
    candidate = os.path.join(os.path.dirname(probe),
                             "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    return candidate if os.path.exists(candidate) else shutil.which("ffmpeg")


def _bitrate_por_paquetes(url: str, seconds: int) -> int | None:
    """Suma el tamano de los paquetes de video que llegan en SEG segundos.

    Es mas fiel que remuxar a archivo (no cuenta encabezados de contenedor) y
    funciona con el rtsp_tunnel de las Bosch, que hace colgar a ffmpeg.
    """
    binary = find_ffprobe()
    if not binary:
        return None
    cmd = [
        binary, "-v", "error", "-rtsp_transport", "tcp",
        "-i", url, "-select_streams", "v:0",
        "-show_entries", "packet=size,pts_time",
        "-read_intervals", f"%+{seconds}",
        "-of", "json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=seconds + 25)
        packets = (json.loads(proc.stdout) or {}).get("packets") or []
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None
    if len(packets) < 5:
        return None

    total = 0
    times: list[float] = []
    for pkt in packets:
        try:
            total += int(pkt["size"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            times.append(float(pkt["pts_time"]))
        except (KeyError, TypeError, ValueError):
            pass

    if len(times) < 2 or total <= 0:
        return None
    span = max(times) - min(times)
    if span < 0.5:
        return None
    return int(total * 8 / span / 1000)


def measure_bitrate(url: str, seconds: int = 10) -> int | None:
    """Bitrate REAL: captura unos segundos sin recodificar y mide los bytes.

    ffprobe no puede reportar el bitrate de un RTSP en vivo (el contenedor no lo
    declara), y ese es justamente el numero que define cuanto disco hace falta.
    """
    # Primero con ffprobe contando paquetes: es el unico camino que funciona con
    # el rtsp_tunnel de las Bosch, donde ffmpeg se cuelga.
    by_packets = _bitrate_por_paquetes(url, seconds)
    if by_packets:
        return by_packets

    binary = find_ffmpeg()
    if not binary:
        return None
    # MPEG-TS y no MKV: acepta HEVC de Bosch y MPEG-4 de las Axis viejas sin
    # renegar por los parametros del codec. Agrega ~10% de encabezados, asi que
    # la cifra queda apenas por encima del bitrate puro de video.
    tmp = os.path.join(tempfile.gettempdir(), f"sistema-videovigilancia-{secrets.token_hex(6)}.ts")
    # -map 0:v:0 -an es imprescindible: las Bosch anuncian una pista de audio
    # que despues nunca envian, y ffmpeg se queda esperandola para siempre.
    # Ademas solo nos interesa el video para dimensionar el disco.
    cmd = [
        binary, "-v", "error", "-rtsp_transport", "tcp",
        "-i", url, "-t", str(seconds),
        "-an", "-c", "copy",
        "-f", "mpegts", "-y", tmp,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 20)
        if not os.path.exists(tmp):
            return None
        size = os.path.getsize(tmp)
        if size < 1024:
            return None
        # Duracion real capturada: casi nunca son los N segundos exactos porque
        # hay que esperar al primer keyframe.
        info = ffprobe_file(tmp)
        duration = info if info and info > 0.5 else float(seconds)
        return int(size * 8 / duration / 1000)
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def ffprobe_file(path: str) -> float | None:
    binary = find_ffprobe()
    if not binary:
        return None
    cmd = [binary, "-v", "error", "-show_format", "-of", "json", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (subprocess.TimeoutExpired, OSError, KeyError, ValueError,
            json.JSONDecodeError):
        return None


def have_ffprobe() -> bool:
    return find_ffprobe() is not None


def ffprobe_install_hint() -> str:
    if sys.platform == "win32":
        return "winget install Gyan.FFmpeg  (y abrir una terminal nueva)"
    if sys.platform == "darwin":
        return "brew install ffmpeg"
    return "sudo apt install ffmpeg"


def ffprobe(url: str, timeout: int = FFPROBE_TIMEOUT) -> dict | None:
    """Abre el stream de verdad y reporta lo que trae. La fuente de verdad."""
    binary = find_ffprobe()
    if not binary:
        return None
    cmd = [
        binary, "-v", "error",
        "-rtsp_transport", "tcp",
        "-i", url,
        "-select_streams", "v:0",
        "-show_streams", "-show_format",
        "-of", "json",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except (FileNotFoundError, OSError):
        return None
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    streams = data.get("streams") or []
    if not streams:
        return None
    video = streams[0]
    fps = None
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or ""
    if "/" in rate:
        num, _, den = rate.partition("/")
        try:
            if float(den):
                fps = round(float(num) / float(den), 1)
        except ValueError:
            pass
    raw_bitrate = _int(data.get("format", {}).get("bit_rate"))
    return {
        "codec": video.get("codec_name"),
        "profile": video.get("profile"),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": fps,
        "bitrate_kbps": raw_bitrate // 1000 if raw_bitrate else None,
    }


def rtsp_url(host: str, path: str, user: str, password: str) -> str:
    creds = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}@" if user else ""
    sep = "" if path.startswith("/") else "/"
    return f"rtsp://{creds}{host}:554{sep}{path}"


def redact(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.username:
        return url
    netloc = f"{parsed.username}:***@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


# ---------------------------------------------------------------------------
# Sondeo de un equipo

def probe_host(host: str, credentials: list[dict], deep: bool,
               expected: dict | None = None, bitrate_seconds: int = 0) -> dict:
    result: dict = {
        "host": host,
        "expected": expected or {},
        "ports": [],
        "onvif": False,
        "credential": None,
        "device": {},
        "clock_skew_s": None,
        "ptz": False,
        "events": False,
        "profiles": [],
        "streams": [],
        "notes": [],
    }

    result["ports"] = alive(host)
    if not result["ports"]:
        result["notes"].append("sin respuesta en 554/80/8000/8080")
        return result

    # --- via ONVIF -----------------------------------------------------------
    # connect() no lleva credenciales: se hace una vez y sirve para saber si el
    # equipo habla ONVIF y para medir el desfase de su reloj.
    device_url = None
    reference = Onvif(host, "", "")
    try:
        reference.connect()
        device_url = reference.device_url
        result["onvif"] = True
    except OnvifError as exc:
        result["notes"].append(f"onvif: {exc}")

    for cred in credentials if device_url else []:
        client = Onvif(host, cred["user"], cred["password"])
        client.device_url = device_url
        client.time_offset = reference.time_offset

        try:
            device = client.device_information()
        except OnvifError:
            continue  # credencial equivocada, probamos la siguiente

        result["credential"] = cred["name"]
        result["device"] = device
        result["clock_skew_s"] = round(client.time_offset)
        if abs(client.time_offset) > 120:
            result["notes"].append(
                f"reloj corrido {round(client.time_offset)}s -- sincronizar NTP"
            )

        try:
            client.capabilities()
            # Ojo: casi todas las camaras publican el servicio PTZ aunque no
            # tengan motor (PTZ digital). El servicio solo dice que la API
            # existe; el PTZ real se confirma contra el inventario.
            result["ptz_servicio"] = bool(client.ptz_url)
            result["events"] = bool(client.events_url)
        except OnvifError as exc:
            result["notes"].append(f"capabilities: {exc}")

        try:
            profiles = client.profiles()
        except OnvifError as exc:
            result["notes"].append(f"profiles: {exc}")
            profiles = []

        for profile in profiles:
            try:
                uri = client.stream_uri(profile["token"])
            except OnvifError:
                uri = None
            profile["uri"] = uri
            result["profiles"].append(profile)
            if profile.get("ptz"):
                result["ptz_perfil"] = True

            if uri and deep:
                authed = _inject_credentials(uri, cred["user"], cred["password"])
                measured = ffprobe(authed)
                if measured and bitrate_seconds:
                    measured["bitrate_real_kbps"] = measure_bitrate(authed, bitrate_seconds)
                result["streams"].append({
                    "source": "onvif",
                    "profile": profile["name"],
                    "url": redact(authed),
                    "ok": measured is not None,
                    "measured": measured,
                })
        # PTZ real = lo que dice el inventario. Si no hay inventario, caemos al
        # perfil (mas fiable que la sola presencia del servicio) y avisamos.
        if expected and "ptz_esperado" in expected:
            result["ptz"] = bool(expected["ptz_esperado"])
            if result.get("ptz_servicio") and not result["ptz"]:
                result["notes"].append(
                    "publica servicio PTZ pero la planilla la marca fija (PTZ digital)"
                )
        else:
            result["ptz"] = bool(result.get("ptz_perfil"))
        break
    else:
        if device_url:
            result["notes"].append("ONVIF presente pero ninguna credencial fue aceptada")

    # --- fallback RTSP por fabricante ---------------------------------------
    # Las Axis 221 / 233D son anteriores a ONVIF: la unica via es probar los
    # patrones de URL del fabricante. Si el equipo no dijo su marca, usamos la
    # que declara la planilla.
    if not result["streams"] and not deep:
        result["notes"].append("sin --deep: no se probaron los patrones RTSP")
    elif not result["streams"]:
        vendor = _guess_vendor(
            result["device"].get("manufacturer") or (expected or {}).get("marca")
        )
        patterns = RTSP_PATTERNS.get(vendor, []) + RTSP_PATTERNS["generic"]
        if not result["onvif"]:
            result["notes"].append(f"sin ONVIF: probando patrones RTSP de '{vendor}'")
        for cred in credentials:
            found = False
            for path in dict.fromkeys(patterns):
                if len(result["streams"]) >= 2:
                    break  # main + sub alcanzan; no hace falta agotar la lista
                url = rtsp_url(host, path, cred["user"], cred["password"])
                measured = ffprobe(url, timeout=7)
                if measured:
                    if bitrate_seconds:
                        measured["bitrate_real_kbps"] = measure_bitrate(url, bitrate_seconds)
                    result["credential"] = result["credential"] or cred["name"]
                    result["streams"].append({
                        "source": "rtsp",
                        "profile": path,
                        "url": redact(url),
                        "ok": True,
                        "measured": measured,
                    })
                    found = True
            if found:
                break

    if not result["streams"]:
        result["notes"].append("ningun stream de video pudo abrirse")

    # Reconciliacion contra la planilla: el equipo que contesta en esta IP,
    # es el que dice el inventario?
    want = (expected or {}).get("modelo")
    got = result["device"].get("model")
    if want and got:
        if _normalize_model(want) == _normalize_model(got):
            result["model_match"] = "ok"
        else:
            result["model_match"] = "distinto"
            result["notes"].append(
                f"la planilla dice '{want}' pero el equipo reporta '{got}'"
            )
    elif want:
        result["model_match"] = "sin_dato"

    want_sn = (expected or {}).get("serie")
    got_sn = result["device"].get("serial")
    if want_sn and got_sn:
        if _normalize_model(want_sn) == _normalize_model(got_sn):
            result["serial_match"] = "ok"
        else:
            result["serial_match"] = "distinto"
            result["notes"].append(
                f"numero de serie: planilla '{want_sn}' vs equipo '{got_sn}'"
            )
    return result


# Bosch publica por ONVIF el nombre comercial, mientras que la planilla usa el
# codigo de pedido. Son el mismo equipo; sin esta tabla el comparador da falsos
# positivos en casi todas las Bosch del parque.
ALIAS_MODELO = {
    "ndp7512z30": "autodomeipstarlight7000i",
    "ndp5512z30l": "autodomeipstarlight5000iir",
    "nbe6502al": "dinionipstarlight6000iir",
    "ndv3502f02": "flexidomeipmicro3000i",
    "nin70122f0a": "flexidomeippanoramic7000mp",
}


def _normalize_model(value: str) -> str:
    key = "".join(ch for ch in value.lower() if ch.isalnum())
    return ALIAS_MODELO.get(key, key)


def _inject_credentials(uri: str, user: str, password: str) -> str:
    parsed = urllib.parse.urlsplit(uri)
    if parsed.username or not user:
        return uri
    netloc = (
        f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}"
        f"@{parsed.hostname}"
    )
    if parsed.port:
        netloc += f":{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _guess_vendor(manufacturer: str | None) -> str:
    text = (manufacturer or "").lower()
    for vendor in ("axis", "sony", "bosch", "genetec"):
        if vendor in text:
            return vendor
    return "generic"


# ---------------------------------------------------------------------------
# Salida

def print_table(results: list[dict]) -> None:
    header = (
        f"{'IP':<16}{'NOMBRE':<17}{'FABRICANTE / MODELO':<30}{'CRED':<9}"
        f"{'ONVIF':<7}{'PRINCIPAL':<21}{'PTZ':<5}{'EVT':<5}"
    )
    print("\n" + header)
    print("-" * len(header))

    for res in sorted(results, key=lambda r: _ip_key(r["host"])):
        dev = res["device"]
        nombre = (res.get("expected") or {}).get("nombre") or "-"
        label = " ".join(filter(None, [dev.get("manufacturer"), dev.get("model")])) or "-"
        if res.get("model_match") == "distinto":
            label = "! " + label
        main = "-"
        working = [s for s in res["streams"] if s["ok"] and s.get("measured")]
        # Las Bosch publican siempre un tercer perfil MJPEG (JPEG_L1S3) que es
        # solo para capturas fijas. Elegir "el de mas pixeles" hacia que el
        # informe mostrara MJPEG como si fuera el stream principal. El stream
        # principal es el de video comprimido de mayor resolucion.
        video = [s for s in working if s["measured"].get("codec") not in ("mjpeg", "jpeg")]
        if video:
            working = video
        if working:
            best = max(
                working,
                key=lambda s: (s["measured"].get("width") or 0) * (s["measured"].get("height") or 0),
            )
            m = best["measured"]
            main = f"{m.get('codec') or '?'} {m.get('width')}x{m.get('height')}"
            if m.get("fps"):
                main += f" @{m['fps']:g}"
        print(
            f"{res['host']:<16}{nombre[:16]:<17}{label[:29]:<30}"
            f"{(res['credential'] or '-')[:8]:<9}"
            f"{('si' if res['onvif'] else 'no'):<7}{main[:20]:<21}"
            f"{('si' if res['ptz'] else '-'):<5}{('si' if res['events'] else '-'):<5}"
        )

    print()
    reachable = [r for r in results if r["ports"]]
    onvif = [r for r in results if r["onvif"]]
    streaming = [r for r in results if any(s["ok"] for s in r["streams"])]
    h265 = [
        r for r in results
        if any((s.get("measured") or {}).get("codec") in ("hevc", "h265") for s in r["streams"])
    ]
    print(f"  equipos que responden : {len(reachable)}")
    print(f"  con ONVIF             : {len(onvif)}")
    print(f"  con video abierto     : {len(streaming)}")
    print(f"  con H.265             : {len(h265)}")
    print(f"  con PTZ               : {sum(1 for r in results if r['ptz'])}")
    print(f"  con eventos ONVIF     : {sum(1 for r in results if r['events'])}")

    mismatched = [r for r in results if r.get("model_match") == "distinto"]
    if mismatched:
        print(f"\n  Modelo distinto al del inventario ({len(mismatched)}):")
        for res in mismatched:
            print(f"    {res['host']:<16} planilla: {res['expected'].get('modelo')}"
                  f"  ->  equipo: {res['device'].get('model')}")

    problems = [r for r in results if r["ports"] and not any(s["ok"] for s in r["streams"])]
    if problems:
        print("\n  Revisar a mano:")
        for res in problems:
            note = res["notes"][-1] if res["notes"] else "sin detalle"
            print(f"    {res['host']:<16} {note}")


def _ip_key(host: str):
    try:
        return (0, int(ipaddress.ip_address(host)))
    except ValueError:
        return (1, host)


# ---------------------------------------------------------------------------
# CLI

def load_inventory(path: str) -> list[tuple[str, dict]]:
    """Lee inventory/cameras.csv y devuelve (ip, datos esperados) por camara."""
    targets: list[tuple[str, dict]] = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ip = (row.get("ip") or "").strip()
            if not ip:
                continue
            targets.append((ip, {
                "nombre": (row.get("nombre") or "").strip(),
                "sitio": (row.get("sitio") or "").strip(),
                "marca": (row.get("marca") or "").strip(),
                "modelo": (row.get("modelo") or "").strip(),
                "serie": (row.get("serie") or "").strip(),
                "red": (row.get("red") or "").strip(),
                "ptz_esperado": (row.get("ptz") or "").strip() == "si",
            }))
    if not targets:
        raise SystemExit(f"{path} no tiene filas con IP")
    return targets


def load_credentials(path: str | None) -> list[dict]:
    if not path:
        return [{"name": "anonimo", "user": "", "password": ""}]
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    creds = []
    for i, item in enumerate(raw, 1):
        creds.append({
            "name": item.get("name") or f"cred-{i}",
            "user": item.get("user", ""),
            "password": item.get("password", ""),
        })
    if not creds:
        raise SystemExit(f"{path} no tiene credenciales")
    mode = os.stat(path).st_mode & 0o077
    if mode:
        print(f"aviso: {path} es legible por otros usuarios; chmod 600 {path}", file=sys.stderr)
    return creds


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventario real de las camaras IP: que soporta cada una."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--inventory",
                        help="CSV del inventario (inventory/cameras.csv): sondea esas IPs "
                             "y compara el modelo real contra el de la planilla")
    target.add_argument("--discover", action="store_true",
                        help="buscar por WS-Discovery en la subred local")
    target.add_argument("--range", help="rango CIDR a escanear, ej 192.0.2.0/24")
    target.add_argument("--hosts", help="lista de IPs separadas por coma")
    parser.add_argument("--site", help="con --inventory, sondear solo un sitio (ej 'Sitio Norte')")
    parser.add_argument("-c", "--credentials", help="archivo JSON con los juegos de usuario/clave")
    parser.add_argument("-u", "--user",
                        help="usar un solo usuario en vez del archivo JSON; "
                             "la clave se pide por teclado y no queda en el historial")
    parser.add_argument("--password",
                        help="clave para --user (si se omite, se pregunta)")
    parser.add_argument("-o", "--out", default="inventario.json", help="archivo JSON de salida")
    parser.add_argument("-j", "--jobs", type=int, default=16, help="sondeos en paralelo")
    parser.add_argument("--bitrate", type=int, default=0, metavar="SEG",
                        help="medir el bitrate real capturando SEG segundos de cada "
                             "stream (ej --bitrate 10). Es el dato que define "
                             "cuanto disco hace falta; suma tiempo al barrido")
    parser.add_argument("--no-deep", action="store_true",
                        help="no abrir los streams con ffprobe (mas rapido, menos certero)")
    args = parser.parse_args()

    deep = not args.no_deep
    if deep and not have_ffprobe():
        print(f"aviso: falta ffprobe, no se puede medir el video real.\n"
              f"       instalar con: {ffprobe_install_hint()}\n"
              f"       por ahora sigo solo con ONVIF (equivale a --no-deep).",
              file=sys.stderr)
        deep = False

    if args.user:
        password = args.password
        if password is None:
            password = getpass.getpass(f"Clave para '{args.user}': ")
        credentials = [{"name": args.user, "user": args.user, "password": password}]
    else:
        credentials = load_credentials(args.credentials)

    targets: list[tuple[str, dict]]
    if args.inventory:
        targets = load_inventory(args.inventory)
        if args.site:
            needle = args.site.lower()
            targets = [t for t in targets if needle in t[1].get("sitio", "").lower()]
            if not targets:
                raise SystemExit(f"ningun sitio coincide con '{args.site}'")
    elif args.discover:
        print("buscando camaras por WS-Discovery...", file=sys.stderr)
        found_hosts = sorted(ws_discover(), key=_ip_key)
        if not found_hosts:
            print("nadie respondio. Las camaras suelen estar en otra VLAN: "
                  "usa --inventory o --range con la subred de camaras.", file=sys.stderr)
            return 1
        targets = [(h, {}) for h in found_hosts]
    elif args.range:
        network = ipaddress.ip_network(args.range, strict=False)
        candidates = [str(ip) for ip in network.hosts()]
        print(f"escaneando {len(candidates)} direcciones en {network}...", file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
            found = pool.map(lambda ip: (ip, bool(alive(ip))), candidates)
        targets = [(ip, {}) for ip, up in found if up]
    else:
        targets = [(h.strip(), {}) for h in args.hosts.split(",") if h.strip()]

    print(f"sondeando {len(targets)} equipos con {len(credentials)} juego(s) de credenciales...",
          file=sys.stderr)

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(probe_host, host, credentials, deep, expected, args.bitrate): host
            for host, expected in targets
        }
        for future in concurrent.futures.as_completed(futures):
            host = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # una camara rota no debe frenar el barrido
                results.append({
                    "host": host, "expected": {}, "ports": [], "onvif": False,
                    "credential": None, "device": {}, "clock_skew_s": None,
                    "ptz": False, "events": False, "profiles": [], "streams": [],
                    "notes": [f"error interno: {exc}"],
                })

    print_table(results)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cameras": sorted(results, key=lambda r: _ip_key(r["host"])),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    print(f"\ninventario escrito en {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

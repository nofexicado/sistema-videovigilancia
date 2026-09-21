"""Ajuste de la calidad de video de una camara, sobre ONVIF Media.

Por que existe: el peso en disco de una camara ES el bitrate de su stream
base, y el bitrate lo decide la camara, no el servidor. Cambiarlo requeria
entrar al equipo por su interfaz web; ahora se hace desde Sistema de Videovigilancia, y con la
cuenta de cuanto disco cuesta ANTES de aplicarlo.

Sobre la estimacion: cuanto pesa un stream depende de la escena, no solo de
los numeros. Una pared quieta y un porton con camiones a la misma resolucion
no pesan igual. Por eso la estimacion se ancla en la MEDICION de esa misma
camara cuando existe (`streams.kbps`) y escala desde ahi; sin medicion previa
se cae a una constante del parque, que es peor pero honesta. Despues de
aplicar se vuelve a medir y el numero real reemplaza al estimado.
"""
from __future__ import annotations

import os
import re
import subprocess
import urllib.parse
from datetime import datetime, timezone

from . import archivo, crypto, db, grabador
from .onvif import ClienteONVIF, ErrorONVIF, buscar, texto, _esc

NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"

# Bits por pixel y por cuadro para una camara sin medicion propia. Sale del
# promedio del parque; es un orden de magnitud, no una promesa.
BPP_PARQUE = 0.07

# Cuanto se mide para saber el bitrate real. Menos de 10 s cae dentro de un
# solo GOP y da cualquier cosa.
SEGUNDOS_MEDICION = 15


class ErrorVideo(Exception):
    pass


# -- acceso a la camara -----------------------------------------------------

def _camara(camara_id: int) -> dict:
    with db.sesion() as con:
        fila = con.execute(
            """SELECT cam.id, cam.nombre, cam.ip, cam.stream_base_id,
                      cr.usuario, cr.secreto
                 FROM camaras cam
                 LEFT JOIN credenciales cr ON cr.id = cam.credencial_id
                WHERE cam.id = ?""", (camara_id,)).fetchone()
        if not fila:
            raise ErrorVideo("no existe esa camara")
        datos = dict(fila)
        if not datos["stream_base_id"]:
            raise ErrorVideo("la camara no tiene stream base cargado")
        base = con.execute(
            "SELECT * FROM streams WHERE id = ?", (datos["stream_base_id"],)).fetchone()
        if not base:
            raise ErrorVideo("el stream base no existe")
        datos["base"] = dict(base)
    if not datos["secreto"]:
        raise ErrorVideo("la camara no tiene credencial cargada")
    return datos


def _rechazar_si_no_es_onvif(datos: dict) -> None:
    """Corta temprano y con un mensaje util.

    No todas las camaras se ajustan por ONVIF: la Axis del repetidor lleva sus
    parametros en la propia URL RTSP (VAPIX) y su stream quedo cargado como
    `manual`. Intentar hablarle ONVIF ahi termina en un error de red que no le
    dice nada a nadie.
    """
    fuente = (datos["base"].get("fuente") or "").lower()
    if fuente in ("manual", "rtsp"):
        raise ErrorVideo(
            "esta camara no se ajusta por ONVIF: su stream esta cargado como "
            f"'{fuente}'. La calidad se cambia en la URL del stream o desde la "
            "interfaz de la camara.")


def _cliente(datos: dict) -> tuple[ClienteONVIF, str]:
    """Cliente ONVIF conectado y la direccion del servicio de medios.

    `capacidades()` del cliente de eventos solo pide la categoria Events, asi
    que la de Media se resuelve aca.
    """
    cli = ClienteONVIF(datos["ip"], datos["usuario"], crypto.descifrar(datos["secreto"]))
    cli.conectar()
    raiz = cli.llamar(
        cli.url_device,
        f'<GetCapabilities xmlns="{NS_DEVICE}"><Category>Media</Category></GetCapabilities>')
    for el in buscar(raiz, "Media"):
        xaddr = texto(el, "XAddr")
        if xaddr:
            return cli, cli._reescribir(xaddr)
    raise ErrorVideo("la camara no expone servicio de medios")


def _perfiles(cli: ClienteONVIF, url_media: str) -> list[str]:
    raiz = cli.llamar(url_media, f'<GetProfiles xmlns="{NS_MEDIA}"/>')
    return [el.get("token") for el in buscar(raiz, "Profiles") if el.get("token")]


def _uri_de(cli: ClienteONVIF, url_media: str, token: str) -> str | None:
    raiz = cli.llamar(
        url_media,
        f'<GetStreamUri xmlns="{NS_MEDIA}"><StreamSetup>'
        '<Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
        '<Transport xmlns="http://www.onvif.org/ver10/schema">'
        '<Protocol>RTSP</Protocol></Transport></StreamSetup>'
        f"<ProfileToken>{_esc(token)}</ProfileToken></GetStreamUri>")
    return texto(raiz, "Uri")


def _huella(url: str) -> str:
    """Ruta + consulta de una URL RTSP, sin credenciales ni host.

    Sirve para comparar la URL guardada con la que devuelve la camara: la
    guardada puede llevar usuario y clave incrustados y la de la camara no.
    """
    if not url:
        return ""
    sin_cred = re.sub(r"//[^/@]*@", "//", url)
    p = urllib.parse.urlsplit(sin_cred)
    return f"{p.path}?{p.query}".rstrip("?")


def _token_perfil(cli: ClienteONVIF, url_media: str, base: dict,
                  stream_id: int) -> str:
    """El token del perfil ONVIF del stream base.

    El relevamiento nunca guardo este token -- 61 de 67 streams lo tenian en
    NULL -- asi que hay que deducirlo, y una vez deducido se guarda para no
    volver a preguntar:

    1. Muchas URL lo llevan puesto (`...?token=media_profile2`). Gratis.
    2. Si no, se le pregunta a la camara por cada perfil y se compara la URI
       que devuelve contra la guardada. Es lo que salva a las Bosch, cuyas URL
       son `rtsp_tunnel?p=1&inst=2...` y no nombran ningun token.
    """
    if base.get("token"):
        return base["token"]

    encontrado = None
    m = re.search(r"[?&]token=([^&]+)", base.get("url") or "")
    if m:
        encontrado = urllib.parse.unquote(m.group(1))
    else:
        objetivo = _huella(base.get("url") or "")
        for token in _perfiles(cli, url_media):
            try:
                uri = _uri_de(cli, url_media, token)
            except Exception:
                continue
            if uri and _huella(uri) == objetivo:
                encontrado = token
                break

    if not encontrado:
        raise ErrorVideo(
            "no se pudo identificar el perfil ONVIF de esta camara: su stream "
            "no coincide con ninguno de los que publica. Si la camara se "
            "configura por URL (Axis con VAPIX, por ejemplo) hay que cambiarla "
            "desde su propia interfaz.")

    # Guardarlo es sólo para no volver a deducirlo: si la base esta ocupada,
    # se sigue igual y se resuelve de nuevo la proxima vez. Que una
    # optimizacion voltee la consulta seria absurdo.
    try:
        with db.sesion() as con:
            con.execute("UPDATE streams SET token = ? WHERE id = ?",
                        (encontrado, stream_id))
    except Exception:
        pass
    return encontrado


def _encoder_del_perfil(cli: ClienteONVIF, url_media: str, token_perfil: str) -> dict:
    """Token y valores actuales del encoder colgado de ese perfil."""
    raiz = cli.llamar(url_media,
                      f'<GetProfile xmlns="{NS_MEDIA}">'
                      f"<ProfileToken>{_esc(token_perfil)}</ProfileToken></GetProfile>")
    for el in buscar(raiz, "VideoEncoderConfiguration"):
        return {
            "token": el.get("token"),
            "codec": texto(el, "Encoding"),
            "ancho": _entero(texto(el, "Width")),
            "alto": _entero(texto(el, "Height")),
            "fps": _entero(texto(el, "FrameRateLimit")),
            "calidad": _numero(texto(el, "Quality")),
            "techo_kbps": _entero(texto(el, "BitrateLimit")),
            "gov": _entero(texto(el, "GovLength")),
            "perfil_h264": texto(el, "H264Profile") or "High",
        }
    raise ErrorVideo("el perfil no tiene encoder de video asignado")


def _opciones(cli: ClienteONVIF, url_media: str, token_conf: str,
              token_perfil: str) -> dict:
    raiz = cli.llamar(url_media,
                      f'<GetVideoEncoderConfigurationOptions xmlns="{NS_MEDIA}">'
                      f"<ConfigurationToken>{_esc(token_conf)}</ConfigurationToken>"
                      f"<ProfileToken>{_esc(token_perfil)}</ProfileToken>"
                      "</GetVideoEncoderConfigurationOptions>")
    calidad_min, calidad_max = 1, 10
    for el in buscar(raiz, "QualityRange"):
        calidad_min = _entero(texto(el, "Min")) or 1
        calidad_max = _entero(texto(el, "Max")) or 10
        break

    resoluciones: list[dict] = []
    fps_min, fps_max = 1, 30
    for h264 in buscar(raiz, "H264"):
        for r in buscar(h264, "ResolutionsAvailable"):
            a, b = _entero(texto(r, "Width")), _entero(texto(r, "Height"))
            if a and b and {"ancho": a, "alto": b} not in resoluciones:
                resoluciones.append({"ancho": a, "alto": b})
        for r in buscar(h264, "FrameRateRange"):
            fps_min = _entero(texto(r, "Min")) or 1
            fps_max = _entero(texto(r, "Max")) or 30
            break
        break
    resoluciones.sort(key=lambda r: r["ancho"] * r["alto"])
    return {"resoluciones": resoluciones, "fps_min": fps_min, "fps_max": fps_max,
            "calidad_min": calidad_min, "calidad_max": calidad_max}


def _entero(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _numero(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# -- lo que consume la API --------------------------------------------------

def leer(camara_id: int) -> dict:
    """Estado actual del stream base y que valores admite la camara."""
    datos = _camara(camara_id)
    _rechazar_si_no_es_onvif(datos)
    cli, url_media = _cliente(datos)
    token_perfil = _token_perfil(cli, url_media, datos["base"],
                                 datos["stream_base_id"])
    enc = _encoder_del_perfil(cli, url_media, token_perfil)
    opts = _opciones(cli, url_media, enc["token"], token_perfil)
    # El ancla de la estimacion, por orden de confianza:
    #   1. lo que esta camara ESCRIBIO en disco en las ultimas 24 h. Es el dato
    #      mas honesto que existe -- son los bytes reales con el perfil actual --
    #      y no cuesta abrir una sesion RTSP.
    #   2. `streams.kbps`, que puede venir del relevamiento y estar viejo o ser
    #      de otro perfil. Se usa solo si no hay grabacion suficiente.
    #
    # La ventana del archivo se acota a lo grabado DESPUES de la ultima
    # medicion: si se acaba de cambiar el perfil, el promedio de 24 h todavia
    # describe el perfil viejo y daria un numero que ya no es cierto. Cuando
    # todavia no hay grabacion suficiente con la configuracion nueva, manda la
    # medicion directa, que es corta pero actual.
    real = archivo.ritmo_camara(camara_id, desde=datos["base"]["medido"])
    if real["confiable"]:
        medido, origen, cuando = real["kbps"], "archivo", None
    else:
        medido, origen = datos["base"]["kbps"], "medicion"
        cuando = datos["base"]["medido"]
    return {
        "camara_id": camara_id,
        "nombre": datos["nombre"],
        "perfil": token_perfil,
        "actual": enc,
        "opciones": opts,
        "medido_kbps": medido,
        "medido_en": cuando,
        "medido_origen": origen,
        "grabado": real,
    }


def estimar_kbps(ancho: int, alto: int, fps: int, calidad: float,
                 ancla: dict | None = None) -> float:
    """Bitrate estimado. Con `ancla` (una medicion real de ESA camara) escala
    desde ella; sin ancla usa la constante del parque."""
    pixeles = ancho * alto * max(fps, 1)
    if ancla and ancla.get("kbps") and ancla.get("ancho"):
        base = ancla["ancho"] * ancla["alto"] * max(ancla["fps"] or 1, 1)
        if base > 0:
            escala = pixeles / base
            # La calidad de ONVIF (1..10) no es lineal en bitrate; el exponente
            # amortigua para no prometer saltos que la camara no hace.
            if ancla.get("calidad"):
                escala *= (calidad / ancla["calidad"]) ** 0.8
            return ancla["kbps"] * escala
    return pixeles * BPP_PARQUE / 1000.0


def aplicar(camara_id: int, ancho: int, alto: int, fps: int,
            calidad: float, techo_kbps: int | None = None) -> dict:
    """Escribe el encoder en la camara y actualiza el stream en la base."""
    datos = _camara(camara_id)
    _rechazar_si_no_es_onvif(datos)
    cli, url_media = _cliente(datos)
    token_perfil = _token_perfil(cli, url_media, datos["base"],
                                 datos["stream_base_id"])
    enc = _encoder_del_perfil(cli, url_media, token_perfil)

    # El HLS del muro corta cada 2 s y necesita un keyframe ahi: un GOP MAS
    # LARGO que eso alarga los segmentos. Uno mas corto no molesta (cuesta un
    # poco de bitrate, pero es la eleccion que ya tenia la camara). Asi que
    # solo se acorta cuando hace falta, en vez de imponer un valor.
    tope_gov = max(1, fps * 2)
    gov = min(enc["gov"] or tope_gov, tope_gov)
    # El techo de bitrate NO se toca salvo que lo pidan expresamente: se
    # conserva el que la camara ya tenia. Calcularlo solo fue un error caro --
    # en estas Sony el techo estaba en 64 kbps y las mantenia en ~108 kbps
    # reales; recalcularlo lo subio a 2940 y la camara paso de 1 a 19 GB/dia
    # por un cambio de resolucion que el usuario creyo menor. Es un parametro
    # que la interfaz no muestra: cambiarlo por atras es cambiar algo que nadie
    # pidio.
    techo = techo_kbps or enc["techo_kbps"] or 4000
    cuerpo = (
        f'<trt:SetVideoEncoderConfiguration xmlns:trt="{NS_MEDIA}" '
        f'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f'<trt:Configuration token="{_esc(enc["token"])}">'
        f'<tt:Name>{_esc(enc["token"])}</tt:Name><tt:UseCount>1</tt:UseCount>'
        f"<tt:Encoding>H264</tt:Encoding>"
        f"<tt:Resolution><tt:Width>{ancho}</tt:Width><tt:Height>{alto}</tt:Height></tt:Resolution>"
        f"<tt:Quality>{calidad}</tt:Quality>"
        f"<tt:RateControl><tt:FrameRateLimit>{fps}</tt:FrameRateLimit>"
        f"<tt:EncodingInterval>1</tt:EncodingInterval>"
        f"<tt:BitrateLimit>{techo}</tt:BitrateLimit></tt:RateControl>"
        f"<tt:H264><tt:GovLength>{gov}</tt:GovLength>"
        f'<tt:H264Profile>{_esc(enc["perfil_h264"])}</tt:H264Profile></tt:H264>'
        f"<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type>"
        f"<tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>"
        f"<tt:Port>63000</tt:Port><tt:TTL>1</tt:TTL>"
        f"<tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
        f"<tt:SessionTimeout>PT1M</tt:SessionTimeout>"
        f"</trt:Configuration>"
        f"<trt:ForcePersistence>true</trt:ForcePersistence>"
        f"</trt:SetVideoEncoderConfiguration>"
    )
    try:
        cli.llamar(url_media, cuerpo)
    except ErrorONVIF as exc:
        raise ErrorVideo(f"la camara rechazo el cambio: {exc}") from exc

    with db.sesion() as con:
        con.execute(
            """UPDATE streams SET ancho = ?, alto = ?, fps = ?, kbps = NULL,
                                  medido = NULL WHERE id = ?""",
            (ancho, alto, float(fps), datos["stream_base_id"]))

    # El ffmpeg que estaba corriendo sigue con el stream viejo hasta que se lo
    # relanza: se reinicia SOLO esta camara, no el servicio.
    grabador.grabador.detener(camara_id)
    grabador.grabador.iniciar(camara_id)
    return {"camara_id": camara_id, "ancho": ancho, "alto": alto,
            "fps": fps, "calidad": calidad, "techo_kbps": techo}


def _ancla(datos: dict) -> dict | None:
    b = datos["base"]
    if not b.get("kbps"):
        return None
    return {"kbps": b["kbps"], "ancho": b["ancho"], "alto": b["alto"],
            "fps": b["fps"], "calidad": None}


def medir(camara_id: int, segundos: int = SEGUNDOS_MEDICION) -> dict:
    """Abre UNA sesion RTSP y mide lo que la camara entrega de verdad."""
    binario = grabador._ffmpeg()
    if not binario:
        raise ErrorVideo("falta ffmpeg en el sistema")
    datos = _camara(camara_id)
    url = grabador._url_con_credenciales(
        datos["base"]["url"], datos["usuario"], crypto.descifrar(datos["secreto"]))
    try:
        p = subprocess.run(
            [binario, "-hide_banner", "-y", "-rtsp_transport", "tcp", "-i", url,
             "-t", str(segundos), "-c", "copy", "-f", "mpegts",
             "NUL" if os.name == "nt" else "/dev/null"],
            capture_output=True, text=True, timeout=segundos + 45)
    except subprocess.TimeoutExpired as exc:
        raise ErrorVideo("la camara no entrego video a tiempo") from exc

    m = re.search(r"video:(\d+)(?:KiB|kB)", p.stderr)
    if not m:
        raise ErrorVideo("no se pudo medir: la camara no entrego video")
    kbps = int(m.group(1)) * 8 / segundos
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.sesion() as con:
        con.execute("UPDATE streams SET kbps = ?, medido = ? WHERE id = ?",
                    (int(kbps), ahora, datos["stream_base_id"]))
    return {"camara_id": camara_id, "kbps": round(kbps),
            "gb_dia": round(kbps * 86400 / 8 / 1024 / 1024, 2), "medido": ahora}

"""Cliente ONVIF minimo para eventos.

SOAP crudo sobre urllib: sin dependencias y con control total del manejo de
errores, que con camaras de tres fabricantes y quince anios de diferencia entre
modelos es lo que mas hace falta.

Usa WS-UsernameToken con digest, calculado sobre la hora que declara la camara.
Varios equipos del parque tienen el reloj corrido (uno 33 dias); si el token se
calculara con la hora del servidor, la autenticacion fallaria en todos ellos.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_EVENTS = "http://www.onvif.org/ver10/events/wsdl"
NS_WSN = "http://docs.oasis-open.org/wsn/bw-2"

RUTAS_DEVICE = ["/onvif/device_service", "/onvif/services", "/onvif/Device"]
TIMEOUT = 10

# Acciones WS-Addressing de las llamadas contra la suscripcion.
#
# Las Sony del parque rechazan PullMessages con "Argument Value Invalid" si el
# pedido no lleva estas cabeceras -- y el rechazo es identico con cualquier
# combinacion de Timeout y MessageLimit, lo que despista bastante. Las Bosch
# aceptan las dos formas, asi que se mandan siempre.
ACCION_PULL = f"{NS_EVENTS}/PullPointSubscription/PullMessagesRequest"
ACCION_RENEW = f"{NS_WSN}/SubscriptionManager/RenewRequest"
ACCION_UNSUBSCRIBE = f"{NS_WSN}/SubscriptionManager/UnsubscribeRequest"


class ErrorONVIF(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def buscar(raiz: ET.Element, nombre: str):
    for el in raiz.iter():
        if _local(el.tag) == nombre:
            yield el


def texto(raiz: ET.Element, nombre: str, defecto=None):
    for el in buscar(raiz, nombre):
        if el.text:
            return el.text.strip()
    return defecto


def _esc(v: str) -> str:
    return (v.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


class ClienteONVIF:
    def __init__(self, host: str, usuario: str, clave: str):
        self.host = host
        self.usuario = usuario
        self.clave = clave
        self.url_device: str | None = None
        self.url_events: str | None = None
        self.desfase = 0.0

    # -- transporte ---------------------------------------------------------

    def _cabecera(self) -> str:
        nonce = secrets.token_bytes(16)
        creado = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + self.desfase, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = hashlib.sha1(nonce + creado.encode() + self.clave.encode()).digest()
        return (
            '<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/'
            '2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken>'
            f"<Username>{_esc(self.usuario)}</Username>"
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
            f"{base64.b64encode(digest).decode()}</Password>"
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
            f"{base64.b64encode(nonce).decode()}</Nonce>"
            '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-wss-wssecurity-utility-1.0.xsd">{creado}</Created>'
            "</UsernameToken></Security></s:Header>"
        )

    def llamar(self, url: str, cuerpo: str, autenticado: bool = True,
               timeout: int = TIMEOUT, accion: str | None = None) -> ET.Element:
        seguridad = self._cabecera() if autenticado else ""
        if accion:
            # Se fusionan las dos cabeceras en un unico <s:Header>: WS-Addressing
            # primero y el UsernameToken despues.
            interno = seguridad.replace("<s:Header>", "").replace("</s:Header>", "")
            cabecera = (
                "<s:Header>"
                f"<a:Action>{accion}</a:Action>"
                f"<a:To>{_esc(url)}</a:To>"
                f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
                "<a:ReplyTo><a:Address>"
                "http://www.w3.org/2005/08/addressing/anonymous"
                "</a:Address></a:ReplyTo>"
                f"{interno}</s:Header>"
            )
        else:
            cabecera = seguridad

        sobre = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:a="http://www.w3.org/2005/08/addressing">'
            f"{cabecera}"
            f"<s:Body>{cuerpo}</s:Body></s:Envelope>"
        ).encode()
        pedido = urllib.request.Request(
            url, data=sobre,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            method="POST")
        try:
            with urllib.request.urlopen(pedido, timeout=timeout) as r:
                return ET.fromstring(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and autenticado:
                return self._llamar_http_auth(url, sobre, timeout)
            # El cuerpo del 500 trae el SOAP Fault, que es lo unico que explica
            # por que una camara rechaza la llamada. Sin esto, todos los fallos
            # se ven igual ("HTTP 500") y no hay forma de saber cual es cual.
            detalle = ""
            try:
                fallo = ET.fromstring(exc.read())
                detalle = (texto(fallo, "Text") or texto(fallo, "faultstring")
                           or texto(fallo, "Value") or "")
            except Exception:
                pass
            raise ErrorONVIF(f"HTTP {exc.code}{': ' + detalle if detalle else ''}") from exc
        except ET.ParseError as exc:
            raise ErrorONVIF(f"XML invalido: {exc}") from exc
        except Exception as exc:
            raise ErrorONVIF(str(exc)) from exc

    def _llamar_http_auth(self, url: str, sobre: bytes, timeout: int) -> ET.Element:
        gestor = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        gestor.add_password(None, url, self.usuario, self.clave)
        abridor = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(gestor),
            urllib.request.HTTPBasicAuthHandler(gestor))
        pedido = urllib.request.Request(
            url, data=sobre,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            method="POST")
        try:
            with abridor.open(pedido, timeout=timeout) as r:
                return ET.fromstring(r.read())
        except Exception as exc:
            raise ErrorONVIF(f"auth HTTP fallo: {exc}") from exc

    # -- conexion -----------------------------------------------------------

    def conectar(self) -> None:
        ultimo = None
        for ruta in RUTAS_DEVICE:
            url = f"http://{self.host}{ruta}"
            try:
                raiz = self.llamar(url, f'<GetSystemDateAndTime xmlns="{NS_DEVICE}"/>',
                                   autenticado=False)
            except ErrorONVIF as exc:
                ultimo = exc
                continue
            self.url_device = url
            self._leer_hora(raiz)
            return
        raise ErrorONVIF(f"sin servicio ONVIF ({ultimo})")

    def _leer_hora(self, raiz: ET.Element) -> None:
        utc = next(buscar(raiz, "UTCDateTime"), None)
        if utc is None:
            return
        try:
            campos = {n: int(texto(utc, n)) for n in
                      ("Year", "Month", "Day", "Hour", "Minute", "Second")}
            cam = datetime(campos["Year"], campos["Month"], campos["Day"],
                           campos["Hour"], campos["Minute"], campos["Second"],
                           tzinfo=timezone.utc)
            self.desfase = cam.timestamp() - datetime.now(timezone.utc).timestamp()
        except (TypeError, ValueError):
            self.desfase = 0.0

    def capacidades(self) -> None:
        raiz = self.llamar(
            self.url_device,
            f'<GetCapabilities xmlns="{NS_DEVICE}"><Category>Events</Category></GetCapabilities>')
        for el in buscar(raiz, "Events"):
            xaddr = texto(el, "XAddr")
            if xaddr:
                self.url_events = self._reescribir(xaddr)
                return
        raise ErrorONVIF("la camara no expone servicio de eventos")

    def _reescribir(self, xaddr: str) -> str:
        p = urllib.parse.urlsplit(xaddr)
        if p.hostname in (None, "0.0.0.0", "127.0.0.1"):
            return urllib.parse.urlunsplit(
                (p.scheme or "http", self.host, p.path, p.query, ""))
        return xaddr

    # -- eventos (PullPoint) ------------------------------------------------

    def crear_pullpoint(self, duracion: str = "PT10M") -> str:
        """Crea la suscripcion y devuelve la direccion a la que hay que pedir.

        Se prueban tres variantes porque los fabricantes no coinciden: las Sony
        rechazan con "Action Failed" el pedido que lleva InitialTerminationTime,
        y algunas exigen un filtro explicito de topicos. Bosch acepta la forma
        completa. Probamos de la mas completa a la mas simple.
        """
        if not self.url_events:
            self.capacidades()

        variantes = [
            f"<InitialTerminationTime>{duracion}</InitialTerminationTime>",
            "",
            '<Filter><TopicExpression Dialect="http://www.onvif.org/ver10/tev/'
            'topicExpression/ConcreteSet" xmlns="http://docs.oasis-open.org/wsn/b-2">'
            "tns1:RuleEngine//.|tns1:VideoSource//.</TopicExpression></Filter>",
        ]

        ultimo: Exception | None = None
        for extra in variantes:
            try:
                raiz = self.llamar(
                    self.url_events,
                    f'<CreatePullPointSubscription xmlns="{NS_EVENTS}">'
                    f"{extra}</CreatePullPointSubscription>")
            except ErrorONVIF as exc:
                ultimo = exc
                continue

            for ref in buscar(raiz, "SubscriptionReference"):
                direccion = texto(ref, "Address")
                if direccion:
                    return self._reescribir(direccion)
            direccion = texto(raiz, "Address")
            if direccion:
                return self._reescribir(direccion)
            ultimo = ErrorONVIF("la camara no devolvio direccion de suscripcion")

        raise ErrorONVIF(str(ultimo) if ultimo else "no se pudo crear la suscripcion")

    def pull(self, direccion: str, espera: str = "PT20S",
             limite: int = 20) -> list[dict]:
        """Pide los mensajes pendientes. Bloquea hasta `espera` si no hay nada."""
        raiz = self.llamar(
            direccion,
            f'<PullMessages xmlns="{NS_EVENTS}">'
            f"<Timeout>{espera}</Timeout><MessageLimit>{limite}</MessageLimit>"
            "</PullMessages>",
            timeout=45, accion=ACCION_PULL)
        return [self._parsear(m) for m in buscar(raiz, "NotificationMessage")]

    def renovar(self, direccion: str, duracion: str = "PT10M") -> None:
        self.llamar(
            direccion,
            '<Renew xmlns="http://docs.oasis-open.org/wsn/b-2">'
            f"<TerminationTime>{duracion}</TerminationTime></Renew>",
            accion=ACCION_RENEW)

    def cancelar(self, direccion: str) -> None:
        try:
            self.llamar(direccion,
                        '<Unsubscribe xmlns="http://docs.oasis-open.org/wsn/b-2"/>',
                        accion=ACCION_UNSUBSCRIBE)
        except ErrorONVIF:
            pass

    @staticmethod
    def _parsear(mensaje: ET.Element) -> dict:
        """Extrae tema, hora, operacion y los pares Nombre/Valor del mensaje.

        `operacion` es el atributo PropertyOperation y distingue un evento real
        ("Changed") del volcado de estado que la camara manda apenas uno se
        suscribe ("Initialized"). Sin leerlo, cada arranque parece una rafaga de
        eventos simultaneos.
        """
        tema = (texto(mensaje, "Topic") or "").strip()
        hora = None
        operacion = None
        datos: dict[str, str] = {}
        for el in buscar(mensaje, "Message"):
            hora = el.get("UtcTime") or hora
            operacion = el.get("PropertyOperation") or operacion
        for item in buscar(mensaje, "SimpleItem"):
            nombre = item.get("Name")
            if nombre:
                datos[nombre] = item.get("Value", "")
        return {"tema": tema, "hora": hora, "operacion": operacion, "datos": datos}

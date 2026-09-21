"""Control PTZ por ONVIF: mover, hacer zoom y ir a una posicion guardada.

Por que existe: de las 37 camaras del parque, 9 son domos que pueden girar y
acercar. Hasta ahora el operador solo podia mirar hacia donde el domo hubiera
quedado apuntando la ultima vez que alguien lo movio desde su interfaz web.

Como funciona el movimiento continuo en ONVIF: no se le dice "gira 30 grados",
se le dice "empeza a girar a esta velocidad" y despues "para". Por eso hay dos
formas de usar esto desde la interfaz:

  * mantener apretada la flecha  -> mover() al apretar, parar() al soltar
  * un toque                     -> mover() con `segundos`, que para solo

La segunda existe porque si el navegador se cierra, se recarga o pierde la red
justo despues de un mover(), el domo queda girando para siempre. El freno del
lado del servidor es la unica garantia real.
"""
from __future__ import annotations

import threading
import time
import urllib.request

from . import crypto, db
from .onvif import ClienteONVIF, ErrorONVIF, buscar, texto

NS_PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_SCHEMA = "http://www.onvif.org/ver10/schema"

# Techo de la velocidad que se le manda. 1.0 es el maximo del estandar; los
# domos Sony y Bosch del parque a 1.0 se pasan de largo y cuesta encuadrar.
VEL_MAX = 0.7

# Cuanto dura como maximo un movimiento por toque. Si la interfaz pide mas, se
# recorta: un domo girando solo es peor que uno mal encuadrado.
SEG_MAX = 5.0

# Cuanto vale una sesion ONVIF ya armada.
#
# Cada movimiento llevaba tres pedidos SOAP antes del que importa:
# GetCapabilities para saber donde vive el servicio PTZ, GetProfiles para
# sacar el token del perfil, y despues si el ContinuousMove. Sobre una camara
# al otro lado de la red eso son cientos de milisegundos por flecha apretada
# y otros tantos por flecha soltada, y encuadrar un domo es apretar y soltar
# veinte veces: el mando se sentia pesado y el domo se pasaba de largo porque
# el freno llegaba tarde. Lo que se averigua una vez no cambia mientras la
# camara siga siendo la misma, asi que se guarda un rato. Si la camara rechaza
# un pedido con la sesion guardada, se arma de nuevo y se reintenta una sola
# vez -- que es exactamente el caso de la camara que se reinicio.
TTL_SESION = 120.0

# Fotos: lo minimo entre dos y lo maximo que se acepta de la camara.
#
# El tope de tiempo no es por nosotros sino por el domo: el navegador pide
# fotos mientras el operador encuadra, y sin este piso una ventana abierta con
# la tecla apretada le pediria cien fotos por segundo. El tope de tamano es
# para no quedarnos con una respuesta que no sea una foto.
MIN_ENTRE_FOTOS = 0.25
MAX_FOTO = 6 * 1024 * 1024

_frenos: dict[int, threading.Timer] = {}
_sesiones: dict[int, dict] = {}
_fotos: dict[int, tuple[float, bytes]] = {}
_lock = threading.Lock()


class ErrorPTZ(Exception):
    pass


def _datos(camara_id: int, exigir_ptz: bool = True) -> dict:
    with db.sesion() as con:
        f = con.execute(
            """SELECT cam.id, cam.nombre, cam.ip, cam.ptz, cam.onvif,
                      cr.usuario, cr.secreto
                 FROM camaras cam
                 LEFT JOIN credenciales cr ON cr.id = cam.credencial_id
                WHERE cam.id = ?""", (camara_id,)).fetchone()
    if not f:
        raise ErrorPTZ("no existe esa camara")
    d = dict(f)
    if exigir_ptz and not d["ptz"]:
        raise ErrorPTZ(f"{d['nombre']} no es una camara PTZ")
    if not d["usuario"] or not d["secreto"]:
        raise ErrorPTZ(f"{d['nombre']} no tiene credenciales cargadas")
    d["clave"] = crypto.descifrar(d["secreto"])
    return d


def _armar(d: dict) -> dict:
    """Averigua todo lo que hace falta para mandarle ordenes a esta camara.

    El token del perfil hace falta en CADA pedido de PTZ: el estandar mueve
    perfiles, no camaras.
    """
    cli = ClienteONVIF(d["ip"], d["usuario"], d["clave"])
    cli.conectar()

    url_ptz = None
    url_media = None
    try:
        raiz = cli.llamar(
            cli.url_device,
            f'<GetCapabilities xmlns="{NS_DEVICE}"><Category>All</Category>'
            '</GetCapabilities>')
        for nombre, destino in (("PTZ", "ptz"), ("Media", "media")):
            # `buscar` es un generador: hay que sacarle el primero, no usarlo
            # como si fuera un elemento.
            nodo = next(buscar(raiz, nombre), None)
            if nodo is None:
                continue
            x = texto(nodo, "XAddr")
            if not x:
                continue
            if destino == "ptz":
                url_ptz = cli._reescribir(x)
            else:
                url_media = cli._reescribir(x)
    except ErrorONVIF as exc:
        raise ErrorPTZ(f"la camara no respondio a ONVIF: {exc}") from exc

    if not url_media:
        url_media = cli.url_device

    raiz = cli.llamar(url_media, f'<GetProfiles xmlns="{NS_MEDIA}"/>')
    token = None
    for p in buscar(raiz, "Profiles"):
        token = p.get("token")
        if token:
            break
    if not token:
        raise ErrorPTZ("la camara no devolvio ningun perfil")
    # `ptz` puede venir vacio: hay camaras fijas que igual sirven para pedirles
    # una foto, y el error de "no se puede mover" lo tira quien intente moverla.
    return {"cli": cli, "ptz": url_ptz, "media": url_media, "token": token}


def _sesion(d: dict, rearmar: bool = False) -> dict:
    """La sesion guardada de esta camara, armandola si hace falta."""
    cid = d["id"]
    ahora = time.time()
    if not rearmar:
        with _lock:
            s = _sesiones.get(cid)
        if s and ahora - s["hora"] < TTL_SESION:
            return s
    s = _armar(d)
    s["hora"] = ahora
    with _lock:
        _sesiones[cid] = s
    return s


def _cliente(d: dict) -> tuple[ClienteONVIF, str, str]:
    """El cliente, la URL del servicio PTZ y el token del perfil."""
    s = _sesion(d)
    if not s["ptz"]:
        raise ErrorPTZ("la camara no expone el servicio PTZ por ONVIF")
    return s["cli"], s["ptz"], s["token"]


def _pedir(d: dict, plantilla: str, que: str):
    """Manda un pedido PTZ reusando la sesion guardada.

    `plantilla` lleva @TOKEN@ donde va el token del perfil, porque el token
    pertenece a la sesion y puede cambiar cuando se rearma. Si la camara
    rechaza el pedido, se rearma la sesion y se reintenta UNA vez: un domo que
    se reinicio tiene tokens nuevos, y sin este reintento el operador veria un
    error y tendria que cerrar y volver a abrir el mando.
    """
    ultimo = None
    for intento in (0, 1):
        s = _sesion(d, rearmar=bool(intento))
        if not s["ptz"]:
            raise ErrorPTZ("la camara no expone el servicio PTZ por ONVIF")
        try:
            return s["cli"].llamar(s["ptz"], plantilla.replace("@TOKEN@",
                                                               s["token"]))
        except ErrorONVIF as exc:
            ultimo = exc
    raise ErrorPTZ(f"la camara rechazo {que}: {ultimo}") from ultimo


def _cancelar_freno(camara_id: int) -> None:
    with _lock:
        t = _frenos.pop(camara_id, None)
    if t:
        t.cancel()


def _programar_freno(camara_id: int, segundos: float) -> None:
    """El freno vive en el SERVIDOR. Si el navegador desaparece despues de
    mandar el movimiento, el domo igual para."""
    _cancelar_freno(camara_id)
    t = threading.Timer(segundos, lambda: _parar_silencioso(camara_id))
    t.daemon = True
    with _lock:
        _frenos[camara_id] = t
    t.start()


def _parar_silencioso(camara_id: int) -> None:
    try:
        parar(camara_id)
    except Exception as exc:                      # noqa: BLE001
        print(f"[ptz] no se pudo frenar la camara {camara_id}: {exc}")


def mover(camara_id: int, x: float = 0.0, y: float = 0.0, z: float = 0.0,
          segundos: float | None = None) -> dict:
    """x: girar (-1 izquierda, 1 derecha). y: inclinar. z: zoom."""
    d = _datos(camara_id)
    lim = lambda v: max(-VEL_MAX, min(VEL_MAX, float(v)))       # noqa: E731
    x, y, z = lim(x), lim(y), lim(z)
    if x == y == z == 0:
        return parar(camara_id)

    _pedir(d, f'<ContinuousMove xmlns="{NS_PTZ}">'
              '<ProfileToken>@TOKEN@</ProfileToken><Velocity>'
              f'<PanTilt x="{x:.3f}" y="{y:.3f}" xmlns="{NS_SCHEMA}"/>'
              f'<Zoom x="{z:.3f}" xmlns="{NS_SCHEMA}"/>'
              '</Velocity></ContinuousMove>', "el movimiento")

    if segundos:
        _programar_freno(camara_id, min(float(segundos), SEG_MAX))
    return {"camara_id": camara_id, "nombre": d["nombre"],
            "x": x, "y": y, "z": z,
            "frena_en": min(float(segundos), SEG_MAX) if segundos else None}


def parar(camara_id: int) -> dict:
    d = _datos(camara_id)
    _cancelar_freno(camara_id)
    _pedir(d, f'<Stop xmlns="{NS_PTZ}"><ProfileToken>@TOKEN@</ProfileToken>'
              '<PanTilt>true</PanTilt><Zoom>true</Zoom></Stop>', "el freno")
    return {"camara_id": camara_id, "nombre": d["nombre"], "estado": "detenida"}


def posiciones(camara_id: int) -> list[dict]:
    """Las posiciones guardadas EN LA CAMARA. No se guardan del lado nuestro a
    proposito: el domo ya las tiene y son las que el instalador dejo puestas."""
    d = _datos(camara_id)
    raiz = _pedir(d, f'<GetPresets xmlns="{NS_PTZ}">'
                     '<ProfileToken>@TOKEN@</ProfileToken></GetPresets>',
                  "leer las posiciones")
    out = []
    for p in buscar(raiz, "Preset"):
        out.append({"token": p.get("token"),
                    "nombre": texto(p, "Name") or p.get("token")})
    return out


def ir_a(camara_id: int, preset: str) -> dict:
    d = _datos(camara_id)
    _cancelar_freno(camara_id)
    _pedir(d, f'<GotoPreset xmlns="{NS_PTZ}">'
              '<ProfileToken>@TOKEN@</ProfileToken>'
              f'<PresetToken>{preset}</PresetToken></GotoPreset>',
           "la posicion")
    return {"camara_id": camara_id, "nombre": d["nombre"], "preset": preset}


def _uri_foto(d: dict) -> str | None:
    """La URL desde donde la camara entrega una foto de lo que ve ahora."""
    s = _sesion(d)
    if "foto" in s:
        return s["foto"]
    uri = None
    try:
        raiz = s["cli"].llamar(
            s["media"], f'<GetSnapshotUri xmlns="{NS_MEDIA}">'
                        f'<ProfileToken>{s["token"]}</ProfileToken>'
                        '</GetSnapshotUri>')
        uri = texto(raiz, "Uri")
    except ErrorONVIF:
        uri = None
    # La camara suele devolver la URL con el nombre que ella cree tener, que
    # desde el servidor no resuelve. `_reescribir` le pone la IP por la que
    # llegamos, que es la misma correccion que ya se hace con los servicios.
    s["foto"] = s["cli"]._reescribir(uri) if uri else None
    return s["foto"]


def _bajar_foto(uri: str, usuario: str, clave: str) -> bytes:
    """La foto en si. Las camaras del parque piden digest; algunas, basic."""
    gestor = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    gestor.add_password(None, uri, usuario, clave)
    abridor = urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(gestor),
        urllib.request.HTTPBasicAuthHandler(gestor))
    try:
        with abridor.open(uri, timeout=6) as r:
            datos = r.read(MAX_FOTO + 1)
    except Exception as exc:                        # noqa: BLE001
        raise ErrorPTZ(f"la camara no entrego la foto: {exc}") from exc
    if not datos:
        raise ErrorPTZ("la camara entrego una foto vacia")
    if len(datos) > MAX_FOTO:
        raise ErrorPTZ("la foto es demasiado grande")
    return datos


def foto(camara_id: int) -> bytes:
    """Una foto de lo que la camara ve AHORA, para poder encuadrar.

    Por que no se usa la transmision que ya tenemos: el muro entrega HLS, y el
    HLS llega entre seis y ocho segundos tarde -- se aprieta una flecha, se
    cuenta hasta seis, y recien entonces se ve donde quedo el domo. Encuadrar
    asi es tan a ciegas como sin imagen. La foto ONVIF llega en menos de medio
    segundo y no abre una sesion RTSP nueva, que es lo que de verdad no
    queremos hacerle a estas camaras.

    El piso de tiempo entre dos fotos protege al domo de la interfaz: si dos
    navegadores estan encuadrando la misma camara, la camara igual saca una
    foto cada cuarto de segundo y los dos ven la misma.
    """
    d = _datos(camara_id, exigir_ptz=False)
    ahora = time.time()
    with _lock:
        guardada = _fotos.get(camara_id)
    if guardada and ahora - guardada[0] < MIN_ENTRE_FOTOS:
        return guardada[1]
    uri = _uri_foto(d)
    if not uri:
        raise ErrorPTZ(f"{d['nombre']} no entrega fotos por ONVIF")
    datos = _bajar_foto(uri, d["usuario"], d["clave"])
    with _lock:
        _fotos[camara_id] = (ahora, datos)
    return datos


def capaz(camara_id: int) -> dict:
    """Si esta camara se puede mover, sin moverla. Lo usa la interfaz para
    decidir si muestra los controles."""
    try:
        d = _datos(camara_id)
    except ErrorPTZ as exc:
        return {"ptz": False, "motivo": str(exc)}
    try:
        _cliente(d)
    except ErrorPTZ as exc:
        return {"ptz": False, "motivo": str(exc), "nombre": d["nombre"]}
    # Si hay foto o no lo decide la interfaz: con foto pone la imagen en el
    # centro del mando, sin foto cae a la transmision del muro y avisa que
    # llega con retraso.
    try:
        hay_foto = bool(_uri_foto(d))
    except Exception:                               # noqa: BLE001
        hay_foto = False
    return {"ptz": True, "nombre": d["nombre"], "foto": hay_foto}

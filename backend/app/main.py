"""API de Sistema de Videovigilancia.

Arrancar con:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Documentacion interactiva en http://localhost:8000/docs
"""

from __future__ import annotations

import re
import subprocess
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field

from . import (almacenamiento, archivo, auditoria, auth, binarios, config,
               db, eventos, evidencia, grabador, grupos, importador,
               preservar, ptz, video, vod)

WEB = Path(__file__).resolve().parents[2] / "web"


def _arrancar_captura() -> None:
    """Levanta la captura y la escucha sin bloquear el arranque del servidor.

    Va en un hilo porque `iniciar_todas` levanta un ffmpeg por camara con un
    escalon de 0,25 s entre cada uno: con 30 camaras son casi 8 segundos, y
    systemd no tiene por que esperarlos para dar el servicio por arriba.

    Los errores se registran y se siguen: que una camara no levante no puede
    impedir que levanten las otras 29.
    """
    if not grabador._ffmpeg():
        print("[autoarranque] falta ffmpeg: no se arranca la captura")
        return
    try:
        r = grabador.iniciar_todas()
        vivas = sum(1 for x in r if x.get("estado") == "iniciando")
        print(f"[autoarranque] captura pedida para {vivas} de {len(r)} camaras")
    except Exception as exc:
        print(f"[autoarranque] fallo la captura: {exc}")
    try:
        e = eventos.iniciar_todas()
        print(f"[autoarranque] escucha de eventos en {len(e)} camaras")
    except Exception as exc:
        print(f"[autoarranque] fallo la escucha de eventos: {exc}")


@asynccontextmanager
async def ciclo_vida(_app: FastAPI):
    db.inicializar()
    grabador.RAIZ_VIDEO.mkdir(parents=True, exist_ok=True)
    huerfanos = grabador.limpiar_huerfanos()
    if huerfanos:
        print(f"[grabador] {huerfanos} proceso(s) huerfano(s) de una ejecucion "
              f"anterior fueron terminados")
    # La retencion no se fija aca: `purgar` la lee de `ajustes` en cada vuelta,
    # asi que cambiarla desde la interfaz surte efecto sin reiniciar.
    archivo.mantenimiento.arrancar()
    if config.ARRANCAR_AL_INICIO:
        threading.Thread(target=_arrancar_captura, name="autoarranque",
                         daemon=True).start()
    yield
    archivo.mantenimiento.detener()
    eventos.escucha.detener_todo()
    grabador.grabador.detener_todo()


app = FastAPI(
    lifespan=ciclo_vida,
    title="Sistema de Videovigilancia",
    description="Sistema de videovigilancia para un parque de camaras IP",
    version="0.1.0",
)

# Sin CORS a proposito. La interfaz se sirve del mismo origen que la API --
# nginx delante de uvicorn-- asi que ningun navegador necesita permiso cruzado.
# Habia una excepcion para un servidor de desarrollo en el puerto 5173 que ya
# no existe; dejar orígenes permitidos que nadie usa solo agranda la superficie.


# --- autenticacion ---------------------------------------------------------
#
# Regla unica, en el backend: sin sesion no se ve nada (salvo el login y los
# estaticos de la pagina); con sesion de operador se puede LEER y visualizar;
# modificar (crear/editar/borrar) es solo de admin. El frontend ademas esconde
# los botones, pero eso es comodidad, no seguridad.

# Rutas accesibles SIN estar logueado: el login en si y lo que necesita el
# navegador para dibujar la pantalla de login (la propia app y sus estaticos).
_PUBLICAS = {"/api/login", "/api/salud"}
_PREFIJOS_PUBLICOS = ("/web/", "/static/")


def _es_publica(ruta: str) -> bool:
    return (ruta in _PUBLICAS or ruta in ("/", "/logo.png", "/favicon.ico")
            or ruta.startswith(_PREFIJOS_PUBLICOS))


# Que puede MODIFICAR un operador. Todo lo que no este aca pide admin.
#
# La regla, dicha en palabras: el operador mira, exporta evidencia, preserva
# material y mueve los domos. No detiene la grabacion, no borra nada, no
# configura camaras ni usuarios.
#
# `detener` SALIO de esta lista. Antes estaba, con la contrasena como unico
# freno, con el argumento de que el que esta en la sala a las tres de la
# manana es el operador. Ahora es solo de admin por decision explicita:
# detener el parque es la accion mas cara del sistema y su rastro tiene que
# apuntar a alguien con la autoridad para tomarla.
_ESCRITURA_OPERADOR = {
    "/api/grabador/iniciar",      # arrancar es seguro: no pierde nada
    "/api/grabador/mirando",      # el muro avisa que recuadros tiene a la vista
    "/api/eventos/escuchar",
    "/api/evidencia",             # exportar evidencia
    "/api/preservaciones",        # proteger material de la purga
    "/api/logout",
}

# Las rutas con id adentro no entran en un set. Un operador puede mover un
# domo --es mirar, no configurar-- y por eso queda en la auditoria.
_PATRONES_OPERADOR = (
    re.compile(r"^/api/camaras/\d+/ptz$"),
)


def _operador_puede(ruta: str) -> bool:
    return (ruta in _ESCRITURA_OPERADOR
            or any(x.match(ruta) for x in _PATRONES_OPERADOR))


# Lo que NO se audita aunque modifique algo.
#
# `mirando` es el latido del muro: el navegador declara cada 8 segundos que
# recuadros tiene a la vista, para decidir que se transcodifica. Tecnicamente
# es un POST, pero no es una accion de nadie -- son 10.800 lineas por dia por
# navegador abierto. Auditarlo no agrega informacion y arruina el registro:
# una auditoria llena de latidos es una auditoria que nadie lee, y entonces no
# sirve para lo unico que existe.
_NO_AUDITAR = {"/api/grabador/mirando"}

# Lecturas que SI se auditan. Ver una grabacion es justamente el acto que hay
# que poder auditar; listar camaras no.
_LECTURAS_AUDITADAS = (
    (re.compile(r"^/api/camaras/(\d+)/ventana$"), "ver_grabacion"),
    (re.compile(r"^/api/evidencia/(\d+)/descargar$"), "descargar_evidencia"),
)


def _ip_de(request: Request) -> str:
    """La IP real del operador. Detras de nginx `request.client` siempre dice
    127.0.0.1; la verdadera viene en la cabecera que pone el proxy."""
    for cab in ("x-real-ip", "x-forwarded-for"):
        v = request.headers.get(cab)
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else ""


@app.middleware("http")
async def control_acceso(request: Request, call_next):
    """Permisos y auditoria en un solo lugar.

    La auditoria va ACA y no en cada endpoint a proposito: asi ninguna ruta
    --ni una que se agregue manana-- puede olvidarse de registrar lo que hizo.
    """
    ruta = request.url.path
    metodo = request.method

    if metodo == "OPTIONS" or _es_publica(ruta):
        return await call_next(request)

    sesion = auth.leer_cookie(request.cookies.get(auth.COOKIE))
    if not sesion:
        # Si todavia no hay NINGUN usuario cargado, el sistema queda abierto en
        # vez de tapiado: si no, un despliegue sin usuarios se autobloquea y no
        # habria como crear el primero desde la interfaz.
        if not auth.hay_usuarios():
            request.state.sesion = {"usuario": "anonimo", "rol": "admin"}
            return await call_next(request)
        return JSONResponse({"detail": "hace falta iniciar sesion"}, status_code=401)

    muta = metodo in ("POST", "PUT", "PATCH", "DELETE")
    if muta and sesion["rol"] != "admin" and not _operador_puede(ruta):
        auditoria.registrar(sesion["usuario"], sesion["rol"], "denegado",
                            metodo, ruta, 403, ip=_ip_de(request))
        return JSONResponse(
            {"detail": "esta accion es solo para administradores"}, status_code=403)

    request.state.sesion = sesion
    request.state.detalle_auditoria = None
    respuesta = await call_next(request)

    # El endpoint puede haber dejado un detalle mas rico que "POST /api/evidencia".
    detalle = getattr(request.state, "detalle_auditoria", None)
    ip = _ip_de(request)
    if ruta in _NO_AUDITAR:
        return respuesta
    if muta:
        accion = ruta.strip("/").split("/")[-1] or "mutacion"
        if ruta == "/api/evidencia":
            accion = "exportar"
        elif ruta.startswith("/api/preservaciones"):
            accion = "preservar" if metodo == "POST" else "despreservar"
        elif ruta.startswith("/api/usuarios"):
            accion = "usuario"
        elif metodo == "DELETE":
            accion = "eliminar"
        auditoria.registrar(sesion["usuario"], sesion["rol"], accion, metodo,
                            ruta, respuesta.status_code, detalle, ip)
    else:
        for patron, accion in _LECTURAS_AUDITADAS:
            if patron.match(ruta) and respuesta.status_code < 400:
                auditoria.registrar(
                    sesion["usuario"], sesion["rol"], accion, metodo, ruta,
                    respuesta.status_code,
                    detalle or _describir_lectura(request, ruta), ip)
                break
    return respuesta


def _describir_lectura(request: Request, ruta: str) -> str:
    """Que se leyo, en palabras. "vio la grabacion de la camara 7 del 10/09 a
    las 14:00" es auditable; "GET /api/camaras/7/ventana" no."""
    m = re.match(r"^/api/camaras/(\d+)/ventana$", ruta)
    if m:
        q = request.query_params
        seg = q.get("inicio", "0")
        try:
            h = int(float(seg))
            hora = "%02d:%02d" % (h // 3600, (h % 3600) // 60)
        except ValueError:
            hora = seg
        return ("vio la grabacion de la camara %s del %s a las %s"
                % (m.group(1), q.get("fecha", "?"), hora))
    m = re.match(r"^/api/evidencia/(\d+)/descargar$", ruta)
    if m:
        return "descargo la exportacion #%s" % m.group(1)
    return ""


class Login(BaseModel):
    usuario: str
    clave: str


@app.post("/api/login", tags=["sistema"])
def login(datos: Login, response: Response, request: Request) -> dict:
    sesion = auth.autenticar(datos.usuario, datos.clave)
    ip = _ip_de(request)
    if not sesion:
        # Los intentos fallidos se registran igual --y son lo primero que se
        # mira cuando algo no cierra-- pero NUNCA con la clave que se probo.
        auditoria.registrar(datos.usuario.strip()[:60], None, "entrar_fallido",
                            "POST", "/api/login", 401,
                            "contrasena incorrecta o usuario inexistente", ip)
        raise HTTPException(401, "usuario o contraseña incorrectos")
    auditoria.registrar(sesion["usuario"], sesion["rol"], "entrar",
                        "POST", "/api/login", 200, None, ip)
    response.set_cookie(
        auth.COOKIE, auth.emitir_cookie(sesion["usuario"], sesion["rol"]),
        max_age=auth.VIGENCIA_SEG, httponly=True, samesite="lax",
        # Secure solo detras de TLS (nginx). Ver config.COOKIE_SEGURA.
        secure=config.COOKIE_SEGURA, path="/")
    return sesion


@app.post("/api/logout", tags=["sistema"])
def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE, path="/")
    return {"estado": "sesion cerrada"}


@app.get("/api/sesion", tags=["sistema"], include_in_schema=False)
def sesion_valida() -> Response:
    """Punto de control de nginx para servir el video.

    nginx entrega `/media/` --los segmentos HLS-- por su cuenta y pregunta aca,
    por cada pedido, si quien lo hace tiene sesion. Tiene que ser lo mas barato
    que exista en la API: para cuando la peticion llega hasta este cuerpo, el
    middleware ya verifico la cookie firmada, que es HMAC sin base de datos ni
    disco. O sea que llegar aca ES la respuesta, y un 204 sin cuerpo alcanza.

    Es la contracara de `location /media/` en despliegue/sistema-videovigilancia.nginx: si se
    saca una, hay que sacar la otra.
    """
    return Response(status_code=204)


@app.get("/api/yo", tags=["sistema"])
def yo(request: Request) -> dict:
    """Quien soy y con que rol. El frontend lo usa para decidir que mostrar."""
    sesion = getattr(request.state, "sesion", None)
    if not sesion:
        raise HTTPException(401, "sin sesion")
    return {**sesion, "sin_usuarios": not auth.hay_usuarios()}


# --- salud -----------------------------------------------------------------

@app.get("/api/salud", tags=["sistema"])
def salud() -> dict:
    with db.sesion() as con:
        camaras = con.execute("SELECT COUNT(*) AS n FROM camaras").fetchone()["n"]
        ultimo = con.execute(
            "SELECT fecha, total, responden, con_video FROM relevamientos "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "estado": "ok",
        "version": app.version,
        "camaras": camaras,
        "ultimo_relevamiento": db.fila_a_dict(ultimo),
        "volumen_tb": config.VOLUMEN_TB,
    }


# --- camaras ---------------------------------------------------------------

@app.get("/api/camaras", tags=["camaras"])
def listar_camaras(
    sitio: str | None = Query(None, description="filtrar por nombre de sitio"),
    estado: str | None = Query(None, description="en_linea | sin_conexion | sin_video"),
    marca: str | None = None,
    ptz: bool | None = None,
) -> dict:
    sql = """SELECT cam.*, s.nombre AS sitio
               FROM camaras cam
               LEFT JOIN sitios s ON s.id = cam.sitio_id
              WHERE 1 = 1"""
    args: list = []
    if sitio:
        sql += " AND s.nombre = ?"
        args.append(sitio)
    if estado:
        sql += " AND cam.estado = ?"
        args.append(estado)
    if marca:
        sql += " AND UPPER(cam.marca) = UPPER(?)"
        args.append(marca)
    if ptz is not None:
        sql += " AND cam.ptz = ?"
        args.append(1 if ptz else 0)
    sql += " ORDER BY s.nombre, cam.nombre"

    with db.sesion() as con:
        filas = [dict(f) for f in con.execute(sql, args).fetchall()]

    for fila in filas:
        fila["ptz"] = bool(fila["ptz"])
        fila["lpr"] = bool(fila["lpr"])
        fila["onvif"] = bool(fila["onvif"])
        fila["eventos_onvif"] = bool(fila["eventos_onvif"])

    return {"total": len(filas), "camaras": filas}


@app.get("/api/camaras/{camara_id}", tags=["camaras"])
def ver_camara(camara_id: int) -> dict:
    with db.sesion() as con:
        cam = con.execute(
            """SELECT cam.*, s.nombre AS sitio
                 FROM camaras cam LEFT JOIN sitios s ON s.id = cam.sitio_id
                WHERE cam.id = ?""",
            (camara_id,),
        ).fetchone()
        if not cam:
            raise HTTPException(404, f"no existe la camara {camara_id}")

        streams = [dict(f) for f in con.execute(
            "SELECT * FROM streams WHERE camara_id = ? ORDER BY ancho DESC",
            (camara_id,)).fetchall()]
        notas = [f["texto"] for f in con.execute(
            "SELECT texto FROM notas_camara WHERE camara_id = ? ORDER BY id",
            (camara_id,)).fetchall()]

    ficha = dict(cam)
    ficha["ptz"] = bool(ficha["ptz"])
    ficha["lpr"] = bool(ficha["lpr"])
    ficha["onvif"] = bool(ficha["onvif"])
    ficha["eventos_onvif"] = bool(ficha["eventos_onvif"])
    ficha["streams"] = streams
    ficha["notas"] = notas

    # Discrepancia entre la planilla y lo que declara el equipo. No es
    # necesariamente un error: Bosch reporta el nombre comercial y la planilla
    # tiene el codigo de producto.
    ficha["modelo_coincide"] = None
    if ficha["modelo"] and ficha["modelo_reportado"]:
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())  # noqa: E731
        ficha["modelo_coincide"] = norm(ficha["modelo"]) == norm(ficha["modelo_reportado"])

    return ficha


@app.get("/api/sitios", tags=["camaras"])
def listar_sitios() -> dict:
    with db.sesion() as con:
        filas = con.execute(
            """SELECT s.id, s.nombre,
                      COUNT(cam.id) AS camaras,
                      SUM(CASE WHEN cam.estado = 'en_linea' THEN 1 ELSE 0 END) AS en_linea
                 FROM sitios s LEFT JOIN camaras cam ON cam.sitio_id = s.id
                GROUP BY s.id, s.nombre
                ORDER BY s.nombre"""
        ).fetchall()
    return {"sitios": [dict(f) for f in filas]}


# --- almacenamiento --------------------------------------------------------

@app.get("/api/almacenamiento", tags=["almacenamiento"])
def estimacion(ciclo_evento: float | None = Query(
        None, ge=0.0, le=1.0,
        description="fraccion del dia grabando por evento (0.10 = 10%)")) -> dict:
    return almacenamiento.resumen(ciclo_evento)


@app.get("/api/almacenamiento/camaras", tags=["almacenamiento"])
def consumo_por_camara(ciclo_evento: float | None = Query(None, ge=0.0, le=1.0)) -> dict:
    filas = almacenamiento.por_camara(ciclo_evento)
    return {"total": len(filas), "camaras": filas}


# --- importacion -----------------------------------------------------------

@app.post("/api/importar", tags=["sistema"])
def importar() -> dict:
    """Recarga la planilla y el ultimo relevamiento desde disco."""
    try:
        return importador.importar_todo()
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc


# --- alta y edicion de camaras ---------------------------------------------

class CamaraNueva(BaseModel):
    nombre: str = Field(min_length=1, max_length=80)
    ip: str = Field(min_length=7, max_length=45)
    marca: str = ""
    modelo: str = ""
    sitio: str = ""
    ubicacion: str = ""
    tipo: str = ""
    ptz: bool = False
    lpr: bool = False
    credencial: str | None = Field(None, description="nombre del juego de credenciales")
    url_rtsp: str | None = Field(None, description="si se conoce; si no, se descubre por ONVIF")


class CamaraEdicion(BaseModel):
    nombre: str | None = None
    sitio: str | None = None
    ubicacion: str | None = None
    tipo: str | None = None
    ptz: bool | None = None
    lpr: bool | None = None
    credencial: str | None = None
    perfil_grabacion: str | None = None


@app.post("/api/camaras", tags=["camaras"], status_code=201)
def crear_camara(datos: CamaraNueva) -> dict:
    with db.sesion() as con:
        if con.execute("SELECT 1 FROM camaras WHERE ip = ?", (datos.ip,)).fetchone():
            raise HTTPException(409, f"ya existe una camara con la IP {datos.ip}")
        if con.execute("SELECT 1 FROM camaras WHERE nombre = ?", (datos.nombre,)).fetchone():
            raise HTTPException(409, f"ya existe una camara llamada {datos.nombre}")

        sitio_id = None
        if datos.sitio:
            con.execute("INSERT OR IGNORE INTO sitios (nombre) VALUES (?)", (datos.sitio,))
            sitio_id = con.execute("SELECT id FROM sitios WHERE nombre = ?",
                                   (datos.sitio,)).fetchone()["id"]

        credencial_id = None
        if datos.credencial:
            fila = con.execute("SELECT id FROM credenciales WHERE nombre = ?",
                               (datos.credencial,)).fetchone()
            if not fila:
                raise HTTPException(400, f"no existe la credencial '{datos.credencial}'")
            credencial_id = fila["id"]

        cur = con.execute(
            """INSERT INTO camaras
                   (nombre, sitio_id, ubicacion, tipo, marca, modelo, ip,
                    ptz, lpr, credencial_id, estado)
               VALUES (?,?,?,?,?,?,?,?,?,?,'desconocido')""",
            (datos.nombre, sitio_id, datos.ubicacion, datos.tipo, datos.marca,
             datos.modelo, datos.ip, int(datos.ptz), int(datos.lpr), credencial_id),
        )
        camara_id = cur.lastrowid

        # Si dieron la URL a mano, ya queda utilizable sin esperar al relevamiento.
        if datos.url_rtsp:
            cur2 = con.execute(
                """INSERT INTO streams (camara_id, nombre, fuente, url, rol, medido)
                   VALUES (?, 'manual', 'manual', ?, 'evento', datetime('now'))""",
                (camara_id, datos.url_rtsp),
            )
            con.execute(
                """UPDATE camaras SET stream_evento_id = ?,
                       perfil_grabacion = ? WHERE id = ?""",
                (cur2.lastrowid, config.PERFIL_SOLO_EVENTO, camara_id),
            )

    return ver_camara(camara_id)


@app.patch("/api/camaras/{camara_id}", tags=["camaras"])
def editar_camara(camara_id: int, datos: CamaraEdicion) -> dict:
    campos, args = [], []
    with db.sesion() as con:
        if not con.execute("SELECT 1 FROM camaras WHERE id = ?", (camara_id,)).fetchone():
            raise HTTPException(404, f"no existe la camara {camara_id}")

        for campo in ("nombre", "ubicacion", "tipo", "perfil_grabacion"):
            valor = getattr(datos, campo)
            if valor is not None:
                campos.append(f"{campo} = ?")
                args.append(valor)
        for campo in ("ptz", "lpr"):
            valor = getattr(datos, campo)
            if valor is not None:
                campos.append(f"{campo} = ?")
                args.append(int(valor))
        if datos.sitio is not None:
            con.execute("INSERT OR IGNORE INTO sitios (nombre) VALUES (?)", (datos.sitio,))
            fila = con.execute("SELECT id FROM sitios WHERE nombre = ?",
                               (datos.sitio,)).fetchone()
            campos.append("sitio_id = ?")
            args.append(fila["id"])
        if datos.credencial is not None:
            fila = con.execute("SELECT id FROM credenciales WHERE nombre = ?",
                               (datos.credencial,)).fetchone()
            if not fila:
                raise HTTPException(400, f"no existe la credencial '{datos.credencial}'")
            campos.append("credencial_id = ?")
            args.append(fila["id"])

        if not campos:
            raise HTTPException(400, "no se envio ningun campo para modificar")
        args.append(camara_id)
        con.execute(f"UPDATE camaras SET {', '.join(campos)} WHERE id = ?", args)

    return ver_camara(camara_id)


class CalidadVideo(BaseModel):
    ancho: int = Field(ge=160, le=4096)
    alto: int = Field(ge=120, le=2160)
    fps: int = Field(ge=1, le=60)
    calidad: float = Field(ge=1, le=10)


@app.get("/api/camaras/{camara_id}/video", tags=["camaras"])
def ver_calidad(camara_id: int) -> dict:
    """Que calidad tiene hoy el stream base y que admite la camara.

    Se le pregunta a la camara en el momento, no a la base: el equipo es la
    autoridad sobre su propio encoder y alguien pudo haberlo cambiado desde su
    interfaz web.
    """
    try:
        return video.leer(camara_id)
    except video.ErrorVideo as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:                      # ONVIF que no contesta
        raise HTTPException(502, f"la camara no respondio: {exc}") from exc


@app.post("/api/camaras/{camara_id}/video", tags=["camaras"])
def cambiar_calidad(camara_id: int, datos: CalidadVideo) -> dict:
    """Escribe el encoder de la camara y relanza SOLO esa captura.

    No reinicia el servicio: el resto del parque sigue grabando sin enterarse.
    """
    try:
        return video.aplicar(camara_id, datos.ancho, datos.alto,
                             datos.fps, datos.calidad)
    except video.ErrorVideo as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"la camara no respondio: {exc}") from exc


@app.post("/api/camaras/{camara_id}/video/medir", tags=["camaras"])
def medir_calidad(camara_id: int) -> dict:
    """Mide el bitrate REAL abriendo una sesion RTSP corta.

    Es la unica forma de saber cuanto pesa de verdad: la estimacion depende de
    la escena. Abre una sola sesion -- estas camaras aguantan pocas.
    """
    try:
        return video.medir(camara_id)
    except video.ErrorVideo as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/camaras/{camara_id}", tags=["camaras"])
def borrar_camara(camara_id: int) -> dict:
    grabador.grabador.detener(camara_id)
    with db.sesion() as con:
        cur = con.execute("DELETE FROM camaras WHERE id = ?", (camara_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"no existe la camara {camara_id}")
    return {"estado": "borrada", "camara_id": camara_id}


@app.get("/api/credenciales", tags=["camaras"])
def listar_credenciales() -> dict:
    """Nombres y usuarios. Las claves nunca salen de la base."""
    with db.sesion() as con:
        filas = con.execute(
            """SELECT c.id, c.nombre, c.usuario, COUNT(cam.id) AS camaras
                 FROM credenciales c
                 LEFT JOIN camaras cam ON cam.credencial_id = c.id
                GROUP BY c.id ORDER BY c.nombre"""
        ).fetchall()
    return {"credenciales": [dict(f) for f in filas]}


# --- grupos del plano ------------------------------------------------------

class GrupoNuevo(BaseModel):
    nombre: str = Field(min_length=1, max_length=60)
    x: float | None = None
    y: float | None = None
    camaras: list[int] = Field(default_factory=list)
    padre_id: int | None = Field(
        None, description="grupo del que cuelga; vacio = yacimiento de primer nivel")


class GrupoEdicion(BaseModel):
    nombre: str | None = Field(None, min_length=1, max_length=60)
    x: float | None = None
    y: float | None = None
    padre_id: int | None = None
    # Sin este flag no se puede pedir "mandalo a la raiz": `padre_id: null` y
    # "no me mandaron padre_id" se ven igual en el JSON.
    mover: bool = False


class GrupoCamaras(BaseModel):
    camaras: list[int] = Field(default_factory=list)


@app.get("/api/grupos", tags=["plano"])
def listar_grupos() -> dict:
    """Los grupos del plano, con sus cámaras y su posición."""
    gs = grupos.listar()
    return {"total": len(gs), "grupos": gs, "sin_grupo": grupos.sin_grupo()}


@app.post("/api/grupos", tags=["plano"], status_code=201)
def crear_grupo(datos: GrupoNuevo) -> dict:
    try:
        gid = grupos.crear(datos.nombre, datos.x, datos.y, datos.camaras,
                           datos.padre_id)
    except db.sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"ya existe un grupo llamado '{datos.nombre}'") from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": gid, "nombre": datos.nombre, "padre_id": datos.padre_id}


@app.patch("/api/grupos/{grupo_id}", tags=["plano"])
def editar_grupo(grupo_id: int, datos: GrupoEdicion) -> dict:
    """Renombrar o mover. Mover es lo que hace el arrastre en el plano."""
    try:
        ok = grupos.editar(grupo_id, datos.nombre, datos.x, datos.y,
                           datos.padre_id, datos.mover)
    except db.sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"ya existe un grupo llamado '{datos.nombre}'") from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not ok:
        raise HTTPException(404, f"no existe el grupo {grupo_id}, o no se envió nada")
    return {"estado": "ok", "grupo_id": grupo_id}


@app.delete("/api/grupos/{grupo_id}", tags=["plano"])
def borrar_grupo(grupo_id: int) -> dict:
    """Borra el grupo y sus subgrupos. Las camaras no se tocan."""
    r = grupos.borrar(grupo_id)
    if not r["borrados"]:
        raise HTTPException(404, f"no existe el grupo {grupo_id}")
    return {"estado": "borrado", "grupo_id": grupo_id, **r}


@app.put("/api/grupos/{grupo_id}/camaras", tags=["plano"])
def asignar_camaras(grupo_id: int, datos: GrupoCamaras) -> dict:
    try:
        n = grupos.asignar(grupo_id, datos.camaras)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"estado": "ok", "grupo_id": grupo_id, "camaras": n}


class MoverCamara(BaseModel):
    grupo_id: int


@app.put("/api/camaras/{camara_id}/grupo", tags=["plano"])
def mover_camara_de_grupo(camara_id: int, datos: MoverCamara) -> dict:
    """Mueve una camara a otro grupo del plano, sacandola del que estaba.

    Es distinto de PATCH /api/camaras/{id} con `sitio`: eso cambia la etiqueta
    del inventario y no toca el arbol.
    """
    try:
        return grupos.mover_camara(camara_id, datos.grupo_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/grupos/sembrar", tags=["plano"])
def sembrar_grupos() -> dict:
    """Arma los grupos iniciales desde los sitios, para no empezar en blanco."""
    return grupos.sembrar()


# --- archivo / linea de tiempo ---------------------------------------------

@app.get("/api/archivo", tags=["archivo"])
def estado_archivo() -> dict:
    datos = archivo.resumen_archivo()
    datos["retencion_dias"] = archivo.retencion_dias()
    datos["ultima_purga"] = archivo.mantenimiento.ultima_purga
    datos["archivando"] = config.ARCHIVAR
    datos["cache_video"] = vod.estado_cache()
    return datos


@app.get("/api/archivo/historial", tags=["archivo"])
def historial_archivo(dias: int = Query(14, ge=1, le=90)) -> dict:
    """Cuanto se escribio cada dia. Es lo que dibujan los graficos."""
    filas = archivo.historial(dias)
    return {"dias": len(filas), "historial": filas,
            "limite_gb": round(archivo.limite_gb(), 1)}


@app.get("/api/camaras/{camara_id}/dias", tags=["archivo"])
def dias_camara(camara_id: int) -> dict:
    """Que dias tienen grabacion, en hora local. Alimenta el selector."""
    dias = archivo.dias_con_grabacion(camara_id)
    return {"camara_id": camara_id, "total": len(dias), "dias": dias}


@app.get("/api/camaras/{camara_id}/segmentos", tags=["archivo"])
def segmentos_camara(
    camara_id: int,
    fecha: str | None = Query(None, description="dia local, YYYY-MM-DD"),
) -> dict:
    """Los tramos de un dia, con la posicion que ocupan en la playlist.

    Pedir SIEMPRE un dia. Antes esto devolvia todo lo que hubiera con un tope
    de 3000 filas ordenadas de mas viejo a mas nuevo, asi que apenas el
    archivo pasaba los dos dias la reproduccion ya no podia ver lo reciente:
    el tope cortaba justo lo que uno quiere mirar.
    """
    tramos = archivo.linea_de_tiempo(camara_id, fecha)
    return {
        "camara_id": camara_id,
        "fecha": fecha,
        "rango": archivo.rango_disponible(camara_id),
        "dias": archivo.dias_con_grabacion(camara_id),
        "total": len(tramos),
        "duracion": round(sum(t["duracion"] for t in tramos), 3),
        "segmentos": tramos,
    }


@app.post("/api/archivo/indexar", tags=["archivo"])
def indexar_archivo() -> dict:
    return {"nuevos": archivo.indexar()}


class Retencion(BaseModel):
    dias: float = Field(ge=0.5, le=3650)


@app.get("/api/archivo/retencion", tags=["archivo"])
def ver_retencion() -> dict:
    """Cuantos dias se guardan y cuantos entrarian con el ritmo actual."""
    return archivo.proyeccion()


@app.put("/api/archivo/retencion", tags=["archivo"])
def cambiar_retencion(datos: Retencion) -> dict:
    """Cambia los dias de retencion.

    Surte efecto en la purga siguiente, que corre cada dos minutos. Bajarla
    borra lo que exceda, y eso no se recupera.
    """
    return archivo.fijar_retencion(datos.dias)


@app.post("/api/archivo/purgar", tags=["archivo"])
def purgar_archivo(retencion_dias: float | None = Query(None, ge=0.01)) -> dict:
    return archivo.purgar(retencion_dias if retencion_dias else archivo.retencion_dias())


# --- grabador / vivo -------------------------------------------------------

@app.get("/api/grabador", tags=["grabador"])
def estado_grabador() -> dict:
    """Lo que el grabador esta haciendo de verdad, no lo que alguien pidio.

    El boton de transmision se pinta con esto: `total` son las camaras que
    tiene a cargo, `corriendo` las que tienen un ffmpeg vivo ahora mismo y
    `en_vivo` las que ademas se pueden mirar en el navegador. Sin los tres el
    muro no puede distinguir "levantando" de "transmitiendo".

    `arrancando` es el arranque escalonado y NADA MAS: son las tareas que
    todavia no levantaron su primer ffmpeg. Antes era `corriendo < total`, que
    parece lo mismo y no lo es: una camara que no responde vive en el ciclo de
    reintentos con el proceso muerto, asi que la cuenta nunca se completaba y
    el muro se quedaba en "Levantando 26/27" para siempre -- con el boton
    deshabilitado, que ademas dejaba sin forma de detener la transmision. Una
    camara que arranco y despues se cayo es una falla (`caidas`), no un
    arranque.
    """
    tareas = grabador.grabador.estado()
    corriendo = sum(1 for t in tareas if t["corriendo"])
    esperando = [t for t in tareas if t["esperando_arranque"]]
    return {
        "corriendo": corriendo,
        "total": len(tareas),
        "en_vivo": sum(1 for t in tareas if t["en_vivo"]),
        "transmitiendo": bool(tareas),
        "arrancando": bool(esperando),
        # Las que ya no estan levantando y aun asi no corren: el muro las
        # nombra para que la falla tenga nombre y no sea solo un numero que
        # no cierra.
        "caidas": [{"camara_id": t["camara_id"], "nombre": t["nombre"],
                    "ultimo_error": t["ultimo_error"]}
                   for t in tareas
                   if not t["corriendo"] and not t["esperando_arranque"]],
        "convirtiendo": sum(1 for t in tareas if t["convertido"]),
        "cupo_conversion": config.MAX_CONVERSIONES,
        "bajo_demanda": grabador.grabador.bajo_demanda,
        "camaras": tareas,
    }


class ModoRendimiento(BaseModel):
    activo: bool


@app.post("/api/grabador/relanzar", tags=["grabador"])
def relanzar_camara(camara_id: int = Query(..., description="la camara a relanzar")) -> dict:
    """Corta y vuelve a levantar la captura de UNA camara.

    No pide la contrasena, a diferencia de `/detener`: lo que esa pregunta
    protege es dejar al PARQUE ENTERO sin grabar, que es un error caro y
    silencioso. Cortar una camara sola es una operacion de mantenimiento
    normal -- una que se colgo, una a la que se le cambio el stream en la base
    y hay que releerlo -- y no habia forma de hacerla sin parar todo.

    Tambien es la unica manera de que el grabador tome un cambio de stream: la
    URL se lee al crear la tarea y despues se reusa hasta que la tarea muere.
    """
    if not grabador._ffmpeg():
        raise HTTPException(400, "falta ffmpeg en el sistema")
    with db.sesion() as con:
        fila = con.execute("SELECT nombre FROM camaras WHERE id = ?",
                           (camara_id,)).fetchone()
    if not fila:
        raise HTTPException(404, f"no existe la camara {camara_id}")
    grabador.grabador.detener(camara_id)
    resultado = grabador.grabador.iniciar(camara_id)
    return {"camara_id": camara_id, "nombre": fila["nombre"], **resultado}


@app.post("/api/grabador/rendimiento", tags=["grabador"])
def modo_rendimiento(datos: ModoRendimiento) -> dict:
    """Prende o apaga el ahorro de CPU (conversion solo de lo que se mira).

    Apagado -- el valor por defecto -- las 8 camaras que el navegador no puede
    reproducir se convierten todo el tiempo y aparecen al instante. Prendido se
    convierte solo lo que hay en pantalla: se ahorra CPU y esas camaras tardan
    unos segundos en aparecer.
    """
    return grabador.grabador.modo_rendimiento(datos.activo)


class Mirando(BaseModel):
    camaras: list[int] = Field(default_factory=list,
                               description="ids visibles en el muro ahora mismo")


@app.post("/api/grabador/mirando", tags=["grabador"])
def declarar_mirando(datos: Mirando) -> dict:
    """El muro declara que recuadros tiene en pantalla.

    Es lo que decide que camaras se transcodifican. Convertir H.265 a H.264
    cuesta CPU y el R710 no da para hacerlo con las 27 camaras a la vez, asi
    que se convierte solo lo que alguien esta mirando de verdad. Hay que
    renovarlo cada pocos segundos: el permiso vence solo (`config.TTL_VISTA`),
    porque una pestana que se cierra de golpe no avisa que dejo de mirar.
    """
    return grabador.grabador.mirar(datos.camaras)


@app.get("/api/camaras/{camara_id}/playlist.m3u8", tags=["archivo"])
def playlist_camara(camara_id: int, fecha: str | None = None) -> Response:
    """Playlist HLS de un dia de grabacion.

    Los segmentos que escribe el grabador son MPEG-TS, que es exactamente el
    contenedor de HLS. El navegador NO reproduce un .ts suelto en un <video>
    -- por eso la reproduccion no andaba aunque los archivos estuvieran bien --
    pero si reproduce esta playlist, y ademas se puede buscar por toda la
    grabacion en vez de ir segmento por segmento.

    Sale de `archivo.linea_de_tiempo`, la MISMA funcion que alimenta la barra
    de la interfaz. Antes cada uno armaba su lista por su cuenta y con reglas
    distintas, y el resultado era que el cursor de la barra y lo que sonaba en
    el video no apuntaban al mismo lado.

    Dos cosas que arregla respecto de la version anterior:

    * **Solo la capa base.** La lista incluia tambien los clips de evento, que
      cubren el mismo instante en otra resolucion. El reproductor veia la
      imagen cambiar de tamano a mitad de camino y se trababa o mostraba el
      mismo momento dos veces.
    * **Duraciones reales.** Declaraba 60 s para todos los segmentos, incluidos
      los que se cortaron antes. Buscar en una playlist cuyas duraciones no son
      las del video deja al reproductor apuntando a otro lado que el usuario.
    """
    tramos = archivo.linea_de_tiempo(camara_id, fecha)
    if not tramos:
        raise HTTPException(404, "no hay grabacion para esa camara y fecha")

    objetivo = int(max(t["duracion"] for t in tramos)) + 1
    lineas = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:VOD",
              f"#EXT-X-TARGETDURATION:{objetivo}", "#EXT-X-MEDIA-SEQUENCE:0"]
    for t in tramos:
        # Un corte real en la grabacion (camara caida, purga) tiene que quedar
        # marcado: sin DISCONTINUITY el reproductor arrastra los timestamps del
        # segmento previo y se traba.
        if t["corte"]:
            lineas.append("#EXT-X-DISCONTINUITY")
        lineas.append(f"#EXTINF:{t['duracion']:.3f},")
        lineas.append(f"/media/{t['archivo']}")
    lineas.append("#EXT-X-ENDLIST")

    return Response("\n".join(lineas) + "\n",
                    media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/camaras/{camara_id}/ventana", tags=["archivo"])
def ventana_video(
    camara_id: int,
    fecha: str = Query(..., description="dia local, YYYY-MM-DD"),
    inicio: float = Query(0, ge=0, le=86400, description="segundo del dia"),
) -> dict:
    """Prepara la ventana de video y dice donde arranca.

    Se pide esto ANTES del .mp4 porque armarlo puede tardar unos segundos --
    copiando es menos de uno, convirtiendo H.265 son varios-- y el frontend
    necesita poder decir "preparando" en vez de dejar un `<video>` mudo. La
    respuesta trae `comienza_en`, el segundo del dia en el que arranca el
    archivo, que es lo que permite ubicar el cursor sin adivinar.
    """
    try:
        datos = vod.construir(camara_id, fecha, inicio)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {k: v for k, v in datos.items() if k != "ruta"}


@app.get("/api/camaras/{camara_id}/video.mp4", tags=["archivo"])
def video_camara(
    camara_id: int,
    fecha: str = Query(..., description="dia local, YYYY-MM-DD"),
    inicio: float = Query(0, ge=0, le=86400, description="segundo del dia"),
) -> FileResponse:
    """El MP4 de una ventana de grabacion. Ver `app/vod.py` por que existe."""
    try:
        datos = vod.construir(camara_id, fecha, inicio)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    # FileResponse manda Accept-Ranges y responde 206: sin eso el navegador no
    # puede buscar dentro del video sin bajarlo entero.
    return FileResponse(datos["ruta"], media_type="video/mp4",
                        headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/eventos/{evento_id}/miniatura.jpg", tags=["eventos"])
def miniatura_evento(evento_id: int) -> FileResponse:
    """Un fotograma del clip, como vista previa.

    Antes la lista de eventos montaba el .ts en un <video>, que quedaba negro
    hasta que alguien pasaba el mouse por encima -- y ni asi, porque el
    navegador no decodifica MPEG-TS suelto. Un JPEG se ve siempre. Se genera
    una sola vez y queda cacheado al lado del clip.
    """
    with db.sesion() as con:
        fila = con.execute("SELECT clip FROM eventos WHERE id = ?",
                           (evento_id,)).fetchone()
    if not fila or not fila["clip"]:
        raise HTTPException(404, "ese evento no tiene clip")

    origen = grabador.RAIZ_VIDEO / fila["clip"]
    destino = origen.with_suffix(".jpg")
    if destino.exists() and destino.stat().st_size > 512:
        return FileResponse(destino, media_type="image/jpeg")
    if not origen.exists():
        raise HTTPException(404, "el clip ya no esta en disco")

    ffmpeg = binarios.ffmpeg()
    if not ffmpeg:
        raise HTTPException(503, "falta ffmpeg")
    subprocess.run(
        [ffmpeg, "-loglevel", "error", "-y", "-i", str(origen),
         "-frames:v", "1", "-vf", "scale=384:-2", "-q:v", "4", str(destino)],
        capture_output=True, timeout=30)
    if not destino.exists() or destino.stat().st_size <= 512:
        raise HTTPException(422, "no se pudo extraer un fotograma del clip")
    return FileResponse(destino, media_type="image/jpeg")


@app.get("/api/eventos/{evento_id}/clip.m3u8", tags=["eventos"])
def playlist_clip(evento_id: int) -> Response:
    """Misma historia que la playlist de camara, para el clip de un evento."""
    with db.sesion() as con:
        fila = con.execute("SELECT clip FROM eventos WHERE id = ?",
                           (evento_id,)).fetchone()
    if not fila or not fila["clip"]:
        raise HTTPException(404, "ese evento no tiene clip")
    cuerpo = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n"
              f"#EXT-X-TARGETDURATION:{config.SEGUNDOS_CLIP + 1}\n"
              "#EXT-X-MEDIA-SEQUENCE:0\n"
              f"#EXTINF:{float(config.SEGUNDOS_CLIP):.3f},\n"
              f"/media/{fila['clip']}\n#EXT-X-ENDLIST\n")
    return Response(cuerpo, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/grabador/iniciar", tags=["grabador"])
def iniciar_grabador(
    camara_id: int | None = Query(None, description="una sola camara; vacio = todas"),
    limite: int | None = Query(None, ge=1, le=64, description="cuantas levantar"),
) -> dict:
    if not grabador._ffmpeg():
        raise HTTPException(400, "falta ffmpeg en el sistema")
    if camara_id:
        return {"resultados": [grabador.grabador.iniciar(camara_id)]}
    resultados = grabador.iniciar_todas(limite)
    # Arrancar la transmision arranca tambien la escucha de eventos de las
    # camaras marcadas. Eran dos botones separados y era facil apretar uno y
    # olvidarse del otro: el sistema quedaba grabando pero sin registrar un
    # solo evento, y eso no se nota hasta que hace falta buscar uno.
    try:
        escuchas = eventos.iniciar_todas()
    except Exception:                       # que un fallo de eventos no tumbe
        escuchas = []                       # la transmision, que es lo critico
    return {"resultados": resultados, "escuchando": len(escuchas)}


class DetenerTransmision(BaseModel):
    clave: str = Field("", description="contrasena del usuario que pide detener")
    camara_id: int | None = Field(None, description="una sola camara; vacio = todas")


@app.post("/api/grabador/detener", tags=["grabador"])
def detener_grabador(datos: DetenerTransmision, request: Request) -> dict:
    """Corta la transmision. Pide la contrasena de nuevo.

    Detener no es solo apagar el muro: mata los ffmpeg, y con ellos la
    grabacion en disco de todo el parque. Un clic de mas deja al sistema sin
    guardar nada y nadie se entera hasta que hace falta el video. Por eso se
    vuelve a pedir la contrasena del que esta logueado -- es la unica accion
    del sistema que lo hace, y es lo que reemplaza al rol: puede detener
    cualquiera que este logueado, pero nadie de un solo clic.
    """
    sesion = getattr(request.state, "sesion", None) or {}
    # Sin usuarios cargados el sistema esta abierto (ver `control_acceso`): no
    # hay contrasena que pedir.
    if auth.hay_usuarios():
        if not datos.clave:
            raise HTTPException(422, "hace falta la contrasena para detener")
        if not auth.autenticar(sesion.get("usuario", ""), datos.clave):
            raise HTTPException(403, "contraseña incorrecta")

    if datos.camara_id:
        return grabador.grabador.detener(datos.camara_id)
    n = grabador.grabador.detener_todo()
    return {"estado": "detenido", "camaras": n}


# --- eventos ---------------------------------------------------------------

@app.get("/api/eventos", tags=["eventos"])
def listar_eventos(
    limite: int = Query(100, ge=1, le=1000),
    camara_id: int | None = None,
    tipo: str | None = None,
) -> dict:
    filas = eventos.listar(limite, camara_id, tipo)
    return {"total": len(filas), "eventos": filas}


@app.get("/api/eventos/resumen", tags=["eventos"])
def resumen_eventos() -> dict:
    return eventos.resumen()


@app.post("/api/eventos/escuchar", tags=["eventos"])
def escuchar_eventos(
    camara_id: int | None = Query(None, description="una sola; vacio = todas"),
    limite: int | None = Query(None, ge=1, le=64),
) -> dict:
    if camara_id:
        return {"resultados": [eventos.escucha.iniciar(camara_id)]}
    return {"resultados": eventos.iniciar_todas(limite)}


class SeleccionEventos(BaseModel):
    camaras: list[int]


@app.get("/api/eventos/seleccion", tags=["eventos"])
def ver_seleccion_eventos() -> dict:
    """Que camaras pueden escuchar eventos, cuales estan marcadas y cuales
    estan escuchando ahora mismo."""
    return {"camaras": eventos.seleccion()}


@app.put("/api/eventos/seleccion", tags=["eventos"])
def guardar_seleccion_eventos(datos: SeleccionEventos) -> dict:
    """Guarda la eleccion y la aplica en el acto.

    Queda en la base, asi que sobrevive a un reinicio y es la misma para todos
    los usuarios: no es una preferencia del navegador.
    """
    return eventos.marcar(datos.camaras)


@app.post("/api/eventos/detener", tags=["eventos"])
def detener_eventos(camara_id: int | None = None) -> dict:
    if camara_id:
        return eventos.escucha.detener(camara_id)
    return {"estado": "detenido", "camaras": eventos.escucha.detener_todo()}


# --- auditoria -------------------------------------------------------------
#
# Solo admin: el registro de quien miro que es informacion sensible por si
# misma. Y no hay endpoint para borrar ni editar una linea, a proposito.

@app.get("/api/auditoria", tags=["auditoria"])
def ver_auditoria(
    limite: int = Query(300, ge=1, le=5000),
    usuario: str | None = None,
    solo_criticas: bool = False,
    desde: str | None = Query(None, description="ISO; por ejemplo 2026-09-01"),
) -> dict:
    """Quien hizo que. Ver app/auditoria.py."""
    filas = auditoria.listar(limite, usuario, solo_criticas, desde)
    return {"total": len(filas), "registros": filas,
            "resumen": auditoria.resumen(),
            "usuarios": [u["nombre"] for u in auth.listar_usuarios()]}


# --- usuarios --------------------------------------------------------------

class PersonaNueva(BaseModel):
    nombre: str = Field(min_length=1, max_length=60)
    apellido: str = Field(min_length=1, max_length=60)
    clave: str = Field(min_length=6, max_length=200)
    rol: str = Field("operador", pattern="^(operador|admin)$")


@app.get("/api/usuarios", tags=["sistema"])
def listar_usuarios() -> dict:
    return {"usuarios": auth.listar_usuarios(), "roles": list(auth.ROLES)}


@app.post("/api/usuarios", tags=["sistema"], status_code=201)
def crear_usuario(datos: PersonaNueva, request: Request) -> dict:
    """Alta de una persona. El nombre de usuario se ARMA, no se elige.

    Se piden nombre y apellido, y el usuario sale de la primera letra del
    apellido mas el nombre: "Mauricio Figueroa" entra como `fmauricio`. Asi el
    registro de auditoria se puede leer sin tener que consultar una tabla
    aparte para saber quien es cada quien.
    """
    try:
        r = auth.crear_persona(datos.nombre, datos.apellido, datos.clave,
                               datos.rol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    request.state.detalle_auditoria = (
        f"creo el usuario {r['usuario']} ({r['nombre_completo']}) como {r['rol']}")
    return r


@app.delete("/api/usuarios/{usuario}", tags=["sistema"])
def borrar_usuario(usuario: str, request: Request) -> dict:
    try:
        ok = auth.borrar_usuario(usuario)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not ok:
        raise HTTPException(404, f"no existe el usuario {usuario}")
    request.state.detalle_auditoria = f"elimino el usuario {usuario}"
    return {"estado": "eliminado", "usuario": usuario}


# --- material preservado ---------------------------------------------------

class PreservacionNueva(BaseModel):
    camara_id: int
    desde: str = Field(description="ISO 8601; si no trae zona se asume UTC")
    hasta: str
    motivo: str = Field(min_length=3, max_length=300)


@app.get("/api/preservaciones", tags=["archivo"])
def ver_preservaciones() -> dict:
    return {"preservaciones": preservar.listar(), "resumen": preservar.resumen()}


@app.get("/api/camaras/{camara_id}/preservaciones", tags=["archivo"])
def preservaciones_camara(camara_id: int) -> dict:
    """Los rangos protegidos de una camara, para dibujarlos en la barra."""
    return {"camara_id": camara_id, "rangos": preservar.de_camara(camara_id)}


@app.post("/api/preservaciones", tags=["archivo"], status_code=201)
def crear_preservacion(datos: PreservacionNueva, request: Request) -> dict:
    """Marca un tramo como intocable por la purga.

    Es la contracara de la retencion configurable: esa borra por antiguedad y
    por espacio sin perdonar, y hasta ahora no habia forma de decirle "esto no".
    """
    sesion = getattr(request.state, "sesion", {}) or {}
    try:
        r = preservar.crear(datos.camara_id, datos.desde, datos.hasta,
                            datos.motivo, sesion.get("usuario", "?"))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    request.state.detalle_auditoria = (
        f"preservo {r['segmentos']} tramos de {r['camara']} "
        f"({r['desde']} a {r['hasta']}) — motivo: {r['motivo']}")
    return r


@app.delete("/api/preservaciones/{pid}", tags=["archivo"])
def borrar_preservacion(pid: int, request: Request) -> dict:
    """Levantar una proteccion. Solo admin: dejar de proteger material es una
    decision tan relevante como protegerlo, y queda registrada igual."""
    r = preservar.borrar(pid)
    if not r:
        raise HTTPException(404, f"no existe la preservacion {pid}")
    request.state.detalle_auditoria = (
        f"LEVANTO la proteccion de {r.get('camara')} "
        f"({r['desde']} a {r['hasta']}) — era por: {r.get('motivo')}")
    return {"estado": "levantada", **r}


# --- exportacion de evidencia ---------------------------------------------

class Exportacion(BaseModel):
    camara_id: int
    desde: str
    hasta: str
    motivo: str = Field(min_length=3, max_length=300)


@app.get("/api/evidencia", tags=["evidencia"])
def ver_exportaciones() -> dict:
    return {"exportaciones": evidencia.listar(), "espacio": evidencia.espacio(),
            "max_minutos": evidencia.MAX_MINUTOS}


@app.post("/api/evidencia", tags=["evidencia"], status_code=201)
def exportar_evidencia(datos: Exportacion, request: Request) -> dict:
    """Arma el MP4 del rango, le calcula el SHA-256 y registra quien se lo llevo.

    Ver app/evidencia.py por que no alcanza con el MP4 de la reproduccion.
    """
    sesion = getattr(request.state, "sesion", {}) or {}
    try:
        r = evidencia.exportar(datos.camara_id, datos.desde, datos.hasta,
                               datos.motivo, sesion.get("usuario", "?"))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    request.state.detalle_auditoria = (
        f"exporto {r['minutos']} min de {r['camara']} "
        f"({r['mb']} MB, sha256 {r['sha256'][:16]}…) — motivo: {datos.motivo.strip()}")
    return r


@app.get("/api/evidencia/{eid}/descargar", tags=["evidencia"])
def descargar_evidencia(eid: int) -> FileResponse:
    par = evidencia.ruta_de(eid)
    if not par:
        raise HTTPException(404, f"no existe la exportacion {eid}")
    ruta, fila = par
    if not ruta.exists():
        raise HTTPException(410, "el archivo ya no esta en disco")
    return FileResponse(ruta, media_type="video/mp4", filename=fila["archivo"])


@app.get("/api/evidencia/{eid}/comprobante", tags=["evidencia"])
def comprobante_evidencia(eid: int) -> FileResponse:
    """El texto con la huella y como verificarla. Va junto con el video."""
    par = evidencia.ruta_de(eid)
    if not par:
        raise HTTPException(404, f"no existe la exportacion {eid}")
    ruta = evidencia.comprobante(eid)
    if not ruta.exists():
        raise HTTPException(410, "no se encuentra el comprobante")
    return FileResponse(ruta, media_type="text/plain; charset=utf-8",
                        filename=ruta.name)


# --- control PTZ -----------------------------------------------------------

class MovimientoPTZ(BaseModel):
    x: float = Field(0, ge=-1, le=1, description="girar: -1 izquierda, 1 derecha")
    y: float = Field(0, ge=-1, le=1, description="inclinar: -1 abajo, 1 arriba")
    z: float = Field(0, ge=-1, le=1, description="zoom: -1 alejar, 1 acercar")
    segundos: float | None = Field(
        None, ge=0.1, le=5,
        description="si viene, la camara frena sola pasado ese tiempo")
    parar: bool = False
    preset: str | None = None


@app.get("/api/camaras/{camara_id}/ptz", tags=["camaras"])
def ver_ptz(camara_id: int) -> dict:
    """Si la camara se puede mover y que posiciones tiene guardadas.

    No la mueve. Lo usa la interfaz para decidir si muestra los controles.
    """
    cap = ptz.capaz(camara_id)
    if not cap.get("ptz"):
        return cap
    try:
        cap["posiciones"] = ptz.posiciones(camara_id)
    except ptz.ErrorPTZ:
        cap["posiciones"] = []          # el domo puede no tener presets
    return cap


@app.get("/api/camaras/{camara_id}/vista.jpg", tags=["camaras"])
def vista_camara(camara_id: int) -> Response:
    """Una foto de lo que la camara ve ahora.

    Existe para poder encuadrar un domo viendolo. La transmision del muro
    llega con seis a ocho segundos de retraso y con eso no se encuadra nada:
    se mueve, se espera, y se descubre que quedo de mas. La foto llega en
    medio segundo.

    No se guarda en cache del navegador ni de nginx a proposito: una foto de
    hace diez segundos es exactamente lo que este endpoint existe para evitar.
    El piso entre dos fotos lo pone `ptz`, del lado de la camara.
    """
    try:
        datos = ptz.foto(camara_id)
    except ptz.ErrorPTZ as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:                       # ONVIF que no contesta
        raise HTTPException(502, f"la camara no respondio: {exc}") from exc
    return Response(datos, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/camaras/{camara_id}/ptz", tags=["camaras"])
def mover_ptz(camara_id: int, datos: MovimientoPTZ, request: Request) -> dict:
    """Mueve un domo. Lo puede hacer un operador: es mirar, no configurar.

    Queda en la auditoria igual, porque dejar un domo apuntando a otro lado es
    una forma silenciosa de dejar de ver algo.
    """
    try:
        if datos.parar:
            r = ptz.parar(camara_id)
            request.state.detalle_auditoria = f"freno el domo {r['nombre']}"
        elif datos.preset:
            r = ptz.ir_a(camara_id, datos.preset)
            request.state.detalle_auditoria = (
                f"llevo el domo {r['nombre']} a la posicion {datos.preset}")
        else:
            r = ptz.mover(camara_id, datos.x, datos.y, datos.z, datos.segundos)
            request.state.detalle_auditoria = (
                f"movio el domo {r['nombre']} "
                f"(giro {r['x']}, inclinacion {r['y']}, zoom {r['z']})")
    except ptz.ErrorPTZ as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:                       # ONVIF que no contesta
        raise HTTPException(502, f"la camara no respondio: {exc}") from exc
    return r


# --- interfaz --------------------------------------------------------------

# Los segmentos HLS que escribe el grabador.
app.mount("/media", StaticFiles(directory=grabador.RAIZ_VIDEO, check_dir=False),
          name="media")


@app.get("/logo.png", include_in_schema=False)
def logo() -> FileResponse:
    ruta = config.RAIZ / "logo.png"
    if not ruta.exists():
        raise HTTPException(404, "no hay logo.png en la raiz del proyecto")
    return FileResponse(ruta, media_type="image/png")


@app.get("/", include_in_schema=False)
def inicio() -> FileResponse:
    return FileResponse(WEB / "index.html")


if WEB.exists():
    app.mount("/web", StaticFiles(directory=WEB, html=True), name="web")

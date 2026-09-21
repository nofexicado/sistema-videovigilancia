"""Eventos ONVIF: suscripcion, registro y grabacion en calidad.

Cada camara detecta movimiento por su cuenta (VMD en Sony y Axis, IVA en Bosch)
y lo publica por ONVIF. El servidor solo escucha. Esa es la razon por la que
este esquema entra en un R710 sin GPU: **no se analiza video en el servidor**.

Al llegar un evento se dispara un clip del stream principal. La capa base sigue
grabando continuo aparte, asi que lo ocurrido ANTES del disparo no se pierde:
queda en la linea de tiempo en calidad baja, y el clip cubre desde la deteccion.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import binarios, config, crypto, db, onvif

# Temas ONVIF que nos interesan, mapeados a un tipo propio. Cada fabricante usa
# el suyo, por eso se compara por subcadena y no por igualdad.
TEMAS = [
    ("rule/celllmotiondetector", "movimiento"),
    ("cellmotiondetector", "movimiento"),
    ("motionalarm", "movimiento"),
    ("motiondetect", "movimiento"),
    ("videosource/motion", "movimiento"),
    # IVA de Bosch. Verificado contra las camaras del parque: es el tema que
    # publican de verdad las Bosch cuando algo entra en el campo vigilado, y
    # es mas util que MotionAlarm porque ya viene filtrado por la analitica.
    ("iva/objectinfield", "intrusion"),
    ("objectinfield", "intrusion"),
    ("objectdetect", "objeto"),
    ("linecrossing", "cruce"),
    ("fielddetector", "intrusion"),
    ("loitering", "merodeo"),
    ("tamper", "sabotaje"),
    ("globalscenechange", "sabotaje"),   # la escena cambio entera: taparon o movieron la camara
    ("videosource/imagetoodark", "senal"),
    ("videosource/imagetoobright", "senal"),
    ("videosource/signalloss", "senal"),
    # Entradas y salidas fisicas de la camara. En este parque suelen estar
    # cableadas a contactos de porton o barrera, asi que valen como evento.
    ("device/trigger/digitalinput", "entrada"),
    ("device/trigger/relay", "rele"),
]

# Valores que significan "empezo" en el SimpleItem State/IsMotion
VERDADEROS = {"true", "1", "active", "yes"}

# Cuando un mismo hecho lo reportan dos analiticas de la misma camara, gana la
# mas especifica. Las Bosch disparan `MotionAlarm` (movimiento crudo) y
# `IVA/ObjectInField` (analitica ya filtrada) en el mismo segundo: sin esto cada
# movimiento entraba dos veces y la lista de eventos servia para poco.
PRIORIDAD = {"patente": 6, "intrusion": 5, "merodeo": 5, "cruce": 5,
             "objeto": 4, "sabotaje": 4, "entrada": 3, "rele": 3,
             "movimiento": 2, "senal": 1}

# Ventana en la que dos avisos de la misma camara se consideran el mismo hecho.
VENTANA_DEDUP = 4          # segundos

DURACION_CLIP = config.SEGUNDOS_CLIP
ENFRIAMIENTO = 25          # segundos entre clips de la misma camara


def _ahora() -> str:
    """Hora del servidor en UTC.

    Todo lo que se fecha en la base va en UTC: los segmentos, los eventos y los
    clips. Se usa el reloj del servidor y no el que reporta la camara porque
    varias del parque llegaron con anos de desfase, y una linea de tiempo con
    dos relojes distintos no sirve para buscar nada.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _raiz_video() -> Path:
    from .grabador import RAIZ_VIDEO
    return RAIZ_VIDEO


def _clasificar(tema: str) -> str | None:
    t = (tema or "").lower()
    for aguja, tipo in TEMAS:
        if aguja in t:
            return tipo
    return None


def _es_inicio(datos: dict) -> bool:
    """Distingue el 'empezo' del 'termino'. Si la camara no manda estado,
    tratamos el mensaje como inicio."""
    for clave in ("State", "IsMotion", "IsPeople", "IsVehicle", "Motion", "Active"):
        if clave in datos:
            return str(datos[clave]).lower() in VERDADEROS
    return True


# --- grabacion del clip ----------------------------------------------------

def grabar_clip(camara_id: int, segundos: int = DURACION_CLIP) -> dict | None:
    """Graba el stream principal a un archivo. Copia directa, sin recodificar."""
    ffmpeg = binarios.ffmpeg()
    if not ffmpeg:
        return None

    with db.sesion() as con:
        fila = con.execute(
            """SELECT cam.nombre, cam.stream_evento_id, cr.usuario, cr.secreto
                 FROM camaras cam
                 LEFT JOIN credenciales cr ON cr.id = cam.credencial_id
                WHERE cam.id = ?""", (camara_id,)).fetchone()
        if not fila or not fila["stream_evento_id"]:
            return None
        stream = con.execute("SELECT url FROM streams WHERE id = ?",
                             (fila["stream_evento_id"],)).fetchone()
    if not stream or not stream["url"]:
        return None

    usuario = fila["usuario"] or ""
    clave = crypto.descifrar(fila["secreto"]) if fila["secreto"] else ""
    p = urllib.parse.urlsplit(stream["url"])
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    cred = (f"{urllib.parse.quote(usuario, safe='')}:"
            f"{urllib.parse.quote(clave, safe='')}@") if usuario else ""
    url = urllib.parse.urlunsplit((p.scheme, cred + host, p.path, p.query, ""))

    carpeta = _raiz_video() / str(camara_id) / "eventos"
    carpeta.mkdir(parents=True, exist_ok=True)
    # UTC, igual que los segmentos de la capa base: el indexador lee la fecha
    # del nombre y ambas capas tienen que caer en la misma linea de tiempo.
    marca = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destino = carpeta / f"evento_{marca}.ts"

    base = [ffmpeg, "-loglevel", "error", "-rtsp_transport", "tcp",
            "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
            "-i", url, "-t", str(segundos), "-map", "0:v:0", "-an"]
    cola = ["-f", "mpegts", "-y", str(destino)]

    # Primero copia directa: calidad original y CPU cero. Varias Bosch entregan
    # el video por un tunel RTSP que ffmpeg no puede remuxar sin tocar, asi que
    # si la copia no produce nada se reintenta convirtiendo. Mejor un clip
    # convertido que ningun clip.
    intentos = [
        ["-c:v", "copy"],
        ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
         "-pix_fmt", "yuv420p", "-crf", "26", "-threads", "2"],
    ]
    for i, video in enumerate(intentos):
        try:
            subprocess.run(base + video + cola, capture_output=True,
                           timeout=segundos + 45)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if destino.exists() and destino.stat().st_size >= 8192:
            return {"ruta": f"{camara_id}/eventos/{destino.name}",
                    "bytes": destino.stat().st_size,
                    "convertido": i > 0}

    try:
        destino.unlink()
    except OSError:
        pass
    return None


# --- suscripcion por camara ------------------------------------------------

class Suscripcion:
    def __init__(self, camara_id: int, nombre: str, host: str,
                 usuario: str, clave: str):
        self.camara_id = camara_id
        self.nombre = nombre
        self.host = host
        self.usuario = usuario
        self.clave = clave
        self.parar = threading.Event()
        self.activa = False
        self.eventos = 0
        self.clips = 0
        self.error: str | None = None
        self.ultimo: str | None = None
        self._ultimo_clip = 0.0
        self._reciente: tuple[float, str, int] | None = None   # (t, tipo, id)

    def correr(self) -> None:
        espera = 5
        while not self.parar.is_set():
            try:
                self._sesion()
                espera = 5
            except onvif.ErrorONVIF as exc:
                self.error = str(exc)
                self.activa = False
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.activa = False
            self.parar.wait(espera)
            espera = min(espera * 2, 120)

    def _sesion(self) -> None:
        cli = onvif.ClienteONVIF(self.host, self.usuario, self.clave)
        cli.conectar()
        cli.capacidades()
        direccion = cli.crear_pullpoint()
        self.activa = True
        self.error = None
        ultima_renovacion = time.time()

        try:
            while not self.parar.is_set():
                mensajes = cli.pull(direccion)
                for msg in mensajes:
                    self._procesar(msg)
                if time.time() - ultima_renovacion > 300:
                    cli.renovar(direccion)
                    ultima_renovacion = time.time()
        finally:
            self.activa = False
            cli.cancelar(direccion)

    def _procesar(self, msg: dict) -> None:
        # Al suscribirse, la camara vuelca el estado actual de cada propiedad
        # con PropertyOperation="Initialized". No son eventos: son una foto de
        # como estan las cosas. Sin descartarlos, cada arranque escribia una
        # rafaga de eventos falsos y grababa clips de nada.
        if (msg.get("operacion") or "").lower() == "initialized":
            return

        tipo = _clasificar(msg.get("tema", ""))
        if not tipo or not _es_inicio(msg.get("datos") or {}):
            return

        # Deduplicacion: si la misma camara ya reporto algo hace un instante,
        # es el mismo hecho visto por otra analitica. Se conserva una sola fila,
        # con el tipo mas especifico de los dos.
        ahora_t = time.time()
        previo = self._reciente
        if previo and ahora_t - previo[0] <= VENTANA_DEDUP:
            if PRIORIDAD.get(tipo, 0) > PRIORIDAD.get(previo[1], 0):
                with db.sesion() as con:
                    con.execute(
                        "UPDATE eventos SET tipo = ?, tema = ? WHERE id = ?",
                        (tipo, msg.get("tema"), previo[2]))
                self._reciente = (previo[0], tipo, previo[2])
            return

        self.eventos += 1
        inicio = _ahora()
        self.ultimo = inicio

        datos = dict(msg.get("datos") or {})
        if msg.get("hora"):
            # La hora que declara la camara se guarda solo como referencia; la
            # que vale es la del servidor.
            datos["_hora_camara"] = msg["hora"]

        with db.sesion() as con:
            cur = con.execute(
                """INSERT INTO eventos (camara_id, tipo, tema, inicio, datos)
                   VALUES (?,?,?,?,?)""",
                (self.camara_id, tipo, msg.get("tema"), inicio,
                 json.dumps(datos, ensure_ascii=False)))
            evento_id = cur.lastrowid
        self._reciente = (ahora_t, tipo, evento_id)

        # Enfriamiento: una camara con movimiento sostenido dispara decenas de
        # mensajes por minuto. Sin esto se grabarian clips encimados sin parar.
        if time.time() - self._ultimo_clip < ENFRIAMIENTO:
            return
        self._ultimo_clip = time.time()

        # El clip se graba en un hilo aparte. `grabar_clip` puede tardar mas de
        # un minuto (graba, y si la copia falla reintenta convirtiendo), y este
        # metodo corre dentro del bucle de pull: bloquearlo significa dejar de
        # escuchar a la camara y arriesgar que la suscripcion expire.
        threading.Thread(target=self._grabar_y_registrar, args=(evento_id,),
                         name=f"clip-{self.camara_id}", daemon=True).start()

    def _grabar_y_registrar(self, evento_id: int) -> None:
        try:
            clip = grabar_clip(self.camara_id)
        except Exception as exc:               # nunca tumbar el hilo del clip
            self.error = f"clip: {exc}"
            return
        if not clip:
            return
        self.clips += 1
        with db.sesion() as con:
            con.execute("UPDATE eventos SET clip = ?, clip_bytes = ? WHERE id = ?",
                        (clip["ruta"], clip["bytes"], evento_id))


class Escucha:
    """Supervisa una suscripcion por camara."""

    def __init__(self) -> None:
        self._subs: dict[int, Suscripcion] = {}
        self._hilos: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()

    def iniciar(self, camara_id: int) -> dict:
        with self._lock:
            if camara_id in self._subs:
                return {"estado": "ya_escuchando", "camara_id": camara_id}

        with db.sesion() as con:
            fila = con.execute(
                """SELECT cam.nombre, cam.ip, cam.eventos_onvif,
                          cr.usuario, cr.secreto
                     FROM camaras cam
                     LEFT JOIN credenciales cr ON cr.id = cam.credencial_id
                    WHERE cam.id = ?""", (camara_id,)).fetchone()
        if not fila:
            return {"estado": "no_existe", "camara_id": camara_id}
        if not fila["eventos_onvif"]:
            return {"estado": "sin_eventos_onvif", "camara_id": camara_id,
                    "nombre": fila["nombre"]}
        if not fila["secreto"]:
            return {"estado": "sin_credencial", "camara_id": camara_id,
                    "nombre": fila["nombre"]}

        sub = Suscripcion(camara_id, fila["nombre"], fila["ip"],
                          fila["usuario"] or "", crypto.descifrar(fila["secreto"]))
        hilo = threading.Thread(target=sub.correr, name=f"eventos-{camara_id}",
                                daemon=True)
        with self._lock:
            self._subs[camara_id] = sub
            self._hilos[camara_id] = hilo
        hilo.start()
        return {"estado": "escuchando", "camara_id": camara_id, "nombre": fila["nombre"]}

    def detener(self, camara_id: int) -> dict:
        with self._lock:
            sub = self._subs.pop(camara_id, None)
            self._hilos.pop(camara_id, None)
        if not sub:
            return {"estado": "no_estaba", "camara_id": camara_id}
        sub.parar.set()
        return {"estado": "detenido", "camara_id": camara_id}

    def detener_todo(self) -> int:
        ids = list(self._subs)
        for cid in ids:
            self.detener(cid)
        return len(ids)

    def estado(self) -> list[dict]:
        with self._lock:
            subs = list(self._subs.values())
        return sorted([{
            "camara_id": s.camara_id, "nombre": s.nombre, "activa": s.activa,
            "eventos": s.eventos, "clips": s.clips, "error": s.error,
            "ultimo": s.ultimo,
        } for s in subs], key=lambda x: x["nombre"])


escucha = Escucha()


def iniciar_todas(limite: int | None = None) -> list[dict]:
    """Levanta la escucha SOLO en las camaras marcadas.

    `escucha_eventos` es la eleccion del usuario y `eventos_onvif` lo que la
    camara soporta: hacen falta las dos. Antes se levantaban todas las que
    podian, y no habia forma de dejar una afuera sin apagar el conjunto.
    """
    with db.sesion() as con:
        filas = con.execute(
            """SELECT id FROM camaras
                WHERE eventos_onvif = 1 AND credencial_id IS NOT NULL
                  AND estado = 'en_linea' AND escucha_eventos = 1
                ORDER BY nombre""").fetchall()
    ids = [f["id"] for f in filas]
    if limite:
        ids = ids[:limite]
    return [escucha.iniciar(cid) for cid in ids]


def seleccion() -> list[dict]:
    """Que camaras pueden escuchar eventos y cuales estan marcadas."""
    with db.sesion() as con:
        filas = con.execute(
            """SELECT cam.id, cam.nombre, cam.ip, s.nombre AS sitio,
                      cam.eventos_onvif, cam.escucha_eventos, cam.estado,
                      cam.credencial_id,
                      (SELECT COUNT(*) FROM eventos e WHERE e.camara_id = cam.id) AS eventos
                 FROM camaras cam LEFT JOIN sitios s ON s.id = cam.sitio_id
                ORDER BY cam.nombre""").fetchall()
    activas = {c["camara_id"] for c in escucha.estado()}
    salida = []
    for f in filas:
        d = dict(f)
        d["puede"] = bool(d.pop("eventos_onvif")) and d.pop("credencial_id") is not None
        d["marcada"] = bool(d.pop("escucha_eventos"))
        d["escuchando"] = d["id"] in activas
        salida.append(d)
    return salida


def marcar(ids: list[int]) -> dict:
    """Deja marcadas exactamente esas camaras, y ninguna mas.

    Se aplica en el acto: las que salen de la lista dejan de escuchar sin
    esperar al proximo arranque, y las que entran empiezan si la transmision
    ya esta andando.
    """
    quiere = set(ids)
    with db.sesion() as con:
        con.execute("UPDATE camaras SET escucha_eventos = 0")
        if quiere:
            marcas = ",".join("?" * len(quiere))
            con.execute(f"UPDATE camaras SET escucha_eventos = 1 WHERE id IN ({marcas})",
                        tuple(quiere))
        filas = con.execute(
            """SELECT id FROM camaras
                WHERE eventos_onvif = 1 AND credencial_id IS NOT NULL
                  AND estado = 'en_linea' AND escucha_eventos = 1""").fetchall()
    debe = {f["id"] for f in filas}
    activas = {c["camara_id"] for c in escucha.estado()}
    for cid in activas - debe:
        escucha.detener(cid)
    # Una camara que no arranca NO puede voltear el guardado: la eleccion ya
    # quedo escrita y lo que falla es levantar la suscripcion de ese equipo.
    # Se informan aparte para que la interfaz lo pueda decir.
    arrancadas, fallidas = [], []
    for cid in debe - activas:
        try:
            arrancadas.append(escucha.iniciar(cid))
        except Exception as exc:
            fallidas.append({"camara_id": cid, "detalle": str(exc)[:200]})
    return {"marcadas": len(quiere), "arrancadas": len(arrancadas),
            "detenidas": len(activas - debe), "fallidas": fallidas}


def listar(limite: int = 100, camara_id: int | None = None,
           tipo: str | None = None) -> list[dict]:
    sql = """SELECT e.*, c.nombre AS camara, s.nombre AS sitio
               FROM eventos e
               JOIN camaras c ON c.id = e.camara_id
               LEFT JOIN sitios s ON s.id = c.sitio_id
              WHERE 1 = 1"""
    args: list = []
    if camara_id:
        sql += " AND e.camara_id = ?"
        args.append(camara_id)
    if tipo:
        sql += " AND e.tipo = ?"
        args.append(tipo)
    sql += " ORDER BY e.inicio DESC LIMIT ?"
    args.append(limite)
    with db.sesion() as con:
        filas = [dict(f) for f in con.execute(sql, args).fetchall()]
    for f in filas:
        f["clip_url"] = f"/media/{f['clip']}" if f["clip"] else None
        try:
            f["datos"] = json.loads(f["datos"]) if f["datos"] else {}
        except json.JSONDecodeError:
            f["datos"] = {}
    return filas


def resumen() -> dict:
    with db.sesion() as con:
        total = con.execute("SELECT COUNT(*) n FROM eventos").fetchone()["n"]
        con_clip = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(clip_bytes),0) b FROM eventos "
            "WHERE clip IS NOT NULL").fetchone()
        por_tipo = con.execute(
            "SELECT tipo, COUNT(*) n FROM eventos GROUP BY tipo ORDER BY n DESC"
        ).fetchall()
    return {
        "total": total,
        "con_clip": con_clip["n"],
        "clips_gb": round(con_clip["b"] / 1e9, 3),
        "por_tipo": [dict(f) for f in por_tipo],
        "escuchando": escucha.estado(),
    }

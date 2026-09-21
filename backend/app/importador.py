"""Carga el parque en la base a partir de dos fuentes.

  inventory/cameras.csv   -> la planilla normalizada (que deberia haber)
  tools/inventario.json   -> el relevamiento de la sonda (que hay realmente)

La planilla manda para los datos administrativos (nombre, sitio, ubicacion, si
es PTZ). El relevamiento manda para todo lo tecnico: codec, resolucion, fps y
bitrate REAL medido. Cuando los dos discrepan gana el equipo, no el papel, pero
la discrepancia queda anotada.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from . import config, crypto, db

# --- helpers ---------------------------------------------------------------

CODECS_SNAPSHOT = {"mjpeg", "jpeg", "png"}


def _norm(texto: str | None) -> str:
    return "".join(ch for ch in (texto or "").lower() if ch.isalnum())


def _sitio_id(con, nombre: str | None) -> int | None:
    nombre = (nombre or "").strip()
    if not nombre:
        return None
    con.execute("INSERT OR IGNORE INTO sitios (nombre) VALUES (?)", (nombre,))
    fila = con.execute("SELECT id FROM sitios WHERE nombre = ?", (nombre,)).fetchone()
    return fila["id"] if fila else None


# --- credenciales ----------------------------------------------------------

def guardar_credencial(nombre: str, usuario: str, clave: str) -> int:
    """Alta o actualizacion de un juego de credenciales. La clave se cifra."""
    with db.sesion() as con:
        con.execute(
            """INSERT INTO credenciales (nombre, usuario, secreto)
               VALUES (?, ?, ?)
               ON CONFLICT(nombre) DO UPDATE SET usuario = excluded.usuario,
                                                 secreto = excluded.secreto""",
            (nombre, usuario, crypto.cifrar(clave)),
        )
        fila = con.execute("SELECT id FROM credenciales WHERE nombre = ?",
                           (nombre,)).fetchone()
        return fila["id"]


def credencial_de_camara(camara_id: int) -> tuple[str, str] | None:
    """Devuelve (usuario, clave) en claro para conectarse. Uso interno del
    grabador; nunca se expone por la API."""
    with db.sesion() as con:
        fila = con.execute(
            """SELECT c.usuario, c.secreto
                 FROM camaras cam JOIN credenciales c ON c.id = cam.credencial_id
                WHERE cam.id = ?""",
            (camara_id,),
        ).fetchone()
    if not fila:
        return None
    return fila["usuario"], crypto.descifrar(fila["secreto"])


# --- planilla --------------------------------------------------------------

def importar_planilla(ruta: Path | None = None) -> int:
    ruta = Path(ruta or config.INVENTARIO_CSV)
    if not ruta.exists():
        raise FileNotFoundError(f"no encuentro el inventario en {ruta}")

    cargadas = 0
    with db.sesion() as con, open(ruta, encoding="utf-8-sig", newline="") as fh:
        for fila in csv.DictReader(fh):
            ip = (fila.get("ip") or "").strip()
            nombre = (fila.get("nombre") or "").strip()
            if not ip or not nombre:
                continue

            con.execute(
                """INSERT INTO camaras
                       (nombre, sitio_id, ubicacion, ambiente, tipo, marca, modelo,
                        serie, mac, ip, mascara, gateway, red, ptz, lpr, fila_excel)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ip) DO UPDATE SET
                       nombre     = excluded.nombre,
                       sitio_id   = excluded.sitio_id,
                       ubicacion  = excluded.ubicacion,
                       ambiente   = excluded.ambiente,
                       tipo       = excluded.tipo,
                       marca      = excluded.marca,
                       modelo     = excluded.modelo,
                       serie      = excluded.serie,
                       mac        = excluded.mac,
                       mascara    = excluded.mascara,
                       gateway    = excluded.gateway,
                       red        = excluded.red,
                       ptz        = excluded.ptz,
                       lpr        = excluded.lpr,
                       fila_excel = excluded.fila_excel""",
                (
                    nombre,
                    _sitio_id(con, fila.get("sitio")),
                    (fila.get("ubicacion") or "").strip(),
                    (fila.get("ambiente") or "").strip(),
                    (fila.get("tipo") or "").strip(),
                    (fila.get("marca") or "").strip(),
                    (fila.get("modelo") or "").strip(),
                    (fila.get("serie") or "").strip(),
                    (fila.get("mac") or "").strip(),
                    ip,
                    (fila.get("mascara") or "").strip(),
                    (fila.get("gateway") or "").strip(),
                    (fila.get("red") or "").strip(),
                    1 if (fila.get("ptz") or "").strip() == "si" else 0,
                    1 if (fila.get("lpr") or "").strip() == "si" else 0,
                    int(fila["fila_excel"]) if (fila.get("fila_excel") or "").isdigit() else None,
                ),
            )
            cargadas += 1
    return cargadas


# --- relevamiento ----------------------------------------------------------

def importar_relevamiento(ruta: Path | None = None) -> dict:
    ruta = Path(ruta or config.RELEVAMIENTO_JSON)
    if not ruta.exists():
        raise FileNotFoundError(f"no encuentro el relevamiento en {ruta}")

    datos = json.loads(ruta.read_text(encoding="utf-8"))
    camaras = datos.get("cameras") or []

    resumen = {"total": len(camaras), "responden": 0, "con_video": 0,
               "actualizadas": 0, "sin_ficha": []}

    with db.sesion() as con:
        for reg in camaras:
            ip = reg.get("host")
            fila = con.execute("SELECT id FROM camaras WHERE ip = ?", (ip,)).fetchone()
            if not fila:
                resumen["sin_ficha"].append(ip)
                continue
            camara_id = fila["id"]

            if reg.get("ports"):
                resumen["responden"] += 1

            streams = _guardar_streams(con, camara_id, reg)
            if streams["video"]:
                resumen["con_video"] += 1

            credencial_id = None
            nombre_cred = reg.get("credential")
            if nombre_cred:
                cred = con.execute("SELECT id FROM credenciales WHERE nombre = ?",
                                   (nombre_cred,)).fetchone()
                credencial_id = cred["id"] if cred else None

            estado = "en_linea" if streams["video"] else (
                "sin_conexion" if not reg.get("ports") else "sin_video")

            con.execute(
                """UPDATE camaras SET
                       modelo_reportado = ?, onvif = ?, eventos_onvif = ?,
                       desfase_reloj_s = ?, estado = ?, perfil_grabacion = ?,
                       stream_base_id = ?, stream_evento_id = ?,
                       credencial_id = COALESCE(?, credencial_id),
                       visto = ?
                   WHERE id = ?""",
                (
                    (reg.get("device") or {}).get("model"),
                    1 if reg.get("onvif") else 0,
                    1 if reg.get("events") else 0,
                    reg.get("clock_skew_s"),
                    estado,
                    streams["perfil"],
                    streams["base_id"],
                    streams["evento_id"],
                    credencial_id,
                    datos.get("generated_at"),
                    camara_id,
                ),
            )

            con.execute("DELETE FROM notas_camara WHERE camara_id = ?", (camara_id,))
            for nota in reg.get("notes") or []:
                con.execute("INSERT INTO notas_camara (camara_id, texto) VALUES (?, ?)",
                            (camara_id, nota))

            resumen["actualizadas"] += 1

        con.execute(
            """INSERT INTO relevamientos (fecha, origen, total, responden, con_video)
               VALUES (?,?,?,?,?)""",
            (datos.get("generated_at"), str(ruta), resumen["total"],
             resumen["responden"], resumen["con_video"]),
        )

    return resumen


def _guardar_streams(con, camara_id: int, reg: dict) -> dict:
    """Guarda los streams y decide cual es la capa base y cual la de evento.

    Regla: de los streams de video que abrieron, el de menor resolucion es la
    base continua y el de mayor resolucion es el de evento. Los MJPEG quedan
    marcados como snapshot: las Bosch publican siempre uno que sirve para
    capturas fijas, no para grabar.
    """
    con.execute("DELETE FROM streams WHERE camara_id = ?", (camara_id,))

    video: list[tuple[int, int]] = []   # (id, pixeles)
    vistos: set[tuple] = set()
    for st in reg.get("streams") or []:
        medido = st.get("measured") or {}
        if not st.get("ok") or not medido:
            continue
        codec = (medido.get("codec") or "").lower()
        ancho = medido.get("width") or 0
        alto = medido.get("height") or 0

        # El fallback RTSP prueba varias URL del fabricante y en camaras como la
        # Axis M1114 dos de ellas devuelven exactamente el mismo stream. Sin
        # deduplicar, el duplicado se tomaria como capa base y dispararia el
        # consumo estimado.
        huella = (codec, ancho, alto, medido.get("fps"))
        if huella in vistos:
            continue
        vistos.add(huella)

        es_snapshot = codec in CODECS_SNAPSHOT or ancho == 0 or alto == 0

        cur = con.execute(
            """INSERT INTO streams
                   (camara_id, token, nombre, fuente, codec, ancho, alto, fps,
                    kbps, url, rol, medido)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (camara_id, None, st.get("profile"), st.get("source"), codec or None,
             ancho or None, alto or None, medido.get("fps"),
             medido.get("bitrate_real_kbps"), st.get("url"),
             "snapshot" if es_snapshot else None),
        )
        if not es_snapshot:
            video.append((cur.lastrowid, ancho * alto))

    if not video:
        return {"video": False, "perfil": config.PERFIL_SIN_CONFIGURAR,
                "base_id": None, "evento_id": None}

    video.sort(key=lambda par: par[1])
    base_id = video[0][0]
    evento_id = video[-1][0]

    if video[0][1] >= video[-1][1]:
        # Una sola calidad disponible: no hay capa base barata que sostener
        # 24/7, asi que esa camara solo puede grabar por evento.
        perfil = config.PERFIL_SOLO_EVENTO
        con.execute("UPDATE streams SET rol = 'evento' WHERE id = ?", (evento_id,))
        base_id = None
    else:
        perfil = config.PERFIL_BASE_EVENTO
        con.execute("UPDATE streams SET rol = 'base' WHERE id = ?", (base_id,))
        con.execute("UPDATE streams SET rol = 'evento' WHERE id = ?", (evento_id,))

    return {"video": True, "perfil": perfil,
            "base_id": base_id, "evento_id": evento_id}


def importar_todo() -> dict:
    db.inicializar()
    planilla = importar_planilla()
    try:
        relevamiento = importar_relevamiento()
    except FileNotFoundError:
        relevamiento = None
    return {"camaras_planilla": planilla, "relevamiento": relevamiento}

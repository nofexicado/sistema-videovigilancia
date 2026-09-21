"""Exportacion de evidencia: un MP4, su huella y quien se lo llevo.

Por que no alcanza con el MP4 de la reproduccion: ese archivo vive en una
cache que se purga, su nombre no dice nada y nadie queda registrado. Si alguien
pregunta "¿este video salio del sistema o lo editaron?", con la cache no hay
respuesta.

Lo que hace esto:

  1. Arma un MP4 del rango pedido, copiando sin recomprimir. Es importante que
     copie: recomprimir cambia cada pixel y destruye la posibilidad de
     comparar el archivo con el material original.
  2. Calcula el SHA-256 del archivo terminado.
  3. Escribe un archivo de texto al lado con la huella y los datos del pedido,
     para que el que reciba el video pueda verificarlo sin tener el sistema.
  4. Anota la exportacion en la base, con usuario y fecha.

El hash no prueba que el video sea autentico --nada lo prueba por si solo--
pero si prueba que el archivo que alguien tiene es byte por byte el que salio
de aca. Eso es lo que se puede afirmar sin exagerar, y es lo que dice el
comprobante.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import archivo, binarios, config, db

SALIDA = config.DATOS / "evidencia"

# Tope de lo que se puede pedir de una vez. No es una limitacion tecnica: es
# para que nadie se lleve el archivo entero con un clic y sin querer.
MAX_MINUTOS = 120


def _dt(v) -> datetime:
    d = v if isinstance(v, datetime) else datetime.fromisoformat(
        str(v).replace("Z", "+00:00"))
    return (d.replace(tzinfo=timezone.utc) if d.tzinfo is None
            else d.astimezone(timezone.utc))


def _tramos(camara_id: int, desde: datetime, hasta: datetime) -> list[dict]:
    """Los segmentos de la capa base que tocan el rango.

    Se incluye el que EMPIEZA antes del rango si lo cubre: un pedido que arranca
    a las 14:00:30 necesita el segmento de las 14:00:00, o el video empieza
    treinta segundos tarde.
    """
    with db.sesion() as con:
        filas = con.execute(
            # Los dos lados con `datetime()`: el indice guarda "...T...+00:00"
            # y `datetime()` devuelve "... ..." con espacio. Comparados como
            # texto no coinciden nunca. Mismo motivo que en preservar.py.
            """SELECT archivo, inicio, duracion FROM segmentos
                WHERE camara_id = ? AND capa = 'base'
                  AND datetime(inicio) < datetime(?)
                  AND datetime(inicio, '+' || CAST(COALESCE(duracion, 60)
                      AS INTEGER) || ' seconds') > datetime(?)
                ORDER BY inicio ASC""",
            (camara_id, hasta.isoformat(timespec="seconds"),
             desde.isoformat(timespec="seconds"))).fetchall()
    return [dict(f) for f in filas]


def exportar(camara_id: int, desde, hasta, motivo: str, usuario: str) -> dict:
    ffmpeg = binarios.ffmpeg()
    if not ffmpeg:
        raise RuntimeError("falta ffmpeg en el sistema")

    d, h = _dt(desde), _dt(hasta)
    if h <= d:
        raise ValueError("el final tiene que ser posterior al comienzo")
    minutos = (h - d).total_seconds() / 60
    if minutos > MAX_MINUTOS:
        raise ValueError(f"el maximo por exportacion es {MAX_MINUTOS} minutos "
                         f"(se pidieron {minutos:.0f})")
    if not motivo.strip():
        raise ValueError("hace falta un motivo: queda en el comprobante")

    with db.sesion() as con:
        cam = con.execute("SELECT nombre FROM camaras WHERE id = ?",
                          (camara_id,)).fetchone()
    if not cam:
        raise LookupError(f"no existe la camara {camara_id}")

    tramos = _tramos(camara_id, d, h)
    if not tramos:
        raise LookupError("no hay grabacion en ese rango")

    SALIDA.mkdir(parents=True, exist_ok=True)
    sello = d.astimezone().strftime("%Y%m%d-%H%M%S")
    base = f"{cam['nombre']}_{sello}_{int(minutos)}min"
    mp4 = SALIDA / f"{base}.mp4"
    lista = SALIDA / f"{base}.lista.txt"

    raiz = archivo._raiz()
    lista.write_text("".join(f"file '{raiz / t['archivo']}'\n" for t in tramos),
                     encoding="utf-8")

    # El recorte fino va con `-ss`/`-t` DESPUES del concat, sobre el resultado:
    # asi el rango sale exacto aunque los segmentos no empiecen justo ahi.
    desplazado = (d - _dt(tramos[0]["inicio"])).total_seconds()
    cmd = [ffmpeg, "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(lista),
           "-ss", f"{max(0.0, desplazado):.3f}", "-t", f"{(h - d).total_seconds():.3f}",
           # `copy`: no se recomprime. Ver el comentario del encabezado.
           "-c", "copy", "-movflags", "+faststart", str(mp4)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    lista.unlink(missing_ok=True)
    if not mp4.exists() or mp4.stat().st_size < 1024:
        raise RuntimeError("ffmpeg no pudo armar el archivo: "
                           + (r.stderr or "").strip()[-300:])

    sha = hashlib.sha256()
    with open(mp4, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            sha.update(bloque)
    huella = sha.hexdigest()
    tam = mp4.stat().st_size

    with db.sesion() as con:
        cur = con.execute(
            """INSERT INTO exportaciones
                   (camara_id, camara, desde, hasta, archivo, sha256, bytes,
                    motivo, usuario)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (camara_id, cam["nombre"], d.isoformat(timespec="seconds"),
             h.isoformat(timespec="seconds"), mp4.name, huella, tam,
             motivo.strip(), usuario))
        eid = cur.lastrowid

    comprobante(eid).write_text(_texto_comprobante(
        eid, cam["nombre"], d, h, mp4.name, huella, tam, motivo, usuario,
        len(tramos)), encoding="utf-8")

    return {"id": eid, "camara": cam["nombre"], "archivo": mp4.name,
            "sha256": huella, "bytes": tam, "mb": round(tam / 1e6, 2),
            "minutos": round(minutos, 1), "tramos": len(tramos),
            "url": f"/api/evidencia/{eid}/descargar",
            "url_comprobante": f"/api/evidencia/{eid}/comprobante"}


def comprobante(eid: int) -> Path:
    return SALIDA / f"comprobante-{eid}.txt"


def _texto_comprobante(eid, camara, d, h, nombre, huella, tam, motivo,
                       usuario, tramos) -> str:
    loc = lambda x: x.astimezone().strftime("%d/%m/%Y %H:%M:%S")   # noqa: E731
    return f"""COMPROBANTE DE EXPORTACION DE VIDEO
Sistema de Videovigilancia
================================================================

Exportacion numero .... {eid}
Archivo ............... {nombre}
Camara ................ {camara}

Desde ................. {loc(d)} (hora local)
Hasta ................. {loc(h)} (hora local)
Duracion .............. {(h - d).total_seconds() / 60:.1f} minutos
Tramos de origen ...... {tramos}

Exportado por ......... {usuario}
Fecha de exportacion .. {loc(datetime.now(timezone.utc))}
Motivo ................ {motivo.strip()}

Tamano ................ {tam} bytes ({tam / 1e6:.2f} MB)
SHA-256 ............... {huella}

----------------------------------------------------------------
COMO VERIFICAR QUE EL ARCHIVO NO FUE MODIFICADO

En Windows (PowerShell):
    Get-FileHash "{nombre}" -Algorithm SHA256

En Linux o macOS:
    sha256sum "{nombre}"

El resultado tiene que coincidir, caracter por caracter, con el
SHA-256 de arriba. Si coincide, el archivo es byte por byte el que
salio del sistema. Si difiere en un solo caracter, el archivo fue
modificado o se copio mal.

El video se extrajo COPIANDO el material original, sin recomprimir.

Esta huella acredita que el archivo no cambio desde que se exporto.
No acredita por si sola el contenido de la grabacion.
================================================================
"""


def listar(limite: int = 200) -> list[dict]:
    with db.sesion() as con:
        filas = con.execute(
            "SELECT * FROM exportaciones ORDER BY id DESC LIMIT ?",
            (limite,)).fetchall()
    out = []
    for f in filas:
        d = dict(f)
        d["mb"] = round((d["bytes"] or 0) / 1e6, 2)
        d["existe"] = (SALIDA / d["archivo"]).exists()
        out.append(d)
    return out


def ruta_de(eid: int) -> tuple[Path, dict] | None:
    with db.sesion() as con:
        f = con.execute("SELECT * FROM exportaciones WHERE id = ?",
                        (eid,)).fetchone()
    if not f:
        return None
    return SALIDA / f["archivo"], dict(f)


def espacio() -> dict:
    if not SALIDA.exists():
        return {"archivos": 0, "gb": 0.0}
    n = tam = 0
    for f in SALIDA.glob("*.mp4"):
        n += 1
        tam += f.stat().st_size
    return {"archivos": n, "gb": round(tam / 1e9, 3)}

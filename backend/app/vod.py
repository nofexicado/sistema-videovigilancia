"""Reproduccion de lo grabado: se arma un MP4 en el servidor y listo.

Por que no se sirve directamente lo que hay en disco:

* **Los segmentos no se pueden concatenar tal cual.** El grabador los escribe
  con `-reset_timestamps 1`, asi que TODOS empiezan en el mismo PTS -- medido
  sobre el parque real, 1,400000 en todos. Encadenados en una playlist HLS el
  tiempo avanza sesenta segundos y vuelve atras en cada tramo. ffmpeg, que es
  tolerante, igual lo protesta ("non monotonically increasing dts"); MediaSource
  no perdona nada y el reproductor se traba o repite el primer minuto. Eso es
  lo que hacia que la reproduccion "costara" aunque los archivos estuvieran
  perfectos.

* **Un tercio del parque no se puede decodificar en el navegador.** El archivo
  se graba con `-c copy`, o sea en el codec nativo de la camara, y de las 26
  que graban hay 7 en HEVC y una en MPEG-4. Chrome dice que no directamente:
  `MediaSource.isTypeSupported('video/mp4;codecs="hvc1..."')` da false. Esas
  grabaciones no habia forma de mirarlas, con el reproductor que fuera.

ffmpeg arregla las dos cosas de una pasada: concatena reescribiendo los
timestamps y, si hace falta, convierte a H.264. Lo que llega al navegador es un
MP4 comun que reproduce `<video>` sin hls.js ni MediaSource en el medio --
menos piezas que se puedan romper, y ademas con busqueda nativa.

Cuesta poco: medido en el R710 con 26 camaras grabando, cinco minutos de video
salen en 0,7 s copiando y en 4,3 s convirtiendo.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import archivo, binarios, config, db

CACHE = config.DATOS / "vod"

# Cuanto dura cada ventana que se arma. Cinco minutos sale del cronometro, no
# del gusto: medido en el R710 con 26 camaras grabando, diez minutos tardaban
# 1,3 s por copia pero 20 s convirtiendo H.265 y 32 s convirtiendo MPEG-4, y
# medio minuto mirando un cartel es inaceptable para lo que mas importa del
# sistema. Con cinco, y con el prefetch de la ventana siguiente, la espera
# solo se paga una vez al abrir.
VENTANA = float(os.environ.get("SISTEMA_VIDEOVIGILANCIA_VENTANA_VOD", "300"))

# Tope del cache en disco. Es material regenerable: cuando se pasa, se borra lo
# menos usado recientemente.
CACHE_GB = float(os.environ.get("SISTEMA_VIDEOVIGILANCIA_CACHE_VOD_GB", "8"))

# Codecs que el navegador reproduce sin ayuda. El resto se convierte.
CODECS_DIRECTOS = {"h264", "avc1"}

# Una ventana se arma UNA vez aunque la pidan tres pestanas a la vez.
_candados: dict[str, threading.Lock] = {}
_candado_maestro = threading.Lock()


def _lock(clave: str) -> threading.Lock:
    with _candado_maestro:
        return _candados.setdefault(clave, threading.Lock())


def codec_de(camara_id: int) -> str:
    """Codec con el que quedo grabada esta camara.

    Sale de la misma tabla que usa el grabador para elegir que capturar, asi
    que es el codec que tienen los .ts en disco -- el archivo es copia pura.
    """
    with db.sesion() as con:
        fila = con.execute(
            """SELECT COALESCE(b.codec, e.codec) AS codec
                 FROM camaras cam
                 LEFT JOIN streams b ON b.id = cam.stream_base_id
                 LEFT JOIN streams e ON e.id = cam.stream_evento_id
                WHERE cam.id = ?""",
            (camara_id,),
        ).fetchone()
    return ((fila["codec"] if fila else "") or "").lower()


def ventana_de(segundo: float) -> float:
    """A que ventana pertenece un instante del dia. Alinearlas a multiplos de
    `VENTANA` hace que dos clicks cercanos caigan en la misma y reusen el
    archivo ya armado en vez de generar uno nuevo por cada click."""
    return float(int(segundo // VENTANA) * VENTANA)


def _tramos_de_ventana(camara_id: int, fecha: str, inicio: float) -> list[dict]:
    """Los tramos que tocan [inicio, inicio+VENTANA) del dia, enteros.

    Enteros a proposito: recortar por tiempo exacto obligaria a recodificar
    tambien las camaras H.264, que hoy salen por copia en menos de un segundo.
    El desfasaje lo resuelve el frontend, que sabe donde arranca la ventana.
    """
    from datetime import datetime

    fin = inicio + VENTANA
    salida = []
    for t in archivo.linea_de_tiempo(camara_id, fecha):
        arranca = datetime.fromisoformat(t["inicio"]).astimezone()
        seg = arranca.hour * 3600 + arranca.minute * 60 + arranca.second
        if seg + t["duracion"] <= inicio or seg >= fin:
            continue
        t = dict(t)
        t["segundo_dia"] = float(seg)
        salida.append(t)
    return salida


def construir(camara_id: int, fecha: str, inicio: float) -> dict:
    """Arma (o reusa) el MP4 de una ventana. Devuelve la ruta y donde empieza.

    `comienza_en` es el segundo del dia en el que arranca el video de verdad:
    como se incluyen tramos enteros, puede ser un poco anterior a lo pedido.
    El frontend lo usa para posicionar el cursor sin adivinar.
    """
    ffmpeg = binarios.ffmpeg()
    if not ffmpeg:
        raise RuntimeError("falta ffmpeg en el sistema")

    inicio = ventana_de(inicio)
    tramos = _tramos_de_ventana(camara_id, fecha, inicio)
    if not tramos:
        raise LookupError("no hay grabacion en esa ventana")

    codec = codec_de(camara_id)
    convierte = codec not in CODECS_DIRECTOS
    firma = hashlib.sha256(
        f"{camara_id}|{fecha}|{inicio}|{VENTANA}|{convierte}|"
        f"{tramos[0]['archivo']}|{tramos[-1]['archivo']}|{len(tramos)}".encode()
    ).hexdigest()[:20]
    destino = CACHE / f"{camara_id}_{fecha}_{int(inicio)}_{firma}.mp4"

    datos = {
        "url": f"/api/camaras/{camara_id}/video.mp4?fecha={fecha}&inicio={int(inicio)}",
        "comienza_en": tramos[0]["segundo_dia"],
        "duracion": round(sum(t["duracion"] for t in tramos), 3),
        "convertido": convierte,
        "codec_origen": codec,
        "tramos": len(tramos),
        "ventana": VENTANA,
    }

    with _lock(firma):
        if destino.exists() and destino.stat().st_size > 1024:
            os.utime(destino, None)          # marca de uso, para la purga
            datos["ruta"] = destino
            datos["listo"] = True
            return datos

        CACHE.mkdir(parents=True, exist_ok=True)
        lista = destino.with_suffix(".txt")
        lista.write_text(
            "".join(f"file '{archivo._raiz() / t['archivo']}'\n" for t in tramos),
            encoding="utf-8")

        # `+genpts` es lo que endereza los timestamps repetidos de los .ts; sin
        # eso el MP4 sale con la misma inconsistencia que tiene el origen.
        # `ultrafast` no es descuido: esto se mira para revisar que paso, no
        # para archivar, y bajar de veryfast a ultrafast corta la espera casi a
        # la mitad. `min(480,ih)` evita agrandar: la Axis 221 entrega 320x240 y
        # estirarla a 480 solo gastaba CPU y bytes sin agregar un pixel de
        # informacion. `-threads 2` para no comerle los nucleos al grabador.
        video = (["-c:v", "copy"] if not convierte else
                 ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                  "-vf", "scale=-2:'min(480,ih)'", "-threads", "2"])
        parcial = destino.with_suffix(".parcial.mp4")
        comando = ([ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lista), "-an", "-map", "0:v:0"] + video +
                   ["-fflags", "+genpts", "-movflags", "+faststart", str(parcial)])
        try:
            proc = subprocess.run(comando, capture_output=True, text=True,
                                  timeout=900)
        finally:
            lista.unlink(missing_ok=True)

        if proc.returncode != 0 or not parcial.exists():
            parcial.unlink(missing_ok=True)
            raise RuntimeError((proc.stderr or "").strip()[-300:] or
                               "ffmpeg no pudo armar el video")
        # Se renombra al final: asi nadie sirve un archivo a medio escribir.
        parcial.replace(destino)

    purgar_cache()
    datos["ruta"] = destino
    datos["listo"] = True
    return datos


def purgar_cache(tope_gb: float | None = None) -> int:
    """Borra lo menos usado cuando el cache se pasa del tope."""
    tope = (tope_gb if tope_gb is not None else CACHE_GB) * 1e9
    if not CACHE.exists():
        return 0
    archivos = []
    total = 0
    for f in CACHE.glob("*.mp4"):
        try:
            st = f.stat()
        except OSError:
            continue
        archivos.append((st.st_atime, st.st_size, f))
        total += st.st_size
    if total <= tope:
        return 0
    archivos.sort()                       # el mas viejo de uso, primero
    borrados = 0
    for _atime, tam, f in archivos:
        if total <= tope:
            break
        try:
            f.unlink()
            total -= tam
            borrados += 1
        except OSError:
            pass
    return borrados


def estado_cache() -> dict:
    if not CACHE.exists():
        return {"archivos": 0, "gb": 0.0, "tope_gb": CACHE_GB, "ventana": VENTANA}
    tam = 0
    n = 0
    for f in CACHE.glob("*.mp4"):
        try:
            tam += f.stat().st_size
            n += 1
        except OSError:
            pass
    libre = 0.0
    try:
        libre = shutil.disk_usage(CACHE).free / 1e9
    except OSError:
        pass
    return {"archivos": n, "gb": round(tam / 1e9, 3), "tope_gb": CACHE_GB,
            "ventana": VENTANA, "libre_gb": round(libre, 1)}


def limpiar_todo() -> int:
    """Borra el cache entero. Util cuando cambia como se arma el video."""
    if not CACHE.exists():
        return 0
    n = 0
    for f in list(CACHE.glob("*")):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n

"""Ubicacion de ffmpeg y ffprobe.

En Windows winget instala ffmpeg en una carpeta que no queda en el PATH de la
sesion actual, asi que ademas de `which` buscamos en las rutas habituales. En
Ubuntu `apt install ffmpeg` los deja en /usr/bin y alcanza con `which`.

Se puede forzar la ruta con SISTEMA_VIDEOVIGILANCIA_FFMPEG / SISTEMA_VIDEOVIGILANCIA_FFPROBE.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_cache: dict[str, str | None] = {}

_RAICES_WINDOWS = [
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links",
    r"%ProgramFiles%\ffmpeg\bin",
    r"C:\ffmpeg\bin",
]


def _buscar(nombre: str) -> str | None:
    forzado = os.environ.get(f"SISTEMA_VIDEOVIGILANCIA_{nombre.upper()}")
    if forzado and Path(forzado).exists():
        return forzado

    hallado = shutil.which(nombre)
    if hallado:
        return hallado

    if sys.platform != "win32":
        return None

    exe = f"{nombre}.exe"
    for raiz in _RAICES_WINDOWS:
        base = Path(os.path.expandvars(raiz))
        if not base.is_dir():
            continue
        directo = base / exe
        if directo.exists():
            return str(directo)
        for dirpath, _dirs, files in os.walk(base):
            if exe in files:
                return str(Path(dirpath) / exe)
    return None


def ruta(nombre: str) -> str | None:
    if nombre not in _cache:
        _cache[nombre] = _buscar(nombre)
    return _cache[nombre]


def ffmpeg() -> str | None:
    return ruta("ffmpeg")


def ffprobe() -> str | None:
    return ruta("ffprobe")

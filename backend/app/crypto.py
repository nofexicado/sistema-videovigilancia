"""Cifrado de las credenciales de las camaras.

Las claves de las camaras nunca se guardan en texto plano en la base. Se
cifran con Fernet (AES-128-CBC + HMAC) usando una llave que vive en un archivo
aparte, con permisos restringidos.

Separar llave y base importa: una copia de seguridad de la base, o el archivo
robado, no alcanzan para leer las contrasenas.
"""

from __future__ import annotations

import os
import stat
import sys

from cryptography.fernet import Fernet, InvalidToken

from . import config

_fernet: Fernet | None = None


def _cargar_llave() -> bytes:
    """Devuelve la llave, creandola en el primer arranque."""
    config.asegurar_directorios()
    ruta = config.CLAVE_PATH

    if ruta.exists():
        return ruta.read_bytes().strip()

    llave = Fernet.generate_key()
    ruta.write_bytes(llave)
    _restringir_permisos(ruta)
    return llave


def _restringir_permisos(ruta) -> None:
    """0600 en Unix. En Windows los permisos POSIX no aplican; el archivo queda
    bajo el perfil del usuario, que es la proteccion que da el sistema."""
    if sys.platform == "win32":
        return
    try:
        os.chmod(ruta, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _motor() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_cargar_llave())
    return _fernet


def cifrar(texto: str) -> bytes:
    return _motor().encrypt(texto.encode("utf-8"))


def descifrar(dato: bytes) -> str:
    """Descifra. Si la llave no corresponde, falla explicito en vez de devolver
    basura: es preferible un error claro a intentar conectar con una clave rota."""
    try:
        return _motor().decrypt(dato).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "no se pudo descifrar la credencial: la llave en "
            f"{config.CLAVE_PATH} no corresponde a esta base de datos"
        ) from exc


def enmascarar(texto: str) -> str:
    """Para mostrar en pantalla o en logs. Nunca devuelve la clave."""
    if not texto:
        return ""
    return "*" * 8

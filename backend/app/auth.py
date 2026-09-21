"""Autenticacion y roles.

Dos roles: `operador` (solo mira: muro, reproduccion, eventos) y `admin` (ademas
modifica: plano, grupos, camaras, credenciales). La regla vive en el backend
-- el frontend solo esconde botones, no es seguridad.

Sin dependencias nuevas: el hash de contraseñas es pbkdf2 de la stdlib y la
sesion es una cookie firmada con HMAC, sin estado en el servidor (asi sobrevive
a un reinicio del servicio sin echar a nadie).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from . import config, db

ITERACIONES = 210_000
VIGENCIA_SEG = 12 * 3600          # una jornada; despues hay que volver a entrar
COOKIE = "sistema-videovigilancia_sesion"
ROLES = ("operador", "admin")

_clave_sesion: bytes | None = None


# -- contraseñas ------------------------------------------------------------

def hashear(clave: str) -> str:
    """`pbkdf2$iteraciones$salt$hash`, todo en hex. El salt es por usuario."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", clave.encode(), salt, ITERACIONES)
    return f"pbkdf2${ITERACIONES}${salt.hex()}${dk.hex()}"


def verificar_clave(clave: str, guardado: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = guardado.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", clave.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
    except (ValueError, TypeError):
        return False
    # compare_digest: tiempo constante, no filtra por cuanto tardo en fallar.
    return hmac.compare_digest(dk.hex(), hash_hex)


# -- usuarios en la base ----------------------------------------------------

# -- como se arma el nombre de usuario --------------------------------------

_SIN_TILDE = str.maketrans("áàäâãéèëêíìïîóòöôõúùüûñçÁÀÄÂÃÉÈËÊÍÌÏÎÓÒÖÔÕÚÙÜÛÑÇ",
                           "aaaaaeeeeiiiiooooouuuuncAAAAAEEEEIIIIOOOOOUUUUNC")


def usuario_de(nombre: str, apellido: str) -> str:
    """Primera letra del apellido seguida del nombre, en minuscula.

    "Mauricio Figueroa" -> "fmauricio".

    Se genera y no se escribe a mano para que el nombre de usuario sea
    predecible: al leer la auditoria seis meses despues, `fmauricio` se
    resuelve solo. Se quitan tildes y espacios porque despues hay que poder
    tipearlo en un teclado cualquiera.
    """
    limpio = lambda t: "".join(                                   # noqa: E731
        c for c in t.strip().translate(_SIN_TILDE).lower() if c.isalnum())
    n, a = limpio(nombre), limpio(apellido)
    if not n or not a:
        raise ValueError("hacen falta nombre y apellido")
    return a[0] + n


def crear_usuario(nombre: str, clave: str, rol: str,
                  nombre_completo: str | None = None) -> None:
    if rol not in ROLES:
        raise ValueError(f"rol invalido: {rol} (usar {' o '.join(ROLES)})")
    with db.sesion() as con:
        con.execute(
            """INSERT INTO usuarios (nombre, rol, clave_hash, nombre_completo)
               VALUES (?,?,?,?)
               ON CONFLICT(nombre) DO UPDATE SET rol=excluded.rol,
                                                 clave_hash=excluded.clave_hash,
                                                 nombre_completo=COALESCE(
                                                   excluded.nombre_completo,
                                                   usuarios.nombre_completo)""",
            (nombre.strip(), rol, hashear(clave), nombre_completo))


def crear_persona(nombre: str, apellido: str, clave: str, rol: str) -> dict:
    """Alta desde la interfaz: se piden nombre y apellido, el usuario se arma."""
    usuario = usuario_de(nombre, apellido)
    completo = f"{apellido.strip().title()}, {nombre.strip().title()}"
    with db.sesion() as con:
        ya = con.execute("SELECT 1 FROM usuarios WHERE nombre = ?",
                         (usuario,)).fetchone()
    if ya:
        raise ValueError(f"ya existe el usuario '{usuario}'")
    crear_usuario(usuario, clave, rol, completo)
    return {"usuario": usuario, "nombre_completo": completo, "rol": rol}


def borrar_usuario(usuario: str) -> bool:
    """No se puede quedar el sistema sin ningun admin: si se borrara el ultimo,
    nadie podria volver a entrar a configurar nada."""
    with db.sesion() as con:
        f = con.execute("SELECT rol FROM usuarios WHERE nombre = ?",
                        (usuario,)).fetchone()
        if not f:
            return False
        if f["rol"] == "admin":
            otros = con.execute(
                "SELECT COUNT(*) n FROM usuarios WHERE rol='admin' AND nombre != ?",
                (usuario,)).fetchone()["n"]
            if not otros:
                raise ValueError("es el ultimo administrador: no se puede borrar")
        con.execute("DELETE FROM usuarios WHERE nombre = ?", (usuario,))
    return True


def autenticar(nombre: str, clave: str) -> dict | None:
    with db.sesion() as con:
        f = con.execute("SELECT nombre, rol, clave_hash FROM usuarios WHERE nombre = ?",
                        (nombre.strip(),)).fetchone()
    if not f or not verificar_clave(clave, f["clave_hash"]):
        return None
    return {"usuario": f["nombre"], "rol": f["rol"]}


def listar_usuarios() -> list[dict]:
    with db.sesion() as con:
        return [dict(f) for f in con.execute(
            "SELECT nombre, rol, nombre_completo, creado FROM usuarios "
            "ORDER BY nombre").fetchall()]


def hay_usuarios() -> bool:
    with db.sesion() as con:
        return con.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone() is not None


# -- cookie de sesion firmada ----------------------------------------------

def _clave() -> bytes:
    """Secreto para firmar cookies. Vive en datos/sesion.key, 0600. Si se
    regenera, todas las sesiones abiertas dejan de valer -- que es justo lo que
    se quiere si se sospecha que se filtro."""
    global _clave_sesion
    if _clave_sesion is not None:
        return _clave_sesion
    ruta = config.DATOS / "sesion.key"
    if ruta.exists():
        _clave_sesion = ruta.read_bytes()
    else:
        config.asegurar_directorios()
        _clave_sesion = secrets.token_bytes(32)
        ruta.write_bytes(_clave_sesion)
        try:
            ruta.chmod(0o600)
        except OSError:
            pass          # en Windows chmod es cosmetico, no pasa nada
    return _clave_sesion


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _de_b64(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def emitir_cookie(usuario: str, rol: str) -> str:
    cuerpo = _b64(json.dumps(
        {"u": usuario, "r": rol, "exp": int(time.time()) + VIGENCIA_SEG},
        separators=(",", ":")).encode())
    firma = _b64(hmac.new(_clave(), cuerpo.encode(), hashlib.sha256).digest())
    return f"{cuerpo}.{firma}"


def leer_cookie(valor: str | None) -> dict | None:
    if not valor or "." not in valor:
        return None
    cuerpo, firma = valor.rsplit(".", 1)
    esperada = _b64(hmac.new(_clave(), cuerpo.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(firma, esperada):
        return None
    try:
        datos = json.loads(_de_b64(cuerpo))
    except (ValueError, json.JSONDecodeError):
        return None
    if datos.get("exp", 0) < time.time():
        return None
    if datos.get("r") not in ROLES:
        return None
    return {"usuario": datos.get("u"), "rol": datos.get("r")}

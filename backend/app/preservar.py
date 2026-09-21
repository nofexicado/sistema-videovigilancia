"""Material preservado: tramos que la purga no puede borrar.

Por que existe: la retencion configurable que tiene el sistema borra por
antiguedad y por espacio, y no perdona. Si alguien baja la retencion de 25 dias
a 3, o si el disco se acerca al tope, se va material que podia ser la unica
prueba de algo. Antes de esto no habia forma de decirle al sistema "esto no".

Se guarda como RANGO de tiempo por camara y no como una marca en cada
segmento. La razon es concreta: `archivo.indexar()` reconstruye la tabla de
segmentos leyendo el disco, y de hecho ya la borro entera una vez en una
migracion. Una marca puesta en una fila de `segmentos` se habria perdido ahi;
un rango de tiempo sobrevive.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import config, db


def _iso(v) -> str:
    """Acepta un ISO con o sin zona y devuelve ISO en UTC, como el indice."""
    if isinstance(v, datetime):
        d = v
    else:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def crear(camara_id: int, desde, hasta, motivo: str, usuario: str) -> dict:
    d, h = _iso(desde), _iso(hasta)
    if h <= d:
        raise ValueError("el final tiene que ser posterior al comienzo")
    with db.sesion() as con:
        cam = con.execute("SELECT nombre FROM camaras WHERE id = ?",
                          (camara_id,)).fetchone()
        if not cam:
            raise LookupError(f"no existe la camara {camara_id}")
        cur = con.execute(
            """INSERT INTO preservaciones (camara_id, desde, hasta, motivo, usuario)
               VALUES (?,?,?,?,?)""", (camara_id, d, h, motivo.strip(), usuario))
        pid = cur.lastrowid
        # El mismo criterio de solapamiento que usa la purga, para que el
        # numero que ve el usuario sea el que de verdad se protegio.
        n = con.execute(
            f"""SELECT COUNT(*) n FROM segmentos
                 WHERE camara_id = ? AND datetime(inicio) < datetime(?)
                   AND datetime(inicio, '+' || CAST(COALESCE(duracion,
                       {config.SEGUNDOS_POR_SEGMENTO}) AS INTEGER)
                       || ' seconds') > datetime(?)""",
            (camara_id, h, d)).fetchone()["n"]
    return {"id": pid, "camara_id": camara_id, "camara": cam["nombre"],
            "desde": d, "hasta": h, "motivo": motivo.strip(),
            "usuario": usuario, "segmentos": n}


def borrar(pid: int) -> dict | None:
    """Levantar una preservacion. Solo admin, y queda en la auditoria: dejar de
    proteger material es una decision tan relevante como protegerlo."""
    with db.sesion() as con:
        f = con.execute(
            """SELECT p.*, c.nombre AS camara FROM preservaciones p
                 LEFT JOIN camaras c ON c.id = p.camara_id
                WHERE p.id = ?""", (pid,)).fetchone()
        if not f:
            return None
        con.execute("DELETE FROM preservaciones WHERE id = ?", (pid,))
    return dict(f)


def listar() -> list[dict]:
    with db.sesion() as con:
        filas = con.execute(
            """SELECT p.*, c.nombre AS camara,
                      (SELECT COUNT(*) FROM segmentos s
                        WHERE s.camara_id = p.camara_id
                          AND datetime(s.inicio) < datetime(p.hasta)
                          AND datetime(s.inicio, '+' || CAST(COALESCE(
                              s.duracion, 60) AS INTEGER) || ' seconds')
                              > datetime(p.desde)) AS segmentos,
                      (SELECT COALESCE(SUM(s.bytes),0) FROM segmentos s
                        WHERE s.camara_id = p.camara_id
                          AND datetime(s.inicio) < datetime(p.hasta)
                          AND datetime(s.inicio, '+' || CAST(COALESCE(
                              s.duracion, 60) AS INTEGER) || ' seconds')
                              > datetime(p.desde)) AS bytes
                 FROM preservaciones p
                 LEFT JOIN camaras c ON c.id = p.camara_id
                ORDER BY p.id DESC""").fetchall()
    return [dict(f) | {"gb": round((f["bytes"] or 0) / 1e9, 3)} for f in filas]


def de_camara(camara_id: int) -> list[dict]:
    """Los rangos de una camara, para que la barra de tiempo los dibuje."""
    with db.sesion() as con:
        filas = con.execute(
            "SELECT id, desde, hasta, motivo FROM preservaciones "
            "WHERE camara_id = ? ORDER BY desde", (camara_id,)).fetchall()
    return [dict(f) for f in filas]


def condicion_sql(alias: str = "") -> str:
    """Fragmento que excluye lo preservado de un DELETE sobre `segmentos`.

    Se devuelve como texto para que la purga lo pegue en su propia consulta:
    filtrar en SQL y no en Python es lo que garantiza que NINGUNA pasada de
    purga pueda saltearse la proteccion por olvido.

    La condicion es de SOLAPAMIENTO y no de comienzo, y la diferencia importa:
    un segmento que arranca 13:59:30 y dura 60 s contiene material protegido si
    se preservo desde las 14:00. Comparando solo `inicio >= desde` ese segmento
    quedaba afuera y se perdian los primeros treinta segundos de justo lo que
    se quiso guardar. Se protege todo segmento que TOQUE el rango.

    Y los DOS lados se normalizan con `datetime()`. Esto no es adorno: el
    indice guarda `2026-09-11T18:47:39+00:00` --con T y con huso-- mientras
    `datetime()` devuelve `2026-09-11 18:47:39` con un espacio. Comparados como
    texto, el espacio (0x20) siempre pierde contra la T (0x54), asi que la
    condicion daba falso SIEMPRE y no se protegia nada. El sintoma era el peor
    posible: la funcion parecia andar y no protegia una sola grabacion.
    """
    p = f"{alias}." if alias else ""
    fin = (f"datetime({p}inicio, '+' || CAST(COALESCE({p}duracion, "
           f"{config.SEGUNDOS_POR_SEGMENTO}) AS INTEGER) || ' seconds')")
    return (f"NOT EXISTS (SELECT 1 FROM preservaciones pr "
            f"WHERE pr.camara_id = {p}camara_id "
            f"AND datetime({p}inicio) < datetime(pr.hasta) "
            f"AND {fin} > datetime(pr.desde))")


def resumen() -> dict:
    with db.sesion() as con:
        f = con.execute("SELECT COUNT(*) n FROM preservaciones").fetchone()
        g = con.execute(
            f"""SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b FROM segmentos s
                 WHERE NOT ({condicion_sql('s')})""").fetchone()
    return {"rangos": f["n"], "segmentos": g["n"],
            "gb": round((g["b"] or 0) / 1e9, 3)}

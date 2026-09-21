"""Registro de auditoria: quien hizo que, cuando y desde donde.

Por que existe: un sistema de videovigilancia sin auditoria no se puede
defender en una investigacion. La pregunta que hay que poder contestar no es
"que paso en el playon" sino "quien mira las camaras, quien se llevo un video
y quien apago la grabacion". Sin este registro, la respuesta honesta es "no
se", y eso convierte al sistema en un problema en vez de una prueba.

Se escribe desde el middleware --asi ninguna ruta puede olvidarse de
registrar-- y los endpoints que tienen algo interesante que contar agregan su
propio detalle.

El registro es SOLO DE ESCRITURA desde el punto de vista de la aplicacion: no
hay endpoint que borre ni edite una fila. Si se pudiera limpiar desde la
interfaz, no serviria para nada.
"""
from __future__ import annotations

from . import db

# Acciones que se nombran a proposito, en vez de dejar sólo el método y la
# ruta: el que lee la auditoría seis meses después no tiene por qué saber que
# `POST /api/grabador/detener` es "apagó la grabación del parque".
NOMBRES = {
    ("POST", "/api/login"):                "entró al sistema",
    ("POST", "/api/logout"):               "salió del sistema",
    ("POST", "/api/grabador/detener"):     "DETUVO la grabación",
    ("POST", "/api/grabador/iniciar"):     "inició la grabación",
    ("POST", "/api/grabador/relanzar"):    "relanzó la captura de una cámara",
    ("POST", "/api/archivo/purgar"):       "PURGÓ el archivo",
    ("PUT",  "/api/archivo/retencion"):    "cambió los días de retención",
    ("POST", "/api/evidencia"):            "EXPORTÓ evidencia",
    ("POST", "/api/preservaciones"):       "preservó material",
    ("POST", "/api/usuarios"):             "creó o modificó un usuario",
    ("POST", "/api/importar"):             "reimportó el inventario",
    ("POST", "/api/eventos/escuchar"):     "inició la escucha de eventos",
    ("PUT",  "/api/eventos/seleccion"):    "cambió qué cámaras escuchan eventos",
}

# Lo que se registra aunque sea una lectura. Ver una grabación es justamente
# el acto que hay que poder auditar.
LECTURAS = {"ver_grabacion", "descargar_evidencia"}

# Algunas acciones se nombran por lo que SON y no por la ruta que usaron. Sin
# esto, un intento fallido de entrar aparecía como "entró al sistema" —porque
# la ruta es la misma— que es exactamente al revés de lo que pasó.
POR_ACCION = {
    "entrar":         "entró al sistema",
    "entrar_fallido": "INTENTO FALLIDO de entrar",
    "salir":          "salió del sistema",
    "denegado":       "intentó una acción sin permiso",
}


def registrar(usuario: str | None, rol: str | None, accion: str,
              metodo: str = "", ruta: str = "", estado: int | None = None,
              detalle: str | None = None, ip: str | None = None) -> None:
    """Anota una linea. Nunca levanta: que falle la auditoria no puede tumbar
    la operacion que se estaba auditando."""
    try:
        with db.sesion() as con:
            con.execute(
                """INSERT INTO auditoria
                       (usuario, rol, ip, accion, metodo, ruta, estado, detalle)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (usuario, rol, ip, accion, metodo, ruta, estado, detalle))
    except Exception as exc:                      # noqa: BLE001
        print(f"[auditoria] no se pudo registrar {accion}: {exc}")


def nombre_de(metodo: str, ruta: str) -> str:
    """Como se lee la accion en la pantalla."""
    n = NOMBRES.get((metodo, ruta))
    if n:
        return n
    # Las rutas con id adentro no entran en el diccionario; se describen por
    # forma. Es feo enumerarlas, pero es mejor que mostrarle a alguien
    # "DELETE /api/camaras/17" y que tenga que interpretarlo.
    partes = ruta.strip("/").split("/")
    if len(partes) >= 3 and partes[1] == "camaras":
        cola = partes[3] if len(partes) > 3 else ""
        if cola == "ptz":
            return "movió una cámara PTZ"
        if cola == "video":
            return "cambió la calidad de una cámara"
        if cola == "grupo":
            return "movió una cámara de locación"
        if metodo == "DELETE":
            return "ELIMINÓ una cámara"
        if metodo == "PATCH":
            return "modificó una cámara"
    if len(partes) >= 2 and partes[1] == "preservaciones":
        # Levantar una proteccion se nombra fuerte: es la accion que deja
        # material otra vez al alcance de la purga.
        return ("LEVANTÓ la protección de un tramo" if metodo == "DELETE"
                else "preservó material")
    if len(partes) >= 2 and partes[1] == "usuarios":
        if metodo == "DELETE":
            return "ELIMINÓ un usuario"
        return "creó o modificó un usuario"
    if len(partes) >= 2 and partes[1] == "evidencia":
        return "EXPORTÓ evidencia"
    if len(partes) >= 2 and partes[1] == "grupos":
        return {"POST": "creó una locación", "PATCH": "editó una locación",
                "DELETE": "ELIMINÓ una locación",
                "PUT": "cambió las cámaras de una locación"}.get(metodo, "tocó una locación")
    return f"{metodo} {ruta}"


def listar(limite: int = 300, usuario: str | None = None,
           solo_criticas: bool = False, desde: str | None = None) -> list[dict]:
    sql = ["SELECT * FROM auditoria WHERE 1 = 1"]
    args: list = []
    if usuario:
        sql.append("AND usuario = ?")
        args.append(usuario)
    if desde:
        sql.append("AND fecha >= ?")
        args.append(desde)
    if solo_criticas:
        # Las que cambian o se llevan material. Son las que se miran primero
        # cuando algo no cierra.
        sql.append("AND (accion IN ('exportar','detener','purgar','retencion',"
                   "'eliminar','usuario','entrar_fallido') OR metodo = 'DELETE')")
    sql.append("ORDER BY id DESC LIMIT ?")
    args.append(limite)
    with db.sesion() as con:
        filas = con.execute(" ".join(sql), args).fetchall()
    salida = []
    for f in filas:
        d = dict(f)
        if d["accion"] in POR_ACCION:
            d["que"] = POR_ACCION[d["accion"]]
        elif d["accion"] in LECTURAS and d["detalle"]:
            d["que"] = d["detalle"]
        else:
            d["que"] = nombre_de(d["metodo"] or "", d["ruta"] or "")
        # Una accion que no salio bien no se puede leer como si hubiera salido.
        if d["estado"] and d["estado"] >= 400 and d["accion"] not in POR_ACCION:
            d["que"] = f"intentó: {d['que']} (falló con {d['estado']})"
        salida.append(d)
    return salida


def resumen() -> dict:
    """Cuatro numeros para la cabecera de la pantalla."""
    with db.sesion() as con:
        f = con.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN accion = 'exportar' THEN 1 ELSE 0 END) exportaciones,
                      SUM(CASE WHEN accion = 'entrar_fallido' THEN 1 ELSE 0 END) fallidos,
                      MIN(fecha) desde
                 FROM auditoria""").fetchone()
        activos = con.execute(
            """SELECT usuario, COUNT(*) n FROM auditoria
                WHERE usuario IS NOT NULL AND fecha >= datetime('now','-7 days')
                GROUP BY usuario ORDER BY n DESC LIMIT 5""").fetchall()
    return {"total": f["total"] or 0,
            "exportaciones": f["exportaciones"] or 0,
            "fallidos": f["fallidos"] or 0,
            "desde": f["desde"],
            "mas_activos": [dict(a) for a in activos]}

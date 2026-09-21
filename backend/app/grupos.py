"""Grupos de camaras del plano, en arbol.

Un grupo es una agrupacion **del usuario**, no del inventario: junta las camaras
que a alguien le sirve mirar juntas -- el laboratorio, el pack de ingreso -- sin
importar en que sitio esten cargadas. Por eso puede cruzar sitios y partirlos, y
por eso una camara puede estar en mas de uno.

**Los grupos anidan.** Los de primer nivel (`padre_id IS NULL`) son los
sitios o yacimientos del parque -- cada uno con su nombre propio -- y son
los unicos que se dibujan sobre el mapa. Adentro de cada uno cuelgan los
subgrupos: departamentos, procesos, talleres, enfermeria, repetidoras, pack de
ingreso. No hay tabla aparte para los yacimientos porque un yacimiento *es* un
grupo sin padre; con una sola tabla se puede reacomodar el arbol sin que nadie
tenga que decidir de antemano cuantos niveles hay.

Un grupo puede tener camaras propias Y subgrupos a la vez: la del porton puede
colgar del yacimiento aunque el resto este repartido en talleres.

La posicion en el plano se guarda en coordenadas del viewBox (1200x820), no en
pixeles de pantalla: el plano se redimensiona con la ventana y los grupos tienen
que quedar donde los pusieron.
"""

from __future__ import annotations

from . import db

# Ancho y alto del lienzo del plano. Las posiciones se recortan a esta caja para
# que un grupo no pueda quedar fuera de la pantalla.
ANCHO = 1200.0
ALTO = 820.0

# Reparto inicial cuando se siembran los grupos desde los sitios, para no
# empezar de una pantalla en blanco.
#
# Las coordenadas caen todas DENTRO del contorno de la isla que dibuja el plano
# y separadas entre si; verificado con punto-en-poligono contra ese trazo. No
# son la ubicacion real de cada yacimiento -- eso lo acomoda el usuario
# arrastrando --, solo un punto de partida que no quede en el mar.
SIEMBRA = [
    ("Sitio Norte", 486, 288, ["Planta Norte", "Deposito Norte"]),
    ("Sitio Este", 470, 185, ["Sitio Este"]),
    ("Sitio Sur", 430, 360, ["Sitio Sur"]),
    ("Sitio Oeste", 620, 420, ["Sitio Oeste"]),
]


def _acotar(x: float, y: float) -> tuple[float, float]:
    return (min(max(float(x), 0.0), ANCHO), min(max(float(y), 0.0), ALTO))


def listar() -> list[dict]:
    """Todos los grupos, planos, con sus camaras y los totales del subarbol.

    Plano y no anidado a proposito: el frontend arma el arbol con `padre_id`, y
    asi la misma respuesta sirve para dibujar el mapa (solo los de primer
    nivel) y para las listas de "mover a...", que necesitan todos.

    `total` / `en_linea` cuentan el SUBARBOL -- el nodo del yacimiento tiene
    que decir cuantas camaras hay abajo, no cuantas cuelgan directamente de el.
    Las propias quedan aparte en `total_propias`.
    """
    with db.sesion() as con:
        grupos = [dict(f) for f in con.execute(
            "SELECT id, nombre, x, y, padre_id FROM grupos ORDER BY nombre").fetchall()]
        filas = con.execute(
            """SELECT gc.grupo_id, cam.id, cam.nombre, cam.marca, cam.modelo,
                      cam.estado, s.nombre AS sitio
                 FROM grupos_camaras gc
                 JOIN camaras cam ON cam.id = gc.camara_id
                 LEFT JOIN sitios s ON s.id = cam.sitio_id
                ORDER BY cam.nombre""").fetchall()

    por_grupo: dict[int, list[dict]] = {}
    for f in filas:
        d = dict(f)
        por_grupo.setdefault(d.pop("grupo_id"), []).append(d)

    indice = {g["id"]: g for g in grupos}
    hijos: dict[object, list[int]] = {}
    for g in grupos:
        g["camaras"] = por_grupo.get(g["id"], [])
        g["total_propias"] = len(g["camaras"])
        g["en_linea_propias"] = sum(
            1 for c in g["camaras"] if c["estado"] == "en_linea")
        hijos.setdefault(g["padre_id"], []).append(g["id"])

    for g in grupos:
        g["hijos"] = hijos.get(g["id"], [])
        g["subgrupos"] = len(g["hijos"])

    def acumular(gid, visitados):
        """Cuenta camaras DISTINTAS del subarbol, no la suma de las cuentas.

        Una camara puede estar colgada del padre y del subgrupo a la vez -- es
        legal y pasa: dos camaras de un subgrupo estaban tambien en el sitio
        padre, y el arbol mostraba "7/7" para un yacimiento con 5 equipos. Sumar cantidades
        la contaba dos veces; unir identidades no. Con esto la suma de los
        yacimientos vuelve a dar el parque.
        """
        if gid in visitados:                 # arbol roto: no colgarse
            return set(), set()
        visitados.add(gid)
        g = indice[gid]
        ids = {c["id"] for c in g["camaras"]}
        vivas = {c["id"] for c in g["camaras"] if c["estado"] == "en_linea"}
        for h in g["hijos"]:
            i, v = acumular(h, visitados)
            ids |= i
            vivas |= v
        g["total"], g["en_linea"] = len(ids), len(vivas)
        return ids, vivas

    for g in grupos:
        acumular(g["id"], set())
    return grupos


def _descendientes(con, grupo_id: int) -> list[int]:
    """Ids de todo lo que cuelga de este grupo, sin incluirlo."""
    salida, pendientes, vistos = [], [grupo_id], {grupo_id}
    while pendientes:
        actual = pendientes.pop()
        for f in con.execute("SELECT id FROM grupos WHERE padre_id = ?", (actual,)):
            if f["id"] in vistos:
                continue
            vistos.add(f["id"])
            salida.append(f["id"])
            pendientes.append(f["id"])
    return salida


def crear(nombre: str, x: float | None = None, y: float | None = None,
          camaras: list[int] | None = None, padre_id: int | None = None) -> int:
    px, py = _acotar(ANCHO / 2 if x is None else x, ALTO / 2 if y is None else y)
    with db.sesion() as con:
        if padre_id is not None and not con.execute(
                "SELECT 1 FROM grupos WHERE id = ?", (padre_id,)).fetchone():
            raise LookupError(f"no existe el grupo padre {padre_id}")
        cur = con.execute(
            "INSERT INTO grupos (nombre, x, y, padre_id) VALUES (?,?,?,?)",
            (nombre.strip(), px, py, padre_id))
        gid = cur.lastrowid
        if camaras:
            con.executemany(
                "INSERT OR IGNORE INTO grupos_camaras (grupo_id, camara_id) VALUES (?,?)",
                [(gid, c) for c in camaras])
    return gid


def editar(grupo_id: int, nombre: str | None = None,
           x: float | None = None, y: float | None = None,
           padre_id: int | None = None, mover: bool = False) -> bool:
    """Renombra, mueve en el plano o lo cuelga de otro padre.

    `mover=True` es lo que distingue "no me mandaron padre" de "mandalo a la
    raiz": sin ese flag no habria como expresar `padre_id = NULL`.
    """
    campos, args = [], []
    if nombre is not None:
        campos.append("nombre = ?")
        args.append(nombre.strip())
    if x is not None and y is not None:
        px, py = _acotar(x, y)
        campos += ["x = ?", "y = ?"]
        args += [px, py]

    with db.sesion() as con:
        if mover:
            if padre_id == grupo_id:
                raise ValueError("un grupo no puede colgar de si mismo")
            if padre_id is not None:
                if not con.execute("SELECT 1 FROM grupos WHERE id = ?",
                                   (padre_id,)).fetchone():
                    raise LookupError(f"no existe el grupo {padre_id}")
                # Colgarlo de su propio descendiente cerraria un ciclo y
                # `listar` se quedaria dando vueltas.
                if padre_id in _descendientes(con, grupo_id):
                    raise ValueError(
                        "no se puede colgar un grupo de uno de sus subgrupos")
            campos.append("padre_id = ?")
            args.append(padre_id)

        if not campos:
            return False
        args.append(grupo_id)
        cur = con.execute(f"UPDATE grupos SET {', '.join(campos)} WHERE id = ?", args)
        return cur.rowcount > 0


def borrar(grupo_id: int) -> dict:
    """Borra el grupo y todo lo que cuelga de el.

    En cascada y no dejando huerfanos: borrar un yacimiento y que sus
    talleres aparezcan sueltos en la raiz del plano sorprende mas que
    borrarlos. La interfaz avisa cuantos subgrupos se van antes de preguntar.

    Las camaras NO se tocan: cae `grupos_camaras`, pero los equipos siguen en
    el inventario.
    """
    with db.sesion() as con:
        if not con.execute("SELECT 1 FROM grupos WHERE id = ?", (grupo_id,)).fetchone():
            return {"borrados": 0, "subgrupos": 0}
        ids = _descendientes(con, grupo_id) + [grupo_id]
        marcas = ",".join("?" * len(ids))
        con.execute(f"DELETE FROM grupos_camaras WHERE grupo_id IN ({marcas})", ids)
        con.execute(f"DELETE FROM grupos WHERE id IN ({marcas})", ids)
        return {"borrados": len(ids), "subgrupos": len(ids) - 1}


def asignar(grupo_id: int, camaras: list[int]) -> int:
    """Reemplaza el contenido del grupo por esta lista."""
    with db.sesion() as con:
        if not con.execute("SELECT 1 FROM grupos WHERE id = ?", (grupo_id,)).fetchone():
            raise LookupError(f"no existe el grupo {grupo_id}")
        con.execute("DELETE FROM grupos_camaras WHERE grupo_id = ?", (grupo_id,))
        if camaras:
            con.executemany(
                "INSERT OR IGNORE INTO grupos_camaras (grupo_id, camara_id) "
                "SELECT ?, id FROM camaras WHERE id = ?",
                [(grupo_id, c) for c in camaras])
        return con.execute(
            "SELECT COUNT(*) AS n FROM grupos_camaras WHERE grupo_id = ?",
            (grupo_id,)).fetchone()["n"]


def mover_camara(camara_id: int, grupo_id: int) -> dict:
    """Deja una camara colgando de UN grupo y de ninguno mas.

    `grupos_camaras` admite muchos grupos por camara, y esa libertad se pago
    cara: la misma camara estaba en un yacimiento Y en su subgrupo a la vez, y
    el arbol la contaba dos veces. Para "mover" —que es lo que la gente quiere
    hacer— la respuesta correcta es exclusiva: se borra de todos y se agrega a
    uno.

    Antes esta operacion escribia `camaras.sitio_id`, que es OTRA cosa: la
    etiqueta que trae el relevamiento. El plano y la barra lateral se dibujan
    con `grupos`, asi que mover una camara "de locacion" no la movia a ningun
    lado visible.
    """
    with db.sesion() as con:
        cam = con.execute("SELECT nombre FROM camaras WHERE id = ?",
                          (camara_id,)).fetchone()
        if not cam:
            raise LookupError(f"no existe la camara {camara_id}")
        gr = con.execute("SELECT nombre FROM grupos WHERE id = ?",
                         (grupo_id,)).fetchone()
        if not gr:
            raise LookupError(f"no existe el grupo {grupo_id}")
        con.execute("DELETE FROM grupos_camaras WHERE camara_id = ?", (camara_id,))
        con.execute("INSERT INTO grupos_camaras (grupo_id, camara_id) VALUES (?,?)",
                    (grupo_id, camara_id))
    return {"camara_id": camara_id, "camara": cam["nombre"],
            "grupo_id": grupo_id, "grupo": gr["nombre"]}


def grupo_de(camara_id: int) -> dict | None:
    """En que grupo esta hoy una camara (el primero, si estuviera en varios)."""
    with db.sesion() as con:
        fila = con.execute(
            """SELECT g.id, g.nombre FROM grupos_camaras gc
                 JOIN grupos g ON g.id = gc.grupo_id
                WHERE gc.camara_id = ? ORDER BY g.nombre LIMIT 1""",
            (camara_id,)).fetchone()
    return dict(fila) if fila else None


def sembrar() -> dict:
    """Crea los grupos iniciales a partir de los sitios del inventario.

    Solo corre si no hay grupos: no pisa nada que el usuario haya armado.
    """
    with db.sesion() as con:
        if con.execute("SELECT 1 FROM grupos LIMIT 1").fetchone():
            return {"creados": 0, "motivo": "ya hay grupos armados"}

        creados = 0
        for nombre, x, y, sitios in SIEMBRA:
            marcas = ",".join("?" * len(sitios))
            cams = [f["id"] for f in con.execute(
                f"""SELECT cam.id FROM camaras cam
                      JOIN sitios s ON s.id = cam.sitio_id
                     WHERE s.nombre IN ({marcas})""", sitios).fetchall()]
            if not cams:
                continue
            gid = con.execute("INSERT INTO grupos (nombre, x, y) VALUES (?,?,?)",
                              (nombre, float(x), float(y))).lastrowid
            con.executemany(
                "INSERT INTO grupos_camaras (grupo_id, camara_id) VALUES (?,?)",
                [(gid, c) for c in cams])
            creados += 1

        # Un sitio que no figure en el reparto igual entra, en fila abajo, para
        # que ninguna camara quede sin grupo al sembrar.
        puestos = {s for _, _, _, sitios in SIEMBRA for s in sitios}
        sueltos = [f["nombre"] for f in con.execute(
            """SELECT DISTINCT s.nombre FROM sitios s
                 JOIN camaras cam ON cam.sitio_id = s.id
                ORDER BY s.nombre""").fetchall() if f["nombre"] not in puestos]
        for i, nombre in enumerate(sueltos):
            cams = [f["id"] for f in con.execute(
                """SELECT cam.id FROM camaras cam JOIN sitios s ON s.id = cam.sitio_id
                    WHERE s.nombre = ?""", (nombre,)).fetchall()]
            gid = con.execute("INSERT INTO grupos (nombre, x, y) VALUES (?,?,?)",
                              (nombre, 150.0 + i * 150, 780.0)).lastrowid
            con.executemany(
                "INSERT INTO grupos_camaras (grupo_id, camara_id) VALUES (?,?)",
                [(gid, c) for c in cams])
            creados += 1

    return {"creados": creados}


def sin_grupo() -> list[dict]:
    """Camaras que no estan en ningun grupo. Sirve para que no se pierdan."""
    with db.sesion() as con:
        return [dict(f) for f in con.execute(
            """SELECT cam.id, cam.nombre, cam.estado, s.nombre AS sitio
                 FROM camaras cam
                 LEFT JOIN sitios s ON s.id = cam.sitio_id
                WHERE cam.id NOT IN (SELECT camara_id FROM grupos_camaras)
                ORDER BY cam.nombre""").fetchall()]

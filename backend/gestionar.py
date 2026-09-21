#!/usr/bin/env python3
"""Herramienta de linea de comandos de Sistema de Videovigilancia.

    python gestionar.py init                 crea la base
    python gestionar.py importar             carga planilla + relevamiento
    python gestionar.py credencial NOMBRE USUARIO
                                             agrega/actualiza un juego de claves
                                             (la clave se pide por teclado)
    python gestionar.py asignar NOMBRE --marca SONY
                                             asigna esa credencial a un grupo
                                             (--camara para una sola, por
                                              nombre, IP o id)
    python gestionar.py estado               resumen del parque
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app import almacenamiento, config, db, importador


def cmd_init(_args) -> int:
    db.inicializar()
    print(f"base creada en {config.DB_PATH}")
    return 0


def cmd_importar(_args) -> int:
    res = importador.importar_todo()
    print(f"planilla: {res['camaras_planilla']} camaras")
    rel = res["relevamiento"]
    if rel:
        print(f"relevamiento: {rel['actualizadas']} actualizadas, "
              f"{rel['responden']} responden, {rel['con_video']} con video")
        if rel["sin_ficha"]:
            print(f"  ojo: {len(rel['sin_ficha'])} IP del relevamiento no estan "
                  f"en la planilla: {', '.join(rel['sin_ficha'][:5])}")
    else:
        print("relevamiento: no encontrado (corre primero tools/probe.py)")
    return 0


def cmd_credencial(args) -> int:
    clave = getpass.getpass(f"Clave para '{args.usuario}': ")
    if not clave:
        print("clave vacia, cancelado", file=sys.stderr)
        return 1
    cid = importador.guardar_credencial(args.nombre, args.usuario, clave)
    print(f"credencial '{args.nombre}' guardada cifrada (id {cid})")
    return 0


def cmd_credenciales_archivo(args) -> int:
    """Carga tools/credentials.json a la base, cifrado, y las vincula a las
    camaras segun cual funciono en el relevamiento."""
    import json
    from pathlib import Path

    ruta = Path(args.ruta) if args.ruta else config.RAIZ / "tools" / "credentials.json"
    if not ruta.exists():
        print(f"no encuentro {ruta}", file=sys.stderr)
        return 1

    juegos = json.loads(ruta.read_text(encoding="utf-8"))
    cargadas = 0
    for i, juego in enumerate(juegos, 1):
        nombre = juego.get("name") or f"cred-{i}"
        usuario = juego.get("user") or ""
        clave = juego.get("password") or ""
        if not clave or clave == "CAMBIAR":
            print(f"  '{nombre}' sin clave, salteada")
            continue
        importador.guardar_credencial(nombre, usuario, clave)
        cargadas += 1
        print(f"  '{nombre}' guardada cifrada")

    # Vincular cada camara con la credencial que la sonda reporto como buena.
    vinculadas = 0
    rel = config.RELEVAMIENTO_JSON
    if rel.exists():
        datos = json.loads(rel.read_text(encoding="utf-8"))
        with db.sesion() as con:
            for reg in datos.get("cameras") or []:
                nombre_cred = reg.get("credential")
                if not nombre_cred:
                    continue
                cur = con.execute(
                    """UPDATE camaras SET credencial_id =
                           (SELECT id FROM credenciales WHERE nombre = ?)
                        WHERE ip = ?""",
                    (nombre_cred, reg.get("host")),
                )
                vinculadas += cur.rowcount

    print(f"\n{cargadas} credenciales cargadas, {vinculadas} camaras vinculadas")
    print("las claves quedan cifradas en la base; la llave esta en "
          f"{config.CLAVE_PATH}")
    return 0


def cmd_asignar(args) -> int:
    with db.sesion() as con:
        cred = con.execute("SELECT id FROM credenciales WHERE nombre = ?",
                           (args.nombre,)).fetchone()
        if not cred:
            print(f"no existe la credencial '{args.nombre}'", file=sys.stderr)
            return 1
        if not (args.marca or args.sitio or args.camara or args.todas):
            # Sin filtro esto reasignaba las 39 camaras de una. Es facil de
            # hacer sin querer y deja al parque entero con la credencial
            # equivocada, asi que hay que pedirlo explicito.
            print("falta un filtro: --camara, --marca, --sitio o --todas",
                  file=sys.stderr)
            return 1

        sql = "UPDATE camaras SET credencial_id = ? WHERE 1 = 1"
        params: list = [cred["id"]]
        if args.marca:
            sql += " AND UPPER(marca) = UPPER(?)"
            params.append(args.marca)
        if args.sitio:
            sql += " AND sitio_id = (SELECT id FROM sitios WHERE nombre = ?)"
            params.append(args.sitio)
        if args.camara:
            # Una sola camara, por nombre, IP o id. Hace falta cuando un equipo
            # no comparte la credencial de su marca -- por ejemplo el que
            # rechaza las suscripciones ONVIF con "Sender not Authorized".
            sql += " AND (nombre = ? OR ip = ? OR CAST(id AS TEXT) = ?)"
            params += [args.camara, args.camara, args.camara]
        cur = con.execute(sql, params)
        if not cur.rowcount:
            print(f"ninguna camara coincide con ese filtro", file=sys.stderr)
            return 1
        print(f"{cur.rowcount} camaras asignadas a '{args.nombre}'")
    return 0


def cmd_usuario(args) -> int:
    """Crea o actualiza un usuario del sistema. La clave se pide por teclado y
    se guarda solo como hash."""
    from app import auth

    if args.rol not in auth.ROLES:
        print(f"rol invalido: {args.rol} (usar {' o '.join(auth.ROLES)})",
              file=sys.stderr)
        return 1
    clave = getpass.getpass(f"Clave para '{args.nombre}' ({args.rol}): ")
    if len(clave) < 6:
        print("clave demasiado corta (minimo 6)", file=sys.stderr)
        return 1
    if getpass.getpass("Repetir: ") != clave:
        print("no coinciden", file=sys.stderr)
        return 1
    auth.crear_usuario(args.nombre, clave, args.rol)
    print(f"usuario '{args.nombre}' ({args.rol}) guardado")
    return 0


def cmd_usuarios(_args) -> int:
    from app import auth
    us = auth.listar_usuarios()
    if not us:
        print("no hay usuarios: sin ellos, el sistema no pide login y queda abierto")
        return 0
    for u in us:
        print(f"  {u['nombre']:<16} {u['rol']:<9} desde {u['creado']}")
    return 0


def cmd_mudanza(args) -> int:
    """Deja la base lista para llevarla a otra maquina.

    El inventario, las credenciales cifradas y los grupos del plano se
    conservan -- es lo que costo armar. Lo que se borra es el INDICE de
    grabacion: apunta a archivos .ts que estan en el disco de la notebook y no
    van a existir en el servidor, asi que si se copia tal cual la linea de
    tiempo muestra tramos que no se pueden reproducir.
    """
    with db.sesion() as con:
        segs = con.execute("SELECT COUNT(*) n FROM segmentos").fetchone()["n"]
        evs = con.execute("SELECT COUNT(*) n FROM eventos").fetchone()["n"]
        cams = con.execute("SELECT COUNT(*) n FROM camaras").fetchone()["n"]
        grps = con.execute("SELECT COUNT(*) n FROM grupos").fetchone()["n"]
        creds = con.execute("SELECT COUNT(*) n FROM credenciales").fetchone()["n"]

        print(f"se conservan: {cams} camaras, {creds} credenciales, {grps} grupos")
        print(f"se borra el indice de grabacion: {segs} segmentos"
              + ("" if args.conservar_eventos else f" y {evs} eventos"))
        if not args.si:
            print("\nagrega --si para hacerlo")
            return 1

        con.execute("DELETE FROM segmentos")
        if not args.conservar_eventos:
            con.execute("DELETE FROM eventos")
        # El estado de las camaras lo midio la notebook, que no rutea a todas
        # las subredes. En el servidor hay que volver a medirlo con vivas.py.
        con.execute("UPDATE camaras SET estado = 'desconocido'")

    print("\nlisto. Copia al servidor:")
    print(f"  {config.DB_PATH}")
    print(f"  {config.CLAVE_PATH}   <-- sin esta llave las credenciales no se leen")
    print("\nNO copies datos/video/: son las grabaciones de prueba.")
    print("En el servidor, despues: py tools/vivas.py --actualizar")
    return 0


def cmd_estado(_args) -> int:
    with db.sesion() as con:
        total = con.execute("SELECT COUNT(*) n FROM camaras").fetchone()["n"]
        por_estado = con.execute(
            "SELECT estado, COUNT(*) n FROM camaras GROUP BY estado ORDER BY n DESC"
        ).fetchall()
        por_perfil = con.execute(
            "SELECT perfil_grabacion p, COUNT(*) n FROM camaras GROUP BY p ORDER BY n DESC"
        ).fetchall()

    print(f"camaras: {total}")
    print("\npor estado:")
    for f in por_estado:
        print(f"  {f['estado']:<16} {f['n']:>3}")
    print("\npor perfil de grabacion:")
    for f in por_perfil:
        print(f"  {f['p']:<16} {f['n']:>3}")

    res = almacenamiento.resumen()
    print(f"\nvolumen: {res['volumen_tb']} TB "
          f"({res['volumen_util_gb']} GB utiles) · "
          f"{res['camaras_medidas']} de {res['camaras']} camaras con bitrate real")
    print(f"\n{'ESCENARIO':<40}{'GB/DIA':>10}{'RETENCION':>12}")
    print("-" * 62)
    for e in res["escenarios"]:
        marca = " *" if e.get("recomendado") else ""
        dias = f"{e['retencion_dias']} d" if e["retencion_dias"] else "-"
        print(f"{e['nombre'][:38]:<40}{e['gb_dia']:>10.1f}{dias:>12}{marca}")

    print("\nlas 5 que mas consumen:")
    for cam in almacenamiento.por_camara()[:5]:
        real = "" if cam["medida_real"] else "  (estimada)"
        print(f"  {cam['nombre']:<18}{cam['gb_dia']:>7.2f} GB/dia   "
              f"{cam['codec'] or '?'} {cam['resolucion'] or ''}{real}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Gestion de Sistema de Videovigilancia")
    sub = parser.add_subparsers(dest="comando", required=True)

    sub.add_parser("init", help="crear la base de datos").set_defaults(func=cmd_init)
    sub.add_parser("importar", help="cargar planilla y relevamiento").set_defaults(func=cmd_importar)
    sub.add_parser("estado", help="resumen del parque").set_defaults(func=cmd_estado)

    p = sub.add_parser("credencial", help="guardar un juego de credenciales cifrado")
    p.add_argument("nombre")
    p.add_argument("usuario")
    p.set_defaults(func=cmd_credencial)

    p = sub.add_parser("credenciales-archivo",
                       help="cargar tools/credentials.json cifrado a la base")
    p.add_argument("ruta", nargs="?")
    p.set_defaults(func=cmd_credenciales_archivo)

    p = sub.add_parser("usuario", help="crear o actualizar un usuario (pide la clave)")
    p.add_argument("nombre")
    p.add_argument("rol", choices=["operador", "admin"])
    p.set_defaults(func=cmd_usuario)

    sub.add_parser("usuarios", help="listar usuarios").set_defaults(func=cmd_usuarios)

    p = sub.add_parser("mudanza", help="dejar la base lista para llevarla al servidor")
    p.add_argument("--si", action="store_true", help="confirmar (si no, solo muestra)")
    p.add_argument("--conservar-eventos", action="store_true",
                   help="no borrar los eventos registrados")
    p.set_defaults(func=cmd_mudanza)

    p = sub.add_parser("asignar", help="asignar una credencial a un grupo de camaras")
    p.add_argument("nombre")
    p.add_argument("--camara", help="una sola: nombre, IP o id")
    p.add_argument("--marca")
    p.add_argument("--sitio")
    p.add_argument("--todas", action="store_true",
                   help="todo el parque (hay que pedirlo explicito)")
    p.set_defaults(func=cmd_asignar)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Chequeo rapido de que camaras estan vivas.

A diferencia de `probe.py`, esto **no abre una sola sesion RTSP ni ONVIF**:
solo intenta una conexion TCP a los puertos y la corta. Es la diferencia entre
tocar el timbre y entrar: no consume las sesiones concurrentes de la camara, no
deja procesos colgados y se puede correr las veces que haga falta.

    py tools/vivas.py                 # todas, desde la base
    py tools/vivas.py --sitio "Sitio Norte"
    py tools/vivas.py --actualizar    # ademas escribe `estado` en la base

Correrlo EN EL SERVIDOR es lo que vale: la notebook no rutea a todas las
subredes, asi que desde ahi varias dan por caidas sin estarlo.
"""

from __future__ import annotations

import argparse
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

PUERTOS = [("rtsp", 554), ("http", 80), ("https", 443)]
TIMEOUT = 2.0


def toca(ip: str, puerto: int, timeout: float = TIMEOUT) -> bool:
    """Un SYN y a otra cosa. No habla el protocolo, solo pregunta si escucha."""
    try:
        with socket.create_connection((ip, puerto), timeout=timeout):
            return True
    except OSError:
        return False


def revisar(cam: dict, timeout: float = TIMEOUT) -> dict:
    abiertos = [n for n, p in PUERTOS if toca(cam["ip"], p, timeout)]
    return {**cam, "puertos": abiertos, "viva": bool(abiertos)}


def cargar(sitio: str | None) -> list[dict]:
    from app import db

    sql = """SELECT cam.id, cam.nombre, cam.ip, cam.marca, cam.modelo, cam.estado,
                    s.nombre AS sitio
               FROM camaras cam LEFT JOIN sitios s ON s.id = cam.sitio_id"""
    args: list = []
    if sitio:
        sql += " WHERE s.nombre = ?"
        args.append(sitio)
    sql += " ORDER BY s.nombre, cam.nombre"
    with db.sesion() as con:
        return [dict(f) for f in con.execute(sql, args).fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sitio")
    ap.add_argument("--hilos", type=int, default=24)
    ap.add_argument("--timeout", type=float, default=TIMEOUT)
    ap.add_argument("--actualizar", action="store_true",
                    help="escribe el resultado en la columna `estado`")
    args = ap.parse_args()

    camaras = cargar(args.sitio)
    if not camaras:
        print("no hay camaras cargadas (corre `py gestionar.py importar`)")
        return 1

    print(f"revisando {len(camaras)} camaras, {args.timeout}s de espera, "
          f"sin abrir sesiones...\n")
    with ThreadPoolExecutor(max_workers=args.hilos) as pool:
        res = list(pool.map(lambda c: revisar(c, args.timeout), camaras))

    sitio_actual = None
    for c in res:
        if c["sitio"] != sitio_actual:
            sitio_actual = c["sitio"]
            print(f"\n{sitio_actual or 'sin sitio'}")
        marca = "  OK  " if c["viva"] else " CAIDA"
        puertos = ",".join(c["puertos"]) or "-"
        print(f" {marca}  {c['nombre']:<17} {c['ip']:<15} {puertos:<16}"
              f" {c['marca']} {c['modelo']}")

    vivas = [c for c in res if c["viva"]]
    caidas = [c for c in res if not c["viva"]]
    print(f"\n{len(vivas)} de {len(res)} responden.")
    if caidas:
        print("no responden: " + ", ".join(f"{c['nombre']} ({c['ip']})" for c in caidas))
        # Redes enteras caidas suelen ser un problema de ruteo, no de camaras.
        redes: dict[str, int] = {}
        for c in caidas:
            redes[".".join(c["ip"].split(".")[:3])] = redes.get(
                ".".join(c["ip"].split(".")[:3]), 0) + 1
        enteras = [r for r, n in redes.items()
                   if n == sum(1 for c in res if c["ip"].startswith(r + "."))]
        if enteras:
            print("\nOJO: estas subredes no contestan NINGUNA camara, lo que apunta a "
                  "ruteo y no a las camaras: " + ", ".join(f"{r}.x" for r in enteras))

    if args.actualizar:
        from app import db
        with db.sesion() as con:
            for c in res:
                con.execute("UPDATE camaras SET estado = ? WHERE id = ?",
                            ("en_linea" if c["viva"] else "sin_conexion", c["id"]))
        print(f"\nbase actualizada: {len(vivas)} en_linea, {len(caidas)} sin_conexion")
    return 0


if __name__ == "__main__":
    sys.exit(main())

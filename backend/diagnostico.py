#!/usr/bin/env python3
"""Diagnostico de eventos y clips. No imprime URLs ni credenciales."""

import sys

from app import db, eventos, onvif, crypto


def probar_eventos(camara_id: int) -> None:
    with db.sesion() as con:
        f = con.execute(
            """SELECT cam.nombre, cam.ip, cam.marca, cr.usuario, cr.secreto
                 FROM camaras cam LEFT JOIN credenciales cr ON cr.id = cam.credencial_id
                WHERE cam.id = ?""", (camara_id,)).fetchone()
    if not f:
        print(f"  camara {camara_id}: no existe")
        return

    cli = onvif.ClienteONVIF(f["ip"], f["usuario"] or "",
                             crypto.descifrar(f["secreto"]) if f["secreto"] else "")
    print(f"\n=== {f['nombre']} ({f['marca']}) ===")
    for paso, fn in (("conectar", cli.conectar), ("capacidades", cli.capacidades)):
        try:
            fn()
            print(f"  {paso:14} OK")
        except onvif.ErrorONVIF as e:
            print(f"  {paso:14} FALLA -> {e}")
            return
    print(f"  servicio       {'/' + cli.url_events.split('/', 3)[-1] if cli.url_events else '-'}")
    try:
        direccion = cli.crear_pullpoint()
        print(f"  pullpoint      OK")
        cli.cancelar(direccion)
    except onvif.ErrorONVIF as e:
        print(f"  pullpoint      FALLA -> {e}")


def probar_clip(camara_id: int) -> None:
    with db.sesion() as con:
        f = con.execute(
            "SELECT nombre, stream_evento_id FROM camaras WHERE id = ?",
            (camara_id,)).fetchone()
    print(f"\n=== clip de {f['nombre']} ===")
    print(f"  stream de evento: {f['stream_evento_id'] or 'NO ASIGNADO'}")
    if not f["stream_evento_id"]:
        return
    r = eventos.grabar_clip(camara_id, 8)
    print(f"  resultado: {r if r else 'FALLO (no se genero archivo)'}")


def probar_ffmpeg(camara_id: int) -> None:
    """Muestra por que falla ffmpeg. Enmascara la clave pase lo que pase."""
    import subprocess
    import urllib.parse

    from app import binarios

    clave = ""
    try:
        with db.sesion() as con:
            f = con.execute(
                """SELECT cam.nombre, cam.stream_evento_id, cr.usuario, cr.secreto
                     FROM camaras cam LEFT JOIN credenciales cr ON cr.id = cam.credencial_id
                    WHERE cam.id = ?""", (camara_id,)).fetchone()
            st = con.execute("SELECT url, codec FROM streams WHERE id = ?",
                             (f["stream_evento_id"],)).fetchone()

        usuario = f["usuario"] or ""
        clave = crypto.descifrar(f["secreto"]) if f["secreto"] else ""
        p = urllib.parse.urlsplit(st["url"])
        host = p.hostname + (f":{p.port}" if p.port else "")
        cred = f"{urllib.parse.quote(usuario, safe='')}:{urllib.parse.quote(clave, safe='')}@"
        # Bosch: aon/aud piden audio. Apagarlos evita que anuncie una pista que
        # despues no envia y deja a ffmpeg esperandola.
        consulta = p.query.replace("aon=1", "aon=0").replace("aud=1", "aud=0")
        url = urllib.parse.urlunsplit((p.scheme, cred + host, p.path, consulta, ""))

        print(f"\n=== ffmpeg sobre {f['nombre']} (codec {st['codec']}) ===")
        print(f"  ruta RTSP: {p.path}?{consulta[:60]}")

        import sys as _s
        modo = "copy" if len(_s.argv) < 3 else _s.argv[2]
        video = (["-c:v", "copy"] if modo == "copy" else
                 ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-threads", "2"])
        print(f"  modo: {modo}")
        cmd = ([binarios.ffmpeg(), "-loglevel", "warning", "-rtsp_transport", "tcp",
                "-analyzeduration", "3000000", "-probesize", "8000000",
                "-i", url, "-t", "6", "-map", "0:v:0", "-an"] + video +
               ["-f", "mpegts", "-y", "prueba.ts"])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        err = r.stderr or ""
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        r = None

    if clave:
        err = err.replace(clave, "***").replace(
            urllib.parse.quote(clave, safe=""), "***")
    print(f"  salida ffmpeg: {getattr(r, 'returncode', '?')}")
    print("  " + (err.strip()[-900:] or "(sin mensajes)").replace("\n", "\n  "))
    import os
    if os.path.exists("prueba.ts"):
        print(f"  bytes generados: {os.path.getsize('prueba.ts')}")
        os.unlink("prueba.ts")
    else:
        print("  no se genero archivo")


if __name__ == "__main__":
    ids = [int(x) for x in sys.argv[1:]] or [11, 25]
    for cid in ids:
        probar_eventos(cid)
    probar_clip(ids[0])

"""Archivo de grabaciones: indexado y purga.

El grabador escribe los segmentos con nombre por fecha y hora. Este modulo:

  1. **Indexa** los archivos nuevos en la tabla `segmentos`, para poder armar la
     linea de tiempo con una consulta en vez de recorrer el disco.
  2. **Purga** los mas viejos cuando el volumen se acerca al limite.

La purga por espacio va primero que la purga por antiguedad: mas vale conservar
menos dias que llenar el disco y que el grabador se caiga a las 3 de la manana.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import binarios, config, db, preservar

# archivo_20260903-141530.ts  ->  2026-09-03 14:15:30
PATRON = re.compile(r"(\d{8})-(\d{6})")

# Cada cuanto se revisa el disco.
#
# 60 s y no 30 porque un segmento dura 60 s (`SEGUNDOS_POR_SEGMENTO`): pasar
# mas seguido que eso no encuentra nada nuevo la mitad de las veces y cuesta
# caro. Medido sobre el R710 con 110.000 segmentos, una pasada de `indexar`
# tarda 2,3 s --1,1 s solo en listar y ordenar los 112.000 ficheros del
# volumen-- asi que a 30 s se iba el 8% de un nucleo en releer lo mismo.
INTERVALO_INDEXADO = 60      # segundos
INTERVALO_PURGA = 120

# Se empieza a purgar cuando el archivo supera este porcentaje del volumen util.
UMBRAL_PURGA = 0.90

# Cuantos segmentos borra cada tanda, y cuantas tandas como maximo por vuelta.
# El producto (100.000) es el techo de una sola llamada a `purgar`: alcanza
# para vaciar el archivo entero del R710 de una vez si hiciera falta, y evita
# que un error de calculo deje el bucle dando vueltas para siempre.
TANDA_PURGA = 500
MAX_TANDAS_PURGA = 200

# Espacio del disco que se deja libre siempre, pase lo que pase.
RESERVA_DISCO_GB = 20.0

# Cuantos segmentos recientes se revisan por si su tamano quedo mal anotado.
# Los viejos ya no cambian, no tiene sentido releerlos en cada vuelta.
VENTANA_REVISION = 400

# Cuanto se dedica a medir duraciones en cada pasada. Es un presupuesto de
# TIEMPO y no una cantidad fija porque ffprobe tarda muy distinto segun el
# equipo: medido el 2026-09-08, 90 ms por segmento en la notebook y 266 ms en
# el R710, que ademas esta escribiendo 27 streams. Con un tope fijo de 120 el
# R710 se habria pasado 32 s midiendo dentro de un ciclo de 30 y el hilo de
# mantenimiento no habria llegado nunca a indexar ni a purgar.
#
# Las nuevas se miden primero (ORDER BY id DESC), asi que lo que alguien va a
# querer reproducir hoy queda exacto en la primera vuelta y el atraso viejo se
# drena de a poco. Mientras tanto la reproduccion usa la estimacion por
# nombre, que solo se equivoca en los segmentos cortados.
SEGUNDOS_MEDICION = float(os.environ.get("SISTEMA_VIDEOVIGILANCIA_SEGUNDOS_MEDICION", "8"))
MAX_MEDICIONES = 400          # tope duro, por si ffprobe contesta al instante


def _raiz() -> Path:
    from .grabador import RAIZ_VIDEO
    return RAIZ_VIDEO


def _fecha_de_nombre(nombre: str) -> datetime | None:
    """Fecha de inicio del segmento, en UTC.

    ffmpeg nombra los archivos con `-strftime`, que usa la hora LOCAL del
    servidor. El resto de la base guarda UTC, asi que se convierte aca: sin
    esto la linea de tiempo queda corrida tantas horas como el huso horario
    (tres, en Argentina).
    """
    m = PATRON.search(nombre)
    if not m:
        return None
    try:
        local = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


# --- duracion real ---------------------------------------------------------

def _medir(ruta: Path) -> float | None:
    """Segundos de video que tiene el archivo, segun ffprobe.

    Hace falta medirlo y no deducirlo. La tentacion es restar la hora del
    nombre del segmento siguiente, pero `-strftime` nombra el archivo cuando
    lo ABRE y ffmpeg corta en el primer fotograma clave despues del tiempo
    pedido: medido sobre el parque real, esa resta da entre 58 y 61 s para
    segmentos que duran 60,06 -- y, peor, da 60 para uno que duro 4 s porque
    la camara se cayo justo ahi. Declararle 60 s a un segmento de 4 deja al
    reproductor esperando 56 s de video que no existen, que es exactamente
    como se siente la reproduccion cuando "cuesta" y se traba.
    """
    ffprobe = binarios.ffprobe()
    if not ffprobe:
        return None
    try:
        salida = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(ruta)],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        dur = float((salida.stdout or "").strip())
    except ValueError:
        return None
    return round(dur, 3) if dur > 0 else None


def medir_pendientes(segundos: float = SEGUNDOS_MEDICION,
                     tope: int = MAX_MEDICIONES) -> int:
    """Mide los segmentos que todavia no tienen duracion anotada.

    Los mas nuevos primero: son los que alguien va a querer reproducir hoy.

    Los ffprobe corren FUERA de la transaccion y las duraciones se escriben
    todas juntas al final. Medir con la transaccion abierta sostiene el
    candado de escritura de SQLite los diez segundos que tarda la tanda, y en
    ese rato los eventos ONVIF que llegan de otros hilos chocan contra
    "database is locked".
    """
    raiz = _raiz()
    with db.sesion() as con:
        filas = [dict(f) for f in con.execute(
            """SELECT id, archivo FROM segmentos
                WHERE duracion IS NULL ORDER BY id DESC LIMIT ?""",
            (tope,)).fetchall()]

    limite = time.time() + segundos
    medidas: list[tuple[float, int]] = []
    for fila in filas:
        if time.time() > limite:
            break
        ruta = raiz / fila["archivo"]
        if not ruta.exists():
            continue
        dur = _medir(ruta)
        if dur is not None:
            medidas.append((dur, fila["id"]))

    if not medidas:
        return 0
    with db.sesion() as con:
        con.executemany("UPDATE segmentos SET duracion = ? WHERE id = ?", medidas)
    return len(medidas)


# --- indexado --------------------------------------------------------------

def indexar() -> int:
    """Registra los segmentos nuevos. Idempotente: se puede llamar siempre."""
    raiz = _raiz()
    if not raiz.exists():
        return 0

    _migrar_horas_locales()

    # Que es lo ultimo indexado de cada camara y capa. Los nombres los pone
    # `strftime` del lado del servidor -- `archivo_20260910-120100.ts` -- asi
    # que ordenan cronologicamente y alcanza con quedarse con el mayor.
    #
    # (Si el reloj del servidor saltara hacia atras, un archivo con nombre
    # anterior al tope quedaria sin indexar. Es un riesgo chico y conocido
    # frente a lo que evita: sin este corte hay que reinsertar decenas de miles
    # de filas cada 30 segundos.)
    with db.sesion() as con:
        ultimos = {(f["camara_id"], f["capa"]): f["ult"] for f in con.execute(
            "SELECT camara_id, capa, MAX(archivo) AS ult "
            "FROM segmentos GROUP BY camara_id, capa")}

    # El recorrido del disco va FUERA de toda transaccion.
    #
    # Antes esto corria con el candado de escritura tomado: listar y hacer
    # stat() sobre las decenas de miles de .ts de todas las camaras, en un
    # volumen giratorio donde 33 ffmpeg escriben al mismo tiempo, tardaba mas
    # que los 30 s de espera de SQLite. El sintoma no aparecia aca sino lejos:
    # los clips de evento no podian registrar su fila y morian con "database is
    # locked" -- 16 en una hora.
    filas = []
    for carpeta in raiz.iterdir():
        if not carpeta.is_dir() or not carpeta.name.isdigit():
            continue
        camara_id = int(carpeta.name)

        # Las dos capas van al mismo indice: la base continua y los clips que
        # dispara cada evento. Si los clips quedaran afuera, la purga no los
        # contaria ni los borraria nunca y el disco se llenaria igual.
        for sub, capa in (("archivo", "base"), ("eventos", "evento")):
            destino = carpeta / sub
            if not destino.is_dir():
                continue

            # `scandir` y no `glob`: glob arma un Path por fichero y los
            # ordena todos, y aca son 112.000 en el R710. Lo unico que hace
            # falta antes de ordenar es el nombre, y el tope descarta de una
            # los que ya estan indexados -- que en regimen son todos menos uno
            # o dos por camara.
            tope = ultimos.get((camara_id, capa))
            try:
                with os.scandir(destino) as entradas:
                    nombres = [e.name for e in entradas
                               if e.name.endswith(".ts")]
            except OSError:
                continue
            if not nombres:
                continue
            nombres.sort()

            # En la capa base, el ULTIMO archivo es el que ffmpeg tiene abierto
            # en este momento: su tamano todavia puede crecer, asi que
            # indexarlo ahora guardaria un valor menor al real. Se espera a que
            # aparezca uno mas nuevo. Los clips, en cambio, se cierran solos al
            # terminar y se indexan todos.
            if capa == "base":
                nombres = nombres[:-1]

            for nombre in nombres:
                rel = f"{camara_id}/{sub}/{nombre}"
                if tope and rel <= tope:   # ya indexado en una pasada previa
                    continue
                inicio = _fecha_de_nombre(nombre)
                if not inicio:
                    continue
                try:
                    tam = (destino / nombre).stat().st_size
                except OSError:
                    continue
                if tam < 8192:      # segmento vacio: la camara se corto
                    continue
                filas.append((camara_id, capa, rel,
                              inicio.isoformat(timespec="seconds"), tam))

    # Y la escritura, en una transaccion tan corta como se pueda.
    nuevos = 0
    if filas:
        with db.sesion() as con:
            for fila in filas:
                cur = con.execute(
                    """INSERT OR IGNORE INTO segmentos
                           (camara_id, capa, archivo, inicio, bytes)
                       VALUES (?, ?, ?, ?, ?)""", fila)
                nuevos += cur.rowcount or 0

    corregidos = _corregir_tamanos()
    medir_pendientes()
    return nuevos + corregidos


def _migrar_horas_locales() -> None:
    """Borra el indice viejo, que guardaba hora local en vez de UTC.

    Las filas se regeneran solas leyendo el disco en esta misma pasada, asi que
    no se pierde nada. Corre una vez por proceso.
    """
    global _migrado
    if _migrado:
        return
    _migrado = True
    with db.sesion() as con:
        con.execute("DELETE FROM segmentos WHERE inicio NOT LIKE '%+00:00'")


_migrado = False


def _corregir_tamanos() -> int:
    """Reajusta los tamanos que quedaron mal anotados.

    Windows no actualiza el tamano de un archivo hasta que quien lo escribe lo
    cierra, asi que un segmento indexado demasiado pronto queda subestimado y
    falsea la contabilidad del disco. Solo se revisan los mas recientes: los
    viejos ya estan cerrados y no cambian mas.
    """
    raiz = _raiz()
    with db.sesion() as con:
        recientes = con.execute(
            "SELECT id, archivo, bytes FROM segmentos ORDER BY id DESC LIMIT ?",
            (VENTANA_REVISION,),
        ).fetchall()

    # Los `stat` van fuera de la transaccion por la misma razon que en
    # `indexar` y en `_borrar`: son 400 lecturas al disco y no hay motivo para
    # hacerlas con el candado de escritura tomado.
    cambios: list[tuple[int, int]] = []
    for fila in recientes:
        try:
            real = (raiz / fila["archivo"]).stat().st_size
        except OSError:
            continue
        if real != (fila["bytes"] or 0):
            cambios.append((real, fila["id"]))

    if not cambios:
        return 0
    with db.sesion() as con:
        con.executemany("UPDATE segmentos SET bytes = ? WHERE id = ?", cambios)
    return len(cambios)


# --- purga -----------------------------------------------------------------

def uso_gb() -> float:
    with db.sesion() as con:
        fila = con.execute("SELECT COALESCE(SUM(bytes), 0) AS b FROM segmentos").fetchone()
    return fila["b"] / 1e9


def limite_gb() -> float:
    """Cuanto puede llegar a ocupar el archivo, en GB.

    El tope nominal sale del volumen configurado (1,8 TB del R710), pero nunca
    puede superar lo que el disco de verdad tiene. Atarlo solo al valor
    configurado significa que en cualquier maquina que no sea ese servidor la
    purga no dispara nunca y el disco se llena entero.
    """
    nominal = config.VOLUMEN_UTIL_GB * UMBRAL_PURGA
    try:
        uso = shutil.disk_usage(config.DATOS)
    except OSError:
        return nominal
    # Lo que el archivo ya ocupa, mas lo que queda libre, menos la reserva.
    real = uso_gb() + uso.free / 1e9 - RESERVA_DISCO_GB
    return max(1.0, min(nominal, real))


def retencion_dias() -> float:
    """Cuantos dias de grabacion se conservan.

    Sale de `ajustes`, y si no hay nada guardado, del valor por defecto del
    entorno. Se lee en cada purga y no al arrancar: asi cambiarlo desde la
    interfaz surte efecto en la vuelta siguiente, sin reiniciar el servicio ni
    dejar el parque sin grabar.
    """
    try:
        with db.sesion() as con:
            fila = con.execute("SELECT valor FROM ajustes WHERE clave = ?",
                               ("retencion_dias",)).fetchone()
        if fila:
            return float(fila["valor"])
    except Exception:
        pass
    return config.RETENCION_DIAS


def fijar_retencion(dias: float) -> dict:
    """Guarda la retencion. Devuelve tambien que se va a borrar con ese valor.

    El aviso importa: bajar la retencion borra material en la purga siguiente
    --dos minutos-- y no se recupera.
    """
    dias = max(0.5, float(dias))
    with db.sesion() as con:
        con.execute(
            "INSERT INTO ajustes (clave, valor, fecha) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor, "
            "fecha = excluded.fecha",
            ("retencion_dias", str(dias)))
    return {"retencion_dias": dias, **proyeccion(dias)}


def proyeccion(dias: float | None = None) -> dict:
    """Que pasa con el disco a X dias de retencion, con el ritmo real de hoy.

    Todo sale de lo que el parque esta escribiendo de verdad (`ritmo`), no de
    una estimacion por bitrates: es la unica cuenta que no miente cuando las
    camaras tienen perfiles distintos.
    """
    r = ritmo()
    tope = limite_gb()
    gb_dia = r["gb_dia"] or 0
    actual = retencion_dias()
    dias = actual if dias is None else float(dias)

    maximo = (tope / gb_dia) if gb_dia > 0 else None
    # A cuanto material equivale hoy lo que ya hay guardado.
    return {
        "gb_dia": gb_dia,
        "confiable": r["confiable"],
        "horas_muestra": r["horas_muestra"],
        "limite_gb": round(tope, 1),
        "uso_gb": round(uso_gb(), 1),
        "retencion_actual": actual,
        "dias_maximos": round(maximo, 1) if maximo else None,
        "gb_a_esos_dias": round(gb_dia * dias, 1),
        "entra": (gb_dia * dias) <= tope if gb_dia > 0 else True,
    }


def purgar(retencion_dias: float | None = None) -> dict:
    """Borra segmentos: primero por espacio, despues por antiguedad."""
    tope = limite_gb()
    borrados, liberado = 0, 0

    # 1) Por antiguedad, si se pidio una retencion explicita.
    #
    # De a tandas y no todo junto. El caso normal --una vuelta cada dos
    # minutos-- borra unas pocas decenas de segmentos, pero bajar la retencion
    # desde la interfaz puede dejar decenas de miles fuera de plazo de golpe:
    # sin el tope, esa sola llamada se pasa minutos borrando y el resto del
    # sistema la espera. Con tandas de 500 se avanza igual, ciclo a ciclo, y
    # cada tanda libera el paso.
    if retencion_dias:
        corte = (datetime.now(timezone.utc) - timedelta(days=retencion_dias))
        limite = corte.isoformat(timespec="seconds")
        for _ in range(MAX_TANDAS_PURGA):
            borrados_, liberado_ = _borrar(
                "id IN (SELECT id FROM segmentos WHERE inicio < ? "
                f"AND {preservar.condicion_sql()} "
                "ORDER BY inicio ASC LIMIT ?)", (limite, TANDA_PURGA))
            borrados += borrados_
            liberado += liberado_
            if borrados_ < TANDA_PURGA:
                break

    # 2) Por espacio: mientras se pase del umbral, se van los mas viejos.
    # `_borrar` devuelve lo que borro de verdad, asi que si los archivos estan
    # bloqueados el bucle corta en vez de reintentar los mismos para siempre.
    for _ in range(MAX_TANDAS_PURGA):
        if uso_gb() <= tope:
            break
        borrados_, liberado_ = _borrar(
            "id IN (SELECT id FROM segmentos "
            f"WHERE {preservar.condicion_sql()} "
            "ORDER BY inicio ASC LIMIT ?)",
            (TANDA_PURGA,))
        if borrados_ == 0:
            break
        borrados += borrados_
        liberado += liberado_

    # Lo que quedo cacheado describe el disco de antes de esta purga.
    global _resumen_cache
    if borrados:
        _resumen_cache = None
    return {"borrados": borrados, "liberado_gb": round(liberado / 1e9, 3),
            "uso_gb": round(uso_gb(), 3), "limite_gb": round(tope, 1),
            "preservado": preservar.resumen()}


def _borrar(condicion: str, args: tuple = ()) -> tuple[int, int]:
    """Borra segmentos y devuelve (cuantos se borraron de verdad, bytes).

    Devolver lo realmente borrado -- y no cuantos se seleccionaron -- es lo que
    permite que quien llama detecte que no se pudo avanzar. Un archivo que el
    sistema tiene bloqueado se saltea y se reintenta en la vuelta siguiente.

    Los `unlink` van FUERA de toda transaccion, igual que el recorrido de
    `indexar`. Borrar con el candado de escritura tomado es barato con veinte
    archivos y catastrofico con veinte mil: bajar la retencion de 25 dias a 3
    selecciona decenas de miles de filas, y mientras el plato borra uno por uno
    ningun otro hilo puede escribir. Los clips de evento y el propio indice del
    grabador se mueren con "database is locked", que es exactamente la falla
    que costo dieciseis clips en una hora la primera vez.
    """
    raiz = _raiz()
    # La proteccion se aplica ACA, en la consulta que elige que borrar, y no
    # mas arriba en cada pasada: asi ninguna pasada de purga --ni una que se
    # agregue despues-- puede saltearla por olvido. Ver preservar.py.
    with db.sesion() as con:
        filas = con.execute(
            f"SELECT id, archivo, bytes FROM segmentos "
            f"WHERE ({condicion}) AND {preservar.condicion_sql()}", args
        ).fetchall()
    if not filas:
        return 0, 0

    ids: list[int] = []
    liberado = 0
    for fila in filas:
        try:
            (raiz / fila["archivo"]).unlink()
        except FileNotFoundError:
            pass                     # ya no esta: igual sacamos la fila
        except OSError:
            continue                 # bloqueado: se reintenta despues
        ids.append(fila["id"])
        liberado += fila["bytes"] or 0

    if not ids:
        return 0, 0
    with db.sesion() as con:
        con.executemany("DELETE FROM segmentos WHERE id = ?",
                        [(i,) for i in ids])
    return len(ids), liberado


# --- consultas para la linea de tiempo -------------------------------------
#
# Todo lo que mira la reproduccion pasa por `linea_de_tiempo`. Antes la vista y
# la playlist armaban la lista por su cuenta y con reglas distintas, asi que el
# cursor de la barra y lo que sonaba en el video no apuntaban al mismo lado.


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _nominal(capa: str) -> float:
    """Cuanto dura un segmento de esta capa cuando sale entero."""
    return float(config.SEGUNDOS_CLIP if capa == "evento"
                 else config.SEGUNDOS_POR_SEGMENTO)


def dias_con_grabacion(camara_id: int, capa: str = "base") -> list[dict]:
    """Los dias que tienen material, en hora LOCAL.

    El indice guarda UTC, pero un operador piensa en el dia que vivio: a las
    22 del martes le corresponde el martes, no el miercoles UTC. La conversion
    se hace aca -- si se dejara para el navegador, el filtro por dia y la
    posicion en la barra usarian husos distintos y la linea de tiempo quedaba
    desordenada, que es lo que hacia que la reproduccion saltara a cualquier
    lado.
    """
    with db.sesion() as con:
        filas = con.execute(
            """SELECT date(inicio, 'localtime') AS dia, COUNT(*) AS n,
                      COALESCE(SUM(bytes), 0) AS bytes
                 FROM segmentos
                WHERE camara_id = ? AND capa = ?
                GROUP BY dia ORDER BY dia""",
            (camara_id, capa),
        ).fetchall()
    return [{"dia": f["dia"], "segmentos": f["n"],
             "gb": round(f["bytes"] / 1e9, 3)} for f in filas]


def linea_de_tiempo(camara_id: int, fecha: str | None = None,
                    capa: str = "base", limite: int = 5000) -> list[dict]:
    """Los tramos de un dia, con duracion real y posicion en la playlist.

    Tres cosas que antes no estaban y rompian la reproduccion:

    * **Solo una capa.** Los clips de evento cubren el mismo instante que la
      base pero en otra resolucion; mezclados en una sola playlist el
      reproductor ve la imagen cambiar de tamano a mitad de camino y se traba.
    * **Duracion real.** No todos los segmentos duran 60 s: el que estaba
      abierto cuando ffmpeg se reinicio dura lo que alcanzo. Se deduce de
      cuando arranca el siguiente.
    * **`offset`**: en que segundo de la playlist empieza cada tramo. Es lo
      que hay que ponerle a `video.currentTime` para caer ahi. Antes se
      calculaba como `indice * 60`, que solo es cierto si ningun segmento se
      corto nunca.
    """
    sql = """SELECT id, camara_id, capa, archivo, inicio, bytes, duracion,
                    LEAD(inicio) OVER (ORDER BY inicio) AS siguiente
               FROM segmentos
              WHERE camara_id = ? AND capa = ?"""
    args: list = [camara_id, capa]
    if fecha:
        sql += " AND date(inicio, 'localtime') = ?"
        args.append(fecha)
    sql += " ORDER BY inicio ASC LIMIT ?"
    args.append(limite)

    with db.sesion() as con:
        filas = [dict(f) for f in con.execute(sql, args).fetchall()]

    nominal = _nominal(capa)
    salida: list[dict] = []
    offset = 0.0
    fin_anterior: datetime | None = None

    for f in filas:
        inicio = _dt(f["inicio"])
        # La medida de ffprobe manda. La resta entre nombres es solo el
        # respaldo para los que todavia no se midieron -- ver `_medir`.
        dur = f["duracion"]
        if not dur:
            dur = nominal
            if f["siguiente"]:
                hueco = (_dt(f["siguiente"]) - inicio).total_seconds()
                if 0 < hueco < nominal:
                    dur = hueco
        # Un salto respecto del tramo anterior es un corte real de la
        # grabacion: el reproductor tiene que saberlo o arrastra los
        # timestamps del tramo previo y se queda trabado.
        corte = fin_anterior is not None and (inicio - fin_anterior).total_seconds() > 1.5
        salida.append({
            "id": f["id"], "archivo": f["archivo"], "capa": f["capa"],
            "inicio": f["inicio"], "bytes": f["bytes"],
            "duracion": round(dur, 3), "offset": round(offset, 3),
            "corte": corte,
            "url": f"/media/{f['archivo']}",
        })
        offset += dur
        fin_anterior = inicio + timedelta(seconds=dur)

    return salida


def segmentos(camara_id: int, desde: str | None = None,
              hasta: str | None = None, limite: int = 3000) -> list[dict]:
    """Consulta cruda por rango UTC. La usa el indice, no la reproduccion."""
    sql = "SELECT * FROM segmentos WHERE camara_id = ?"
    args: list = [camara_id]
    if desde:
        sql += " AND inicio >= ?"
        args.append(desde)
    if hasta:
        sql += " AND inicio <= ?"
        args.append(hasta)
    sql += " ORDER BY inicio ASC LIMIT ?"
    args.append(limite)
    with db.sesion() as con:
        return [dict(f) for f in con.execute(sql, args).fetchall()]


def rango_disponible(camara_id: int) -> dict:
    with db.sesion() as con:
        fila = con.execute(
            """SELECT MIN(inicio) AS desde, MAX(inicio) AS hasta,
                      COUNT(*) AS n, COALESCE(SUM(bytes),0) AS bytes
                 FROM segmentos WHERE camara_id = ?""",
            (camara_id,),
        ).fetchone()
    return dict(fila) if fila else {}


# --- cuanto se esta escribiendo de verdad ----------------------------------

def ritmo_camara(camara_id: int, horas: float = 24.0,
                 desde: str | None = None) -> dict:
    """Lo que UNA camara esta escribiendo de verdad, medido sobre el indice.

    Es el mejor dato que hay para anclar una estimacion de calidad: sale de los
    bytes que quedaron en disco con el perfil que la camara tiene puesto AHORA,
    y no cuesta abrir una sesion RTSP. `streams.kbps` no sirve para eso -- ahi
    quedo lo que anoto el relevamiento, que puede ser de otro perfil y de hace
    semanas.

    Solo cuenta la capa base (los clips de evento son otra cosa y entran de a
    ratos, no continuo) y solo los segmentos con `duracion` ya medida: la mide
    ffprobe en una pasada posterior, asi que los recien escritos la tienen en
    NULL. Contar sus bytes contra un divisor que no los incluye inflaba el
    ritmo -- daba 273 kbps donde los archivos decian 104.
    """
    corte = (datetime.now(timezone.utc) - timedelta(hours=horas)) \
        .isoformat(timespec="seconds")
    # `desde` acota la ventana a lo grabado DESPUES de un cambio de perfil: el
    # promedio de las ultimas 24 h describe la configuracion vieja, no la nueva.
    if desde and desde > corte:
        corte = desde
    with db.sesion() as con:
        f = con.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b,
                      COALESCE(SUM(duracion),0) d
                 FROM segmentos
                WHERE camara_id = ? AND inicio >= ?
                  AND (capa IS NULL OR capa = 'base')
                  AND duracion IS NOT NULL""",
            (camara_id, corte),
        ).fetchone()

    if not f["n"] or not f["d"] or f["d"] < 120:
        return {"kbps": None, "gb_dia": None, "horas_muestra": 0.0,
                "segmentos": f["n"] if f else 0, "confiable": False}
    kbps = f["b"] * 8 / f["d"] / 1000
    return {
        "kbps": round(kbps),
        "gb_dia": round(kbps * 86400 / 8 / 1024 / 1024, 2),
        "horas_muestra": round(f["d"] / 3600, 2),
        "segmentos": f["n"],
        "confiable": f["d"] >= 1800,
    }


def ritmo(horas: float = 24.0) -> dict:
    """GB por dia que el sistema esta escribiendo, medido sobre el indice.

    Es la diferencia entre una proyeccion y un dato: la estimacion por
    bitrates dice cuanto *deberia* ocupar el parque entero grabando; esto dice
    cuanto esta ocupando lo que realmente se esta grabando ahora, con las
    camaras que hay levantadas y los perfiles que tienen puestos.

    Devuelve tambien el tamano de la muestra. Extrapolar un dia entero desde
    veinte minutos de grabacion da un numero grande y falso, asi que la
    interfaz tiene que poder decir sobre que se calculo.
    """
    corte = datetime.now(timezone.utc) - timedelta(hours=horas)
    with db.sesion() as con:
        f = con.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b,
                      MIN(inicio) d, MAX(inicio) h,
                      COUNT(DISTINCT camara_id) c
                 FROM segmentos WHERE inicio >= ?""",
            (corte.isoformat(timespec="seconds"),),
        ).fetchone()

    vacio = {"gb_dia": 0.0, "horas_muestra": 0.0, "camaras": 0,
             "segmentos": 0, "confiable": False}
    if not f["n"] or not f["d"]:
        return vacio

    # El ultimo segmento tambien cubre su propia duracion.
    span = ((_dt(f["h"]) - _dt(f["d"])).total_seconds()
            + config.SEGUNDOS_POR_SEGMENTO)
    if span < 120:
        return vacio
    return {
        "gb_dia": round(f["b"] / 1e9 / (span / 86400), 2),
        "horas_muestra": round(span / 3600, 2),
        "camaras": f["c"],
        "segmentos": f["n"],
        # Menos de una hora de muestra no alcanza para proyectar un dia.
        "confiable": span >= 3600,
    }


def historial(dias: int = 14) -> list[dict]:
    """GB escritos por dia local, del mas viejo al mas nuevo.

    Es lo que dibujan los graficos de Almacenamiento: cuanto entra por dia y
    como se acumula contra el tope de purga.

    Devuelve el calendario COMPLETO, con los dias sin grabacion en cero. Si se
    saltearan, el eje de los graficos pondria un martes al lado de un viernes
    a la misma distancia que dos dias seguidos y la curva mentiria sobre el
    ritmo.
    """
    with db.sesion() as con:
        filas = con.execute(
            """SELECT date(inicio, 'localtime') AS dia, COUNT(*) AS n,
                      COALESCE(SUM(bytes), 0) AS bytes,
                      COALESCE(SUM(duracion), 0) AS seg,
                      SUM(CASE WHEN duracion IS NULL THEN 1 ELSE 0 END) AS sin_medir,
                      COUNT(DISTINCT camara_id) AS camaras
                 FROM segmentos
                GROUP BY dia ORDER BY dia DESC LIMIT ?""",
            (dias,),
        ).fetchall()

    por_dia = {f["dia"]: f for f in filas}
    hoy = datetime.now().date()
    primero = min(por_dia) if por_dia else hoy.isoformat()
    desde = max(datetime.fromisoformat(primero).date(),
                hoy - timedelta(days=dias - 1))

    salida = []
    acumulado = 0.0
    dia = desde
    while dia <= hoy:
        clave = dia.isoformat()
        f = por_dia.get(clave)
        gb = (f["bytes"] / 1e9) if f else 0.0
        acumulado += gb
        segundos = ((f["seg"] + f["sin_medir"] * config.SEGUNDOS_POR_SEGMENTO)
                    if f else 0.0)
        salida.append({
            "dia": clave,
            "gb": round(gb, 3),
            "gb_acumulado": round(acumulado, 3),
            "segmentos": f["n"] if f else 0,
            "camaras": f["camaras"] if f else 0,
            "horas": round(segundos / 3600, 2),
        })
        dia += timedelta(days=1)
    return salida


# Lo ultimo que devolvio `resumen_archivo`, con el momento en que se calculo.
_resumen_cache: tuple[float, dict] | None = None
_resumen_lock = threading.Lock()

# Cuanto vale un resumen antes de recalcularlo.
#
# `/api/archivo` lo pide la barra lateral en CADA cambio de vista y la pantalla
# de Almacenamiento cada diez segundos. Calcularlo cuesta un segundo largo en
# el R710 --tres agregados sobre los 110.000 segmentos, mas el ritmo y el tope
# de disco-- asi que sin esto cada clic del operador le costaba al servidor un
# segundo de CPU para redibujar un numero que cambia una vez por minuto, que es
# lo que tarda en cerrarse un segmento.
VIGENCIA_RESUMEN = 20.0


def resumen_archivo(refrescar: bool = False) -> dict:
    """Estado del archivo. Cacheado `VIGENCIA_RESUMEN` segundos.

    `refrescar=True` fuerza el recalculo: lo usa lo que acaba de cambiar el
    disco (una purga a mano) y necesita ver el efecto en el acto.
    """
    global _resumen_cache
    with _resumen_lock:
        if not refrescar and _resumen_cache:
            calculado, datos = _resumen_cache
            if time.time() - calculado < VIGENCIA_RESUMEN:
                return datos
    datos = _calcular_resumen()
    with _resumen_lock:
        _resumen_cache = (time.time(), datos)
    return datos


def _calcular_resumen() -> dict:
    with db.sesion() as con:
        total = con.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b,
                      MIN(inicio) desde, MAX(inicio) hasta,
                      COUNT(DISTINCT camara_id) camaras
                 FROM segmentos"""
        ).fetchone()
        por_capa = {f["capa"]: f for f in con.execute(
            """SELECT capa, COUNT(*) n, COALESCE(SUM(duracion), 0) s,
                      SUM(CASE WHEN duracion IS NULL THEN 1 ELSE 0 END) sin_medir
                 FROM segmentos GROUP BY capa""").fetchall()}
        por_camara = con.execute(
            """SELECT c.id, c.nombre, COUNT(s.id) n, COALESCE(SUM(s.bytes),0) b,
                      MIN(s.inicio) desde, MAX(s.inicio) hasta
                 FROM camaras c JOIN segmentos s ON s.camara_id = c.id
                GROUP BY c.id, c.nombre ORDER BY b DESC"""
        ).fetchall()

    # Cuanta historia hay: del material mas viejo al mas nuevo.
    profundidad = None
    if total["desde"] and total["hasta"]:
        profundidad = round(
            (_dt(total["hasta"]) - _dt(total["desde"])).total_seconds() / 86400, 2)

    # Cuanto se grabo DE VERDAD. No es lo mismo: entre el segmento mas viejo y
    # el mas nuevo puede haber cuatro dias y una hora de grabacion. Decir
    # "4 dias grabados" cuando hay una hora por camara es el numero que mas
    # engana de toda la pantalla.
    #
    # Se suman las duraciones medidas; a las que todavia no se midieron se les
    # pone la nominal, que para un segmento entero se equivoca por decimas.
    segundos = 0.0
    sin_medir = 0
    for capa, f in por_capa.items():
        segundos += f["s"] + f["sin_medir"] * _nominal(capa)
        sin_medir += f["sin_medir"]
    camaras = total["camaras"] or 0
    cobertura = round(segundos / 86400 / camaras, 2) if camaras else None

    gb = total["b"] / 1e9
    tope = limite_gb()
    medida = ritmo()
    gb_dia = medida["gb_dia"]

    return {
        "segmentos": total["n"],
        "gb": round(gb, 3),
        "desde": total["desde"],
        "hasta": total["hasta"],
        "camaras_grabando": camaras,
        "horas_grabadas": round(segundos / 3600, 2),
        "segmentos_sin_medir": sin_medir,
        "profundidad_dias": profundidad,
        "cobertura_dias": cobertura,
        # Se deja el nombre viejo apuntando al numero honesto: cualquier cosa
        # que todavia lo lea muestra la cobertura, no el lapso.
        "dias_cubiertos": cobertura,
        "limite_gb": round(tope, 1),
        "limite_nominal_gb": round(config.VOLUMEN_UTIL_GB * UMBRAL_PURGA, 1),
        "uso_pct": round(gb / tope * 100, 1) if tope > 0 else None,
        "ritmo": medida,
        # Con el ritmo real: cuanto falta para tocar el tope de purga, y
        # cuantos dias de historia entran en el volumen a este paso.
        "dias_hasta_llenar": (round(max(0.0, tope - gb) / gb_dia, 1)
                              if gb_dia > 0 else None),
        "retencion_alcanzable_dias": (round(tope / gb_dia, 1)
                                      if gb_dia > 0 else None),
        "camaras": [dict(f) | {"gb": round(f["b"] / 1e9, 3)} for f in por_camara],
    }


# --- tarea de fondo --------------------------------------------------------

class Mantenimiento:
    """Hilo que indexa y purga periodicamente."""

    def __init__(self) -> None:
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None
        self.ultimo_indexado = 0
        self.ultima_purga: dict | None = None
        self.ultimo_error: str | None = None

    def arrancar(self) -> None:
        if self._hilo and self._hilo.is_alive():
            return
        self._parar.clear()
        self._hilo = threading.Thread(target=self._bucle, name="archivo",
                                      daemon=True)
        self._hilo.start()

    def detener(self) -> None:
        self._parar.set()

    def _bucle(self) -> None:
        ultima_purga = 0.0
        while not self._parar.is_set():
            try:
                self.ultimo_indexado = indexar()
            except Exception as exc:            # nunca matar el hilo
                self.ultimo_error = f"indexado: {exc}"
            try:
                if time.time() - ultima_purga > INTERVALO_PURGA:
                    self.ultima_purga = purgar(retencion_dias())
                    ultima_purga = time.time()
            except Exception as exc:
                self.ultima_purga = {"error": str(exc)}
            self._parar.wait(INTERVALO_INDEXADO)


mantenimiento = Mantenimiento()

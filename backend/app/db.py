"""Base de datos.

SQLite a proposito: cero instalacion, corre igual en Windows y en el R710, y
para 45 camaras aguanta de sobra (el indice de segmentos se mide en millones de
filas, que SQLite maneja sin problema). El esquema esta escrito en SQL estandar
para poder mudarlo a PostgreSQL sin reescribir nada si el parque crece.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from . import config

ESQUEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sitios (
    id      INTEGER PRIMARY KEY,
    nombre  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS credenciales (
    id       INTEGER PRIMARY KEY,
    nombre   TEXT NOT NULL UNIQUE,
    usuario  TEXT NOT NULL,
    secreto  BLOB NOT NULL,          -- cifrado, ver crypto.py
    creado   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS camaras (
    id                INTEGER PRIMARY KEY,
    nombre            TEXT NOT NULL UNIQUE,
    sitio_id          INTEGER REFERENCES sitios(id),
    ubicacion         TEXT,
    ambiente          TEXT,
    tipo              TEXT,
    marca             TEXT,
    modelo            TEXT,           -- el de la planilla (codigo de producto)
    modelo_reportado  TEXT,           -- el que declara el equipo (nombre comercial)
    serie             TEXT,
    mac               TEXT,
    ip                TEXT NOT NULL UNIQUE,
    mascara           TEXT,
    gateway           TEXT,
    red               TEXT,
    ptz               INTEGER NOT NULL DEFAULT 0,
    lpr               INTEGER NOT NULL DEFAULT 0,
    credencial_id     INTEGER REFERENCES credenciales(id),
    onvif             INTEGER NOT NULL DEFAULT 0,
    eventos_onvif     INTEGER NOT NULL DEFAULT 0,
    desfase_reloj_s   INTEGER,
    estado            TEXT NOT NULL DEFAULT 'desconocido',
    perfil_grabacion  TEXT NOT NULL DEFAULT 'sin_configurar',
    stream_base_id    INTEGER,
    stream_evento_id  INTEGER,
    fila_excel        INTEGER,
    visto             TEXT
);

CREATE TABLE IF NOT EXISTS streams (
    id         INTEGER PRIMARY KEY,
    camara_id  INTEGER NOT NULL REFERENCES camaras(id) ON DELETE CASCADE,
    token      TEXT,
    nombre     TEXT,
    fuente     TEXT,               -- onvif | rtsp
    codec      TEXT,
    ancho      INTEGER,
    alto       INTEGER,
    fps        REAL,
    kbps       INTEGER,            -- bitrate REAL medido, no el declarado
    url        TEXT,               -- sin credenciales
    rol        TEXT,               -- base | evento | snapshot
    medido     TEXT
);

CREATE INDEX IF NOT EXISTS idx_streams_camara ON streams(camara_id);

CREATE TABLE IF NOT EXISTS relevamientos (
    id          INTEGER PRIMARY KEY,
    fecha       TEXT NOT NULL,
    origen      TEXT,
    total       INTEGER,
    responden   INTEGER,
    con_video   INTEGER
);

-- Grabacion persistente. Cada fila es un archivo real en disco; el indice
-- permite armar la linea de tiempo sin recorrer el sistema de archivos.
CREATE TABLE IF NOT EXISTS segmentos (
    id         INTEGER PRIMARY KEY,
    camara_id  INTEGER NOT NULL REFERENCES camaras(id) ON DELETE CASCADE,
    capa       TEXT NOT NULL DEFAULT 'base',   -- base | evento
    archivo    TEXT NOT NULL UNIQUE,           -- ruta relativa a datos/video
    inicio     TEXT NOT NULL,                  -- ISO 8601, hora de inicio
    fin        TEXT,
    duracion   REAL,
    bytes      INTEGER,
    creado     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_seg_camara_inicio ON segmentos(camara_id, inicio);
CREATE INDEX IF NOT EXISTS idx_seg_inicio ON segmentos(inicio);

-- Eventos que reportan las propias camaras (VMD de Sony/Axis, IVA de Bosch).
-- Usar la deteccion de la camara en vez de analizar video en el servidor es lo
-- que permite que el R710 sostenga 45 camaras: el costo de CPU es cero.
CREATE TABLE IF NOT EXISTS eventos (
    id          INTEGER PRIMARY KEY,
    camara_id   INTEGER NOT NULL REFERENCES camaras(id) ON DELETE CASCADE,
    tipo        TEXT NOT NULL,            -- movimiento | persona | vehiculo | patente | senal
    tema        TEXT,                     -- topic ONVIF crudo, para diagnostico
    inicio      TEXT NOT NULL,
    fin         TEXT,
    datos       TEXT,                     -- JSON con los SimpleItem del mensaje
    clip        TEXT,                     -- ruta del clip en calidad, si se grabo
    clip_bytes  INTEGER,
    creado      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ev_camara_inicio ON eventos(camara_id, inicio);
CREATE INDEX IF NOT EXISTS idx_ev_inicio ON eventos(inicio);

CREATE TABLE IF NOT EXISTS notas_camara (
    id         INTEGER PRIMARY KEY,
    camara_id  INTEGER NOT NULL REFERENCES camaras(id) ON DELETE CASCADE,
    texto      TEXT NOT NULL,
    creado     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notas_camara ON notas_camara(camara_id);

-- Grupos de camaras armados a mano, con su lugar en el plano. Son del usuario,
-- no del inventario: un grupo puede juntar camaras de sitios distintos ("pack
-- de ingreso") o partir un sitio en varios ("laboratorio", "playa de carga").
-- Por eso viven aca y no como una columna de `camaras`.
CREATE TABLE IF NOT EXISTS grupos (
    id      INTEGER PRIMARY KEY,
    nombre  TEXT NOT NULL UNIQUE,
    -- Grupo padre. NULL = nodo de primer nivel, que en el plano es un
    -- yacimiento; con padre es un subgrupo suyo (un taller, la enfermeria,
    -- una repetidora). Un solo arbol alcanza: no hace falta una tabla aparte
    -- de "sitios del plano" porque un yacimiento ES un grupo sin padre.
    padre_id INTEGER REFERENCES grupos(id),
    -- Posicion en el plano, en coordenadas del viewBox 1200x820. Se guardan
    -- asi y no en pixeles para que el plano se pueda redimensionar.
    x       REAL NOT NULL DEFAULT 600,
    y       REAL NOT NULL DEFAULT 410,
    creado  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Sin UNIQUE sobre camara_id a proposito: una camara puede estar en mas de un
-- grupo (la del porton sirve al ingreso y al perimetro).
CREATE TABLE IF NOT EXISTS grupos_camaras (
    grupo_id   INTEGER NOT NULL REFERENCES grupos(id) ON DELETE CASCADE,
    camara_id  INTEGER NOT NULL REFERENCES camaras(id) ON DELETE CASCADE,
    PRIMARY KEY (grupo_id, camara_id)
);

CREATE INDEX IF NOT EXISTS idx_gc_camara ON grupos_camaras(camara_id);

-- Registro de auditoria: quien hizo que. Ver auditoria.py.
--
-- No hay endpoint que borre ni edite una fila de aca, a proposito: un registro
-- que se puede limpiar desde la interfaz no sirve como registro.
CREATE TABLE IF NOT EXISTS auditoria (
    id      INTEGER PRIMARY KEY,
    fecha   TEXT NOT NULL DEFAULT (datetime('now')),
    usuario TEXT,
    rol     TEXT,
    ip      TEXT,
    accion  TEXT NOT NULL,
    metodo  TEXT,
    ruta    TEXT,
    estado  INTEGER,
    detalle TEXT
);

CREATE INDEX IF NOT EXISTS idx_aud_fecha ON auditoria(id DESC);
CREATE INDEX IF NOT EXISTS idx_aud_usuario ON auditoria(usuario, id DESC);

-- Tramos de grabacion que la purga NO puede tocar.
--
-- Se guardan como RANGOS de tiempo y no como una marca en cada segmento: el
-- indice de segmentos se puede regenerar leyendo el disco --y se regenera--,
-- asi que una marca ahi se perderia. Un rango sobrevive a todo eso.
CREATE TABLE IF NOT EXISTS preservaciones (
    id        INTEGER PRIMARY KEY,
    camara_id INTEGER NOT NULL REFERENCES camaras(id) ON DELETE CASCADE,
    desde     TEXT NOT NULL,            -- ISO 8601 UTC
    hasta     TEXT NOT NULL,
    motivo    TEXT,
    usuario   TEXT,
    creado    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pres_camara ON preservaciones(camara_id, desde);

-- Cada exportacion de evidencia, con su huella. Es lo que permite despues
-- demostrar que el archivo que alguien tiene en la mano es el que salio de
-- aca y no se toco.
CREATE TABLE IF NOT EXISTS exportaciones (
    id        INTEGER PRIMARY KEY,
    camara_id INTEGER NOT NULL,
    camara    TEXT,
    desde     TEXT NOT NULL,
    hasta     TEXT NOT NULL,
    archivo   TEXT NOT NULL,
    sha256    TEXT NOT NULL,
    bytes     INTEGER,
    motivo    TEXT,
    usuario   TEXT,
    fecha     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_exp_fecha ON exportaciones(id DESC);

-- Ajustes que el usuario cambia desde la interfaz y tienen que sobrevivir a
-- un reinicio. Las variables de entorno del .service siguen siendo el valor
-- por defecto; lo que este aca manda sobre ellas.
CREATE TABLE IF NOT EXISTS ajustes (
    clave  TEXT PRIMARY KEY,
    valor  TEXT NOT NULL,
    fecha  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Usuarios del sistema. La contraseña se guarda SOLO como hash pbkdf2 (ver
-- auth.py); no hay forma de recuperar la original desde la base. El rol es
-- 'operador' (solo mira) o 'admin' (ademas modifica).
CREATE TABLE IF NOT EXISTS usuarios (
    id          INTEGER PRIMARY KEY,
    nombre      TEXT NOT NULL UNIQUE,
    rol         TEXT NOT NULL DEFAULT 'operador',
    clave_hash  TEXT NOT NULL,
    creado      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def conectar() -> sqlite3.Connection:
    config.asegurar_directorios()
    # El grabador escribe sin parar (indice de segmentos, duraciones), y el
    # default de sqlite3 son 5 s de espera por el candado de escritura: poco.
    # Con eso, una consulta de la interfaz que llegue en mal momento moria con
    # "database is locked" en vez de esperar su turno.
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    # `synchronous = NORMAL` es la combinacion recomendada con WAL: se sigue
    # haciendo fsync en cada checkpoint, pero no en cada commit. Lo que se
    # arriesga es perder las ultimas transacciones ante un corte de luz o un
    # panic del kernel -- nunca ante una caida del proceso, que es lo que de
    # verdad pasa-- y aca esas transacciones son filas de un indice que la
    # pasada siguiente vuelve a leer del disco. A cambio, el grabador deja de
    # esperar al plato en cada insercion. FULL era el default y no lo pide
    # nada de lo que guardamos.
    con.execute("PRAGMA synchronous = NORMAL")
    # 64 MB de paginas en memoria en vez de los 2 MB por defecto. Medido sobre
    # los 110.000 segmentos del R710, el resumen por camara --un JOIN con
    # GROUP BY sobre la tabla entera-- baja de 283 ms a 113 ms.
    con.execute("PRAGMA cache_size = -64000")
    return con


@contextmanager
def sesion():
    con = conectar()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def inicializar() -> None:
    con = conectar()
    try:
        con.executescript(ESQUEMA)
        _migrar(con)
        con.commit()
    finally:
        con.close()


def _migrar(con) -> None:
    """Cambios de esquema sobre bases que ya existen.

    `CREATE TABLE IF NOT EXISTS` no agrega columnas a una tabla que ya esta
    creada, asi que las altas posteriores van aca. Es idempotente: se fija
    antes de tocar nada.
    """
    columnas = {f["name"] for f in con.execute("PRAGMA table_info(grupos)")}
    if "padre_id" not in columnas:
        con.execute("ALTER TABLE grupos ADD COLUMN padre_id INTEGER "
                    "REFERENCES grupos(id)")

    # En que camaras QUIERE el usuario escuchar eventos. Es distinto de
    # `eventos_onvif`, que dice si la camara PUEDE: una cosa es la capacidad
    # del equipo y otra la decision de operacion. Arranca en 1 para las que
    # pueden, que es como venia comportandose "Escuchar eventos".
    # El nombre y apellido de la persona, aparte del usuario con el que entra.
    # La auditoria tiene que poder decir "Figueroa, Mauricio" y no "fmauricio".
    columnas = {f["name"] for f in con.execute("PRAGMA table_info(usuarios)")}
    if "nombre_completo" not in columnas:
        con.execute("ALTER TABLE usuarios ADD COLUMN nombre_completo TEXT")

    columnas = {f["name"] for f in con.execute("PRAGMA table_info(camaras)")}
    if "escucha_eventos" not in columnas:
        con.execute("ALTER TABLE camaras ADD COLUMN escucha_eventos "
                    "INTEGER NOT NULL DEFAULT 1")


def fila_a_dict(fila: sqlite3.Row | None) -> dict | None:
    return dict(fila) if fila is not None else None

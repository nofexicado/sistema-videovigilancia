"""Configuracion central de Sistema de Videovigilancia.

Todo lo que dependa del entorno (rutas, tamano del volumen, politica de
grabacion) se resuelve aca, para que el resto del codigo no tenga rutas
escritas a mano.
"""

from __future__ import annotations

import os
from pathlib import Path

# Sistema de Videovigilancia/backend/app/config.py -> Sistema de Videovigilancia/
RAIZ = Path(__file__).resolve().parents[2]

DATOS = Path(os.environ.get("SISTEMA_VIDEOVIGILANCIA_DATOS", RAIZ / "datos"))
DB_PATH = DATOS / "sistema-videovigilancia.db"
CLAVE_PATH = DATOS / "clave.key"

INVENTARIO_CSV = Path(os.environ.get("SISTEMA_VIDEOVIGILANCIA_INVENTARIO",
                                     RAIZ / "inventory" / "cameras.csv"))
RELEVAMIENTO_JSON = Path(os.environ.get("SISTEMA_VIDEOVIGILANCIA_RELEVAMIENTO",
                                        RAIZ / "tools" / "inventario.json"))

# Volumen de grabacion. El R710 hoy expone 1,80 TB; dejamos un margen para el
# indice, las miniaturas y el propio sistema de archivos.
VOLUMEN_TB = float(os.environ.get("SISTEMA_VIDEOVIGILANCIA_VOLUMEN_TB", "1.80"))
MARGEN_VOLUMEN = 0.09
VOLUMEN_UTIL_GB = VOLUMEN_TB * 1000 * (1 - MARGEN_VOLUMEN)

# Fraccion del dia que una camara promedio pasa grabando por evento. Medido
# sobre el parque real ronda el 10%; se ajusta cuando el grabador lleve
# estadistica propia.
CICLO_EVENTO = float(os.environ.get("SISTEMA_VIDEOVIGILANCIA_CICLO_EVENTO", "0.10"))

# Grabacion permanente. Cuando esta activa, ademas de la ventana en vivo se
# escriben segmentos que quedan en disco hasta que la purga los levante.
ARCHIVAR = os.environ.get("SISTEMA_VIDEOVIGILANCIA_ARCHIVAR", "1") not in ("0", "false", "no")

# Cookie de sesion con marca Secure: el navegador solo la manda por HTTPS.
# Se prende cuando nginx pone TLS adelante (el .service lo setea). En
# desarrollo local, servido por HTTP, tiene que quedar apagado o el navegador
# nunca devuelve la cookie y no se puede entrar.
COOKIE_SEGURA = os.environ.get("SISTEMA_VIDEOVIGILANCIA_COOKIE_SEGURA", "0") not in ("0", "false", "no", "")
SEGUNDOS_POR_SEGMENTO = int(os.environ.get("SISTEMA_VIDEOVIGILANCIA_SEGMENTO", "60"))
SEGUNDOS_CLIP = int(os.environ.get("SISTEMA_VIDEOVIGILANCIA_CLIP", "20"))
RETENCION_DIAS = float(os.environ.get("SISTEMA_VIDEOVIGILANCIA_RETENCION_DIAS", "14"))

# Arrancar la captura y la escucha de eventos al levantar el servicio.
#
# Existe porque el 2026-09-10 la actualizacion automatica de Ubuntu parcheo
# Python 3.14 y con eso reinicio el servicio a las 06:39. Volvio `active`, la
# interfaz respondia, y el parque estuvo DOS HORAS Y MEDIA sin grabar porque la
# captura esperaba que alguien apretara un boton. El hueco no avisa: es la
# clase de falla que se descubre cuando hace falta un video que no existe.
#
# Se arranca escalonado y en un hilo aparte, asi que no bloquea el arranque ni
# le cae en tromba a las camaras. Apagarlo (0) deja el comportamiento manual,
# que sirve para desarrollo.
ARRANCAR_AL_INICIO = os.environ.get("SISTEMA_VIDEOVIGILANCIA_ARRANCAR_AL_INICIO", "1") not in (
    "0", "false", "no", "")

# Conversion bajo demanda: convertir solo lo que alguien esta mirando.
#
# Apagado por defecto, a proposito. Medido el 2026-09-07 sobre el parque real,
# convertir cuesta 2 a 4,5% de un nucleo por camara, y son 8 las que lo
# necesitan: unos 25% de un nucleo en total sobre hardware moderno. Encendido
# ahorra eso, pero las 8 tardan entre 5 y 20 s en aparecer cuando se las mira.
# Ese precio solo vale la pena si la CPU del R710 realmente molesta, y eso hay
# que medirlo alla. Se prende con la variable o con el boton del muro.
CONVERSION_DEMANDA = os.environ.get("SISTEMA_VIDEOVIGILANCIA_CONVERSION_DEMANDA", "0") not in (
    "0", "false", "no", "")

# Cuando esta encendida, estos tres numeros son el freno:
#
# * MAX_CONVERSIONES: cuantas conversiones simultaneas se permiten. El R710
#   tiene 12 nucleos de 2010 y cada conversion usa `-threads 2`, asi que 6 es
#   el techo con el que no se queda sin CPU para nada mas.
# * TTL_VISTA: cuanto vale un aviso de "estoy mirando esta camara" sin que lo
#   renueven. El muro avisa cada 8 s; 25 s aguanta dos avisos perdidos.
# * ESPERA_RECONFIG: minimo entre dos reinicios de la misma camara. Cada
#   cambio de modo reabre la sesion RTSP, y hay camaras que no toleran que se
#   las abra y cierre a repeticion.
MAX_CONVERSIONES = int(os.environ.get("SISTEMA_VIDEOVIGILANCIA_MAX_CONVERSIONES", "6"))
TTL_VISTA = float(os.environ.get("SISTEMA_VIDEOVIGILANCIA_TTL_VISTA", "25"))
ESPERA_RECONFIG = float(os.environ.get("SISTEMA_VIDEOVIGILANCIA_ESPERA_RECONFIG", "15"))

# Perfiles de grabacion posibles.
PERFIL_BASE_EVENTO = "base+evento"   # substream continuo + principal por evento
PERFIL_SOLO_EVENTO = "solo_evento"   # sin cobertura continua (una sola calidad)
PERFIL_SIN_CONFIGURAR = "sin_configurar"


def asegurar_directorios() -> None:
    DATOS.mkdir(parents=True, exist_ok=True)

"""Grabador: supervisa un ffmpeg por camara y escribe HLS en disco.

Decisiones que importan:

* **No se recodifica.** El flujo sale de la camara ya comprimido y se copia tal
  cual (`-c copy`). Es lo unico que permite sostener decenas de camaras en un
  R710 sin GPU. La unica excepcion es H.265, que varios navegadores no
  reproducen: esas se transcodifican a H.264 en baja resolucion **solo para
  ver en vivo**, nunca para grabar.

* **Y esa excepcion cuesta CPU, asi que se paga solo cuando se usa.** Convertir
  no depende del codec sino de si alguien esta mirando esa camara: el muro
  avisa que recuadros tiene en pantalla y solo esos se convierten, hasta un
  tope (`config.MAX_CONVERSIONES`). Una camara H.265 que nadie mira sigue
  grabando en archivo por copia, a costo cero. Sin esto el grabador convertia
  las 8 camaras H.265/MPEG-4 del parque las 24 horas, mire alguien o no, y el
  R710 no da para eso.

* **HLS fragmentado sirve para las dos cosas.** Los mismos segmentos alimentan
  el muro en vivo y quedan en disco como grabacion. No hay dos pipelines.

* **Un proceso por camara, vigilado.** Si ffmpeg se cae (corte de red, camara
  reiniciada) el supervisor lo vuelve a levantar con espera progresiva, sin
  martillar a una camara que esta caida.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import binarios, config, db

RAIZ_VIDEO = config.DATOS / "video"

# Los ffmpeg son procesos hijos: si el servidor muere de golpe (kill, corte de
# luz) quedan huerfanos consumiendo CPU. Anotamos sus PID para poder limpiarlos
# en el arranque siguiente.
PIDS = config.DATOS / "grabador.pids"

# Codecs que el navegador no reproduce de forma confiable y hay que convertir
# para el muro en vivo.
CODECS_A_CONVERTIR = {"hevc", "h265", "mpeg4", "mjpeg"}

ESPERA_REINTENTO = [2, 5, 10, 30, 60]   # segundos, progresivo

# Un proceso que aguanto esto sin caerse se considera sano y se le perdonan los
# fallos anteriores. Sin esto `reintentos` solo sube: dos cortes de red en toda
# la vida del servidor dejaban a la camara marcada como "hay que convertirla"
# para siempre, que es justo lo que este modulo trata de evitar.
SEGUNDOS_PARA_PERDONAR = 120

# Cuanto se le tolera a un ffmpeg vivo que no escribe nada.
#
# Un proceso puede quedarse corriendo sin producir un solo byte: si la camara
# entrega paquetes sin timestamps, el muxer los rechaza de a uno y las dos
# salidas quedan vacias, pero ffmpeg no termina nunca. Como el ciclo de
# reintentos solo mira procesos que murieron, ese caso no se detectaba: una
# camara estuvo un dia entero escribiendo archivos de 0 bytes y en
# el muro se veia un recuadro negro, sin ningun error en ningun lado.
#
# El margen tiene que ser mayor que un segmento de archivo (60 s) para no matar
# a una camara sana que todavia no cerro el primero.
SEGUNDOS_SIN_ESCRIBIR = 90

# Cada cuanto se mira si crecio algo. Doce segundos alcanzan: lo que se busca
# es un proceso que no escribe NADA, no medir su ritmo.
INTERVALO_VIGIA = 12

# Separacion entre los arranques de "Iniciar transmision". Levantar las 33
# camaras en el mismo instante deja a los 33 ffmpeg cortando el segmento de
# archivo (60 s) y el de HLS (2 s) en el mismo momento: 33 archivos que se
# cierran y se abren a la vez sobre un solo volumen giratorio es un pico de
# escritura que el muro ve como un tiron simultaneo en todos los recuadros.
# Con un cuarto de segundo de separacion los cortes quedan repartidos.
ESCALON_ARRANQUE = 0.25

# Cuanto dura "levantando" como mucho, contado desde que se creo la tarea.
# Con 39 camaras el escalonado tarda 10 s; el resto es margen para que ffmpeg
# nazca. Pasado esto, una tarea sin proceso ya no esta arrancando: esta caida.
# El limite es por tiempo y no por lista de fallas a proposito -- hay maneras
# de no arrancar nunca (ffmpeg ausente, OSError al lanzar) que dejan el hilo
# muerto sin volver a intentar, y enumerarlas una por una es quedarse corto.
MARGEN_ARRANQUE = 30.0


class Vistas:
    """Que camaras se estan mirando ahora mismo.

    El muro renueva el aviso cada pocos segundos; si deja de hacerlo (se cerro
    la pestana, se cambio de vista) el permiso vence solo. Es deliberado que
    caduque en vez de depender de un aviso de "ya no miro": un navegador que se
    cierra de golpe no avisa nada.
    """

    def __init__(self) -> None:
        self._hasta: dict[int, float] = {}
        self._desde: dict[int, float] = {}
        self._lock = threading.Lock()

    def marcar(self, ids, segundos: float | None = None) -> None:
        ttl = segundos if segundos is not None else config.TTL_VISTA
        ahora = time.time()
        with self._lock:
            for cid in ids:
                if self._hasta.get(cid, 0) <= ahora:
                    self._desde[cid] = ahora
                self._hasta[cid] = ahora + ttl

    def soltar(self, ids=None) -> None:
        with self._lock:
            if ids is None:
                self._hasta.clear()
                self._desde.clear()
                return
            for cid in ids:
                self._hasta.pop(cid, None)
                self._desde.pop(cid, None)

    def mirando(self, camara_id: int) -> bool:
        with self._lock:
            return self._hasta.get(camara_id, 0) > time.time()

    def activas(self) -> set[int]:
        ahora = time.time()
        with self._lock:
            return {cid for cid, hasta in self._hasta.items() if hasta > ahora}

    def desde(self, camara_id: int) -> float:
        """Cuando empezo a mirarse. Ordena la cola cuando faltan plazas."""
        with self._lock:
            return self._desde.get(camara_id, 0.0)


vistas = Vistas()


def _ffmpeg() -> str | None:
    return binarios.ffmpeg()


def _url_con_credenciales(url: str, usuario: str, clave: str) -> str:
    """La base guarda la URL sin la clave. Se inyecta recien al ejecutar."""
    partes = urllib.parse.urlsplit(url)
    host = partes.hostname or ""
    if partes.port:
        host = f"{host}:{partes.port}"
    if usuario:
        cred = (f"{urllib.parse.quote(usuario, safe='')}:"
                f"{urllib.parse.quote(clave, safe='')}@")
    else:
        cred = ""
    return urllib.parse.urlunsplit(
        (partes.scheme, cred + host, partes.path, partes.query, "")
    )


@dataclass
class Tarea:
    camara_id: int
    nombre: str
    url: str
    codec: str
    # Hace falta para reponer timestamps: `setts` necesita saber a que ritmo
    # numerar los paquetes que llegan sin ninguno.
    fps: float | None = None
    proceso: subprocess.Popen | None = None
    reintentos: int = 0
    ultimo_error: str | None = None
    iniciado: float | None = None
    # Si esta tarea llego a levantar su ffmpeg alguna vez. NO se limpia nunca:
    # es lo que separa "todavia no arranco" de "arranco y despues se cayo".
    # `iniciado` no sirve para eso porque se pisa en cada relanzamiento y se
    # borra cuando la tarea se queda esperando sin nada que escribir.
    arranco: bool = False
    creada: float = field(default_factory=time.time)
    convertida_por_falla: bool = False
    repone_timestamps: bool = False
    # Lo mato el vigia por no escribir, no se cayo solo. Se distingue porque un
    # proceso asi puede haber vivido mas que SEGUNDOS_PARA_PERDONAR y no hay
    # que perdonarle nada: no estuvo sano, estuvo mudo.
    sin_produccion: bool = False
    demora_inicial: float = 0.0
    detener: threading.Event = field(default_factory=threading.Event)

    # Conversion bajo demanda. `convirtiendo` es lo que hace el proceso que
    # esta corriendo AHORA; `convertir` es lo que deberia hacer el proximo. El
    # reconciliador es quien mueve el segundo y reinicia ffmpeg para que el
    # primero lo alcance.
    convertir: bool = False
    convirtiendo: bool = False
    esperando_cupo: bool = False
    ultimo_cambio: float = 0.0
    reconfigurar: threading.Event = field(default_factory=threading.Event)

    @property
    def requiere_conversion(self) -> bool:
        """Si esta camara no se puede mirar en el navegador sin convertir."""
        return self.codec in CODECS_A_CONVERTIR or self.convertida_por_falla


class Grabador:
    """Supervisor de los procesos de captura."""

    def __init__(self) -> None:
        self._tareas: dict[int, Tarea] = {}
        self._hilos: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()
        self._reconciliador: threading.Thread | None = None
        self._alto = threading.Event()
        # `_reconciliar` corre desde el hilo del reconciliador y tambien desde
        # el pedido HTTP del muro. Sin este candado las dos pasadas pueden
        # decidir a la vez y reiniciar el mismo ffmpeg dos veces.
        self._lock_cupo = threading.Lock()
        # Apagado = como siempre: las que necesitan conversion la tienen
        # prendida todo el tiempo. Ver config.CONVERSION_DEMANDA.
        self.bajo_demanda = config.CONVERSION_DEMANDA

    # -- ciclo de vida ------------------------------------------------------

    def iniciar(self, camara_id: int, demora: float = 0.0) -> dict:
        with self._lock:
            if camara_id in self._tareas and not self._tareas[camara_id].detener.is_set():
                return {"estado": "ya_corriendo", "camara_id": camara_id}

        datos = self._datos_camara(camara_id)
        if not datos:
            return {"estado": "sin_stream", "camara_id": camara_id,
                    "detalle": "la camara no tiene un stream utilizable o le falta credencial"}

        tarea = Tarea(camara_id=camara_id, nombre=datos["nombre"],
                      url=datos["url"], codec=datos["codec"],
                      fps=datos.get("fps"), demora_inicial=demora)
        # Si ya la estaban mirando cuando se la levanto, arranca convirtiendo:
        # no tiene sentido lanzarla en un modo que el reconciliador va a
        # cambiar dos segundos despues, reabriendo la sesion RTSP al pedo.
        tarea.convertir = not self.bajo_demanda or vistas.mirando(camara_id)
        tarea.ultimo_cambio = time.time()
        with self._lock:
            self._tareas[camara_id] = tarea
            hilo = threading.Thread(target=self._supervisar, args=(tarea,),
                                    name=f"grabador-{camara_id}", daemon=True)
            self._hilos[camara_id] = hilo
        hilo.start()
        self._asegurar_reconciliador()
        return {"estado": "iniciando", "camara_id": camara_id, "nombre": datos["nombre"]}

    def detener(self, camara_id: int) -> dict:
        with self._lock:
            tarea = self._tareas.get(camara_id)
        if not tarea:
            return {"estado": "no_estaba_corriendo", "camara_id": camara_id}
        tarea.detener.set()
        if tarea.proceso and tarea.proceso.poll() is None:
            tarea.proceso.terminate()
            try:
                tarea.proceso.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tarea.proceso.kill()
        with self._lock:
            self._tareas.pop(camara_id, None)
            self._hilos.pop(camara_id, None)
        vistas.soltar([camara_id])
        return {"estado": "detenido", "camara_id": camara_id}

    def detener_todo(self) -> int:
        cuantas = len(self._tareas)
        for cid in list(self._tareas):
            self.detener(cid)
        self._alto.set()
        vistas.soltar()
        return cuantas

    # -- conversion bajo demanda --------------------------------------------

    def mirar(self, ids: list[int]) -> dict:
        """El muro avisa que recuadros tiene en pantalla.

        Es un aviso con vencimiento, no un interruptor: hay que renovarlo. Ver
        `Vistas`. Devuelve en que estado quedo cada camara pedida para que la
        interfaz pueda decir "preparando" o "sin cupo" en vez de un recuadro
        negro sin explicacion.
        """
        vistas.marcar(ids)
        self._reconciliar()
        with self._lock:
            tareas = {t.camara_id: t for t in self._tareas.values()}

        detalle = []
        for cid in ids:
            t = tareas.get(cid)
            if not t:
                detalle.append({"camara_id": cid, "estado": "no_esta_grabando"})
            elif not t.requiere_conversion:
                detalle.append({"camara_id": cid, "estado": "directo"})
            elif t.convirtiendo:
                detalle.append({"camara_id": cid, "estado": "convirtiendo"})
            elif t.esperando_cupo:
                detalle.append({"camara_id": cid, "estado": "sin_cupo"})
            else:
                detalle.append({"camara_id": cid, "estado": "preparando"})
        return {"mirando": len(ids), "cupo": config.MAX_CONVERSIONES,
                "convirtiendo": sum(1 for t in tareas.values() if t.convirtiendo),
                "camaras": detalle}

    def modo_rendimiento(self, activo: bool) -> dict:
        """Prende o apaga la conversion bajo demanda en caliente.

        Prendido ahorra CPU y las camaras H.265 tardan en aparecer; apagado es
        el comportamiento de siempre. El cambio no es instantaneo: aplicarlo
        reinicia el ffmpeg de las camaras afectadas y eso respeta el minimo de
        `config.ESPERA_RECONFIG` entre reinicios, por las camaras.
        """
        cambio = activo != self.bajo_demanda
        self.bajo_demanda = activo
        if cambio:
            self._reconciliar()
        with self._lock:
            afectadas = sum(1 for t in self._tareas.values() if t.requiere_conversion)
        return {"bajo_demanda": self.bajo_demanda, "cambio": cambio,
                "camaras_afectadas": afectadas,
                "segundos_hasta_aplicar": config.ESPERA_RECONFIG if cambio else 0}

    def _asegurar_reconciliador(self) -> None:
        with self._lock:
            if self._reconciliador and self._reconciliador.is_alive():
                return
            self._alto.clear()
            self._reconciliador = threading.Thread(
                target=self._ciclo_reconciliacion, name="grabador-cupo", daemon=True)
        self._reconciliador.start()

    def _ciclo_reconciliacion(self) -> None:
        while not self._alto.wait(2):
            try:
                self._reconciliar()
            except Exception:   # noqa: BLE001 - un fallo aca no puede matar el hilo
                pass

    def _reconciliar(self) -> None:
        """Ajusta quien convierte y quien no, y reinicia solo lo que cambio."""
        with self._lock_cupo:
            self._reconciliar_bajo_candado()

    def _reconciliar_bajo_candado(self) -> None:
        mirando = vistas.activas()
        with self._lock:
            tareas = list(self._tareas.values())

        if not self.bajo_demanda:
            # Modo normal: todo lo que necesita conversion la tiene, se mire o
            # no. Sin cupo ni esperas -- es el comportamiento de siempre.
            con_plaza = {t.camara_id for t in tareas if t.requiere_conversion}
            mirando = con_plaza
        else:
            candidatas = [t for t in tareas
                          if t.requiere_conversion and t.camara_id in mirando]
            # Las que ya estan convirtiendo conservan la plaza; entre las demas
            # gana la que se pidio primero. Sin esa preferencia, un muro con mas
            # recuadros que cupo se pondria a turnar camaras y a reabrir sesiones
            # RTSP sin parar.
            candidatas.sort(key=lambda t: (not t.convirtiendo,
                                           vistas.desde(t.camara_id)))
            con_plaza = {t.camara_id for t in candidatas[:config.MAX_CONVERSIONES]}

        ahora = time.time()
        for t in tareas:
            quiere = t.camara_id in con_plaza
            t.esperando_cupo = (self.bajo_demanda and t.requiere_conversion
                                and t.camara_id in mirando and not quiere)

            # Lo que cambia el comando de ffmpeg NO es este flag sino el modo
            # efectivo (`convirtiendo`): una camara que no necesita conversion
            # se captura exactamente igual con `convertir` en true o en false.
            # Comparar el flag hacia que, unos segundos despues de "Iniciar
            # transmision", el reconciliador reiniciara el ffmpeg de todas las
            # camaras normales -- y de todas a la vez, porque arrancan juntas.
            # Cada reinicio reescribe vivo.m3u8 desde cero y el reproductor se
            # queda sin nada que cargar: eso era el parpadeo sincronizado del
            # muro. Ahora la que no convierte solo anota la intencion.
            if not t.requiere_conversion:
                t.convertir = quiere
                continue

            if quiere == t.convirtiendo:
                t.convertir = quiere
                continue
            if quiere == t.convertir:
                continue
            # El minimo entre cambios protege a la camara, no al servidor:
            # cada reinicio reabre la sesion RTSP y hay equipos que no lo
            # toleran a repeticion.
            if ahora - t.ultimo_cambio < config.ESPERA_RECONFIG:
                continue
            t.convertir = quiere
            t.ultimo_cambio = ahora
            self._reiniciar(t)

    def _reiniciar(self, tarea: Tarea) -> None:
        """Corta el ffmpeg actual para que el supervisor lo relance con el modo
        nuevo. No es una falla: `reconfigurar` se lo avisa."""
        tarea.reconfigurar.set()
        proc = tarea.proceso
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def estado(self) -> list[dict]:
        salida = []
        with self._lock:
            tareas = list(self._tareas.values())
        for t in tareas:
            vivo = t.proceso is not None and t.proceso.poll() is None
            salida.append({
                "camara_id": t.camara_id,
                "nombre": t.nombre,
                "corriendo": vivo,
                # Todavia dentro del arranque escalonado. Lo que sigue sin
                # levantar despues de esto es una falla, no una demora.
                "esperando_arranque": (not t.arranco
                                       and time.time() - t.creada < MARGEN_ARRANQUE),
                "codec": t.codec,
                # `convertido` es lo que esta haciendo ahora, no lo que haria
                # si alguien mirara: es lo unico que explica el consumo de CPU.
                "convertido": t.convirtiendo,
                "timestamps_repuestos": t.repone_timestamps,
                "requiere_conversion": t.requiere_conversion,
                "motivo_conversion": ("codec no compatible con el navegador"
                                      if t.codec in CODECS_A_CONVERTIR
                                      else "el flujo no se pudo copiar tal cual"
                                      if t.convertida_por_falla else None),
                "mirando": vistas.mirando(t.camara_id),
                "esperando_cupo": t.esperando_cupo,
                # Sin conversion activa una camara H.265 graba pero no se puede
                # mirar. El muro necesita saberlo para no cargar un playlist
                # que no existe.
                "en_vivo": vivo and (not t.requiere_conversion or t.convirtiendo),
                "reintentos": t.reintentos,
                "ultimo_error": t.ultimo_error,
                "segundos": round(time.time() - t.iniciado) if t.iniciado else None,
                "playlist": f"/media/{t.camara_id}/vivo.m3u8",
            })
        return sorted(salida, key=lambda x: x["nombre"])

    # -- interno ------------------------------------------------------------

    def _datos_camara(self, camara_id: int) -> dict | None:
        from . import crypto

        with db.sesion() as con:
            fila = con.execute(
                """SELECT cam.nombre, cam.stream_evento_id, cam.stream_base_id,
                          cr.usuario, cr.secreto
                     FROM camaras cam
                     LEFT JOIN credenciales cr ON cr.id = cam.credencial_id
                    WHERE cam.id = ?""",
                (camara_id,),
            ).fetchone()
            if not fila:
                return None

            # Para el muro en vivo preferimos la capa base (mas liviana); si no
            # hay, usamos la de evento.
            stream_id = fila["stream_base_id"] or fila["stream_evento_id"]
            if not stream_id:
                return None
            stream = con.execute("SELECT url, codec, fps FROM streams WHERE id = ?",
                                 (stream_id,)).fetchone()

        if not stream or not stream["url"]:
            return None

        usuario = fila["usuario"] or ""
        clave = crypto.descifrar(fila["secreto"]) if fila["secreto"] else ""
        return {
            "nombre": fila["nombre"],
            "url": _url_con_credenciales(stream["url"], usuario, clave),
            "codec": (stream["codec"] or "").lower(),
            "fps": stream["fps"],
        }

    def _comando(self, tarea: Tarea, destino: Path) -> list[str]:
        binario = _ffmpeg()
        salida = destino / "vivo.m3u8"
        base = [
            binario, "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-fflags", "+genpts+discardcorrupt",
            "-err_detect", "ignore_err",
            "-i", tarea.url,
            "-an",                       # las Bosch anuncian audio que no envian
        ]

        # Algunos equipos entregan un flujo que ffmpeg no puede remuxar sin
        # tocar. Se prueban dos arreglos, del mas barato al mas caro:
        #
        # 1. Reponer los timestamps (`setts`). Es lo que necesitan las camaras
        #    que mandan paquetes sin PTS ni DTS -- algunas escriben
        #    archivos de 0 bytes por esto. Sigue siendo copia: no cuesta CPU.
        # 2. Convertir. Cuesta CPU de verdad, y es el ultimo recurso: solo si
        #    reponer los timestamps tampoco alcanzo.
        if tarea.reintentos >= 2:
            tarea.repone_timestamps = True
        if tarea.reintentos >= 4:
            tarea.convertida_por_falla = True

        # Una camara que hay que convertir y que nadie esta mirando no lleva
        # ventana en vivo: nadie la va a pedir, y escribirla en H.265 seria
        # gastar disco en un playlist que el navegador no puede reproducir. Se
        # queda solo con el archivo, que es copia pura.
        tarea.convirtiendo = tarea.requiere_conversion and tarea.convertir
        con_vivo = not tarea.requiere_conversion or tarea.convirtiendo

        # `setts` numera los paquetes a un ritmo fijo, asi que hay que decirle
        # cual: se usa el fps medido de la camara. Si estuviera mal, el archivo
        # queda con la linea de tiempo corrida -- pero eso es reparable, y un
        # archivo de 0 bytes no.
        reponer = []
        if tarea.repone_timestamps:
            fps = tarea.fps if tarea.fps and tarea.fps > 0 else 25
            reponer = ["-bsf:v", f"setts=ts=N*90000/{fps:g}"]

        if tarea.convirtiendo:
            # Vista previa, no grabacion: 360p a 10 fps alcanza de sobra para
            # mirar un muro de 12 recuadros y cuesta una fraccion de lo que
            # costaba a 480p/25. `-threads 2` evita que una sola camara se
            # quede con todos los nucleos y ahogue a las demas.
            video = [
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-profile:v", "baseline", "-level", "3.0", "-pix_fmt", "yuv420p",
                "-vf", "scale=-2:360,fps=10",
                "-g", "20", "-b:v", "450k", "-maxrate", "600k", "-bufsize", "1200k",
                "-threads", "2",
            ]
        else:
            video = ["-c:v", "copy"] + reponer

        # SALIDA 1 - vivo. Ventana de 12 segmentos (~24 s): da margen para que un
        # tiron de red no deje al reproductor sin nada que cargar. Es efimera:
        # delete_segments la mantiene chica y va borrando lo viejo.
        hls = [
            "-map", "0:v:0",
        ] + video + [
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "12",
            "-hls_delete_threshold", "4",
            "-hls_flags", "delete_segments+independent_segments+temp_file",
            "-hls_segment_filename", str(destino / "seg_%05d.ts"),
            str(salida),
        ]

        if not con_vivo:
            hls = []

        if not config.ARCHIVAR:
            return base + hls

        # SALIDA 2 - archivo permanente. SIEMPRE copia el flujo original, aunque
        # el vivo se este convirtiendo: la grabacion guarda la calidad de la
        # camara y no cuesta nada de CPU. Convertir es solo para poder mirar.
        carpeta = destino / "archivo"
        carpeta.mkdir(parents=True, exist_ok=True)
        archivo = [
            "-map", "0:v:0", "-c:v", "copy", *reponer,
            "-f", "segment",
            "-segment_time", str(config.SEGUNDOS_POR_SEGMENTO),
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            "-strftime", "1",
            str(carpeta / "archivo_%Y%m%d-%H%M%S.ts"),
        ]
        return base + hls + archivo

    def _supervisar(self, tarea: Tarea) -> None:
        destino = RAIZ_VIDEO / str(tarea.camara_id)
        destino.mkdir(parents=True, exist_ok=True)
        _limpiar_vivo(destino)

        # La espera va aca y no en `iniciar_todas` para no dejar colgado el
        # pedido HTTP del muro: cada camara se demora en su propio hilo.
        if tarea.demora_inicial:
            tarea.detener.wait(tarea.demora_inicial)

        while not tarea.detener.is_set():
            if not _ffmpeg():
                tarea.ultimo_error = "ffmpeg no esta instalado"
                return

            comando = self._comando(tarea, destino)
            if not tarea.convirtiendo and tarea.requiere_conversion:
                # El playlist de la vuelta anterior ya no se va a actualizar:
                # si queda en disco, el muro lo carga y muestra imagen vieja.
                _limpiar_vivo(destino)

            # Sin archivo permanente y sin nadie mirando no hay nada que
            # escribir. En vez de lanzar un ffmpeg sin salidas -- que falla y
            # entra en un ciclo de reintentos contra la camara -- se espera.
            if not config.ARCHIVAR and not tarea.convirtiendo and tarea.requiere_conversion:
                tarea.proceso = None
                tarea.iniciado = None
                tarea.reconfigurar.wait(5)
                tarea.reconfigurar.clear()
                continue

            try:
                proc = subprocess.Popen(
                    comando,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as exc:
                tarea.ultimo_error = str(exc)
                return

            tarea.proceso = proc
            tarea.iniciado = time.time()
            tarea.arranco = True
            tarea.sin_produccion = False
            _anotar_pid(proc.pid)
            # `communicate` se queda esperando a que ffmpeg termine, y el caso
            # que hay que cazar es justamente el que no termina nunca. Por eso
            # el control va en un hilo aparte, mirando el disco.
            threading.Thread(target=self._vigilar_produccion,
                             args=(tarea, proc, destino), daemon=True,
                             name=f"vigia-{tarea.camara_id}").start()
            _, err = proc.communicate()
            duracion = time.time() - (tarea.iniciado or time.time())

            if tarea.detener.is_set():
                return

            # Lo matamos nosotros para cambiarle el modo: no es una falla, no
            # cuenta como reintento y se vuelve a levantar en el acto.
            if tarea.reconfigurar.is_set():
                tarea.reconfigurar.clear()
                tarea.ultimo_error = None
                continue

            # Aguantar mucho tiempo solo cuenta si ademas estuvo escribiendo:
            # un proceso mudo puede pasarse las horas vivo y perdonarle los
            # fallos lo dejaria sin convertir nunca.
            if duracion >= SEGUNDOS_PARA_PERDONAR and not tarea.sin_produccion:
                tarea.reintentos = 0
                if not tarea.convirtiendo and not tarea.repone_timestamps:
                    # Corrio sano copiando TAL CUAL: la razon por la que se la
                    # habia marcado como "no se puede copiar" ya no aplica.
                    #
                    # La condicion mira los dos arreglos porque un proceso que
                    # aguanto dos minutos gracias a `setts` corrio sano POR el
                    # arreglo, no a pesar de el. Sacarselo aca lo devolvia a
                    # copia pura, que vuelve a fallar, y la camara quedaba
                    # rebotando entre los dos modos cada cinco minutos.
                    tarea.convertida_por_falla = False

            if tarea.sin_produccion:
                tarea.ultimo_error = (
                    f"ffmpeg seguia vivo pero no escribio nada en "
                    f"{SEGUNDOS_SIN_ESCRIBIR}s; se lo reinicia")
            else:
                tarea.ultimo_error = ((err or "").strip()[-300:]
                                      or f"ffmpeg salio con {proc.returncode}")
            espera = ESPERA_REINTENTO[min(tarea.reintentos, len(ESPERA_REINTENTO) - 1)]
            tarea.reintentos += 1
            tarea.detener.wait(espera)


    def _vigilar_produccion(self, tarea: Tarea, proc: subprocess.Popen,
                            destino: Path) -> None:
        """Mata al ffmpeg que sigue vivo pero dejo de escribir.

        Se mira el disco y no la salida de ffmpeg a proposito: lo que importa
        no es que el proceso se queje sino que la grabacion avance. Un ffmpeg
        que rechaza cada paquete escribe en stderr sin parar y aun asi no deja
        un solo byte de video.

        Al matarlo, el ciclo de `_supervisar` lo cuenta como falla, y a los dos
        intentos `_comando` pasa a convertir en vez de copiar -- que es lo unico
        que funciona con estas camaras.
        """
        huella = _produccion(destino)
        ultimo_avance = time.time()

        while proc.poll() is None:
            if tarea.detener.wait(INTERVALO_VIGIA):
                return
            # Un cambio de modo reinicia el proceso por su cuenta; no es una
            # falla y el vigia no tiene nada que hacer aca.
            if tarea.reconfigurar.is_set() or tarea.detener.is_set():
                return

            ahora = _produccion(destino)
            if ahora != huella:
                huella = ahora
                ultimo_avance = time.time()
                continue

            if time.time() - ultimo_avance < SEGUNDOS_SIN_ESCRIBIR:
                continue

            tarea.sin_produccion = True
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except OSError:
                pass
            return


def _produccion(destino: Path) -> tuple:
    """Huella de lo que se escribio, para saber si avanzo entre dos vueltas.

    No sirve sumar todo el archivo: la purga borra segmentos viejos mientras
    esto corre y el total podria bajar aunque la camara este grabando bien. Se
    mira solo el segmento de archivo mas nuevo -- el que ffmpeg tiene abierto y
    va creciendo -- y el peso de la ventana en vivo.
    """
    ultimo = (None, 0)
    carpeta = destino / "archivo"
    if carpeta.is_dir():
        try:
            ficheros = sorted(carpeta.glob("*.ts"))
        except OSError:
            ficheros = []
        if ficheros:
            reciente = ficheros[-1]
            try:
                ultimo = (reciente.name, reciente.stat().st_size)
            except OSError:
                ultimo = (reciente.name, 0)

    vivo = 0
    try:
        for seg in destino.glob("*.ts"):
            try:
                vivo += seg.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return ultimo + (vivo,)


def _limpiar_vivo(destino: Path) -> None:
    """Borra la ventana en vivo. La carpeta `archivo/` no se toca: ahi vive la
    grabacion y borrarla seria perder material."""
    for patron in ("*.ts", "*.m3u8"):
        for viejo in destino.glob(patron):
            try:
                viejo.unlink()
            except OSError:
                pass


def _anotar_pid(pid: int) -> None:
    try:
        config.asegurar_directorios()
        with open(PIDS, "a", encoding="utf-8") as fh:
            fh.write(f"{pid}\n")
    except OSError:
        pass


def limpiar_huerfanos() -> int:
    """Mata los ffmpeg que quedaron vivos de una ejecucion anterior.

    Se llama al arrancar el servidor. Sin esto, cada reinicio deja atras los
    procesos de la vez pasada y la CPU se satura hasta que el video empieza a
    entrecortarse -- que es exactamente lo que pasa si no se limpia.
    """
    if not PIDS.exists():
        return 0

    muertos = 0
    for linea in PIDS.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea.isdigit():
            continue
        pid = int(linea)
        try:
            proc = psutil_like_terminar(pid)
            if proc:
                muertos += 1
        except Exception:
            pass

    try:
        PIDS.unlink()
    except OSError:
        pass
    return muertos


def psutil_like_terminar(pid: int) -> bool:
    """Termina un PID si sigue vivo y realmente es un ffmpeg nuestro."""
    import signal

    if sys.platform == "win32":
        salida = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True, text=True,
        )
        return salida.returncode == 0
    try:
        nombre = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if "ffmpeg" not in nombre:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


grabador = Grabador()


def iniciar_todas(limite: int | None = None) -> list[dict]:
    """Levanta la captura de todas las camaras en linea con credencial cargada."""
    with db.sesion() as con:
        filas = con.execute(
            """SELECT id FROM camaras
                WHERE estado = 'en_linea'
                  AND credencial_id IS NOT NULL
                  AND (stream_base_id IS NOT NULL OR stream_evento_id IS NOT NULL)
                ORDER BY nombre"""
        ).fetchall()
    ids = [f["id"] for f in filas]
    if limite:
        ids = ids[:limite]
    return [grabador.iniciar(cid, demora=i * ESCALON_ARRANQUE)
            for i, cid in enumerate(ids)]

"""Calculo de consumo y retencion a partir de los bitrates REALES medidos.

Nada de estimaciones por tabla de fabricante: las cifras salen de los bytes que
cada camara entrego durante el relevamiento. Las camaras que no se pudieron
medir se completan con la mediana del resto, que es mas honesto que el promedio
porque el parque tiene dos equipos muy pesados que lo distorsionan.
"""

from __future__ import annotations

from statistics import median

from . import config, db

SEGUNDOS_DIA = 86400


def _gb_por_dia(kbps: float) -> float:
    return kbps * 1000 / 8 * SEGUNDOS_DIA / 1e9


def _mediana(valores: list[int]) -> float:
    return float(median(valores)) if valores else 0.0


def _cargar() -> list[dict]:
    with db.sesion() as con:
        filas = con.execute(
            """SELECT cam.id, cam.nombre, cam.estado, cam.perfil_grabacion,
                      b.kbps AS base_kbps, e.kbps AS evento_kbps,
                      e.ancho AS ancho, e.alto AS alto, e.fps AS fps, e.codec AS codec
                 FROM camaras cam
                 LEFT JOIN streams b ON b.id = cam.stream_base_id
                 LEFT JOIN streams e ON e.id = cam.stream_evento_id
                ORDER BY cam.nombre"""
        ).fetchall()
    return [dict(f) for f in filas]


def resumen(ciclo_evento: float | None = None) -> dict:
    """Proyeccion de consumo del perfil con el que trabaja el sistema.

    El perfil ya no se elige: el parque graba **base continua + eventos en
    calidad** (`config.PERFIL_BASE_EVENTO`). Antes esto devolvia tres
    escenarios para comparar, que servia para decidir; decidido, la
    comparacion es ruido en pantalla.

    Ojo con que es una PROYECCION -- suma los bitrates medidos de las 39
    camaras como si todas grabaran todo el dia. Lo que se esta escribiendo de
    verdad lo mide `archivo.ritmo()`, que es otro numero y no tiene por que
    coincidir: hoy no estan las 39 levantadas.
    """
    ciclo = config.CICLO_EVENTO if ciclo_evento is None else ciclo_evento
    camaras = _cargar()

    eventos_medidos = [c["evento_kbps"] for c in camaras if c["evento_kbps"]]
    bases_medidas = [c["base_kbps"] for c in camaras if c["base_kbps"]]
    med_evento = _mediana(eventos_medidos)
    med_base = _mediana(bases_medidas)

    # Completamos las no medidas con la mediana para que el total represente al
    # parque entero y no solo a la parte que respondio.
    suma_evento = sum(c["evento_kbps"] or med_evento for c in camaras)
    suma_base = sum(c["base_kbps"] or med_base for c in camaras)

    gb_dia = _gb_por_dia(suma_base + suma_evento * ciclo)
    retencion = (round(config.VOLUMEN_UTIL_GB / gb_dia, 1)
                 if gb_dia > 0 else None)

    return {
        "volumen_tb": config.VOLUMEN_TB,
        "volumen_util_gb": round(config.VOLUMEN_UTIL_GB),
        "ciclo_evento": ciclo,
        "camaras": len(camaras),
        "camaras_medidas": len(eventos_medidos),
        "mediana_evento_kbps": round(med_evento),
        "mediana_base_kbps": round(med_base),
        "perfil": config.PERFIL_BASE_EVENTO,
        "perfil_nombre": "Base continua + eventos en calidad",
        "gb_dia": round(gb_dia, 1),
        "retencion_dias": retencion,
        "base_kbps": round(suma_base),
        "evento_kbps": round(suma_evento),
    }


def por_camara(ciclo_evento: float | None = None) -> list[dict]:
    """Consumo diario de cada camara, de mayor a menor."""
    ciclo = config.CICLO_EVENTO if ciclo_evento is None else ciclo_evento
    camaras = _cargar()
    med_evento = _mediana([c["evento_kbps"] for c in camaras if c["evento_kbps"]])
    med_base = _mediana([c["base_kbps"] for c in camaras if c["base_kbps"]])

    salida = []
    for cam in camaras:
        evento = cam["evento_kbps"] or med_evento
        base = cam["base_kbps"] or med_base
        gb = _gb_por_dia(base + evento * ciclo)
        salida.append({
            "id": cam["id"],
            "nombre": cam["nombre"],
            "estado": cam["estado"],
            "perfil": cam["perfil_grabacion"],
            "codec": cam["codec"],
            "resolucion": f"{cam['ancho']}x{cam['alto']}" if cam["ancho"] else None,
            "fps": cam["fps"],
            "evento_kbps": cam["evento_kbps"],
            "base_kbps": cam["base_kbps"],
            "medida_real": bool(cam["evento_kbps"]),
            "gb_dia": round(gb, 2),
        })
    salida.sort(key=lambda c: c["gb_dia"], reverse=True)
    return salida

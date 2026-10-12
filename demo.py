"""
Corrida de demostración del flujo de delegación: un escenario por cada
comportamiento que pide la consigna. Genera la traza de "quién decidió qué y
por qué" en trazas/traza_orquestador.json y trazas/traza_orquestador.log.

Uso:
    python demo.py              # los 5 escenarios
    python demo.py --desde 3    # retoma desde el escenario 3

La traza se guarda después de CADA escenario.
Si el proveedor corta (cuota, red), lo hecho no se pierde y se retoma con
--desde. La corrida completa hace unas 60 llamadas al LLM: con gpt-4o-mini
cuesta unos pocos centavos de dólar; la capa gratuita de Gemini (20 pedidos
por día) no alcanza.
"""
import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import config
import prompts
from errores import ERRORES_DEL_PROVEEDOR, avisar_error_del_llm
from graph import crear_orquestador
from llm import describir_llm
from traza import correr_tarea

# En Windows, al redirigir la salida a un archivo (python demo.py > salida.txt),
# Python escribe en cp1252, que no tiene los emojis de la traza: forzamos UTF-8.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


@dataclass(frozen=True)
class Escenario:
    numero: int
    nombre: str
    demuestra: str
    tarea: str
    esperado: str
    simulacion: dict | None = None


ESCENARIOS = [
    Escenario(1, "Delegación completa (catálogo)",
              "Supervisor -> Investigador -> Analista -> síntesis, con datos del catálogo",
              "¿Cuántos CV y cuántos Nm más tiene el Virtus Exclusive que el Tera High, y cuántas unidades "
              "de esas dos versiones hay en total en la sucursal Palermo?",
              "49 CV, 80 Nm y 3 unidades"),
    Escenario(2, "Delegación completa (fichas técnicas)",
              "El Investigador usa la otra herramienta (búsqueda en las fichas) y el Validador lee el formato '89,88 %'",
              "Según Latin NCAP, ¿cuántos puntos porcentuales de diferencia hay entre el Tera y el Nivus en "
              "protección de ocupante adulto?",
              "2,59 puntos (Nivus 92,47 % vs. Tera 89,88 %)"),
    Escenario(3, "Falla de un especialista",
              "El Investigador falla (simulado): el supervisor no se rompe y lo reintenta",
              "¿Cuánto torque más tiene el Taos que el Nivus Sense?",
              "80 Nm", {"fallar_investigador": 1}),
    Escenario(4, "Bucle de reparación",
              "El Analista entrega una cuenta mal hecha (simulado): el Validador la detecta y se repara con el error específico",
              "¿Qué porcentaje más de potencia tiene el Vento GLI que el Polo Highline?",
              "≈ 132,32 %", {"corromper_analisis": 1}),
    Escenario(5, "Escalado a revisión humana",
              "El Analista se equivoca siempre (simulado): tras MAX_REPARACIONES se escala en vez de reintentar sin fin",
              "¿Cuántas unidades de la Saveiro hay en total, sumando todas sus versiones, en la sucursal Córdoba?",
              "8 unidades, pero el resultado se marca para revisión humana", {"corromper_analisis": 99}),
]

ARCHIVO_JSON = Path(config.TRAZAS_DIR) / "traza_orquestador.json"
ARCHIVO_LOG = Path(config.TRAZAS_DIR) / "traza_orquestador.log"


def encabezado() -> dict:
    return {
        "llm": describir_llm(),
        "prompts_version": prompts.PROMPTS_VERSION,
        "prompts_huella": prompts.huella_prompts(),
        "busqueda_backend": config.backend_busqueda(),
        "max_pasos": config.max_pasos(),
        "max_reparaciones": config.max_reparaciones(),
        "recursion_limit": config.recursion_limit(),
    }


def guardar(resultados: list[dict]) -> None:
    ARCHIVO_JSON.parent.mkdir(exist_ok=True)
    traza = {"actualizado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "escenarios_completados": f"{len(resultados)} de {len(ESCENARIOS)}", "escenarios": resultados}
    ARCHIVO_JSON.write_text(json.dumps(traza, ensure_ascii=False, indent=2), encoding="utf-8")
    log = [f"Traza del orquestador — {traza['actualizado_en']} — escenarios: {traza['escenarios_completados']}\n"]
    for r in resultados:
        c = r["configuracion"]
        log.append(f"{'=' * 100}\n### {r['numero']}. {r['escenario']} — {r['demuestra']}\n"
                   f"[LLM: {c['llm']} | prompts {c['prompts_version']} ({c['prompts_huella']}) | {r['ejecutado_en']}]\n"
                   f"Respuesta esperada: {r['esperado']}\n" + "\n".join(r["log"]) + "\n" + r["resumen_texto"])
    ARCHIVO_LOG.write_text("\n\n".join(log), encoding="utf-8")


def resumir(corrida) -> tuple[dict, str]:
    final = corrida.estado_final
    decisiones = final.get("decisiones", [])
    validaciones = final.get("validaciones", [])
    resumen = {
        "intervenciones_de_especialistas": final.get("pasos", 0),
        "decisiones_llm": sum(d.origen == "llm" for d in decisiones),
        "decisiones_por_regla": sum(d.origen == "regla" for d in decisiones),
        "validaciones_fallidas": sum(not v.ok for v in validaciones),
        "escalado_humano": final.get("escalado_humano", False),
    }
    texto = (f"Resumen: {resumen['intervenciones_de_especialistas']} intervenciones de especialistas | "
             f"decisiones: {resumen['decisiones_llm']} del LLM, {resumen['decisiones_por_regla']} por regla | "
             f"validaciones fallidas: {resumen['validaciones_fallidas']} | "
             f"escalado a humano: {'SÍ' if resumen['escalado_humano'] else 'no'}")
    return resumen, texto


def previos(desde: int) -> list[dict]:
    if desde == 1:
        ARCHIVO_JSON.unlink(missing_ok=True)
        ARCHIVO_LOG.unlink(missing_ok=True)
        return []
    if not ARCHIVO_JSON.exists():
        raise SystemExit(f"No hay traza previa en {ARCHIVO_JSON}: corré primero 'python demo.py' sin --desde.")
    hechos = [r for r in json.loads(ARCHIVO_JSON.read_text(encoding="utf-8"))["escenarios"] if r["numero"] < desde]
    faltantes = set(range(1, desde)) - {r["numero"] for r in hechos}
    if faltantes:
        raise SystemExit(f"La traza guardada no tiene los escenarios {sorted(faltantes)}: usá --desde {min(faltantes)}.")
    return hechos


async def main(desde: int) -> None:
    resultados = previos(desde)
    app = crear_orquestador()
    for e in (e for e in ESCENARIOS if e.numero >= desde):
        print(f"\n{'=' * 100}\n{e.numero}. {e.nombre} — {e.demuestra}\n{'=' * 100}")
        corrida = await correr_tarea(app, e.tarea, simulacion=e.simulacion)
        resumen, texto = resumir(corrida)
        print(texto)
        resultados.append({
            "numero": e.numero, "escenario": e.nombre, "demuestra": e.demuestra, "tarea": e.tarea,
            "esperado": e.esperado, "simulacion": e.simulacion, "configuracion": encabezado(),
            "ejecutado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "respuesta_final": corrida.respuesta, "resumen": resumen, "resumen_texto": texto,
            "log": corrida.lineas, "eventos": corrida.eventos,
        })
        guardar(resultados)
    print(f"\n✅ Traza completa ({len(resultados)} de {len(ESCENARIOS)} escenarios) en {ARCHIVO_JSON} y {ARCHIVO_LOG}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Demostración del orquestador multi-agente")
    parser.add_argument("--desde", type=int, default=1, choices=range(1, len(ESCENARIOS) + 1))
    desde = parser.parse_args().desde
    try:
        asyncio.run(main(desde))
    except ERRORES_DEL_PROVEEDOR as error:
        avisar_error_del_llm(error)
        if ARCHIVO_JSON.exists():
            hechos = json.loads(ARCHIVO_JSON.read_text(encoding="utf-8"))["escenarios"]
            siguiente = max((r["numero"] for r in hechos), default=0) + 1
            print(f"\n💾 Traza parcial guardada ({len(hechos)} escenarios). Para retomar: python demo.py --desde {siguiente}")

"""
El grafo del orquestador (topología jerárquica, patrón Supervisor):

    START -> supervisor --(Literal)--> investigador -> validador -> supervisor
                        \\-----------> analista     -> validador -> supervisor
                         \\----------> sintesis -> END

- Los especialistas nunca hablan entre sí: todo pasa por el supervisor.
- Todo lo que produce un especialista pasa por el VALIDADOR determinístico
  antes de volver al supervisor (la "puerta de calidad").
- El supervisor decide con dos capas:
    1. Reglas determinísticas, que se evalúan primero y no gastan LLM: límite
       de pasos, bucle de reparación con el error específico, escalado a
       revisión humana y guardas de suficiencia.
    2. Si ninguna regla aplica, decide el LLM con su rúbrica
       (with_structured_output -> Literal de nombres de nodo).
  Cada decisión queda registrada con su origen ("regla" o "llm") y su razón.
- Aislamiento de escritura: con_parcela() verifica en cada paso que un nodo
  solo escriba las claves de su parcela (state.PARCELAS).
"""
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy

import config
import prompts
from llm import es_error_transitorio, get_llm
from state import (
    PARCELAS,
    Contribucion,
    DecisionRegistrada,
    DecisionSupervisor,
    EstadoOrquestador,
    ResultadoValidacion,
)
from validation import validar_analisis, validar_investigacion

Nodo = Callable[[EstadoOrquestador, RunnableConfig], Awaitable[dict]]

# Reintento ante fallas TRANSITORIAS del LLM (503, rate limit por minuto).
# Una cuota diaria agotada o una API key inválida no se reintentan.
REINTENTO_LLM = RetryPolicy(initial_interval=2.0, backoff_factor=2.0, max_attempts=4, retry_on=es_error_transitorio)


class ParcelaInvadida(RuntimeError):
    """Un nodo intentó escribir una clave del estado que no es suya."""


def con_parcela(nombre: str, nodo: Nodo) -> Nodo:
    async def envuelto(state: EstadoOrquestador, config: RunnableConfig) -> dict:
        actualizacion = await nodo(state, config)
        ajenas = set(actualizacion) - PARCELAS[nombre]
        if ajenas:
            raise ParcelaInvadida(f"El nodo '{nombre}' intentó escribir fuera de su parcela: {sorted(ajenas)}")
        return actualizacion

    return envuelto


# --- Reglas determinísticas del supervisor -----------------------------------
def _instruccion_original(state: EstadoOrquestador, agente: str) -> str:
    """La última instrucción 'de fondo' que recibió el agente (no un prompt de reparación)."""
    for d in reversed(state["decisiones"]):
        if d.siguiente == agente and d.origen == "llm":
            return d.instruccion
    return state["tarea"]


def _aporte_anterior(state: EstadoOrquestador, agente: str) -> str:
    aporte = state["investigacion"] if agente == "investigador" else state["analisis"]
    return aporte.model_dump_json(indent=2) if aporte else "(no produjo un resultado)"


def decidir_por_reglas(state: EstadoOrquestador) -> tuple[DecisionSupervisor | None, dict]:
    """Devuelve (decisión, cambios extra al estado), o (None, {}) si decide el LLM."""
    if state["pasos"] >= config.max_pasos():
        return DecisionSupervisor(
            siguiente="FINALIZAR",
            razon=f"Se alcanzó el límite de {config.max_pasos()} intervenciones (MAX_PASOS): "
                  "se finaliza con lo obtenido para evitar un supervisor infinito.",
        ), {}

    val = state["validacion"]
    if val is not None and not val.ok:
        agente = val.agente
        usadas = state["reparaciones"].get(agente, 0)
        if usadas >= config.max_reparaciones():
            return DecisionSupervisor(
                siguiente="FINALIZAR",
                razon=f"El {agente} no pasó la validación después de {usadas} reparaciones: se escala a "
                      "revisión humana en vez de seguir reintentando (evita el bucle infinito de corrección).",
            ), {"escalado_humano": True}

        original = _instruccion_original(state, agente)
        if val.fallo_de_ejecucion:
            instruccion = prompts.REINTENTO_POR_FALLO.format(error=" | ".join(val.errores), instruccion_original=original)
            razon = f"El {agente} falló al ejecutarse: se reintenta ({usadas + 1}/{config.max_reparaciones()})."
        else:
            instruccion = prompts.REPARACION.format(
                aporte_anterior=_aporte_anterior(state, agente),
                errores="\n".join(f"- {e}" for e in val.errores),
                instruccion_original=original,
            )
            razon = (f"Bucle de reparación ({usadas + 1}/{config.max_reparaciones()}): se reenvía al {agente} su "
                     "output con los errores específicos del validador, no un reintento ciego.")
        return DecisionSupervisor(siguiente=agente, instruccion=instruccion, razon=razon), {
            "reparaciones": {**state["reparaciones"], agente: usadas + 1}
        }

    return None, {}


def aplicar_guardas(decision: DecisionSupervisor, state: EstadoOrquestador) -> tuple[DecisionSupervisor, bool]:
    """Corrige decisiones del LLM que violan el criterio de suficiencia. Devuelve (decisión, si se corrigió)."""
    if state["investigacion"] is None and decision.siguiente in ("analista", "FINALIZAR"):
        return DecisionSupervisor(
            siguiente="investigador",
            instruccion=f"Buscá todos los datos necesarios para resolver esta tarea: {state['tarea']}",
            razon=f"Guarda: el LLM eligió '{decision.siguiente}' sin ninguna investigación hecha; "
                  "primero hacen falta datos.",
        ), True
    if decision.siguiente != "FINALIZAR" and not decision.instruccion.strip():
        return decision.model_copy(update={"instruccion": state["tarea"]}), True
    return decision, False


# --- Construcción del grafo ---------------------------------------------------
def _veces_que_corrio(state: EstadoOrquestador, agente: str) -> int:
    return sum(1 for c in state["contribuciones"] if c.agente == agente)


def construir_grafo(supervisor, investigador, analista, sintetizador) -> StateGraph:
    """Los cuatro componentes se inyectan: graph.py no sabe si son agentes reales o dobles de test."""

    async def nodo_supervisor(state: EstadoOrquestador, config_: RunnableConfig) -> dict:
        decision, extra = decidir_por_reglas(state)
        origen = "regla"
        if decision is None:
            decision = await supervisor.decidir(state)
            decision, corregida = aplicar_guardas(decision, state)
            origen = "regla" if corregida else "llm"

        actualizacion = {
            "siguiente": decision.siguiente,
            "instruccion": decision.instruccion,
            "decisiones": [DecisionRegistrada(**decision.model_dump(), paso=state["pasos"], origen=origen)],
            **extra,
        }
        if decision.siguiente != "FINALIZAR":
            actualizacion["pasos"] = state["pasos"] + 1
        return actualizacion

    async def nodo_investigador(state: EstadoOrquestador, config_: RunnableConfig) -> dict:
        inicio, paso = time.perf_counter(), state["pasos"]
        simulacion = config_.get("configurable", {}).get("simulacion", {})
        try:
            if _veces_que_corrio(state, "investigador") < simulacion.get("fallar_investigador", 0):
                raise RuntimeError("Fallo simulado del investigador (prueba del camino de error)")
            aporte, evidencia = await investigador.ejecutar(state["instruccion"])
        except Exception as e:
            if es_error_transitorio(e):
                raise  # lo reintenta la RetryPolicy del nodo
            return {"contribuciones": [Contribucion(
                paso=paso, agente="investigador", ok=False, resumen="(falló)",
                error=f"{type(e).__name__}: {e}", duracion_s=time.perf_counter() - inicio)]}
        return {
            "investigacion": aporte,
            "evidencia": evidencia,
            "contribuciones": [Contribucion(paso=paso, agente="investigador", ok=True, resumen=aporte.resumen,
                                            duracion_s=time.perf_counter() - inicio)],
            "messages": [AIMessage(aporte.resumen, name="investigador")],
        }

    async def nodo_analista(state: EstadoOrquestador, config_: RunnableConfig) -> dict:
        inicio, paso = time.perf_counter(), state["pasos"]
        simulacion = config_.get("configurable", {}).get("simulacion", {})
        try:
            aporte = await analista.ejecutar(state["instruccion"], state["investigacion"])
        except Exception as e:
            if es_error_transitorio(e):
                raise
            return {"contribuciones": [Contribucion(
                paso=paso, agente="analista", ok=False, resumen="(falló)",
                error=f"{type(e).__name__}: {e}", duracion_s=time.perf_counter() - inicio)]}
        resumen = aporte.conclusion
        if _veces_que_corrio(state, "analista") < simulacion.get("corromper_analisis", 0):
            # Simula un error aritmético del LLM ("suena bien pero está mal") para probar la puerta de calidad.
            # Se arma un objeto NUEVO: mutar el aporte en el lugar es estado mutable compartido (si
            # alguien más tiene una referencia, también lo vería alterado).
            primero = aporte.calculos[0].model_copy(update={"resultado": aporte.calculos[0].resultado + 10})
            aporte = aporte.model_copy(update={"calculos": [primero, *aporte.calculos[1:]]})
            resumen += " [SIMULACIÓN: resultado alterado a propósito para probar el validador]"
        return {
            "analisis": aporte,
            "contribuciones": [Contribucion(paso=paso, agente="analista", ok=True, resumen=resumen,
                                            duracion_s=time.perf_counter() - inicio)],
            "messages": [AIMessage(aporte.conclusion, name="analista")],
        }

    async def nodo_validador(state: EstadoOrquestador, config_: RunnableConfig) -> dict:
        agente = state["siguiente"]
        ultima = next((c for c in reversed(state["contribuciones"]) if c.agente == agente), None)
        if ultima is None or not ultima.ok:
            resultado = ResultadoValidacion(
                agente=agente, ok=False, fallo_de_ejecucion=True,
                errores=[ultima.error if ultima else "el especialista no produjo ningún resultado"],
            )
        elif agente == "investigador":
            resultado = validar_investigacion(state["investigacion"], state["evidencia"])
        else:
            resultado = validar_analisis(state["analisis"], state["investigacion"])
        return {"validacion": resultado, "validaciones": [resultado]}

    async def nodo_sintesis(state: EstadoOrquestador, config_: RunnableConfig) -> dict:
        respuesta = await sintetizador.redactar(state)
        return {"respuesta_final": respuesta, "messages": [AIMessage(respuesta, name="sintesis")]}

    def enrutar(state: EstadoOrquestador) -> Literal["investigador", "analista", "sintesis"]:
        return "sintesis" if state["siguiente"] == "FINALIZAR" else state["siguiente"]

    grafo = StateGraph(EstadoOrquestador)
    grafo.add_node("supervisor", con_parcela("supervisor", nodo_supervisor), retry_policy=REINTENTO_LLM)
    grafo.add_node("investigador", con_parcela("investigador", nodo_investigador), retry_policy=REINTENTO_LLM)
    grafo.add_node("analista", con_parcela("analista", nodo_analista), retry_policy=REINTENTO_LLM)
    grafo.add_node("validador", con_parcela("validador", nodo_validador))
    grafo.add_node("sintesis", con_parcela("sintesis", nodo_sintesis), retry_policy=REINTENTO_LLM)

    grafo.add_edge(START, "supervisor")
    grafo.add_conditional_edges("supervisor", enrutar, ["investigador", "analista", "sintesis"])
    grafo.add_edge("investigador", "validador")
    grafo.add_edge("analista", "validador")
    grafo.add_edge("validador", "supervisor")
    grafo.add_edge("sintesis", END)
    return grafo


def crear_orquestador() -> CompiledStateGraph:
    """Orquestador con los agentes reales, usando el LLM configurado en el .env."""
    from agents.analyst_agent import AgenteAnalista
    from agents.research_agent import AgenteInvestigador
    from agents.supervisor import Supervisor
    from agents.synthesis_agent import Sintetizador

    llm = get_llm()
    return construir_grafo(Supervisor(llm), AgenteInvestigador(llm), AgenteAnalista(llm), Sintetizador(llm)).compile()


def config_de_ejecucion(simulacion: dict | None = None) -> RunnableConfig:
    """recursion_limit (techo del grafo) + simulación opcional de fallos para probar el camino de error."""
    return {"recursion_limit": config.recursion_limit(), "configurable": {"simulacion": simulacion or {}}}

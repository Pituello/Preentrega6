"""
Síntesis final: redacta la respuesta al usuario con lo validado. Solo se
ejecuta cuando el supervisor decide FINALIZAR (o cuando un límite lo fuerza).
"""
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

import prompts
from agents.supervisor import resumir_estado


class Sintetizador:
    def __init__(self, llm: BaseChatModel):
        self._llm = llm

    async def redactar(self, estado: dict) -> str:
        extra = []
        if estado.get("escalado_humano"):
            extra.append("ATENCIÓN: el proceso se escaló a REVISIÓN HUMANA porque un resultado no pasó la "
                         "validación después de los reintentos permitidos.")
        val = estado.get("validacion")
        if val and not val.ok:
            extra.append("La última validación FALLÓ: " + " | ".join(val.errores))
        mensaje = resumir_estado(estado) + ("\n\n" + "\n".join(extra) if extra else "")
        respuesta = await self._llm.ainvoke([SystemMessage(prompts.SINTESIS), HumanMessage(mensaje)])
        contenido = respuesta.content
        if isinstance(contenido, list):  # Gemini puede devolver bloques en vez de un string
            contenido = "".join(b.get("text", "") for b in contenido if isinstance(b, dict))
        return str(contenido).strip()

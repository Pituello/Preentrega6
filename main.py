"""
Orquestador multi-agente: consulta interactiva o de una sola pregunta.
Muestra, paso a paso, quién decidió qué y por qué.

Uso:
    python main.py                                   # modo interactivo
    python main.py "¿Cuánta más potencia tiene el Virtus Exclusive que el Tera High?"
"""
import asyncio
import sys

from errores import ERRORES_DEL_PROVEEDOR, avisar_error_del_llm
from graph import crear_orquestador
from llm import describir_llm
from traza import correr_tarea

# En Windows, al redirigir la salida a un archivo (python demo.py > salida.txt),
# Python escribe en cp1252, que no tiene los emojis de la traza: forzamos UTF-8.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


async def main(pregunta: str | None) -> None:
    app = crear_orquestador()
    print(f"🚗 Orquestador de análisis técnico Volkswagen — LLM: {describir_llm()}\n")
    if pregunta:
        await correr_tarea(app, pregunta)
        return
    print("Escribí 'salir' para terminar.\n")
    while True:
        pregunta = input("🧑 Vos: ").strip()
        if pregunta.lower() in ("salir", "exit", "quit", ""):
            break
        await correr_tarea(app, pregunta)
        print("-" * 90)


if __name__ == "__main__":
    try:
        asyncio.run(main(" ".join(sys.argv[1:]) or None))
    except ERRORES_DEL_PROVEEDOR as error:
        avisar_error_del_llm(error)

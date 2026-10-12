"""
Configuración central, leída del .env.

Se carga el .env apenas se importa el módulo, y los valores ajustables se leen
con funciones al momento de usarlos (no como constantes al importar): la
corrección de TEMPERATURE que marcó la devolución de la Pre-Entrega 3.
"""
import os

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.getenv("DATA_DIR", "data")
TRAZAS_DIR = os.getenv("TRAZAS_DIR", "trazas")


def _entero(nombre: str, default: int, minimo: int = 1) -> int:
    valor = os.getenv(nombre, str(default))
    try:
        numero = int(valor)
    except ValueError:
        raise ValueError(f"{nombre}='{valor}' en el .env no es un número entero")
    if numero < minimo:
        raise ValueError(f"{nombre}={numero} tiene que ser >= {minimo}")
    return numero


def max_pasos() -> int:
    """
    Error a evitar "Supervisor Infinito": cantidad máxima de intervenciones de
    especialistas por tarea. Al llegar, el supervisor finaliza con lo que haya.
    """
    return _entero("MAX_PASOS", 8)


def max_reparaciones() -> int:
    """
    Error a evitar "Bucle Infinito de Corrección": cuántas veces se le devuelve
    a un mismo especialista un output que no pasó la validación. Superado ese
    límite, se escala a revisión humana en vez de seguir reintentando.
    """
    return _entero("MAX_REPARACIONES", 2, minimo=0)


def recursion_limit() -> int:
    """Techo de LangGraph (cada vuelta especialista -> validador -> supervisor usa 3 pasos)."""
    return _entero("RECURSION_LIMIT", 40, minimo=5)


def backend_busqueda() -> str:
    """'local' (BM25 sobre /data, default) o 'pinecone' (índice de la Pre-Entrega 4)."""
    backend = os.getenv("BUSQUEDA_BACKEND", "local").strip().lower()
    if backend not in ("local", "pinecone"):
        raise ValueError(f"BUSQUEDA_BACKEND='{backend}' no válido: usá 'local' o 'pinecone'")
    return backend

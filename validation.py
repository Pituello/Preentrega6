"""
Validador determinístico: la "puerta de calidad" entre los especialistas y el
supervisor. No le pregunta a un
LLM si el resultado "parece correcto" (un modelo puede ser muy persuasivo y
estar equivocado): lo verifica con código.

Investigación (provenance):
- cada dato tiene que citar una fuente que las herramientas realmente
  devolvieron en esa investigación;
- y su valor tiene que aparecer en el texto de esa fuente. Así se detecta un
  número alucinado (un "31 de noviembre"), aunque venga con fuente;
- y tiene que traer al menos un dato. Una investigación vacía pasaba la
  validación "sin errores" y el supervisor la reenviaba sin fin (visto en la
  corrida real): ahora entra al bucle de reparación y, si no hay forma de
  obtener el dato, termina escalada a revisión humana.

Análisis:
- cada cálculo se recalcula con la calculadora segura, y el resultado
  declarado tiene que coincidir;
- cada número de la expresión tiene que provenir de la investigación
  validada o del resultado de un cálculo anterior de la misma respuesta
  (encadenar cuentas es válido: ese resultado ya fue verificado). El
  analista no puede "traer" datos propios.

Los errores salen redactados para devolvérselos al agente en el bucle de
reparación: un error específico, no un "intentá de nuevo".
"""
from tools.calculadora import evaluar_expresion, numeros_de_expresion
from tools.evidencia import coincide, evidencia_por_fuente, extraer_numeros

from state import AporteAnalisis, AporteInvestigacion, ResultadoValidacion

# Números que el analista puede usar sin que vengan de la investigación:
# el 100 de los porcentajes y el 2 de un promedio entre dos valores.
CONSTANTES_PERMITIDAS = {1.0, 2.0, 100.0}
TOLERANCIA_RELATIVA = 1e-3


def validar_investigacion(aporte: AporteInvestigacion, evidencia: list[str]) -> ResultadoValidacion:
    por_fuente = evidencia_por_fuente(evidencia)
    if not aporte.datos:
        return ResultadoValidacion(agente="investigador", ok=False, errores=[
            "No devolviste ningún dato. Si una herramienta respondió con ERROR, leé el mensaje y "
            "volvé a intentar con lo que indica (por ejemplo, con el nombre del modelo solo, sin la "
            "versión). Si el dato realmente no existe en ninguna fuente, decilo en el resumen."
        ])
    errores = []
    for dato in aporte.datos:
        if dato.fuente not in por_fuente:
            disponibles = ", ".join(sorted(por_fuente)) or "ninguna"
            errores.append(
                f"El dato '{dato.descripcion}' cita la fuente '{dato.fuente}', pero las herramientas no "
                f"devolvieron esa fuente en esta investigación (fuentes devueltas: {disponibles})."
            )
        elif not coincide(dato.valor, extraer_numeros(por_fuente[dato.fuente])):
            errores.append(
                f"El valor {dato.valor:g} del dato '{dato.descripcion}' no aparece en el texto que devolvió "
                f"la fuente '{dato.fuente}'. Copiá el número exactamente como figura en la fuente."
            )
    return ResultadoValidacion(agente="investigador", ok=not errores, errores=errores)


def validar_analisis(aporte: AporteAnalisis, investigacion: AporteInvestigacion | None) -> ResultadoValidacion:
    if investigacion is None or not investigacion.datos:
        return ResultadoValidacion(
            agente="analista", ok=False,
            errores=["No hay datos de investigación validados: el análisis no tiene de dónde partir."],
        )
    datos = {d.valor for d in investigacion.datos} | CONSTANTES_PERMITIDAS
    errores = []
    anteriores: list[float] = []  # resultados ya verificados de esta misma respuesta
    for calculo in aporte.calculos:
        try:
            recalculado = evaluar_expresion(calculo.expresion)
            numeros = numeros_de_expresion(calculo.expresion)
        except (ValueError, SyntaxError) as e:
            errores.append(f"La expresión '{calculo.expresion}' de '{calculo.descripcion}' no es válida: {e}.")
            continue
        resultado_ok = abs(recalculado - calculo.resultado) <= TOLERANCIA_RELATIVA * max(1.0, abs(recalculado))
        if not resultado_ok:
            errores.append(
                f"En '{calculo.descripcion}', el resultado declarado ({calculo.resultado:g}) no coincide con el "
                f"recálculo de '{calculo.expresion}' ({recalculado:g}). Usá la calculadora y copiá su resultado."
            )
        permitidos = datos | set(anteriores)
        ajenos = [n for n in numeros if not coincide(n, permitidos)]
        if resultado_ok and not ajenos:
            anteriores.append(calculo.resultado)
        if ajenos:
            errores.append(
                f"En '{calculo.descripcion}', los números {', '.join(f'{n:g}' for n in ajenos)} no provienen de "
                f"la investigación. Usá solo estos datos: "
                + "; ".join(f"{d.descripcion} = {d.valor:g} {d.unidad}" for d in investigacion.datos)
                + " (o el resultado de un cálculo anterior correcto)."
            )
    return ResultadoValidacion(agente="analista", ok=not errores, errores=errores)

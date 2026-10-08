"""
normalizacion_interes.py
------------------------
Interpreta la columna COMENTARIOS del Excel de licitaciones INFORMADAS a las
empresas (pestaña «Estadísticas de interés»). Hace el mismo papel que
`normalizacion_resultados.py` para RESULTADO, y devuelve lo mismo
(categoria, regla), así que el resto de la pestaña no distingue de dónde viene.

DEFINICIÓN DE ÉXITO
-------------------
Éxito (POSITIVO) = se le ha informado a una empresa de la licitación Y esta ha
mostrado interés y confirmado que encaja con su perfil.
Fracaso (NEGATIVO) = la empresa contestó que no le interesa o que no encaja
(fuera de su actividad, no cumple los requisitos, sin capacidad, descartada...).
Se IGNORA lo que no permite saber si hubo interés o encaje: sin comentario
(todavía sin respuesta registrada), «Respuesta de cortesía», «Al final no se
presentaron» a secas, «No se va a presentar» sin motivo...

CÓMO DECIDE (por este orden; cada redacción lleva anotada la regla aplicada)
-----------------------------------------------------------------------------
1. Sin comentario                                          -> IGNORADO
2. Se presentó, o se va a presentar, o formó consorcio      -> POSITIVO
   (presentarse supone interés y encaje; «no se presentaron» NO cuenta
   como presentación, ni «descartó presentarse»)
3. Dice que no encaja / no cumple / no le interesa /        -> NEGATIVO
   no tiene capacidad / lo descarta... (aunque también muestre algo de
   interés: si no encaja, el encaje no está confirmado)
4. Muestra interés (le interesa, la van a analizar,         -> POSITIVO
   le parece interesante...)
5. Cualquier otra cosa                                      -> IGNORADO

El punto 2 va antes que el 3 porque quien al final se presenta ha confirmado
el encaje aunque antes dudara («En principio no les interesa, pero la van a
analizar... Al final sí se presentaron»).

Para ajustar el criterio de una redacción concreta basta con tocar las listas
REGLAS_* de más abajo. Módulo sin dependencias de Streamlit ni de Supabase.
"""
import re

from normalizacion_resultados import IGNORADO, NEGATIVO, POSITIVO, limpiar_texto

__all__ = ["clasificar_comentario", "POSITIVO", "NEGATIVO", "IGNORADO"]

# Quita primero las negaciones de «presentarse» («no se presentaron», «no podrían presentarse»,
# «decidieron no presentarse», «descartado presentarnos»), para no confundirlas con presentarse.
_PRESENTACION_NEGADA = re.compile(
    r"\bno\s+(?:(?:se|van|va|a|han|ha|hemos|pudo|pudieron|podrian|podria|pudimos|presentaria|presentarian|sienten|preparados|para)\s+)*presen\w*"
    r"|\bdescart\w*\s+presen\w*"
)
# Formas de «presentar» (no vale `presen\w*`: casaría con «presencialidad» o «presentación»).
_PRESENTACION = re.compile(
    r"\bpresent(?:o|e|en|an|aron|ado|ados|ada|adas|ar|arse|arnos|arme|aran|arian|aria|ara)\b"
    r"|\bformaron consorcio\b|\bprimer lugar\b"
)

# Quita las negaciones de «interés» («no les interesa», «no mostraron interés») para no contarlas como interés.
_INTERES_NEGADO = re.compile(r"\b(?:no|sin)\s+(?:\w+\s+){0,2}?interes\w*")

REGLAS_NEGATIVAS = [
    (r"\bno\s+(?:\w+\s+){0,2}?interes\w*", "no le interesa"),
    (r"\bno\s+(?:les\s+|le\s+|nos\s+)?(?:encaj\w*|parece encajar)\b|\bno\s+se\s+ajust\w*", "no encaja"),
    (r"\bno\s+es\s+(?:de\s+)?su\s+(?:interes|linea|core|ambito|perfil|especialidad)\b|\bno\s+esta\s+en\s+su\s+core\b"
     r"|\bel\s+area\s+no\s+es\b", "no es de su ámbito"),
    (r"\bse\s+sale\s+de\b|\bse\s+aleja\b|\bfuera\s+de\s+nuestro\s+alcance\b", "fuera de su actividad"),
    (r"\bno\s+cumpl\w*", "no cumple los requisitos"),
    (r"\bno\s+(?:tienen|tiene|cuentan|cuenta|contamos|tenemos)\s+(?:actualmente\s+)?(?:con\s+)?"
     r"(?:la\s+|el\s+|las\s+|los\s+|suficiente\s+)?(?:experiencia|referencias|capacidad|habilidades|perfil|recursos|organizacion)\b",
     "sin experiencia / capacidad / perfil"),
    (r"\bno\s+(?:estamos|estan)\s+(?:lo\s+)?suficientemente\b|\bno\s+se\s+sienten\s+preparad\w*", "no está preparada"),
    (r"\bno\s+se\s+pueden\s+comprometer\b|\bno\s+llegan\s+a\b|\bno\s+les\s+gusta\b|\bmala\s+experiencia\b|\bprefieren\b",
     "descarta por otros motivos"),
    (r"\bdescart\w*|\bdecid\w*\s+no\b|\bpueden\s+aportar\s+muy\s+poco\b", "descartada"),
    (r"\bal\s+final\s+no\b(?!\s+se\b)", "decisión final negativa"),
]

REGLAS_INTERES = [
    (r"\binteres\w*", "muestra interés"),
    (r"\bvan\s+a\s+(?:analizar|estudiar|valorar|revisar|evaluar)\b", "la van a analizar / valorar"),
    (r"\bvan\s+a\s+tener\s+en\s+cuenta\b|\bpara\s+que\s+la\s+eval\w*|\bponer\s+una\s+alerta\b|\bpreguntaron\b",
     "pide información / la evalúa"),
]


def _primera_regla(texto: str, reglas: list):
    for patron, descripcion in reglas:
        if re.search(patron, texto):
            return descripcion
    return None


def clasificar_comentario(texto) -> tuple:
    """
    Devuelve (categoria, regla):
      categoria in {POSITIVO, NEGATIVO, IGNORADO}
      regla     = por qué (texto corto, para auditar la clasificación)
    """
    limpio = limpiar_texto(texto)
    if not limpio:
        return IGNORADO, "sin comentario (aún sin respuesta registrada)"

    sin_negaciones = _INTERES_NEGADO.sub(" ", _PRESENTACION_NEGADA.sub(" ", limpio))

    if _PRESENTACION.search(sin_negaciones):
        return POSITIVO, "se presenta / forma consorcio"

    regla = _primera_regla(limpio, REGLAS_NEGATIVAS)
    if regla:
        return NEGATIVO, regla

    regla = _primera_regla(sin_negaciones, REGLAS_INTERES)
    if regla:
        return POSITIVO, regla

    return IGNORADO, "sin señal clara de interés ni de rechazo"

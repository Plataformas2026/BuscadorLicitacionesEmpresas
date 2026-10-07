"""
normalizacion_resultados.py
---------------------------
Interpreta los dos campos de texto libre de "REFERENCIAS P BÚSQUEDAS" que
necesita la pestaña de Estadísticas:

- `RESULTADO`: lo escribe cada usuario a mano ("ADJUDICADA", "ganada en UTE",
  "no conseguido", "Ejecutada / en curso", "Con Teyde. NO ADJUDICADA",
  "OFERTA RECHAZADA. Jefe de grupo: ...", "SIN INFO. Socios: ...", ...).
  `clasificar_resultado` lo agrupa en POSITIVO / NEGATIVO / IGNORADO.
- `FECHA`: llega como "2019", "2020-08-01", "2011-2012", "Abril de 2026",
  "02-04/21", "12/2020 - 10/2021", "2019-ACTUALIDAD"... `extraer_anio` saca el año.

Módulo SIN dependencias de Streamlit ni de Supabase: son funciones puras,
fáciles de probar y de ajustar. Para cambiar cómo se interpreta una
redacción concreta basta con tocar las listas REGLAS_* de más abajo.

CRITERIOS (explícitos porque el texto es libre)
------------------------------------------------
- Una redacción solo cuenta si su significado es CLARO. Lo que no lo es se
  IGNORA (no se cuenta ni como éxito ni como fracaso): vacíos, "-", "?",
  "sin información", pendientes ("en evaluación", "presentada", "lista
  corta"), procesos que no llegaron a resolverse ("cancelado", "desierta",
  "no presentada"), y textos que solo nombran socios o roles
  ("Socios: ...", "Leader", "Colaborador").
- Se mira PRIMERO si hay una negación ("no adjudicada", "no pasamos la lista
  corta", "rechazada"...) y DESPUÉS lo positivo, porque "no adjudicada"
  contiene la palabra "adjudicada".
- Cuentan como POSITIVO, además de "adjudicada/ganada/conseguida": lo
  "ejecutado / en ejecución / finalizado" (un proyecto solo se ejecuta si se
  adjudicó), "subvención concedida" y un "sí" suelto (la columna se titula
  ADJUDICADA/NO ADJUDICADA/SIN INFORMACIÓN, así que "sí" responde a
  "¿adjudicada?").
- Año de una FECHA: el PRIMER año que aparece ("2019-2020" -> 2019,
  "16 meses (2018-2020)" -> 2018): es el año en que arrancó la licitación o
  el contrato.
"""
import re
import unicodedata
from datetime import date

POSITIVO = "Positivo"
NEGATIVO = "Negativo"
IGNORADO = "Ignorado"

# Años que se aceptan al leer una FECHA (descarta basura tipo "6684263").
ANIO_MINIMO = 1990


def _anio_maximo() -> int:
    return date.today().year + 1


# ============================================================
# TEXTO
# ============================================================

def limpiar_texto(texto) -> str:
    """Minúsculas, sin tildes, sin signos y con espacios simples (\"Sí.\" -> \"si\")."""
    if texto is None:
        return ""
    texto = str(texto).casefold()
    texto = "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")
    texto = re.sub(r"[^a-z0-9ñ]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


# ============================================================
# RESULTADO
# ============================================================
# Cada regla es (expresión regular sobre el texto ya limpiado, descripción
# breve). Gana la PRIMERA que coincide, en este orden:
#   1) no concluyente (cancelado, no presentado...)  -> IGNORADO
#   2) negativo                                      -> NEGATIVO
#   3) positivo                                      -> POSITIVO
#   4) cualquier otra cosa                           -> IGNORADO

_NEGADOR = r"(?:no|nao|non|not)"

REGLAS_NO_CONCLUYENTES = [
    # «no presentada», «no se presentó»: justo detrás del «no» (si no, «No adjudicada. Presentada» se ignoraría)
    (r"\b" + _NEGADOR + r"\s+(?:se\s+)?presen\w*", "no se presentó"),
    (r"\bsin\s+presentar\b", "no se presentó"),
    (r"\bcancel\w*", "proceso cancelado"),
    (r"\bdesiert[oa]s?\b", "proceso desierto"),
    (r"\banulad[oa]s?\b", "proceso anulado"),
    (r"\b" + _NEGADOR + r"\s+(?:se\s+)?ejecut\w*", "no ejecutado (no concluyente)"),
]

REGLAS_NEGATIVAS = [
    (r"\b" + _NEGADOR + r"\s+(?:se\s+)?(?:ha\s+|fue\s+|han\s+|fueron\s+)?(?:sido\s+)?"
     r"(?:adjud\w*|ganad\w*|consig\w*|conseg\w*|seleccion\w*|conced\w*|resultamos)",
     "negación de adjudicada / ganada / conseguida / seleccionada"),
    (r"\b" + _NEGADOR + r"\s+pasa\w*", "no pasamos (preselección)"),
    (r"\b" + _NEGADOR + r"\s+lista\s+(?:corta|restringida)\b", "no entra en lista corta"),
    (r"\b(?:rechazad\w*|denegad\w*|eliminad\w*|descartad\w*|desestimad\w*|perdid\w*|perdimos)\b",
     "rechazada / denegada / eliminada / descartada"),
    (r"\b(?:non|pas)\s+(?:attribu\w*|retenu\w*|selectionn\w*)", "no atribuido / retenido (fr)"),
    (r"\b(?:lost|rejected)\b|\bnot\s+(?:awarded|selected|won|successful)\b", "lost / not awarded (en)"),
]

REGLAS_POSITIVAS = [
    (r"\badj\w{0,4}dicad\w*", "adjudicada"),   # admite la errata «adjuidicada»
    (r"\bganad[oa]s?\b", "ganada"),            # «ganadería/ganadero» no encajan
    (r"\bganhou\b|\bvencedor\w*", "ganó (pt)"),
    (r"\bconseguid[oa]s?\b", "conseguida"),
    (r"\bconcedid[oa]s?\b", "concedida (subvención)"),
    (r"\bejecutad[oa]s?\b|\ben ejecucion\b", "ejecutada / en ejecución"),
    (r"\bfinalizad[oa]s?\b", "finalizada"),
    (r"\bawarded\b|\bwon\b|\bsuccessful\b", "awarded / won (en)"),
    (r"\battribu\w+|\bretenu\w*", "attribué / retenu (fr)"),
    (r"^(?:si|yes|oui)$", "«sí» como respuesta a «¿adjudicada?»"),
]

# Solo para explicar POR QUÉ se ignora algo que no encaja en lo anterior.
_PENDIENTE = re.compile(
    r"\b(?:pendient\w*|evaluacion|proceso|esperando|presentad[oa]s?|preparacion|sin resolver|"
    r"lista corta|recibido|propuesta|resolucion|aun sin respuesta|en curso|preparacao)\b"
)
_SIN_INFORMACION = re.compile(r"^(?:sin info\w*|no aplica)\b|^(?:na|n a|n d|s i)$")


def _primera_regla(texto: str, reglas: list):
    for patron, descripcion in reglas:
        if re.search(patron, texto):
            return descripcion
    return None


def clasificar_resultado(texto) -> tuple:
    """
    Devuelve (categoria, regla):
      categoria in {POSITIVO, NEGATIVO, IGNORADO}
      regla     = por qué (texto corto, para auditar la clasificación)
    """
    limpio = limpiar_texto(texto)

    if not limpio:
        return IGNORADO, "vacío"

    regla = _primera_regla(limpio, REGLAS_NO_CONCLUYENTES)
    if regla:
        return IGNORADO, regla

    regla = _primera_regla(limpio, REGLAS_NEGATIVAS)
    if regla:
        return NEGATIVO, regla

    regla = _primera_regla(limpio, REGLAS_POSITIVAS)
    if regla:
        return POSITIVO, regla

    if _SIN_INFORMACION.search(limpio) or len(limpio) <= 2:
        return IGNORADO, "sin información"
    if _PENDIENTE.search(limpio):
        return IGNORADO, "pendiente / en proceso"
    return IGNORADO, "texto sin resultado claro (socios, roles, otros)"


# ============================================================
# FECHA -> AÑO
# ============================================================

_ANIO_4_CIFRAS = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
# «05/20», «02-04/21», «12/20-02/21»: mes(es)/año con 2 cifras. Solo se usa si
# no hay ningún año de 4 cifras en el texto.
_MES_ANIO_2_CIFRAS = re.compile(r"(?<![\d/])\d{1,2}(?:-\d{1,2})?/(\d{2})(?![\d/])")


def _anio_valido(anio: int):
    return anio if ANIO_MINIMO <= anio <= _anio_maximo() else None


def extraer_anio(fecha):
    """Primer año que aparece en el texto de FECHA, o None si no hay ninguno fiable."""
    if fecha is None:
        return None
    if hasattr(fecha, "year"):  # date / datetime / Timestamp
        return _anio_valido(int(fecha.year))

    texto = str(fecha).strip()
    if not texto:
        return None

    coincidencia = _ANIO_4_CIFRAS.search(texto)
    if coincidencia:
        return _anio_valido(int(coincidencia.group(1)))

    coincidencia = _MES_ANIO_2_CIFRAS.search(texto)
    if coincidencia:
        aa = int(coincidencia.group(1))
        return _anio_valido(2000 + aa) or _anio_valido(1900 + aa)
    return None

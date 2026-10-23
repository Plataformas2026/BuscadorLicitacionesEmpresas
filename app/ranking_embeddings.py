"""
ranking_embeddings.py
---------------------
FASE 1 de «Coincidencia Inteligente»: UN ÚNICO ranking por embeddings (sustituye a los dos rankings anteriores,
«Palabras clave» y «Relevancia real»).

QUÉ COMPARA
-----------
La similitud coseno entre el embedding de la LICITACIÓN (título + descripción + categoría) y el de cada EMPRESA, calculados
con el mismo modelo de la app (`intfloat/multilingual-e5-small`, prefijo «passage: » en ambos lados, como en el resto del
proyecto). El texto de la empresa se construye EXCLUSIVAMENTE con estos cuatro campos del Excel:

    1. EMPRESA (nombre)                                   -> nombre_empresa
    2. SECTOR                                             -> sector
    3. palabras clave extraídas de la descripción         -> palabras_clave
    4. PRINCIPALES PROYECTOS TIPO                         -> proyectos_tipo

Ningún otro campo (geografía, tamaño, certificaciones, descripción de la actividad, facturación...) entra en el texto ni
en la puntuación. De las empresas ordenadas de mayor a menor similitud se conservan las `top` primeras (15).

QUÉ PALABRAS «PUNTUARON»
------------------------
Un embedding es una similitud global del texto, no una suma de palabras; por eso la explicación es una aproximación
honesta con dos listas calculadas aparte (no cambian la nota):
  - coincidencias literales: palabras (raíz común) que aparecen a la vez en la licitación y en cada uno de los cuatro campos;
  - frases clave más afines: las frases de «palabras clave» cuyo embedding individual se parece más al de la licitación.

Este módulo no usa Streamlit ni Supabase: recibe el codificador ya cargado.
"""
import re
import unicodedata

import numpy as np

from empresas_excel import a_linea, trocear

PREFIJO = "passage"
CAMPOS_EMBEDDING = ("nombre_empresa", "sector", "palabras_clave", "proyectos_tipo")
ETIQUETAS_CAMPO = {
    "nombre_empresa": "Empresa", "sector": "Sector", "palabras_clave": "Palabras clave", "proyectos_tipo": "Proyectos tipo",
}
MAX_CARACTERES_LICITACION = 3000
MAX_FRASES_POR_EMPRESA = 60

_VACIAS = set("""de la el los las un una unos unas y e o u en del al a para por con sin sobre entre hasta desde que se su sus
lo como mas pero es son ser este esta estos estas the and of for to in on with by an or is are be from that this as at
le les des du et ou pour dans sur par au aux une um uma os as dos das do da em no na nos nas servicios servicio
contrato contratacion proyecto proyectos empresa empresas""".split())


def _norm(texto) -> str:
    texto = unicodedata.normalize("NFD", str(texto or "").casefold())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


# ------------------------------------------------------------------
# Textos que se convierten en embedding
# ------------------------------------------------------------------

def texto_empresa(empresa: dict) -> str:
    """Texto de la empresa para el embedding: SOLO nombre, sector, palabras clave y proyectos tipo."""
    sector = re.sub(r"^\s*\d+\s*[.)]\s*", "", str(empresa.get("sector") or ""))  # quita la numeración «9. »
    partes = [
        ("Empresa", empresa.get("nombre_empresa")),
        ("Sector", sector),
        ("Palabras clave", a_linea(empresa.get("palabras_clave"), ", ")),
        ("Proyectos tipo", a_linea(empresa.get("proyectos_tipo"), "; ")),
    ]
    return "\n".join(f"{etiqueta}: {valor}" for etiqueta, valor in partes if valor and str(valor).strip())


def texto_licitacion(licitacion: dict) -> str:
    """Texto de la licitación: título, descripción y categoría (el país no se incluye: aquí solo cuenta lo técnico)."""
    descripcion = licitacion.get("descripcion") or licitacion.get("descripcion_manual") or ""
    partes = [f"Titulo: {licitacion.get('titulo') or ''}. {descripcion}".strip()]
    if licitacion.get("categoria"):
        partes.append(f"Categoria: {licitacion['categoria']}")
    return " ".join("\n".join(partes).split())[:MAX_CARACTERES_LICITACION]


def _unitarios(matriz) -> np.ndarray:
    matriz = np.atleast_2d(np.array(matriz, dtype=float))
    normas = np.linalg.norm(matriz, axis=1, keepdims=True)
    normas[normas == 0] = 1.0
    return matriz / normas


def vectorizar(encoder, textos: list) -> np.ndarray:
    """Embeddings «passage: texto» (unitarios) de una lista de textos en una sola llamada al modelo."""
    if not textos:
        return np.zeros((0, 0))
    return _unitarios(encoder.encode([f"{PREFIJO}: {t}" for t in textos]))


# ------------------------------------------------------------------
# Índice de empresas y ranking
# ------------------------------------------------------------------

def construir_indice(encoder, empresas: list) -> dict:
    """Embedding de las empresas (UNA vez; lo cachea quien llama). {'ids', 'textos', 'matriz'}."""
    textos = [texto_empresa(e) for e in empresas]
    return {"ids": [e["numero_interno"] for e in empresas], "textos": textos, "matriz": vectorizar(encoder, textos)}


def ranking(encoder, indice: dict, licitacion: dict, top: int = 15) -> tuple:
    """
    ([{'posicion', 'numero_interno', 'similitud', 'puntuacion'}...] de las `top` empresas más parecidas, vector de la licitación).
    `puntuacion` = similitud coseno × 100 (un solo decimal). Empate -> orden del Excel.
    """
    consulta = vectorizar(encoder, [texto_licitacion(licitacion)])[0]
    similitudes = indice["matriz"] @ consulta
    orden = sorted(range(len(similitudes)), key=lambda i: (-similitudes[i], i))[:top]
    filas = [{
        "posicion": n, "numero_interno": indice["ids"][i], "similitud": float(similitudes[i]),
        "puntuacion": round(float(similitudes[i]) * 100, 1),
    } for n, i in enumerate(orden, start=1)]
    return filas, consulta


# ------------------------------------------------------------------
# Qué palabras puntuaron
# ------------------------------------------------------------------

def _stemmer(texto_licitacion_norm: str):
    try:
        import snowballstemmer
    except Exception:  # noqa: BLE001 - sin snowball se comparan las palabras tal cual (menos coincidencias, nunca falsas)
        return lambda p: p
    ingles = len(re.findall(r"\b(?:the|and|of|for|with)\b", texto_licitacion_norm))
    espanol = len(re.findall(r"\b(?:de|la|el|y|para|con)\b", texto_licitacion_norm))
    stem = snowballstemmer.stemmer("english" if ingles > espanol else "spanish").stemWord
    return stem


def _palabras(texto, stem) -> dict:
    """{raíz: palabra tal cual aparece} de las palabras significativas (≥4 letras, no vacías) del texto."""
    resultado = {}
    for original in re.findall(r"[^\W_]{4,}", str(texto or "").casefold()):
        palabra = _norm(original)
        if palabra not in _VACIAS and len(palabra) >= 4:
            resultado.setdefault(stem(palabra), original)  # se muestra la palabra con sus tildes
    return resultado


_CACHE_FRASES = {}


def explicar(encoder, empresa: dict, licitacion: dict, vector_licitacion, max_frases: int = 5) -> dict:
    """
    Qué palabras de la empresa se parecen a la licitación (aproximación; no cambia la puntuación):
      'literales': {campo: [palabras comunes]} para los cuatro campos del ranking,
      'frases': [(frase de palabras clave, similitud 0-100)] las más afines semánticamente,
      'frases_literales': [frases de palabras clave cuyas palabras significativas aparecen todas en la licitación].
    """
    lic_norm = _norm(texto_licitacion(licitacion))
    stem = _stemmer(lic_norm)
    raices_lic = _palabras(texto_licitacion(licitacion), stem)
    literales = {}
    for campo in CAMPOS_EMBEDDING:
        comunes = [palabra for raiz, palabra in _palabras(empresa.get(campo), stem).items() if raiz in raices_lic]
        if comunes:
            literales[campo] = comunes[:10]
    frases = trocear(empresa.get("palabras_clave"), MAX_FRASES_POR_EMPRESA)
    literales_frase = []
    for frase in frases:
        raices = _palabras(frase, stem)
        if raices and all(r in raices_lic for r in raices):
            literales_frase.append(frase)
    pendientes = [f for f in frases if _norm(f) not in _CACHE_FRASES]
    if pendientes:
        if len(_CACHE_FRASES) > 20000:
            _CACHE_FRASES.clear()
        for f, v in zip(pendientes, vectorizar(encoder, pendientes)):
            _CACHE_FRASES[_norm(f)] = v
    consulta = np.asarray(vector_licitacion, dtype=float)
    puntuadas = sorted(((f, float(_CACHE_FRASES[_norm(f)] @ consulta) * 100) for f in frases), key=lambda x: -x[1])
    return {
        "literales": literales,
        "frases": [(f, round(s, 1)) for f, s in puntuadas[:max_frases]],
        "frases_literales": literales_frase[:max_frases],
    }

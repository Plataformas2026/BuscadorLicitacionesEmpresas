"""
matching.py
-----------
Lógica de la Pestaña 2 (Coincidencia Inteligente: Licitación -> Empresas).

Flujo:
  1. `localizar_licitacion()` -- el usuario escribe un título, un enlace o
     un identificador; se intenta primero un match exacto (enlace o
     codigo_unico) y, si no lo hay, una búsqueda semántica que devuelve
     varios candidatos para que el usuario confirme cuál es.
  2. `obtener_coincidencias()` -- con la licitación ya confirmada (existe
     en nuestra tabla), se llama a la función RPC
     `buscar_empresas_para_licitacion`, que resuelve TODO en SQL (nunca
     hace falta mover un vector de 384 posiciones a través de Python).
  3. Si la licitación NO está en nuestra tabla (el usuario la pega/escribe
     directamente), `obtener_coincidencias_texto_libre()` calcula el
     embedding al vuelo y usa `buscar_empresas_por_embedding`.
  4. `obtener_referencias_por_empresas()` -- trae en UNA sola consulta
     (nunca N+1) los títulos de licitaciones antiguas de todas las
     empresas candidatas, desde `empresas_referencias`.
  5. `explicar_coincidencia()` -- construye la explicación de cada
     coincidencia SIEMPRE con evidencia concreta, nunca "no hay datos",
     en tres capas (nada de LLM ni API externa, para mantener la app
     dentro de la arquitectura gratuita):
       Capa 1 (literal multilingüe): proyectos tipo, descripción de
       actividad, preferencias de licitación, país/zona, palabras clave
       -- estas últimas ahora en los 4 idiomas que ya tiene cada empresa
       (antes solo se comparaban las españolas contra licitaciones que a
       menudo están en inglés o francés), y licitaciones antiguas del
       Excel de referencias.
       Capa 2 (puente temático multilingüe): cuando ninguna palabra
       literal coincide -- típico cuando la licitación está en otro
       idioma o usa sinónimos -- se comprueba si ambos textos comparten
       algún tema del diccionario `TEMAS_MULTILINGUE` (agua y
       saneamiento, energía, TIC, medio ambiente...), construido sobre
       los 6 sectores reales del Excel más los temas transversales más
       habituales en licitaciones de cooperación internacional.
       Capa 3 (último recurso -- similitud semántica por campo): si
       tampoco hay puente temático, se compara el embedding de la
       licitación contra el de cada campo del perfil de la empresa por
       separado (mismo modelo `intfloat/multilingual-e5-small` ya
       cargado, sin llamada externa) para señalar qué aspecto concreto
       del perfil está generando la afinidad, en vez de dejar la
       recomendación sin ninguna explicación.
  6. `obtener_lugares_empresa()` -- los 4 campos geográficos de la
     empresa (los mismos del Directorio de Empresas) en texto plano,
     sin tabla.
"""
import re
import unicodedata

import numpy as np
import pandas as pd
import streamlit as st
from sentence_transformers import SentenceTransformer
from supabase import Client

from search import buscar_semantica

PATRON_URL = re.compile(r"^https?://", re.IGNORECASE)

# Palabras demasiado comunes en español/francés/inglés como para aportar
# señal en el solapamiento de texto libre (ver _hay_solapamiento). NO se
# aplica al diccionario temático (_temas_presentes), que usa un
# vocabulario propio y controlado.
PALABRAS_VACIAS = {
    "de", "la", "el", "los", "las", "en", "y", "a", "del", "para", "con", "por",
    "un", "una", "unos", "unas", "que", "se", "su", "sus", "al", "o", "the", "of",
    "and", "for", "to", "in", "on", "an", "des", "du", "les", "le", "au", "aux",
}

# Diccionario temático multilingüe: el puente semántico cuando las
# palabras exactas no coinciden por diferencia de idioma o de
# vocabulario (Capa 2 de explicar_coincidencia). Construido sobre los 6
# sectores reales del Excel de empresas (ver ingest/sync_empresas_drive.py)
# más los temas transversales más habituales en licitaciones de
# cooperación internacional (medio ambiente, género, salud...). No
# pretende ser exhaustivo -- es una heurística de texto, no un LLM --
# pero cubre el vocabulario más frecuente en 4 idiomas.
TEMAS_MULTILINGUE = {
    "agua_saneamiento": {
        "es": ["agua", "aguas", "saneamiento", "potable", "residuales", "alcantarillado",
               "depuracion", "abastecimiento", "hidrico", "hidrica", "riego", "pozos", "pozo"],
        "en": ["water", "sanitation", "sewage", "wastewater", "drainage", "irrigation",
               "sludge", "drinking", "borehole", "boreholes"],
        "fr": ["eau", "eaux", "assainissement", "egout", "egouts", "irrigation", "potable", "forage"],
        "pt": ["agua", "aguas", "saneamento", "esgoto", "potavel"],
    },
    "energia": {
        "es": ["energia", "energias", "renovable", "renovables", "solar", "eolica",
               "fotovoltaica", "electrificacion", "electrica", "hidroelectrica"],
        "en": ["energy", "renewable", "renewables", "solar", "wind", "photovoltaic",
               "electrification", "power", "hydropower", "grid"],
        "fr": ["energie", "energies", "renouvelable", "renouvelables", "solaire",
               "eolienne", "electrification", "hydroelectrique"],
        "pt": ["energia", "renovavel", "renovaveis", "solar", "eolica", "eletrificacao"],
    },
    "transporte_infraestructura": {
        "es": ["transporte", "carretera", "carreteras", "puerto", "puertos", "aeropuerto",
               "infraestructura", "infraestructuras", "vial", "ferrocarril", "puente", "puentes"],
        "en": ["transport", "transportation", "road", "roads", "port", "ports", "airport",
               "infrastructure", "railway", "bridge", "bridges", "highway"],
        "fr": ["transport", "route", "routes", "port", "ports", "aeroport", "infrastructure",
               "infrastructures", "chemin de fer", "pont", "ponts"],
        "pt": ["transporte", "estrada", "estradas", "porto", "portos", "aeroporto",
               "infraestrutura", "infraestruturas", "ponte", "pontes"],
    },
    "tic_digitalizacion": {
        "es": ["tecnologia", "tecnologias", "digital", "software", "datos", "informatico",
               "digitalizacion", "conectividad", "informatica"],
        "en": ["technology", "digital", "software", "data", "ict", "connectivity",
               "digitalization", "digitalisation", "it"],
        "fr": ["technologie", "technologies", "numerique", "logiciel", "donnees", "connectivite"],
        "pt": ["tecnologia", "tecnologias", "digital", "software", "dados", "conectividade"],
    },
    "medio_ambiente_clima": {
        "es": ["ambiental", "ambientales", "clima", "climatico", "climatica", "biodiversidad",
               "sostenibilidad", "sostenible", "emisiones", "conservacion", "forestal", "residuos"],
        "en": ["environmental", "environment", "climate", "biodiversity", "sustainability",
               "sustainable", "emissions", "conservation", "forestry", "waste"],
        "fr": ["environnement", "environnemental", "climat", "biodiversite", "durabilite",
               "durable", "emissions", "conservation", "forestier", "dechets"],
        "pt": ["ambiental", "clima", "biodiversidade", "sustentabilidade", "sustentavel",
               "emissoes", "residuos"],
    },
    "consultoria_formacion": {
        "es": ["consultoria", "formacion", "capacitacion", "asesoramiento", "capacidades",
               "asistencia tecnica", "consultor", "consultores"],
        "en": ["consulting", "consultancy", "training", "capacity building", "advisory",
               "technical assistance", "capacity", "consultant", "consultants"],
        "fr": ["conseil", "formation", "renforcement des capacites", "assistance technique",
               "consultant", "consultants"],
        "pt": ["consultoria", "formacao", "capacitacao", "assistencia tecnica", "consultor"],
    },
    "genero_inclusion": {
        "es": ["genero", "mujeres", "inclusion", "igualdad", "vulnerable", "vulnerables"],
        "en": ["gender", "women", "inclusion", "equality", "vulnerable"],
        "fr": ["genre", "femmes", "inclusion", "egalite", "vulnerable"],
        "pt": ["genero", "mulheres", "inclusao", "igualdade", "vulneravel"],
    },
    "salud": {
        "es": ["salud", "sanitario", "sanitaria", "hospital", "medico", "medica", "enfermedades"],
        "en": ["health", "medical", "hospital", "healthcare", "disease", "diseases"],
        "fr": ["sante", "medical", "hopital", "maladies"],
        "pt": ["saude", "medico", "hospital", "doencas"],
    },
    "agropecuario": {
        "es": ["agricola", "agropecuario", "agricultura", "ganaderia", "rural", "cultivos", "pesca"],
        "en": ["agriculture", "agricultural", "livestock", "rural", "farming", "crops", "fisheries"],
        "fr": ["agricole", "agriculture", "elevage", "rural", "cultures", "peche"],
        "pt": ["agricola", "agricultura", "pecuaria", "rural", "pesca"],
    },
    "comercio_sector_privado": {
        "es": ["comercio", "empresarial", "inversion", "pyme", "pymes", "sector privado", "financiero"],
        "en": ["trade", "business", "investment", "sme", "smes", "private sector", "financial"],
        "fr": ["commerce", "entreprise", "investissement", "pme", "secteur prive"],
        "pt": ["comercio", "empresarial", "investimento", "setor privado"],
    },
    "turismo": {
        "es": ["turismo", "turistico", "turistica", "promocion", "marketing"],
        "en": ["tourism", "tourist", "promotion", "marketing"],
        "fr": ["tourisme", "touristique", "promotion", "marketing"],
        "pt": ["turismo", "turistico", "promocao", "marketing"],
    },
}
ETIQUETAS_TEMA = {
    "agua_saneamiento": "agua y saneamiento",
    "energia": "energía",
    "transporte_infraestructura": "transporte e infraestructuras",
    "tic_digitalizacion": "TIC y digitalización",
    "medio_ambiente_clima": "medio ambiente y cambio climático",
    "consultoria_formacion": "consultoría y formación",
    "genero_inclusion": "género e inclusión",
    "salud": "salud",
    "agropecuario": "sector agropecuario",
    "comercio_sector_privado": "comercio y sector privado",
    "turismo": "turismo",
}
_VOCABULARIO_POR_TEMA = {
    tema: set().union(*idiomas.values()) for tema, idiomas in TEMAS_MULTILINGUE.items()
}


def localizar_licitacion(supabase: Client, encoder: SentenceTransformer, entrada: str) -> list:
    """
    Devuelve una lista de licitaciones candidatas para la entrada del
    usuario. Si hay un match EXACTO por enlace o por codigo_unico,
    devuelve solo ese (confianza total). Si no, hace una búsqueda
    semántica por el texto y devuelve los primeros candidatos para que el
    usuario elija.
    """
    entrada = entrada.strip()
    if not entrada:
        return []

    columnas = (
        "codigo_unico, titulo, descripcion, pais, fuente_origen, url_oficial, "
        "fecha_publicacion, fecha_limite"
    )

    # 1. Match exacto por enlace
    if PATRON_URL.match(entrada):
        respuesta = (
            supabase.table("licitaciones_internacionales")
            .select(columnas)
            .eq("url_oficial", entrada)
            .limit(1)
            .execute()
        )
        if respuesta.data:
            return respuesta.data

    # 2. Match exacto por codigo_unico (con o sin mayúsculas)
    respuesta = (
        supabase.table("licitaciones_internacionales")
        .select(columnas)
        .ilike("codigo_unico", entrada)
        .limit(1)
        .execute()
    )
    if respuesta.data:
        return respuesta.data

    # 3. Búsqueda semántica por título/texto -> varios candidatos a elegir
    candidatos = buscar_semantica(supabase, encoder, entrada, match_threshold=0.15, match_count=8)
    return candidatos[:8]


def obtener_coincidencias(supabase: Client, codigo_unico: str, match_threshold: float = 0.15, match_count: int = 30) -> list:
    respuesta = supabase.rpc(
        "buscar_empresas_para_licitacion",
        {
            "codigo_unico_licitacion": codigo_unico,
            "match_threshold": match_threshold,
            "match_count": match_count,
        },
    ).execute()
    return respuesta.data or []


def obtener_coincidencias_texto_libre(
    supabase: Client,
    encoder: SentenceTransformer,
    titulo: str,
    descripcion: str = "",
    match_threshold: float = 0.15,
    match_count: int = 30,
) -> list:
    """Para licitaciones que el usuario pega/escribe y que NO están en nuestra tabla."""
    texto_completo = f"Título: {titulo}. {descripcion}".strip()
    vector_query = encoder.encode(f"passage: {texto_completo}").tolist()
    respuesta = supabase.rpc(
        "buscar_empresas_por_embedding",
        {
            "query_embedding": vector_query,
            "match_threshold": match_threshold,
            "match_count": match_count,
        },
    ).execute()
    return respuesta.data or []


def obtener_referencias_por_empresas(supabase: Client, ids_empresa: list) -> dict:
    """
    Trae, en UNA sola consulta por lote (nunca una por empresa), los
    títulos de licitaciones antiguas de todas las empresas candidatas --
    la hoja "REFERENCIAS P BÚSQUEDAS" del Excel, ya cargada en
    `empresas_referencias` (ver ingest/sync_empresas_drive.py). Se
    agrupan por id_empresa para que explicar_coincidencia() solo tenga
    que consultar un diccionario en memoria.
    """
    ids_empresa = [i for i in (ids_empresa or []) if i]
    if not ids_empresa:
        return {}

    respuesta = (
        supabase.table("empresas_referencias")
        .select("id_empresa, titulo, resultado_normalizado")
        .in_("id_empresa", ids_empresa)
        .execute()
    )

    agrupado = {}
    for fila in respuesta.data or []:
        if fila.get("titulo"):
            agrupado.setdefault(fila["id_empresa"], []).append(fila)
    return agrupado


def _normalizar(texto: str) -> str:
    """Minúsculas y sin acentos -- necesario para el diccionario temático
    (p. ej. 'energía' debe reconocerse igual que 'energia') y, de paso,
    hace más robustas las comparaciones de país que ya existían (p. ej.
    'São Tomé' vs 'Sao Tome')."""
    texto = (texto or "").casefold()
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def _palabras_significativas(texto: str) -> set:
    texto_norm = _normalizar(texto)
    palabras = re.findall(r"[a-z0-9]+", texto_norm)
    return {p for p in palabras if len(p) > 3 and p not in PALABRAS_VACIAS}


def _tokenizar(texto: str) -> set:
    """Como _palabras_significativas pero SIN filtro de longitud ni de
    palabras vacías -- para comparar contra el vocabulario controlado de
    TEMAS_MULTILINGUE, donde hay palabras cortas legítimas (p. ej. 'eau', 'tic')."""
    return set(re.findall(r"[a-z0-9]+", _normalizar(texto)))


def _hay_solapamiento(texto_a: str, texto_b: str, minimo: int = 2) -> bool:
    """Solapamiento de palabras significativas entre dos textos -- heurística de texto, nada de LLM."""
    if not texto_a or not texto_b:
        return False
    return len(_palabras_significativas(texto_a) & _palabras_significativas(texto_b)) >= minimo


def _terminos_comunes(texto_a: str, texto_b: str, maximo: int = 6) -> list:
    """Palabras significativas presentes en ambos textos -- evidencia concreta de POR QUÉ solapan, no solo un sí/no."""
    if not texto_a or not texto_b:
        return []
    comunes = _palabras_significativas(texto_a) & _palabras_significativas(texto_b)
    return sorted(comunes)[:maximo]


def _temas_presentes(texto: str) -> set:
    """Temas del diccionario TEMAS_MULTILINGUE detectados en el texto, en cualquiera de los 4 idiomas."""
    if not texto:
        return set()
    palabras = _tokenizar(texto)
    return {tema for tema, vocabulario in _VOCABULARIO_POR_TEMA.items() if palabras & vocabulario}


def _puente_tematico(texto_empresa: str, texto_licitacion: str) -> list:
    """
    Capa 2: temas presentes en AMBOS textos aunque no compartan ninguna
    palabra exacta -- el puente semántico multilingüe para cuando la
    licitación está en otro idioma o usa sinónimos distintos a los de la
    empresa. Devuelve las etiquetas legibles, ordenadas.
    """
    comunes = _temas_presentes(texto_empresa) & _temas_presentes(texto_licitacion)
    return sorted(ETIQUETAS_TEMA.get(t, t) for t in comunes)


def _similitud_coseno(vector_a, vector_b) -> float:
    a, b = np.array(vector_a), np.array(vector_b)
    denominador = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denominador) if denominador else 0.0


def _bridge_semantico_por_campo(encoder: SentenceTransformer, texto_licitacion: str, empresa: dict):
    """
    Capa 3 (último recurso): si ni las palabras literales (multilingües)
    ni el diccionario temático encuentran ninguna relación, se compara el
    embedding de la licitación contra el de cada campo del perfil de la
    empresa POR SEPARADO, con el mismo modelo multilingüe que ya usa toda
    la app (sin llamada externa ni LLM), para poder señalar qué aspecto
    concreto del perfil es el que más se relaciona -- en vez de dejar la
    recomendación sin ninguna explicación. Devuelve (etiqueta_campo,
    similitud) del campo más afín, o (None, 0.0) si no hay ningún campo
    con contenido.
    """
    campos = {
        "descripción de actividad": empresa.get("descripcion_actividad"),
        "proyectos tipo declarados": "; ".join(empresa.get("proyectos_tipo") or []) or None,
        "preferencias de licitación declaradas": empresa.get("preferencias_licitaciones"),
        "sector y subsector declarados": ", ".join(filter(None, [empresa.get("sector"), empresa.get("subsector")])) or None,
    }
    campos_con_texto = {etiqueta: texto for etiqueta, texto in campos.items() if texto}
    if not campos_con_texto:
        return None, 0.0

    vector_licitacion = encoder.encode(f"query: {texto_licitacion}")

    mejor_etiqueta, mejor_similitud = None, 0.0
    for etiqueta, texto in campos_con_texto.items():
        vector_campo = encoder.encode(f"passage: {texto}")
        similitud = _similitud_coseno(vector_licitacion, vector_campo)
        if similitud > mejor_similitud:
            mejor_etiqueta, mejor_similitud = etiqueta, similitud

    return mejor_etiqueta, mejor_similitud


def _recortar(texto: str, longitud: int = 140) -> str:
    texto = texto or ""
    return texto if len(texto) <= longitud else texto[:longitud].rstrip() + "…"


def _construir_resumen(similarity, señales_literales: list, temas_puente: list, campo_semantico: tuple) -> str:
    """
    Primera frase de la explicación: sintetiza el nivel de encaje y, sobre
    todo, DE QUÉ depende -- nunca se limita a decir que no hay datos.
    Cuatro niveles, de más a menos concreto:
      1. Coincidencias literales (Capa 1, incluye ahora las 4 variantes
         de idioma de las palabras clave).
      2. Puente temático multilingüe (Capa 2): mismo tema, aunque las
         palabras exactas no coincidan por idioma o vocabulario.
      3. Relación semántica por campo (Capa 3): el modelo de embeddings
         multilingüe relaciona la licitación con un campo concreto del
         perfil, aunque no haya ni palabras ni tema en común detectados.
      4. Solo como última instancia, si ninguna de las tres capas
         anteriores encuentra nada, se dice explícitamente.
    """
    pct = round(similarity * 100, 1) if similarity is not None else None
    pct_texto = f" ({pct}% de afinidad semántica)" if pct is not None else ""

    if señales_literales:
        return f"Se recomienda esta empresa por coincidencias concretas en: {', '.join(señales_literales)}{pct_texto}."

    if temas_puente:
        return (
            f"No hay coincidencias literales de palabras -- probablemente por diferencia de "
            f"idioma o de vocabulario entre la licitación y el perfil de la empresa -- pero "
            f"ambas están conceptualmente relacionadas por tema: {', '.join(temas_puente)}{pct_texto}."
        )

    if campo_semantico and campo_semantico[0]:
        etiqueta, sim_campo = campo_semantico
        pct_campo = round(sim_campo * 100, 1)
        global_texto = f", con una afinidad semántica global del perfil del {pct}%" if pct is not None else ""
        return (
            f"No se han encontrado coincidencias literales ni temáticas explícitas, pero el "
            f"modelo de similitud semántica multilingüe relaciona esta licitación especialmente "
            f"con el campo «{etiqueta}» del perfil de la empresa ({pct_campo}% de afinidad en ese "
            f"campo){global_texto} -- una conexión conceptual a valorar, aunque más débil que una "
            f"coincidencia directa."
        )

    if pct is not None:
        return (
            f"No se han encontrado coincidencias literales, temáticas ni semánticas claras en "
            f"ningún campo del perfil; la recomendación se apoya únicamente en la similitud "
            f"semántica global del perfil completo{pct_texto} -- conviene revisarla manualmente "
            f"antes de confiar en ella."
        )
    return "Coincidencia detectada por similitud semántica general del perfil de la empresa."


def explicar_coincidencia(
    texto_licitacion: str,
    pais_licitacion: str,
    empresa: dict,
    referencias_empresa: list = None,
    encoder: SentenceTransformer = None,
) -> list:
    """
    Devuelve una lista de frases (bullets) que ARGUMENTAN por qué esta
    empresa encaja con la licitación, con evidencia concreta en vez de
    afirmaciones genéricas -- y NUNCA se limita a decir que no hay datos
    (ver docstring del módulo para las 3 capas). El primer elemento es
    siempre el resumen de _construir_resumen().
    """
    motivos_detalle = []
    señales = []
    texto_norm = _normalizar(texto_licitacion)

    proyectos_coincidentes = [
        p for p in (empresa.get("proyectos_tipo") or [])
        if p and _hay_solapamiento(p, texto_licitacion, minimo=1)
    ]
    if proyectos_coincidentes:
        motivos_detalle.append(
            "Ha ejecutado proyectos del mismo tipo que el objeto de esta licitación: "
            + "; ".join(proyectos_coincidentes[:3]) + "."
        )
        señales.append("proyectos tipo ya realizados")

    if empresa.get("descripcion_actividad"):
        comunes = _terminos_comunes(empresa["descripcion_actividad"], texto_licitacion)
        if comunes:
            motivos_detalle.append(
                f"Su descripción de actividad («{_recortar(empresa['descripcion_actividad'])}») "
                f"comparte términos concretos con la licitación: {', '.join(comunes)}."
            )
            señales.append("descripción de actividad")

    if empresa.get("preferencias_licitaciones"):
        comunes = _terminos_comunes(empresa["preferencias_licitaciones"], texto_licitacion)
        if comunes:
            motivos_detalle.append(
                f"Sus preferencias de licitación declaradas («{empresa['preferencias_licitaciones']}») "
                f"coinciden en: {', '.join(comunes)}."
            )
            señales.append("preferencias de licitación declaradas")

    paises_empresa = set(_normalizar(p) for p in (empresa.get("experiencia_paises") or []))
    paises_interes_empresa = set(_normalizar(p) for p in (empresa.get("paises_interes") or []))
    zonas_empresa = set(_normalizar(z) for z in (empresa.get("zona_geografica_interes") or []))
    ambito_geografico_norm = _normalizar(empresa.get("ambito_geografico") or "")
    if pais_licitacion:
        pais_norm = _normalizar(pais_licitacion)
        if pais_norm in paises_empresa:
            motivos_detalle.append(f"Experiencia previa acreditada en {pais_licitacion}.")
            señales.append("ubicación geográfica")
        elif pais_norm in paises_interes_empresa:
            motivos_detalle.append(f"La empresa tiene interés declarado en {pais_licitacion}.")
            señales.append("ubicación geográfica")
        elif any(pais_norm in z or z in pais_norm for z in zonas_empresa):
            motivos_detalle.append(f"La empresa tiene interés en la zona geográfica de {pais_licitacion}.")
            señales.append("ubicación geográfica")
        elif ambito_geografico_norm and pais_norm in ambito_geografico_norm:
            motivos_detalle.append(f"El ámbito geográfico de operación de la empresa menciona {pais_licitacion}.")
            señales.append("ubicación geográfica")

    # Las 4 variantes de idioma de las palabras clave que ya tiene cada
    # empresa (antes solo se comparaba la española, aunque la licitación
    # esté en inglés o francés -- el motivo más habitual por el que no
    # aparecía ninguna coincidencia literal).
    todas_las_palabras_clave = (
        (empresa.get("palabras_clave") or [])
        + (empresa.get("palabras_clave_en") or [])
        + (empresa.get("palabras_clave_fr") or [])
        + (empresa.get("palabras_clave_pt") or [])
    )
    palabras_coincidentes = sorted({
        palabra for palabra in todas_las_palabras_clave
        if palabra and _normalizar(palabra) in texto_norm
    })
    if palabras_coincidentes:
        motivos_detalle.append("Palabras clave de la empresa (en su idioma original) presentes en la licitación: " + ", ".join(palabras_coincidentes) + ".")
        señales.append("palabras clave")

    for referencia in (referencias_empresa or []):
        titulo_referencia = referencia.get("titulo")
        if titulo_referencia and _hay_solapamiento(titulo_referencia, texto_licitacion, minimo=2):
            resultado = referencia.get("resultado_normalizado")
            if resultado == "adjudicada":
                sufijo = " (adjudicada)"
            elif resultado == "no_adjudicada":
                sufijo = " (no adjudicada)"
            else:
                sufijo = ""
            motivos_detalle.append(f"Ya se presentó a una licitación similar: \"{_recortar(titulo_referencia, 100)}\"{sufijo}.")
            señales.append("historial de licitaciones similares")
            break  # una sola referencia antigua basta como señal; evita repetir el mismo motivo

    if empresa.get("sector"):
        detalle_sector = empresa["sector"]
        # Evita el "Sector X (Sector X)" cuando subsector y sector son literalmente el mismo texto.
        if empresa.get("subsector") and _normalizar(empresa["subsector"]) != _normalizar(empresa["sector"]):
            detalle_sector += f" ({empresa['subsector']})"
        if _hay_solapamiento(empresa["sector"], texto_licitacion, minimo=1):
            motivos_detalle.append(f"Su sector de actividad ({detalle_sector}) coincide temáticamente con el objeto de la licitación.")
            señales.append("sector de actividad")
        else:
            motivos_detalle.append(f"Sector de actividad de la empresa: {detalle_sector}.")

    # Capa 2 -- puente temático multilingüe: solo se activa si la Capa 1 no
    # ha encontrado NINGUNA coincidencia literal, para no repetir lo mismo
    # con otras palabras. Se guarda en `temas_puente`, NO en `señales`
    # (que se reserva para las coincidencias literales de la Capa 1): así
    # el resumen puede distinguir con qué capa se ha encontrado la
    # relación, y la Capa 3 solo se intenta si esta tampoco encuentra nada.
    temas_puente = []
    if not señales:
        texto_perfil_empresa = " ".join(filter(None, [
            empresa.get("descripcion_actividad"),
            "; ".join(empresa.get("proyectos_tipo") or []),
            empresa.get("preferencias_licitaciones"),
            empresa.get("sector"), empresa.get("subsector"),
            " ".join(todas_las_palabras_clave),
        ]))
        temas_puente = _puente_tematico(texto_perfil_empresa, texto_licitacion)
        if temas_puente:
            motivos_detalle.append(
                "Puente temático: aunque no comparten palabras exactas, el perfil de la empresa "
                f"y la licitación tratan sobre {', '.join(temas_puente)}."
            )

    # Capa 3 -- último recurso, similitud semántica por campo: solo si
    # tampoco hay coincidencia literal NI puente temático, y solo si se ha
    # pasado un encoder (siempre disponible desde render_tab2).
    campo_semantico = (None, 0.0)
    if not señales and not temas_puente and encoder is not None:
        campo_semantico = _bridge_semantico_por_campo(encoder, texto_licitacion, empresa)
        if campo_semantico[0] and campo_semantico[1] >= 0.20:
            pct_campo = round(campo_semantico[1] * 100, 1)
            motivos_detalle.append(
                f"El modelo de similitud semántica multilingüe relaciona especialmente esta "
                f"licitación con «{campo_semantico[0]}» del perfil de la empresa ({pct_campo}% "
                f"de afinidad en ese campo), lo que sugiere una conexión conceptual aunque no "
                f"compartan palabras ni tema detectado."
            )
        else:
            campo_semantico = (None, 0.0)  # por debajo del umbral: no se cuenta como señal

    resumen = _construir_resumen(empresa.get("similarity"), señales, temas_puente, campo_semantico)
    return [resumen] + motivos_detalle


def obtener_lugares_empresa(empresa: dict) -> list:
    """
    Los 4 campos geográficos de la empresa, ya definidos en el Directorio
    de Empresas (Experiencia países, Zona geográfica de interés, Países
    de interés, Ámbito geográfico), como pares (etiqueta, texto) listos
    para mostrarse en texto plano -- sin tabla ni columna de
    coincidencia: esa valoración ya la dan los bullets de
    explicar_coincidencia() de forma más específica.
    """
    def _texto(valor):
        if isinstance(valor, list):
            return ", ".join(v for v in valor if v) or "No especificado"
        return valor or "No especificado"

    return [
        ("Experiencia países", _texto(empresa.get("experiencia_paises"))),
        ("Zona geográfica de interés", _texto(empresa.get("zona_geografica_interes"))),
        ("Países de interés", _texto(empresa.get("paises_interes"))),
        ("Ámbito geográfico", _texto(empresa.get("ambito_geografico"))),
    ]


def formatear_tabla_coincidencias(coincidencias: list) -> pd.DataFrame:
    if not coincidencias:
        return pd.DataFrame()
    df = pd.DataFrame(coincidencias)
    return pd.DataFrame({
        "Relevancia (%)": (df["similarity"] * 100).round(1),
        "Empresa": df["nombre_empresa"],
        "ID": df["id_empresa"],
        "Sector": df["sector"].fillna("No especificado"),
        "Tipo": df["tipo_empresa"].fillna("No especificado"),
    })


# ------------------------------------------------------------------
# Interfaz de la Pestaña 2
# ------------------------------------------------------------------
def _resetear_seleccion():
    st.session_state.tab2_candidatos = None
    st.session_state.tab2_licitacion_elegida = None
    st.session_state.tab2_coincidencias = None


def render_tab2(supabase: Client, encoder: SentenceTransformer):
    st.subheader("Coincidencia Inteligente: Licitación → Empresas")
    st.caption(
        "Introduce el título, el enlace oficial o el identificador de una licitación. "
        "Si no está en nuestra base de datos, también puedes describirla directamente."
    )

    for clave, valor in (
        ("tab2_candidatos", None),
        ("tab2_licitacion_elegida", None),
        ("tab2_coincidencias", None),
    ):
        if clave not in st.session_state:
            st.session_state[clave] = valor

    entrada = st.text_input(
        "Título, enlace o identificador de la licitación",
        placeholder="ej. AFDB-spn-namibia-... · https://www.afdb.org/... · perforación de pozos en Namibia",
        key="tab2_entrada",
        on_change=_resetear_seleccion,
    )

    if st.button("Buscar licitación", key="tab2_buscar_licitacion", use_container_width=True):
        with st.spinner("Buscando la licitación..."):
            st.session_state.tab2_candidatos = localizar_licitacion(supabase, encoder, entrada)
            st.session_state.tab2_licitacion_elegida = None
            st.session_state.tab2_coincidencias = None

    candidatos = st.session_state.tab2_candidatos

    # --- Selección de la licitación entre los candidatos encontrados ---
    if candidatos:
        if len(candidatos) == 1:
            st.session_state.tab2_licitacion_elegida = candidatos[0]
            st.success(f"Licitación identificada: **{candidatos[0]['titulo']}**")
        else:
            opciones = {f"{c['titulo']}  ·  {c.get('pais') or 's/país'}": c for c in candidatos}
            etiqueta_elegida = st.selectbox(
                "Se han encontrado varias licitaciones parecidas — elige la correcta:",
                list(opciones.keys()),
                key="tab2_selector_candidato",
            )
            st.session_state.tab2_licitacion_elegida = opciones[etiqueta_elegida]

    elif candidatos is not None:  # se buscó y no hubo NINGÚN resultado
        st.warning("No se ha encontrado ninguna licitación parecida en nuestra base de datos.")

    # --- Fallback: introducir la licitación directamente ---
    with st.expander("¿No está en la base de datos? Descríbela directamente"):
        titulo_manual = st.text_input("Título de la licitación", key="tab2_titulo_manual")
        descripcion_manual = st.text_area("Descripción / objeto del contrato", key="tab2_descripcion_manual", height=80)
        if st.button("Usar esta descripción", key="tab2_usar_manual"):
            if titulo_manual.strip():
                st.session_state.tab2_licitacion_elegida = {
                    "titulo": titulo_manual,
                    "pais": None,
                    "codigo_unico": None,
                    "descripcion_manual": descripcion_manual,
                }
                st.session_state.tab2_coincidencias = None
            else:
                st.warning("Escribe al menos un título.")

    licitacion = st.session_state.tab2_licitacion_elegida

    if licitacion:
        st.markdown(f"**Licitación seleccionada:** {licitacion['titulo']}")

        if st.button("Buscar empresas coincidentes", key="tab2_buscar_empresas", use_container_width=True):
            with st.spinner("Cruzando con la base de datos de empresas..."):
                if licitacion.get("codigo_unico"):
                    st.session_state.tab2_coincidencias = obtener_coincidencias(supabase, licitacion["codigo_unico"])
                else:
                    st.session_state.tab2_coincidencias = obtener_coincidencias_texto_libre(
                        supabase, encoder, licitacion["titulo"], licitacion.get("descripcion_manual", "")
                    )

        coincidencias = st.session_state.tab2_coincidencias
        if coincidencias is not None:
            if not coincidencias:
                st.warning("No se han encontrado empresas con un perfil afín a esta licitación.")
            else:
                st.success(f"**{len(coincidencias)}** empresas encajan con esta licitación, de más a menos afín:")
                # Título + descripción de la licitación (ver docstring del módulo,
                # sección 2): antes solo se usaba el título.
                texto_licitacion = " ".join(filter(None, [
                    licitacion.get("titulo"),
                    licitacion.get("descripcion"),
                    licitacion.get("descripcion_manual"),
                ]))
                pais_licitacion = licitacion.get("pais")

                # Una sola consulta por lote para el historial de referencias de
                # TODAS las empresas candidatas (nunca N+1).
                ids_empresa = [e["id_empresa"] for e in coincidencias if e.get("id_empresa")]
                referencias_por_empresa = obtener_referencias_por_empresas(supabase, ids_empresa)

                for empresa in coincidencias:
                    with st.container(border=True):
                        col_nombre, col_score = st.columns([4, 1])
                        with col_nombre:
                            st.markdown(f"**{empresa['nombre_empresa']}**  ·  `{empresa['id_empresa']}`")
                        with col_score:
                            st.markdown(f"**{round(empresa['similarity'] * 100, 1)}%**")

                        referencias_empresa = referencias_por_empresa.get(empresa["id_empresa"], [])
                        motivos = explicar_coincidencia(texto_licitacion, pais_licitacion, empresa, referencias_empresa, encoder)
                        for motivo in motivos:
                            st.markdown(f"- {motivo}")

                        st.caption(f"Lugar de la licitación: {pais_licitacion or 'No especificado'}")
                        for etiqueta, valor in obtener_lugares_empresa(empresa):
                            st.caption(f"{etiqueta}: {valor}")

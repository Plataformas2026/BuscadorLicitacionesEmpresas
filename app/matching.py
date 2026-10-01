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

Novedades de la v2 (categoría de la licitación + nuevos datos del Excel):
  - La CATEGORÍA de la licitación entra en la comparación: se recupera
    junto al resto de campos (localizar_licitacion), se añade al texto con
    el que se compara y se contrasta con el sector/subsector, proyectos tipo
    y palabras clave de la empresa. En el flujo de texto libre también va en
    el embedding que se calcula al vuelo.
  - Capa 1 (literal) aprovecha ahora: organismo/financiador de la licitación
    frente a los clientes principales y a las referencias de la empresa;
    país de la licitación frente al país de sus referencias; y
    certificaciones citadas en la licitación (ISO, ENAC...).
  - Capa 2 (puente temático) incluye también los tipos de proyecto y sectores
    de las referencias de la empresa.
  - Capa 3 (similitud por campo) compara además palabras clave, clientes
    principales y el historial de títulos de la empresa, y cachea los
    embeddings para no recalcular el de la licitación por cada empresa.
  - Los títulos de las demás pestañas del Excel (tabla
    `empresas_hojas_extra`, solo los que NO están ya en
    `empresas_referencias`) se suman a las referencias de cada empresa.
  - Los campos geográficos de la empresa son ahora texto libre (p. ej.
    "España (Tenerife, La Gomera)"): se buscan por palabra completa y se
    ignoran fragmentos demasiado cortos para dar falsas coincidencias.
"""
import re
import unicodedata

import numpy as np
import pandas as pd
import streamlit as st
from sentence_transformers import SentenceTransformer
from supabase import Client

from search import buscar_semantica
from ia_explicacion import generar_justificacion_ia, groq_configurado, justificacion_en_cache

PATRON_URL = re.compile(r"^https?://", re.IGNORECASE)

PALABRAS_VACIAS = {
    "de", "la", "el", "los", "las", "en", "y", "a", "del", "para", "con", "por",
    "un", "una", "unos", "unas", "que", "se", "su", "sus", "al", "o", "the", "of",
    "and", "for", "to", "in", "on", "an", "des", "du", "les", "le", "au", "aux",
}

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

MIN_LONGITUD_FRAGMENTO_GEOGRAFICO = 4

ORGANISMOS_ALIAS = {
    "BID": {"bid", "idb", "iadb", "banco interamericano de desarrollo",
            "inter american development bank", "interamerican development bank",
            "banque interamericaine de developpement"},
    "Banco Mundial": {"banco mundial", "world bank", "banque mondiale", "ibrd"},
    "Banco Africano de Desarrollo": {"afdb", "african development bank", "banco africano de desarrollo",
                                     "banque africaine de developpement"},
    "AFD": {"afd", "agence francaise de developpement", "agencia francesa de desarrollo"},
    "CAF": {"caf", "corporacion andina de fomento", "banco de desarrollo de america latina"},
    "BCIE": {"bcie", "cabei", "banco centroamericano de integracion economica"},
    "PNUD/UNDP": {"undp", "pnud", "united nations development programme",
                  "programa de las naciones unidas para el desarrollo"},
    "Naciones Unidas": {"ungm", "naciones unidas", "united nations", "nations unies"},
    "GIZ": {"giz", "deutsche gesellschaft fur internationale zusammenarbeit"},
    "Unión Europea": {"union europea", "european union", "union europeenne", "comision europea",
                      "european commission", "europeaid", "ted"},
    "AECID": {"aecid", "agencia espanola de cooperacion internacional"},
    "Banco Asiático de Desarrollo": {"asian development bank", "banco asiatico de desarrollo"},
}

PALABRAS_ORGANISMO_GENERICAS = {
    "banco", "bank", "banque", "desarrollo", "development", "developpement", "agencia", "agency",
    "agence", "ministerio", "ministry", "ministere", "gobierno", "government", "programa", "program",
    "programme", "naciones", "unidas", "nations", "united", "internacional", "international",
    "fondo", "fund", "comision", "commission", "union", "republica", "republic", "direccion",
    "secretaria", "unidad", "instituto", "institute", "servicio", "servicios", "cooperacion",
    "cooperation", "regional", "nacional", "national", "empresa", "sociedad", "autoridad",
}

PREFIJOS_CERTIFICACION = ("iso", "une", "ohsas", "enac")
ACRONIMOS_CERTIFICACION = {"emas", "ens", "cmmi", "itil", "ecovadis", "prince2", "pmp"}


def _completar_campos_licitacion(supabase: Client, candidatos: list) -> list:
    faltan = [
        c for c in candidatos
        if c.get("codigo_unico") and any(k not in c for k in ("categoria", "organismo", "fuente_origen"))
    ]
    if not faltan:
        return candidatos
    try:
        respuesta = (
            supabase.table("licitaciones_internacionales")
            .select("codigo_unico, categoria, organismo, fuente_origen")
            .in_("codigo_unico", [c["codigo_unico"] for c in faltan])
            .execute()
        )
    except Exception:
        return candidatos

    extra = {f["codigo_unico"]: f for f in (respuesta.data or [])}
    for candidato in faltan:
        fila = extra.get(candidato["codigo_unico"], {})
        for campo in ("categoria", "organismo", "fuente_origen"):
            candidato.setdefault(campo, fila.get(campo))
    return candidatos


def localizar_licitacion(supabase: Client, encoder: SentenceTransformer, entrada: str) -> list:
    entrada = entrada.strip()
    if not entrada:
        return []

    columnas = (
        "codigo_unico, titulo, descripcion, pais, organismo, categoria, tipo_aviso, fuente_origen, "
        "url_oficial, fecha_publicacion, fecha_limite"
    )

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

    respuesta = (
        supabase.table("licitaciones_internacionales")
        .select(columnas)
        .ilike("codigo_unico", entrada)
        .limit(1)
        .execute()
    )
    if respuesta.data:
        return respuesta.data

    candidatos = buscar_semantica(supabase, encoder, entrada, match_threshold=0.15, match_count=8)
    return _completar_campos_licitacion(supabase, candidatos[:8])


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
    categoria: str = "",
    pais: str = "",
) -> list:
    partes = [f"Titulo: {titulo}. {descripcion}".strip()]
    if categoria:
        partes.append(f"Categoria: {categoria}")
    if pais:
        partes.append(f"Pais: {pais}")
    texto_completo = "\n".join(partes)
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


def _clave_titulo(titulo: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _normalizar(titulo)))


COLUMNAS_REFERENCIAS = (
    "numero_interno, titulo, resultado_normalizado, pais, organismo_financiador, "
    "agencia_ejecutora, tipo_proyecto, sector"
)


def _consulta_paginada(construir_consulta, tamano_pagina: int = 1000) -> list:
    filas, inicio = [], 0
    while True:
        datos = construir_consulta().range(inicio, inicio + tamano_pagina - 1).execute().data or []
        filas.extend(datos)
        if len(datos) < tamano_pagina:
            return filas
        inicio += tamano_pagina


def obtener_referencias_por_empresas(supabase: Client, numeros_internos: list) -> dict:
    numeros_internos = [i for i in (numeros_internos or []) if i]
    if not numeros_internos:
        return {}

    filas = _consulta_paginada(
        lambda: supabase.table("empresas_referencias")
        .select(COLUMNAS_REFERENCIAS)
        .in_("numero_interno", numeros_internos)
        .order("id")
    )

    agrupado = {}
    for fila in filas:
        if fila.get("titulo"):
            agrupado.setdefault(fila["numero_interno"], []).append(fila)

    try:
        filas_extra = _consulta_paginada(
            lambda: supabase.table("empresas_hojas_extra")
            .select("numero_interno, hoja, titulos_nuevos")
            .in_("numero_interno", numeros_internos)
            .eq("aporta_titulos", True)
            .order("id")
        )
    except Exception:
        filas_extra = []

    for fila in filas_extra:
        numero = fila["numero_interno"]
        vistos = {_clave_titulo(r["titulo"]) for r in agrupado.get(numero, [])}
        for titulo in fila.get("titulos_nuevos") or []:
            clave = _clave_titulo(titulo)
            if clave and clave not in vistos:
                vistos.add(clave)
                agrupado.setdefault(numero, []).append({
                    "numero_interno": numero, "titulo": titulo,
                    "resultado_normalizado": None, "hoja_origen": fila.get("hoja"),
                })
    return agrupado


def _normalizar(texto: str) -> str:
    texto = (texto or "").casefold()
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def _palabras_significativas(texto: str) -> set:
    texto_norm = _normalizar(texto)
    palabras = re.findall(r"[a-z0-9]+", texto_norm)
    return {p for p in palabras if len(p) > 3 and p not in PALABRAS_VACIAS}


def _tokenizar(texto: str) -> set:
    return set(re.findall(r"[a-z0-9]+", _normalizar(texto)))


def _recortar(texto: str, max_len: int = 60) -> str:
    texto = (texto or "").strip()
    return texto[:max_len] + "..." if len(texto) > max_len else texto


@st.cache_data(show_spinner=False, max_entries=4096)
def _vector_cacheado(_encoder, prefijo: str, texto: str):
    return _encoder.encode(f"{prefijo}: {texto}")


def _similitud_coseno(vector_a, vector_b) -> float:
    a, b = np.array(vector_a), np.array(vector_b)
    denominador = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denominador) if denominador else 0.0


def _bridge_semantico_por_campo(
    encoder: SentenceTransformer, texto_licitacion: str, empresa: dict, referencias: list = None
):
    palabras_clave = (
        (empresa.get("palabras_clave") or []) + (empresa.get("palabras_clave_en") or [])
        + (empresa.get("palabras_clave_fr") or []) + (empresa.get("palabras_clave_pt") or [])
    )
    titulos_historial = [r["titulo"] for r in (referencias or []) if r.get("titulo")][:12]

    campos = {
        "descripción de actividad": empresa.get("descripcion_actividad"),
        "proyectos tipo declarados": "; ".join(empresa.get("proyectos_tipo") or []) or None,
        "preferencias de licitación declaradas": empresa.get("preferencias_licitaciones"),
        "sector y subsector declarados": ", ".join(filter(None, [empresa.get("sector"), empresa.get("subsector")])) or None,
        "palabras clave declaradas": ", ".join(p for p in palabras_clave if p) or None,
        "clientes principales declarados": empresa.get("clientes_principales"),
        "licitaciones y proyectos anteriores": "; ".join(titulos_historial) or None,
    }
    campos_con_texto = {etiqueta: texto for etiqueta, texto in campos.items() if texto}
    if not campos_con_texto:
        return None, 0.0

    vector_licitacion = _vector_cacheado(encoder, "query", texto_licitacion)

    mejor_etiqueta, mejor_similitud = None, 0.0
    for etiqueta, texto in campos_con_texto.items():
        vector_campo = _vector_cacheado(encoder, "passage", texto)
        sim = _similitud_coseno(vector_licitacion, vector_campo)
        if sim > mejor_similitud:
            mejor_similitud = sim
            mejor_etiqueta = etiqueta

    return mejor_etiqueta, mejor_similitud


def explicar_coincidencia(
    licitacion: dict, empresa: dict, referencias: list = None, encoder: SentenceTransformer = None
) -> str:
    """Construye la justificación estructurada basada en la evidencia de la empresa."""
    puntos = []
    
    # Capa 1: Coincidencias explícitas / directas
    if licitacion.get("pais") and empresa.get("pais"):
        if _normalizar(licitacion["pais"]) in _normalizar(empresa["pais"]):
            puntos.append(f"• **Ubicación geográfica**: Presencia o ámbito en {licitacion['pais']}.")

    # Capa 2: Puente temático
    texto_lic = f"{licitacion.get('titulo', '')} {licitacion.get('descripcion', '')}"
    texto_emp = f"{empresa.get('descripcion_actividad', '')} {' '.join(empresa.get('proyectos_tipo') or [])}"
    
    temas = _temas_presentes(texto_lic) & _temas_presentes(texto_emp)
    if temas:
        etiquetas = [ETIQUETAS_TEMA[t] for t in temas if t in ETIQUETAS_TEMA]
        puntos.append(f"• **Afinidad temática**: Coincidencia en áreas de {', '.join(etiquetas)}.")

    # Capa 3: Similitud semántica como respaldo
    if not puntos and encoder:
        campo, sim = _bridge_semantico_por_campo(encoder, texto_lic, empresa, referencias)
        if campo and sim > 0.30:
            puntos.append(f"• **Similitud semántica**: Relación relevante detectada con su {campo}.")

    if not puntos:
        puntos.append("• **Afinidad general**: Coincidencia basada en el perfil global de la empresa.")

    return "\n".join(puntos)


# ==============================================================================
# SECCIÓN DE INTERFAZ Y RENDERIZADO EN STREAMLIT (INTEGRACIÓN REINTENTO IA)
# ==============================================================================

TEXTO_ERROR_IA = "No se ha podido generar la justificación con IA"

def _renderizar_bloque_ia(empresa: dict, licitacion: dict, explicacion_base: str):
    """
    Gestiona la visualización del análisis con IA y añade el botón de reintento 
    si la API devolvió un fallo temporal o límite de cuota.
    """
    empresa_id = empresa.get("numero_interno") or empresa.get("id")
    clave_state = f"ia_justificacion_{empresa_id}"
    justificacion_actual = st.session_state.get(clave_state)

    if justificacion_actual:
        if TEXTO_ERROR_IA in justificacion_actual:
            st.warning(justificacion_actual)
            if st.button("🔄 Reintentar generación con IA", key=f"retry_ia_{empresa_id}"):
                # Borramos la clave con el error del estado para desbloquear la petición
                del st.session_state[clave_state]
                
                # Intentamos regenerar inmediatamente
                with st.spinner("Reintentando análisis con IA..."):
                    nueva_res = generar_justificacion_ia(
                        licitacion=licitacion,
                        empresa=empresa,
                        explicacion_base=explicacion_base
                    )
                    st.session_state[clave_state] = nueva_res
                st.rerun()
        else:
            st.info(f"**Análisis avanzado (IA):**\n\n{justificacion_actual}")
    else:
        if st.button("✨ Generar justificación con IA", key=f"btn_ia_{empresa_id}"):
            with st.spinner("Generando justificación avanzada..."):
                res = generar_justificacion_ia(
                    licitacion=licitacion,
                    empresa=empresa,
                    explicacion_base=explicacion_base
                )
                st.session_state[clave_state] = res
                st.rerun()


def mostrar_tarjeta_empresa(empresa: dict, licitacion: dict, referencias: list, encoder: SentenceTransformer):
    """Renderiza la tarjeta informativa de una empresa candidata."""
    with st.expander(f"🏢 {empresa.get('nombre_empresa', 'Empresa sin nombre')} - Afinidad: {int(empresa.get('similitud', 0)*100)}%"):
        explicacion = explicar_coincidencia(licitacion, empresa, referencias, encoder)
        st.markdown("**Motivos de coincidencia:**")
        st.markdown(explicacion)

        st.divider()

        # Integración del bloque interactivo de IA
        if groq_configurado():
            _renderizar_bloque_ia(empresa, licitacion, explicacion)
        else:
            st.caption("ℹ️ Configure la API Key de Groq para habilitar el análisis avanzado con IA.")

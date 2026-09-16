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
     coincidencia a partir de datos concretos: similitud semántica,
     proyectos tipo ya realizados, descripción de actividad, preferencias
     de licitación declaradas, país/zona geográfica, palabras clave, y
     licitaciones antiguas parecidas a las que ya se presentó la empresa.
     Nada inventado ni una llamada a un LLM externo, para mantener la app
     dentro de la arquitectura gratuita.
  6. `construir_comparacion_lugares()` -- tabla visual comparando el
     lugar de la licitación contra los 4 campos geográficos de la
     empresa (los mismos que en el Directorio de Empresas).
"""
import re

import pandas as pd
import streamlit as st
from sentence_transformers import SentenceTransformer
from supabase import Client

from search import buscar_semantica

PATRON_URL = re.compile(r"^https?://", re.IGNORECASE)

# Palabras demasiado comunes en español/francés/inglés como para aportar
# señal en el solapamiento de texto (ver _hay_solapamiento).
PALABRAS_VACIAS = {
    "de", "la", "el", "los", "las", "en", "y", "a", "del", "para", "con", "por",
    "un", "una", "unos", "unas", "que", "se", "su", "sus", "al", "o", "the", "of",
    "and", "for", "to", "in", "on", "an", "des", "du", "les", "le", "au", "aux",
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
    return (texto or "").casefold()


def _palabras_significativas(texto: str) -> set:
    texto_norm = _normalizar(texto)
    palabras = re.findall(r"[a-zàâäéèêëïîôöùûüçñ0-9]+", texto_norm)
    return {p for p in palabras if len(p) > 3 and p not in PALABRAS_VACIAS}


def _hay_solapamiento(texto_a: str, texto_b: str, minimo: int = 2) -> bool:
    """Solapamiento de palabras significativas entre dos textos -- heurística de texto, nada de LLM."""
    if not texto_a or not texto_b:
        return False
    return len(_palabras_significativas(texto_a) & _palabras_significativas(texto_b)) >= minimo


def explicar_coincidencia(
    texto_licitacion: str,
    pais_licitacion: str,
    empresa: dict,
    referencias_empresa: list = None,
) -> list:
    """
    Devuelve una lista de frases (bullets) explicando por qué esta
    empresa encaja con la licitación, basada en datos concretos:
      - nivel de similitud semántica
      - proyectos tipo de la empresa que coinciden con el objeto de la licitación
      - descripción de actividad de la empresa frente a la licitación
      - preferencias de licitación declaradas por la empresa
      - coincidencia de país / zona geográfica
      - palabras clave de la empresa presentes en el texto de la licitación
      - licitaciones antiguas parecidas a las que ya se ha presentado la empresa
      - sector/subsector, como contexto
    """
    motivos = []
    texto_norm = _normalizar(texto_licitacion)

    similarity = empresa.get("similarity")
    if similarity is not None:
        pct = round(similarity * 100, 1)
        if similarity >= 0.35:
            nivel = "muy alta"
        elif similarity >= 0.25:
            nivel = "alta"
        elif similarity >= 0.18:
            nivel = "media"
        else:
            nivel = "orientativa"
        motivos.append(f"Afinidad semántica {nivel} con el objeto de la licitación ({pct}%).")

    proyectos_coincidentes = [
        p for p in (empresa.get("proyectos_tipo") or [])
        if p and _hay_solapamiento(p, texto_licitacion, minimo=1)
    ]
    if proyectos_coincidentes:
        motivos.append(
            "Coincide con proyectos tipo ya realizados por la empresa: "
            + "; ".join(proyectos_coincidentes[:3]) + "."
        )

    if empresa.get("descripcion_actividad") and _hay_solapamiento(empresa["descripcion_actividad"], texto_licitacion, minimo=2):
        motivos.append("La actividad habitual de la empresa coincide con el objeto de la licitación.")

    if empresa.get("preferencias_licitaciones") and _hay_solapamiento(empresa["preferencias_licitaciones"], texto_licitacion, minimo=1):
        motivos.append(f"Encaja con las preferencias de licitación declaradas por la empresa: {empresa['preferencias_licitaciones']}.")

    paises_empresa = set(_normalizar(p) for p in (empresa.get("experiencia_paises") or []))
    paises_interes_empresa = set(_normalizar(p) for p in (empresa.get("paises_interes") or []))
    zonas_empresa = set(_normalizar(z) for z in (empresa.get("zona_geografica_interes") or []))
    ambito_geografico_norm = _normalizar(empresa.get("ambito_geografico") or "")
    if pais_licitacion:
        pais_norm = _normalizar(pais_licitacion)
        if pais_norm in paises_empresa:
            motivos.append(f"Experiencia previa acreditada en {pais_licitacion}.")
        elif pais_norm in paises_interes_empresa:
            motivos.append(f"La empresa tiene interés declarado en {pais_licitacion}.")
        elif any(pais_norm in z or z in pais_norm for z in zonas_empresa):
            motivos.append(f"La empresa tiene interés en la zona geográfica de {pais_licitacion}.")
        elif ambito_geografico_norm and pais_norm in ambito_geografico_norm:
            motivos.append(f"El ámbito geográfico de operación de la empresa menciona {pais_licitacion}.")

    palabras_coincidentes = [
        palabra for palabra in (empresa.get("palabras_clave") or [])
        if palabra and _normalizar(palabra) in texto_norm
    ]
    if palabras_coincidentes:
        motivos.append("Palabras clave de la empresa presentes en la licitación: " + ", ".join(palabras_coincidentes) + ".")

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
            titulo_recortado = titulo_referencia if len(titulo_referencia) <= 100 else titulo_referencia[:100] + "…"
            motivos.append(f"Ya se presentó a una licitación similar: \"{titulo_recortado}\"{sufijo}.")
            break  # una sola referencia antigua basta como señal; evita repetir el mismo motivo

    if empresa.get("sector"):
        detalle_sector = empresa["sector"]
        if empresa.get("subsector"):
            detalle_sector += f" ({empresa['subsector']})"
        motivos.append(f"Sector de actividad: {detalle_sector}.")

    if not motivos:
        motivos.append("Coincidencia detectada por similitud semántica general del perfil de la empresa.")

    return motivos


def construir_comparacion_lugares(pais_licitacion: str, empresa: dict) -> pd.DataFrame:
    """
    Tabla visual: lugar de la licitación frente a los 4 campos
    geográficos de la empresa ya definidos en el Directorio de Empresas
    (Experiencia países, Zona geográfica de interés, Países de interés,
    Ámbito geográfico) -- para ver de un vistazo si conviene por
    ubicación, no solo por afinidad semántica.
    """
    pais_norm = _normalizar(pais_licitacion) if pais_licitacion else ""

    def _fila(etiqueta, valor):
        if isinstance(valor, list):
            texto = ", ".join(v for v in valor if v)
            valores_norm = [_normalizar(v) for v in valor if v]
            coincide = bool(pais_norm) and any(
                pais_norm == v or pais_norm in v or v in pais_norm for v in valores_norm
            )
        else:
            texto = valor or ""
            coincide = bool(pais_norm) and bool(texto) and pais_norm in _normalizar(texto)

        if not texto:
            estado = "Sin datos"
        elif coincide:
            estado = "Coincide"
        else:
            estado = "No coincide"

        return {"Campo de la empresa": etiqueta, "Valor declarado": texto or "No especificado", "Respecto al lugar de la licitación": estado}

    filas = [
        _fila("Experiencia países", empresa.get("experiencia_paises") or []),
        _fila("Zona geográfica de interés", empresa.get("zona_geografica_interes") or []),
        _fila("Países de interés", empresa.get("paises_interes") or []),
        _fila("Ámbito geográfico", empresa.get("ambito_geografico")),
    ]
    return pd.DataFrame(filas)


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
                        motivos = explicar_coincidencia(texto_licitacion, pais_licitacion, empresa, referencias_empresa)
                        for motivo in motivos:
                            st.markdown(f"- {motivo}")

                        st.caption(f"Lugar de la licitación: {pais_licitacion or 'No especificado'}")
                        st.dataframe(
                            construir_comparacion_lugares(pais_licitacion, empresa),
                            hide_index=True,
                            use_container_width=True,
                        )

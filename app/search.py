"""
search.py
---------
Lógica de datos de la Pestaña 1 (Buscador de Licitaciones). Sigue el
mismo patrón que el resto de apps de esta familia: la función RPC
`buscar_licitaciones_internacionales` hace la búsqueda semántica; el
listado plano (sin texto de búsqueda) se resuelve con una consulta
paginada directa a la tabla.
"""
import pandas as pd
import streamlit as st
from sentence_transformers import SentenceTransformer
from supabase import Client

COLUMNAS_LISTADO = (
    "codigo_unico, fuente_origen, tipo_aviso, titulo, descripcion, pais, "
    "organismo, categoria, url_oficial, fecha_publicacion, fecha_limite, "
    "es_novedad, es_actualizada"
)
TAMANO_LOTE = 1000


def buscar_semantica(
    supabase: Client,
    encoder: SentenceTransformer,
    texto: str,
    match_threshold: float = 0.2,
    match_count: int = 500,
) -> list:
    vector_query = encoder.encode(f"query: {texto.strip()}").tolist()
    respuesta = supabase.rpc(
        "buscar_licitaciones_internacionales",
        {
            "query_embedding": vector_query,
            "match_threshold": match_threshold,
            "match_count": match_count,
        },
    ).execute()
    return respuesta.data or []


def listar_todas(supabase: Client, fuente: str = None) -> list:
    resultados, inicio = [], 0
    while True:
        consulta = (
            supabase.table("licitaciones_internacionales")
            .select(COLUMNAS_LISTADO)
            .order("fecha_publicacion", desc=True)
        )
        if fuente:
            consulta = consulta.eq("fuente_origen", fuente)
        respuesta = consulta.range(inicio, inicio + TAMANO_LOTE - 1).execute()
        filas = respuesta.data
        if not filas:
            break
        resultados.extend(filas)
        if len(filas) < TAMANO_LOTE:
            break
        inicio += TAMANO_LOTE
    return resultados


@st.cache_data(ttl=1800, show_spinner=False)
def obtener_paises_disponibles(_supabase: Client) -> list:
    """Opciones del filtro de país, calculadas a partir de los datos reales."""
    respuesta = _supabase.table("licitaciones_internacionales").select("pais").execute()
    paises = {f["pais"] for f in (respuesta.data or []) if f.get("pais")}
    return sorted(paises)


def formatear_para_tabla(resultados: list) -> pd.DataFrame:
    if not resultados:
        return pd.DataFrame()
    df = pd.DataFrame(resultados)
    return pd.DataFrame({
        "Título": df["titulo"],
        "País": df.get("pais", pd.Series(dtype=str)).fillna("No especificado"),
        "Fuente": df["fuente_origen"],
        "Publicación": df.get("fecha_publicacion", pd.Series(dtype=str)).fillna("No especificada"),
        "Cierre": df.get("fecha_limite", pd.Series(dtype=str)).fillna("No especificado"),
        "Enlace": df["url_oficial"],
    })


# ------------------------------------------------------------------
# Interfaz de la Pestaña 1
# ------------------------------------------------------------------
def render_tab1(supabase: Client, encoder: SentenceTransformer):
    from config import FUENTES_LICITACIONES

    st.subheader("Buscador de Licitaciones")
    st.caption("Fuente activa en esta primera versión: Banco Africano de Desarrollo (AfDB).")

    col_texto, col_pais, col_fuente = st.columns([3, 1.5, 1.2])

    with col_texto:
        consulta_texto = st.text_input(
            "Búsqueda en lenguaje natural",
            placeholder="ej. perforación de pozos de agua, construcción de carreteras...",
            key="tab1_consulta_texto",
        )
    with col_pais:
        paises_disponibles = obtener_paises_disponibles(supabase)
        filtro_pais = st.multiselect("País", paises_disponibles, key="tab1_filtro_pais")
    with col_fuente:
        filtro_fuente = st.selectbox("Fuente", ["Todas"] + FUENTES_LICITACIONES, key="tab1_filtro_fuente")

    buscar_click = st.button("Buscar licitaciones", key="tab1_buscar", use_container_width=True)

    if "tab1_resultados" not in st.session_state:
        st.session_state.tab1_resultados = None

    if buscar_click:
        with st.spinner("Buscando..."):
            if consulta_texto.strip():
                resultados = buscar_semantica(supabase, encoder, consulta_texto)
            else:
                resultados = listar_todas(supabase)

            if filtro_pais:
                resultados = [r for r in resultados if r.get("pais") in filtro_pais]
            if filtro_fuente != "Todas":
                resultados = [r for r in resultados if r.get("fuente_origen") == filtro_fuente]

            st.session_state.tab1_resultados = resultados

    resultados = st.session_state.tab1_resultados
    if resultados is None:
        st.info("Define tu búsqueda y filtros, y pulsa **Buscar licitaciones**.")
    elif not resultados:
        st.warning("No se han encontrado licitaciones que coincidan con la búsqueda y los filtros indicados.")
    else:
        st.success(f"Se han encontrado **{len(resultados)}** licitaciones.")
        tabla = formatear_para_tabla(resultados)
        st.dataframe(
            tabla,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Enlace": st.column_config.LinkColumn("Enlace", display_text="Ver convocatoria"),
            },
        )

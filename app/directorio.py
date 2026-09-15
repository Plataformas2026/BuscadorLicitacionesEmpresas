"""
directorio.py
-------------
Pestaña 3: Directorio y Visualización de Empresas.

- Tarjetas: nombre (enlaza a la web de la empresa) + los 4 campos
  geográficos obligatorios (Experiencia países, Zona geográfica de
  interés, Países de interés, Ámbito geográfico) + botón "Ver ficha".
- Ficha: columna izquierda con TODO el contenido del Excel en formato de
  lista con viñetas, tal cual (desde `datos_excel`, no desde los campos
  ya troceados); columna derecha con la tabla de referencias de esa
  empresa (SECTOR, TIPO PROYECTO, AGENCIA EJECUTORA, TITULO, FECHA,
  IMPORTE, RESULTADO).
- Filtros: únicamente ID, SECTOR, SUBSECTOR, tipo de empresa (pública/
  clúster/asociación/privada) y CNAE -- con sus opciones calculadas a
  partir de lo que de verdad hay en la tabla.
- Búsqueda semántica: detecta el idioma de la consulta (español, inglés,
  francés o portugués -- con `langdetect`, sin ninguna API de pago) y
  compara contra el embedding de ESE idioma en Supabase (ver
  `buscar_empresas_directorio` en sql/schema.sql).
"""
import pandas as pd
import streamlit as st
from sentence_transformers import SentenceTransformer
from supabase import Client

IDIOMAS_SOPORTADOS = {"es", "en", "fr", "pt"}

COLUMNAS_TARJETA = (
    "id_empresa, numero_interno, nombre_empresa, sector, subsector, tipo_empresa, "
    "web, experiencia_paises, zona_geografica_interes, paises_interes, ambito_geografico"
)

# Columnas que NO se muestran en el volcado de la ficha (son de uso
# interno: identificadores técnicos, vectores de embedding, marcas de
# tiempo...). Todo lo demás de `datos_excel` se muestra tal cual venga.
CAMPOS_TECNICOS_OCULTOS = {
    "id", "embedding", "embedding_en", "embedding_fr", "embedding_pt",
    "texto_completo", "texto_completo_en", "texto_completo_fr", "texto_completo_pt",
    "fecha_alta", "fecha_actualizacion", "datos_excel",
}


# ------------------------------------------------------------------
# Datos
# ------------------------------------------------------------------
@st.cache_data(ttl=1800, show_spinner=False)
def obtener_opciones_filtro(_supabase: Client) -> dict:
    """
    Opciones EXCLUSIVAMENTE para ID / Sector / Subsector / Tipo de
    empresa / CNAE, calculadas a partir de los datos reales (no hay una
    lista cerrada de antemano, sobre todo para CNAE).
    """
    respuesta = _supabase.rpc("obtener_opciones_filtro_empresas", {}).execute()
    fila = (respuesta.data or [{}])[0] if respuesta.data else {}

    respuesta_ids = _supabase.table("empresas").select("id_empresa").order("id_empresa").execute()

    return {
        "ids": [f["id_empresa"] for f in (respuesta_ids.data or [])],
        "sectores": fila.get("sectores") or [],
        "subsectores": fila.get("subsectores") or [],
        "tipos": fila.get("tipos") or [],
        "cnaes": fila.get("cnaes") or [],
    }


def listar_empresas(
    supabase: Client,
    id_empresa: str = None,
    sector: str = None,
    subsector: str = None,
    tipo: str = None,
    cnae: str = None,
) -> list:
    consulta = supabase.table("empresas").select(COLUMNAS_TARJETA)
    if id_empresa:
        consulta = consulta.eq("id_empresa", id_empresa)
    if sector:
        consulta = consulta.eq("sector", sector)
    if subsector:
        consulta = consulta.eq("subsector", subsector)
    if tipo:
        consulta = consulta.eq("tipo_empresa", tipo)
    if cnae:
        consulta = consulta.eq("cnae", cnae)
    respuesta = consulta.order("nombre_empresa").execute()
    return respuesta.data or []


def detectar_idioma(texto: str) -> str:
    """Español por defecto si no se puede detectar o el idioma no es uno de los soportados."""
    from langdetect import LangDetectException, detect

    try:
        idioma = detect(texto)
    except LangDetectException:
        return "es"
    return idioma if idioma in IDIOMAS_SOPORTADOS else "es"


def buscar_semantica_empresas(
    supabase: Client,
    encoder: SentenceTransformer,
    texto: str,
    match_threshold: float = 0.15,
    match_count: int = 200,
):
    """Busca por DESCRIPCIÓN + PROYECTOS TIPO + PALABRAS CLAVE (ver sql/schema.sql). Devuelve (resultados, idioma_detectado)."""
    idioma = detectar_idioma(texto)
    vector_query = encoder.encode(f"query: {texto.strip()}").tolist()
    respuesta = supabase.rpc(
        "buscar_empresas_directorio",
        {
            "query_embedding": vector_query,
            "idioma": idioma,
            "match_threshold": match_threshold,
            "match_count": match_count,
        },
    ).execute()
    return respuesta.data or [], idioma


def obtener_ficha_empresa(supabase: Client, id_empresa: str) -> dict:
    respuesta = supabase.table("empresas").select("*").eq("id_empresa", id_empresa).limit(1).execute()
    datos = respuesta.data or []
    return datos[0] if datos else {}


def obtener_referencias_empresa(supabase: Client, id_empresa: str) -> list:
    respuesta = supabase.rpc("obtener_referencias_empresa", {"id_empresa_buscado": id_empresa}).execute()
    return respuesta.data or []


# ------------------------------------------------------------------
# Utilidades de presentación
# ------------------------------------------------------------------
def _normalizar_url(web) -> str:
    if not web:
        return None
    web = str(web).strip().splitlines()[0].strip()  # por si hay más de una URL en la celda
    if not web:
        return None
    if not web.lower().startswith(("http://", "https://")):
        web = "https://" + web
    return web


def _resumen_campo(valor, max_chars: int = 100) -> str:
    if valor is None:
        return "No especificado"
    if isinstance(valor, list):
        if not valor:
            return "No especificado"
        texto = ", ".join(str(v) for v in valor)
    else:
        texto = str(valor).strip()
        if not texto:
            return "No especificado"
    if len(texto) > max_chars:
        return texto[:max_chars].rstrip() + "…"
    return texto


# ------------------------------------------------------------------
# Tarjeta de empresa
# ------------------------------------------------------------------
def _renderizar_tarjeta(empresa: dict):
    with st.container(border=True):
        nombre = empresa.get("nombre_empresa") or "Empresa sin nombre"
        url_web = _normalizar_url(empresa.get("web"))
        if url_web:
            st.markdown(f"#### [{nombre}]({url_web})")
        else:
            st.markdown(f"#### {nombre}")

        subtitulo = " · ".join(filter(None, [empresa.get("sector"), empresa.get("tipo_empresa")]))
        if subtitulo:
            st.caption(subtitulo)

        st.markdown(f"**Experiencia países:** {_resumen_campo(empresa.get('experiencia_paises'))}")
        st.markdown(f"**Zona geográfica de interés:** {_resumen_campo(empresa.get('zona_geografica_interes'))}")
        st.markdown(f"**Países de interés:** {_resumen_campo(empresa.get('paises_interes'))}")
        st.markdown(f"**Ámbito geográfico:** {_resumen_campo(empresa.get('ambito_geografico'))}")

        if st.button("Ver ficha", key=f"tab3_ver_{empresa['id_empresa']}", use_container_width=True):
            st.session_state.tab3_empresa_seleccionada = empresa["id_empresa"]
            st.rerun()


# ------------------------------------------------------------------
# Ficha de la empresa (dos columnas)
# ------------------------------------------------------------------
def _mostrar_ficha(supabase: Client, id_empresa: str):
    empresa = obtener_ficha_empresa(supabase, id_empresa)
    if not empresa:
        st.warning("No se ha encontrado esta empresa (puede que se haya retirado en la última sincronización).")
        if st.button("← Volver al directorio", key="tab3_volver_sin_ficha"):
            st.session_state.tab3_empresa_seleccionada = None
            st.rerun()
        return

    if st.button("← Volver al directorio", key="tab3_cerrar_ficha"):
        st.session_state.tab3_empresa_seleccionada = None
        st.rerun()

    st.markdown(f"## {empresa.get('nombre_empresa') or 'Empresa sin nombre'}")
    url_web = _normalizar_url(empresa.get("web"))
    pie = f"ID: `{empresa.get('id_empresa')}` · Nº interno: `{empresa.get('numero_interno')}`"
    if url_web:
        pie += f" · [{url_web}]({url_web})"
    st.caption(pie)

    col_izq, col_der = st.columns([3, 2])

    with col_izq:
        st.markdown("#### Información completa (tal cual en el Excel)")
        datos_excel = empresa.get("datos_excel") or {}
        huecos = 0
        for cabecera, valor in datos_excel.items():
            if valor is None:
                huecos += 1
                continue
            if isinstance(valor, str) and not valor.strip():
                huecos += 1
                continue
            st.markdown(f"- **{cabecera}:** {valor}")
        if huecos:
            st.caption(f"({huecos} campos de esta empresa estaban vacíos en el Excel y no se muestran)")

    with col_der:
        st.markdown("#### Referencias de licitaciones")
        referencias = obtener_referencias_empresa(supabase, id_empresa)
        if not referencias:
            st.info("No hay referencias registradas para esta empresa en la hoja de referencias del Excel.")
        else:
            tabla = pd.DataFrame([{
                "SECTOR": r.get("sector") or "",
                "TIPO PROYECTO": r.get("tipo_proyecto") or "",
                "AGENCIA EJECUTORA": r.get("agencia_ejecutora") or "",
                "TITULO": r.get("titulo") or "",
                "FECHA": r.get("fecha") or "",
                "IMPORTE": r.get("importe") or "",
                "RESULTADO": r.get("resultado") or "",
            } for r in referencias])
            st.caption(f"{len(referencias)} referencias")
            st.dataframe(tabla, use_container_width=True, hide_index=True, height=500)


# ------------------------------------------------------------------
# Interfaz de la Pestaña 3
# ------------------------------------------------------------------
def render_tab3(supabase: Client, encoder: SentenceTransformer):
    st.subheader("Directorio y Visualización de Empresas")

    for clave, valor in (
        ("tab3_empresa_seleccionada", None),
        ("tab3_resultados_busqueda", None),
        ("tab3_idioma_detectado", None),
    ):
        if clave not in st.session_state:
            st.session_state[clave] = valor

    # Si hay una empresa seleccionada, se muestra SOLO la ficha (más
    # legible que apilarla encima del listado de tarjetas).
    if st.session_state.tab3_empresa_seleccionada:
        _mostrar_ficha(supabase, st.session_state.tab3_empresa_seleccionada)
        return

    opciones = obtener_opciones_filtro(supabase)

    consulta_texto = st.text_input(
        "Búsqueda semántica (español, inglés, francés o portugués)",
        placeholder="ej. gestión del agua en África · water management projects · projets d'énergies renouvelables · gestão de resíduos",
        key="tab3_busqueda_semantica",
    )

    col_id, col_sector, col_subsector, col_tipo, col_cnae = st.columns(5)
    with col_id:
        filtro_id = st.selectbox("ID", ["Todos"] + opciones["ids"], key="tab3_filtro_id")
    with col_sector:
        filtro_sector = st.selectbox("Sector", ["Todos"] + opciones["sectores"], key="tab3_filtro_sector")
    with col_subsector:
        filtro_subsector = st.selectbox("Subsector", ["Todos"] + opciones["subsectores"], key="tab3_filtro_subsector")
    with col_tipo:
        filtro_tipo = st.selectbox(
            "Empresa pública / clúster / asociación / privada",
            ["Todos"] + opciones["tipos"],
            key="tab3_filtro_tipo",
        )
    with col_cnae:
        filtro_cnae = st.selectbox("CNAE", ["Todos"] + opciones["cnaes"], key="tab3_filtro_cnae")

    buscar_click = st.button("Buscar empresas", key="tab3_buscar", use_container_width=True)

    if buscar_click:
        with st.spinner("Buscando..."):
            if consulta_texto.strip():
                resultados, idioma = buscar_semantica_empresas(supabase, encoder, consulta_texto)
                st.session_state.tab3_idioma_detectado = idioma
            else:
                resultados = listar_empresas(
                    supabase,
                    id_empresa=None if filtro_id == "Todos" else filtro_id,
                    sector=None if filtro_sector == "Todos" else filtro_sector,
                    subsector=None if filtro_subsector == "Todos" else filtro_subsector,
                    tipo=None if filtro_tipo == "Todos" else filtro_tipo,
                    cnae=None if filtro_cnae == "Todos" else filtro_cnae,
                )
                st.session_state.tab3_idioma_detectado = None

            # Con texto de búsqueda, los filtros se aplican COMO REFINAMIENTO
            # sobre los resultados semánticos (para poder combinar ambos).
            if consulta_texto.strip():
                if filtro_id != "Todos":
                    resultados = [r for r in resultados if r.get("id_empresa") == filtro_id]
                if filtro_sector != "Todos":
                    resultados = [r for r in resultados if r.get("sector") == filtro_sector]
                if filtro_subsector != "Todos":
                    resultados = [r for r in resultados if r.get("subsector") == filtro_subsector]
                if filtro_tipo != "Todos":
                    resultados = [r for r in resultados if r.get("tipo_empresa") == filtro_tipo]
                if filtro_cnae != "Todos":
                    resultados = [r for r in resultados if r.get("cnae") == filtro_cnae]

            st.session_state.tab3_resultados_busqueda = resultados

    resultados = st.session_state.tab3_resultados_busqueda

    if resultados is None:
        st.info("Escribe una búsqueda y/o elige filtros, y pulsa **Buscar empresas**.")
        return

    if st.session_state.tab3_idioma_detectado:
        nombres_idioma = {"es": "español", "en": "inglés", "fr": "francés", "pt": "portugués"}
        st.caption(f"Idioma detectado en la búsqueda: {nombres_idioma.get(st.session_state.tab3_idioma_detectado, st.session_state.tab3_idioma_detectado)}")

    if not resultados:
        st.warning("No se han encontrado empresas que coincidan con la búsqueda y los filtros indicados.")
        return

    st.success(f"{len(resultados)} empresas encontradas")

    columnas = st.columns(3)
    for indice, empresa in enumerate(resultados):
        with columnas[indice % 3]:
            _renderizar_tarjeta(empresa)

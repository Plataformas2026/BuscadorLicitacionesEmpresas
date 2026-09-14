"""
directorio.py
-------------
Lógica de datos de la Pestaña 3 (Directorio y Visualización de Empresas).
Los filtros (sector, subsector, tipo, país de experiencia) se calculan de
forma dinámica a partir de lo que de verdad hay en la tabla `empresas` --
igual que se hace en otras apps de esta familia -- porque no hay una
taxonomía cerrada garantizada de antemano.
"""
import streamlit as st
from supabase import Client

COLUMNAS_TARJETA = (
    "id_empresa, nombre_empresa, sector, subsector, tipo_empresa, tamano, web"
)

COLUMNAS_FICHA_COMPLETA = (
    "numero_interno, id_empresa, nombre_empresa, sector, subsector, "
    "tipo_empresa, cif, web, descripcion_actividad, palabras_clave, "
    "proyectos_tipo, experiencia_paises, zona_geografica, tamano, "
    "facturacion_anual, contacto_nombre, contacto_cargo, contacto_email"
)


@st.cache_data(ttl=1800, show_spinner=False)
def obtener_opciones_filtro(_supabase: Client) -> dict:
    respuesta = _supabase.table("empresas").select(
        "sector, subsector, tipo_empresa, experiencia_paises"
    ).execute()
    filas = respuesta.data or []

    return {
        "sectores": sorted({f["sector"] for f in filas if f.get("sector")}),
        "subsectores": sorted({f["subsector"] for f in filas if f.get("subsector")}),
        "tipos": sorted({f["tipo_empresa"] for f in filas if f.get("tipo_empresa")}),
        "paises": sorted({p for f in filas for p in (f.get("experiencia_paises") or [])}),
    }


def listar_empresas(
    supabase: Client,
    sectores: list = None,
    subsectores: list = None,
    tipos: list = None,
    paises: list = None,
    texto_libre: str = "",
) -> list:
    consulta = supabase.table("empresas").select(COLUMNAS_TARJETA)

    if sectores:
        consulta = consulta.in_("sector", sectores)
    if subsectores:
        consulta = consulta.in_("subsector", subsectores)
    if tipos:
        consulta = consulta.in_("tipo_empresa", tipos)
    if paises:
        consulta = consulta.overlaps("experiencia_paises", paises)
    if texto_libre.strip():
        patron = f"%{texto_libre.strip()}%"
        consulta = consulta.or_(f"nombre_empresa.ilike.{patron},descripcion_actividad.ilike.{patron}")

    respuesta = consulta.order("nombre_empresa").execute()
    return respuesta.data or []


def obtener_ficha_empresa(supabase: Client, id_empresa: str) -> dict:
    respuesta = (
        supabase.table("empresas")
        .select(COLUMNAS_FICHA_COMPLETA)
        .eq("id_empresa", id_empresa)
        .limit(1)
        .execute()
    )
    datos = respuesta.data or []
    return datos[0] if datos else {}


# ------------------------------------------------------------------
# Interfaz de la Pestaña 3
# ------------------------------------------------------------------
def _renderizar_lista(valores: list) -> str:
    return ", ".join(valores) if valores else "No especificado"


def _mostrar_ficha(empresa: dict):
    with st.container(border=True):
        col_cerrar, _ = st.columns([1, 5])
        with col_cerrar:
            if st.button("✕ Cerrar ficha", key="tab3_cerrar_ficha"):
                st.session_state.tab3_empresa_seleccionada = None
                st.rerun()

        st.markdown(f"### {empresa.get('nombre_empresa') or 'Empresa sin nombre'}")
        st.caption(f"ID interno: `{empresa.get('id_empresa')}`  ·  Nº: {empresa.get('numero_interno')}")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown(f"**Sector:** {empresa.get('sector') or 'No especificado'}")
            st.markdown(f"**Subsector:** {empresa.get('subsector') or 'No especificado'}")
            st.markdown(f"**Tipo de empresa:** {empresa.get('tipo_empresa') or 'No especificado'}")
            st.markdown(f"**CIF:** {empresa.get('cif') or 'No especificado'}")
            st.markdown(f"**Web:** {empresa.get('web') or 'No especificada'}")
            st.markdown(f"**Tamaño:** {empresa.get('tamano') or 'No especificado'}")
            st.markdown(f"**Facturación anual:** {empresa.get('facturacion_anual') or 'No especificada'}")
        with col2:
            st.markdown(f"**Experiencia en países:** {_renderizar_lista(empresa.get('experiencia_paises'))}")
            st.markdown(f"**Zona geográfica:** {_renderizar_lista(empresa.get('zona_geografica'))}")
            st.markdown(f"**Proyectos tipo:** {_renderizar_lista(empresa.get('proyectos_tipo'))}")
            st.markdown(f"**Palabras clave:** {_renderizar_lista(empresa.get('palabras_clave'))}")
            st.markdown("**Contacto:**")
            st.markdown(
                f"{empresa.get('contacto_nombre') or 'No especificado'}"
                f"{' — ' + empresa['contacto_cargo'] if empresa.get('contacto_cargo') else ''}"
            )
            if empresa.get("contacto_email"):
                st.markdown(f"✉️ {empresa['contacto_email']}")

        st.markdown("**Descripción de la actividad:**")
        st.write(empresa.get("descripcion_actividad") or "No especificada")


def render_tab3(supabase: Client):
    from config import TIPOS_EMPRESA

    st.subheader("🏢 Directorio y Visualización de Empresas")

    if "tab3_empresa_seleccionada" not in st.session_state:
        st.session_state.tab3_empresa_seleccionada = None

    opciones = obtener_opciones_filtro(supabase)

    col_sector, col_subsector, col_tipo, col_pais = st.columns(4)
    with col_sector:
        filtro_sector = st.multiselect("Sector", opciones["sectores"], key="tab3_filtro_sector")
    with col_subsector:
        filtro_subsector = st.multiselect("Subsector", opciones["subsectores"], key="tab3_filtro_subsector")
    with col_tipo:
        filtro_tipo = st.multiselect("Tipo de empresa", TIPOS_EMPRESA, key="tab3_filtro_tipo")
    with col_pais:
        filtro_pais = st.multiselect("Experiencia en país", opciones["paises"], key="tab3_filtro_pais")

    texto_libre = st.text_input("Buscar por nombre o actividad", key="tab3_texto_libre")

    # Ficha de detalle (si hay una empresa seleccionada, se muestra primero)
    if st.session_state.tab3_empresa_seleccionada:
        ficha = obtener_ficha_empresa(supabase, st.session_state.tab3_empresa_seleccionada)
        if ficha:
            _mostrar_ficha(ficha)
        st.divider()

    empresas = listar_empresas(
        supabase,
        sectores=filtro_sector,
        subsectores=filtro_subsector,
        tipos=filtro_tipo,
        paises=filtro_pais,
        texto_libre=texto_libre,
    )

    if not empresas:
        st.info("No hay empresas que coincidan con los filtros seleccionados.")
        return

    st.caption(f"{len(empresas)} empresas encontradas")

    # Agrupar por sector cuando el usuario ha seleccionado varios sectores
    # a la vez (así se ve claramente qué empresas caen en cada uno).
    if len(filtro_sector) > 1:
        grupos = {}
        for empresa in empresas:
            grupos.setdefault(empresa.get("sector") or "Sin sector", []).append(empresa)
    else:
        grupos = {None: empresas}

    for nombre_grupo, empresas_grupo in grupos.items():
        if nombre_grupo:
            st.markdown(f"#### {nombre_grupo}")

        columnas = st.columns(3)
        for indice, empresa in enumerate(empresas_grupo):
            with columnas[indice % 3]:
                st.markdown(
                    f"""
                    <div class="tarjeta">
                        <div class="tarjeta-titulo">{empresa.get('nombre_empresa') or 'Sin nombre'}</div>
                        <div class="tarjeta-subtitulo">{empresa.get('sector') or 'Sector no especificado'}</div>
                        <span class="chip">{empresa.get('tipo_empresa') or 'N/D'}</span>
                        <span class="chip">{empresa.get('tamano') or 'Tamaño N/D'}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                if st.button("Ver ficha →", key=f"tab3_ver_{empresa['id_empresa']}", use_container_width=True):
                    st.session_state.tab3_empresa_seleccionada = empresa["id_empresa"]
                    st.rerun()

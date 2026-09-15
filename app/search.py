"""
search.py
---------
Pestaña 1: Buscador de Licitaciones.

Filtros EXCLUSIVAMENTE: búsqueda en lenguaje natural, Fuente, Lugar
(calculado a partir de los datos reales) y Fecha de cierre.

Sistema "Visto"/"Guardado": única parte de la app con permiso de
escritura desde el navegador (con la clave anónima) -- ver sql/schema.sql
sección 5, que concede UPDATE SOLO sobre las columnas
visto/visto_en/guardado/guardado_en, nunca sobre el resto de la fila.
No hay login en la app, así que este estado es COMPARTIDO por todo el
que la use, no por usuario individual.

  - "Visto": desaparece de la vista principal "Buscar" y pasa a la
    sección "Vistas"; ingest/limpiar_licitaciones_vistas.py la borra
    a los 3 días de marcarse, SALVO que también esté "Guardada".
  - "Guardado": pasa a "Favoritos" de forma permanente, nunca se borra
    automáticamente.
"""
from datetime import date, datetime, timezone

import streamlit as st
from sentence_transformers import SentenceTransformer
from supabase import Client

COLUMNAS_LISTADO = (
    "codigo_unico, fuente_origen, tipo_aviso, titulo, descripcion, pais, "
    "organismo, categoria, url_oficial, url_documento, fecha_publicacion, "
    "fecha_limite, es_novedad, es_actualizada, visto, visto_en, guardado, guardado_en"
)
TAMANO_LOTE = 1000


# ------------------------------------------------------------------
# Datos
# ------------------------------------------------------------------
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


def listar_todas(supabase: Client) -> list:
    resultados, inicio = [], 0
    while True:
        respuesta = (
            supabase.table("licitaciones_internacionales")
            .select(COLUMNAS_LISTADO)
            .order("fecha_publicacion", desc=True)
            .range(inicio, inicio + TAMANO_LOTE - 1)
            .execute()
        )
        filas = respuesta.data
        if not filas:
            break
        resultados.extend(filas)
        if len(filas) < TAMANO_LOTE:
            break
        inicio += TAMANO_LOTE
    return resultados


@st.cache_data(ttl=1800, show_spinner=False)
def obtener_lugares_disponibles(_supabase: Client) -> list:
    """Opciones del filtro "Lugar", calculadas a partir de los datos reales (columna `pais`)."""
    respuesta = _supabase.table("licitaciones_internacionales").select("pais").execute()
    lugares = {f["pais"] for f in (respuesta.data or []) if f.get("pais")}
    return sorted(lugares)


def marcar_visto(supabase: Client, codigo_unico: str, valor: bool):
    datos = {
        "visto": valor,
        "visto_en": datetime.now(timezone.utc).isoformat() if valor else None,
    }
    supabase.table("licitaciones_internacionales").update(datos).eq("codigo_unico", codigo_unico).execute()


def marcar_guardado(supabase: Client, codigo_unico: str, valor: bool):
    datos = {
        "guardado": valor,
        "guardado_en": datetime.now(timezone.utc).isoformat() if valor else None,
    }
    supabase.table("licitaciones_internacionales").update(datos).eq("codigo_unico", codigo_unico).execute()


def listar_vistos(supabase: Client) -> list:
    respuesta = (
        supabase.table("licitaciones_internacionales")
        .select(COLUMNAS_LISTADO)
        .eq("visto", True)
        .order("visto_en", desc=True)
        .execute()
    )
    return respuesta.data or []


def listar_guardados(supabase: Client) -> list:
    respuesta = (
        supabase.table("licitaciones_internacionales")
        .select(COLUMNAS_LISTADO)
        .eq("guardado", True)
        .order("guardado_en", desc=True)
        .execute()
    )
    return respuesta.data or []


# ------------------------------------------------------------------
# Interfaz
# ------------------------------------------------------------------
def _renderizar_licitacion(supabase: Client, licitacion: dict, contexto: str):
    codigo = licitacion["codigo_unico"]

    with st.container(border=True):
        col_estado, col_info = st.columns([1, 6])

        with col_estado:
            visto_actual = bool(licitacion.get("visto"))
            guardado_actual = bool(licitacion.get("guardado"))

            nuevo_visto = st.checkbox("Visto", value=visto_actual, key=f"{contexto}_visto_{codigo}")
            nuevo_guardado = st.checkbox("Guardado", value=guardado_actual, key=f"{contexto}_guardado_{codigo}")

            if nuevo_visto != visto_actual:
                marcar_visto(supabase, codigo, nuevo_visto)
                # Se actualiza también el propio diccionario en memoria (y,
                # por tanto, la copia que pueda seguir en
                # st.session_state.tab1_resultados de un "Buscar" anterior):
                # si no, en el siguiente rerun `visto_actual` se recalcularía
                # a partir del snapshot desactualizado, el checkbox seguiría
                # "detectando" el mismo cambio, y se entraría en un bucle
                # infinito de reruns (bug real encontrado al probarlo).
                licitacion["visto"] = nuevo_visto
                licitacion["visto_en"] = datetime.now(timezone.utc).isoformat() if nuevo_visto else None
                st.rerun()
            if nuevo_guardado != guardado_actual:
                marcar_guardado(supabase, codigo, nuevo_guardado)
                licitacion["guardado"] = nuevo_guardado
                licitacion["guardado_en"] = datetime.now(timezone.utc).isoformat() if nuevo_guardado else None
                st.rerun()

        with col_info:
            titulo = licitacion.get("titulo") or "Sin título"
            url = licitacion.get("url_oficial")
            st.markdown(f"**[{titulo}]({url})**" if url else f"**{titulo}**")

            detalles = [d for d in [licitacion.get("fuente_origen"), licitacion.get("pais")] if d]
            if licitacion.get("fecha_publicacion"):
                detalles.append(f"Publicación: {licitacion['fecha_publicacion']}")
            detalles.append(f"Cierre: {licitacion.get('fecha_limite') or 'No detectada'}")
            st.caption(" · ".join(detalles))

            descripcion = licitacion.get("descripcion")
            if descripcion:
                st.write(descripcion[:280] + ("…" if len(descripcion) > 280 else ""))


def _vista_buscar(supabase: Client, encoder: SentenceTransformer):
    from config import FUENTES_LICITACIONES

    col_texto, col_fuente, col_lugar, col_cierre = st.columns([3, 1.2, 1.6, 1.6])

    with col_texto:
        consulta_texto = st.text_input(
            "Búsqueda en lenguaje natural",
            placeholder="ej. perforación de pozos de agua, construcción de carreteras...",
            key="tab1_consulta_texto",
        )
    with col_fuente:
        filtro_fuente = st.selectbox("Fuente", ["Todas"] + FUENTES_LICITACIONES, key="tab1_filtro_fuente")
    with col_lugar:
        lugares_disponibles = obtener_lugares_disponibles(supabase)
        filtro_lugar = st.multiselect("Lugar", lugares_disponibles, key="tab1_filtro_lugar")
    with col_cierre:
        filtro_fecha_cierre = st.date_input(
            "Fecha de cierre (a partir de)", value=date.today(), key="tab1_filtro_fecha_cierre"
        )

    buscar_click = st.button("Buscar licitaciones", key="tab1_buscar", use_container_width=True)

    if "tab1_resultados" not in st.session_state:
        st.session_state.tab1_resultados = None

    if buscar_click:
        with st.spinner("Buscando..."):
            if consulta_texto.strip():
                resultados = buscar_semantica(supabase, encoder, consulta_texto)
            else:
                resultados = listar_todas(supabase)

            # Las marcadas como "Visto" nunca aparecen en la vista principal.
            resultados = [r for r in resultados if not r.get("visto")]

            if filtro_fuente != "Todas":
                resultados = [r for r in resultados if r.get("fuente_origen") == filtro_fuente]
            if filtro_lugar:
                resultados = [r for r in resultados if r.get("pais") in filtro_lugar]

            fecha_minima = filtro_fecha_cierre.isoformat()
            resultados = [
                r for r in resultados
                if not r.get("fecha_limite") or r["fecha_limite"] >= fecha_minima
            ]

            st.session_state.tab1_resultados = resultados

    resultados = st.session_state.tab1_resultados
    if resultados is None:
        st.info("Define tu búsqueda y filtros, y pulsa **Buscar licitaciones**.")
        return
    if not resultados:
        st.warning("No se han encontrado licitaciones que coincidan con la búsqueda y los filtros indicados.")
        return

    visibles = [r for r in resultados if not r.get("visto")]
    if not visibles:
        st.warning("No se han encontrado licitaciones que coincidan con la búsqueda y los filtros indicados.")
        return

    st.success(f"Se han encontrado {len(visibles)} licitaciones.")
    for licitacion in visibles:
        # Se filtra por `visto` en cada render (no solo al pulsar "Buscar")
        # para que marcar "Visto" la haga desaparecer al instante, tal como
        # se pide, sin esperar a una nueva búsqueda.
        _renderizar_licitacion(supabase, licitacion, contexto="buscar")


def _vista_vistos(supabase: Client):
    st.markdown("#### Licitaciones vistas")
    st.caption(
        "Se eliminan automáticamente a los 3 días de marcarse como vistas, "
        "salvo que también estén guardadas en Favoritos. Desmarca «Visto» "
        "para que vuelvan a aparecer en el buscador."
    )
    vistos = listar_vistos(supabase)
    if not vistos:
        st.info("No hay licitaciones marcadas como vistas.")
        return
    for licitacion in vistos:
        _renderizar_licitacion(supabase, licitacion, contexto="vistas")


def _vista_guardados(supabase: Client):
    st.markdown("#### Licitaciones guardadas (Favoritos)")
    st.caption("Permanecen aquí indefinidamente hasta que las desmarques.")
    guardados = listar_guardados(supabase)
    if not guardados:
        st.info("No hay licitaciones guardadas todavía.")
        return
    for licitacion in guardados:
        _renderizar_licitacion(supabase, licitacion, contexto="favoritos")


def render_tab1(supabase: Client, encoder: SentenceTransformer):
    st.subheader("Buscador de Licitaciones")
    st.caption("Fuente activa en esta primera versión: Banco Africano de Desarrollo (AfDB).")

    vista = st.radio(
        "Vista",
        ["Buscar", "Vistas", "Favoritos"],
        horizontal=True,
        key="tab1_vista",
        label_visibility="collapsed",
    )

    if vista == "Vistas":
        _vista_vistos(supabase)
    elif vista == "Favoritos":
        _vista_guardados(supabase)
    else:
        _vista_buscar(supabase, encoder)

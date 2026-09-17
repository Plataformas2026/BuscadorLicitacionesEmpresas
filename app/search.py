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

import pandas as pd
import streamlit as st
from sentence_transformers import SentenceTransformer
from supabase import Client

COLUMNAS_LISTADO = (
    "codigo_unico, fuente_origen, tipo_aviso, titulo, descripcion, pais, paises, "
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
# Mismos campos que se venían mostrando antes de pasar a formato tabla:
# Título, Fuente, Lugar, Publicación, Cierre, Enlace -- más las dos
# columnas de casilla "Visto"/"Guardado". "codigo_unico" viaja oculto en
# los datos (column_order la deja fuera de la vista) para poder saber a
# qué fila de Supabase corresponde cada casilla marcada.
def _tabla_licitaciones(supabase: Client, resultados: list, contexto: str):
    if not resultados:
        return

    filas = []
    for r in resultados:
        # Combinar 'pais' y 'paises' para mostrar una cadena limpia y sin duplicados en la tabla
        conjunto_paises = set()
        if r.get("pais") and isinstance(r.get("pais"), str):
            conjunto_paises.add(r.get("pais").strip())
        
        lista_paises = r.get("paises")
        if isinstance(lista_paises, list):
            for p in lista_paises:
                if p and isinstance(p, str) and p.strip():
                    conjunto_paises.add(p.strip())

        texto_lugar = ", ".join(sorted(conjunto_paises)) if conjunto_paises else "No especificado"

        filas.append({
            "codigo_unico": r["codigo_unico"],
            "Visto": bool(r.get("visto")),
            "Guardado": bool(r.get("guardado")),
            "Título": r.get("titulo") or "Sin título",
            "Fuente": r.get("fuente_origen") or "No especificada",
            "Lugar": texto_lugar,
            "Publicación": r.get("fecha_publicacion") or "No especificada",
            "Cierre": r.get("fecha_limite") or "No especificada",
            "Enlace": r.get("url_oficial") or "",
        })

    df_base = pd.DataFrame(filas)

    df_editado = st.data_editor(
        df_base,
        key=f"tab1_editor_{contexto}",
        hide_index=True,
        use_container_width=True,
        column_order=["Visto", "Guardado", "Título", "Fuente", "Lugar", "Publicación", "Cierre", "Enlace"],
        disabled=["Título", "Fuente", "Lugar", "Publicación", "Cierre", "Enlace"],
        column_config={
            "Visto": st.column_config.CheckboxColumn("Visto"),
            "Guardado": st.column_config.CheckboxColumn("Guardado"),
            "Enlace": st.column_config.LinkColumn("Enlace", display_text="Ver convocatoria"),
        },
    )

    # Se compara fila a fila contra el estado ANTES de este render (`df_base`,
    # construido a partir de `resultados`) para detectar qué casillas ha
    # tocado el usuario. Igual que con las tarjetas de la versión anterior:
    # tras aplicar el cambio en Supabase, se actualiza también el propio
    # diccionario en `resultados` (que puede seguir viviendo en
    # st.session_state de una búsqueda anterior) ANTES de llamar a
    # st.rerun() -- si no, en el siguiente rerun se volvería a detectar la
    # misma diferencia y se entraría en un bucle infinito de reruns (bug
    # real ya encontrado y corregido en la versión de tarjetas).
    hubo_cambio = False
    for posicion in df_base.index:
        codigo = df_base.loc[posicion, "codigo_unico"]
        licitacion = next((r for r in resultados if r["codigo_unico"] == codigo), None)
        if licitacion is None:
            continue

        visto_antes = bool(df_base.loc[posicion, "Visto"])
        visto_despues = bool(df_editado.loc[posicion, "Visto"])
        if visto_despues != visto_antes:
            marcar_visto(supabase, codigo, visto_despues)
            licitacion["visto"] = visto_despues
            licitacion["visto_en"] = datetime.now(timezone.utc).isoformat() if visto_despues else None
            hubo_cambio = True

        guardado_antes = bool(df_base.loc[posicion, "Guardado"])
        guardado_despues = bool(df_editado.loc[posicion, "Guardado"])
        if guardado_despues != guardado_antes:
            marcar_guardado(supabase, codigo, guardado_despues)
            licitacion["guardado"] = guardado_despues
            licitacion["guardado_en"] = datetime.now(timezone.utc).isoformat() if guardado_despues else None
            hubo_cambio = True

    if hubo_cambio:
        st.rerun()


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
        filtro_fuente = st.multiselect("Fuente", FUENTES_LICITACIONES, key="tab1_filtro_fuente")
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

            if filtro_fuente:
                resultados = [r for r in resultados if r.get("fuente_origen") in filtro_fuente]
            if filtro_lugar:
                resultados_filtrados = []
                for r in resultados:
                    paises_licitacion = set()
                    if r.get("pais"):
                        paises_licitacion.add(r.get("pais").strip())
                    lista_paises = r.get("paises")
                    if isinstance(lista_paises, list):
                        for p in lista_paises:
                            if p and isinstance(p, str):
                                paises_licitacion.add(p.strip())
                    if any(f in paises_licitacion for f in filtro_lugar):
                        resultados_filtrados.append(r)
                resultados = resultados_filtrados

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
    # Se filtra por `visto` en cada render (no solo al pulsar "Buscar") para
    # que marcar "Visto" la haga desaparecer al instante, sin esperar a una
    # nueva búsqueda.
    _tabla_licitaciones(supabase, visibles, contexto="buscar")


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
    _tabla_licitaciones(supabase, vistos, contexto="vistas")


def _vista_guardados(supabase: Client):
    st.markdown("#### Licitaciones guardadas (Favoritos)")
    st.caption("Permanecen aquí indefinidamente hasta que las desmarques.")
    guardados = listar_guardados(supabase)
    if not guardados:
        st.info("No hay licitaciones guardadas todavía.")
        return
    _tabla_licitaciones(supabase, guardados, contexto="favoritos")


def render_tab1(supabase: Client, encoder: SentenceTransformer):
    st.subheader("Buscador de Licitaciones")
    # st.caption("Fuente activa en esta primera versión: Banco Africano de Desarrollo (AfDB).")

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

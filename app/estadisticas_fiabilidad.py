"""
estadisticas_fiabilidad.py
--------------------------
Pestaña Estadísticas, sección "Fiabilidad de la herramienta".

QUÉ HACE
--------
Cuando una licitación se adjudica a una empresa, el usuario lo registra aquí
y responde a una pregunta: ¿la empresa adjudicada estaba entre las 5 primeras
recomendaciones de la herramienta? Cada respuesta se guarda en la tabla
`fiabilidad_herramienta` (empresa, titulo, bool_acierto) y con ellas se
calcula, por empresa, el % de veces que la herramienta acierta; un carrusel
las ordena de la que más acierta a la que menos, para ver con qué perfiles
hay que afinar las búsquedas.

LA TABLA
--------
Se crea con `sql/migracion_fiabilidad_herramienta.sql` (se ejecuta una vez en
el editor SQL de Supabase). (empresa, titulo) es único: volver a registrar el
mismo caso corrige la respuesta anterior en vez de duplicarla. La app escribe
con la clave anónima, igual que ya hace para marcar licitaciones como vistas o
guardadas. Esta sección no depende de los filtros de la pestaña: habla de la
herramienta, no de los resultados de las empresas.

Si la tabla aún no existe, esta sección muestra cómo crearla y el resto de la
pestaña sigue funcionando.
"""
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from supabase import Client

from estadisticas_comun import ALTURA_CARRUSEL, ancho_completo, construir_html_carrusel, leer_paginado, mostrar_html

TABLA_FIABILIDAD = "fiabilidad_herramienta"
NUM_RECOMENDACIONES = 5
MAX_CARACTERES_TITULO = 500

RESPUESTA_SI = "Sí"
RESPUESTA_NO = "No"

CLAVE_EMPRESA = "tab4_fiab_empresa"
CLAVE_TITULO = "tab4_fiab_titulo"
CLAVE_RESPUESTA = "tab4_fiab_respuesta"
CLAVE_MENSAJE = "tab4_fiab_mensaje"


# ============================================================
# DATOS
# ============================================================

@st.cache_data(ttl=300, show_spinner=False)
def cargar_fiabilidad(_supabase: Client) -> pd.DataFrame:
    filas = leer_paginado(
        lambda: _supabase.table(TABLA_FIABILIDAD).select("empresa, titulo, bool_acierto").order("id")
    )
    df = pd.DataFrame(filas, columns=["empresa", "titulo", "bool_acierto"])
    df["bool_acierto"] = df["bool_acierto"].astype(bool)
    return df


@st.cache_data(ttl=1800, show_spinner=False)
def listar_empresas_dossier(_supabase: Client) -> list:
    """Nombres de las empresas del dossier (las que puede recomendar la herramienta)."""
    filas = leer_paginado(lambda: _supabase.table("empresas").select("nombre_empresa").order("nombre_empresa"))
    return sorted({(f.get("nombre_empresa") or "").strip() for f in filas} - {""}, key=str.casefold)


def limpiar_titulo(titulo) -> str:
    """Quita saltos de línea y espacios sobrantes (si no, el mismo título valdría como casos distintos)."""
    return " ".join(str(titulo or "").split())


def guardar_respuesta(supabase: Client, empresa: str, titulo: str, acierto: bool) -> str:
    """Guarda (o corrige) un caso. Devuelve «guardada» si es nuevo y «actualizada» si ya existía."""
    titulo = limpiar_titulo(titulo)
    existente = (
        supabase.table(TABLA_FIABILIDAD).select("id").eq("empresa", empresa).eq("titulo", titulo).limit(1).execute().data
    )
    supabase.table(TABLA_FIABILIDAD).upsert(
        {
            "empresa": empresa,
            "titulo": titulo,
            "bool_acierto": bool(acierto),
            "actualizado_en": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="empresa,titulo",
    ).execute()
    return "actualizada" if existente else "guardada"


def fiabilidad_por_empresa(datos: pd.DataFrame) -> pd.DataFrame:
    """
    % de acierto por empresa, de mayor a menor (a igual %, antes la que tiene más casos, que es más
    fiable; después, orden alfabético). Columnas: empresa, aciertos, total, pct.
    """
    if datos.empty:
        return pd.DataFrame(columns=["empresa", "aciertos", "total", "pct"])
    agrupado = (
        datos.groupby("empresa")
        .agg(aciertos=("bool_acierto", "sum"), total=("bool_acierto", "size"))
        .reset_index()
    )
    agrupado["aciertos"] = agrupado["aciertos"].astype(int)
    agrupado["pct"] = agrupado["aciertos"] / agrupado["total"] * 100
    return agrupado.sort_values(["pct", "total", "empresa"], ascending=[False, False, True]).reset_index(drop=True)


def _es_tabla_inexistente(error: Exception) -> bool:
    texto = str(error)
    return TABLA_FIABILIDAD in texto and any(
        pista in texto for pista in ("PGRST205", "does not exist", "Could not find", "schema cache", "42P01")
    )


# ============================================================
# FORMULARIO
# ============================================================

def _al_guardar(supabase: Client):
    """
    Callback del botón del formulario. Se ejecuta ANTES de repintar la pantalla, así que puede
    vaciar los campos tras guardar con éxito y dejarlos tal cual si falta algo (el usuario no
    pierde lo que había escrito). El aviso se guarda en session_state y se muestra al repintar.
    """
    empresa = st.session_state.get(CLAVE_EMPRESA)
    titulo = limpiar_titulo(st.session_state.get(CLAVE_TITULO))
    respuesta = st.session_state.get(CLAVE_RESPUESTA)

    if not empresa:
        st.session_state[CLAVE_MENSAJE] = ("error", "Elige la empresa que ha sido adjudicada.")
        return
    if not titulo:
        st.session_state[CLAVE_MENSAJE] = ("error", "Escribe el título de la licitación.")
        return
    if respuesta not in (RESPUESTA_SI, RESPUESTA_NO):
        st.session_state[CLAVE_MENSAJE] = ("error", "Responde Sí o No a la pregunta sobre las recomendaciones.")
        return

    try:
        estado = guardar_respuesta(supabase, empresa, titulo, respuesta == RESPUESTA_SI)
    except Exception as error:
        if _es_tabla_inexistente(error):
            st.session_state[CLAVE_MENSAJE] = ("error", _texto_falta_tabla())
        else:
            st.session_state[CLAVE_MENSAJE] = ("error", f"No se ha podido guardar: {error}")
        return

    cargar_fiabilidad.clear()
    st.session_state[CLAVE_MENSAJE] = ("success", f"Respuesta {estado}: {empresa}.")
    st.session_state[CLAVE_EMPRESA] = None
    st.session_state[CLAVE_TITULO] = ""
    st.session_state[CLAVE_RESPUESTA] = None


def _texto_falta_tabla() -> str:
    return (
        f"Falta la tabla `{TABLA_FIABILIDAD}` en Supabase. Ejecuta una vez el archivo "
        "`sql/migracion_fiabilidad_herramienta.sql` en el editor SQL de Supabase y recarga la página."
    )


def _formulario(supabase: Client, empresas: list):
    with st.container(border=True):
        st.markdown("**Registrar un caso**")
        st.caption(
            "Cuando una licitación se adjudique a una empresa, apúntala aquí. Si registras otra vez la misma "
            "empresa y título, se corrige la respuesta anterior."
        )
        with st.form("tab4_fiab_form", clear_on_submit=False):
            col_empresa, col_titulo = st.columns([2, 3])
            with col_empresa:
                st.selectbox(
                    "Empresa adjudicada", empresas, index=None, placeholder="Elige la empresa", key=CLAVE_EMPRESA
                )
            with col_titulo:
                st.text_input(
                    "Título de la licitación",
                    max_chars=MAX_CARACTERES_TITULO,
                    placeholder="Título tal y como aparece en la licitación",
                    key=CLAVE_TITULO,
                )
            st.radio(
                f"¿La empresa que ha sido adjudicada estaba entre las {NUM_RECOMENDACIONES} primeras "
                "recomendaciones de la herramienta?",
                [RESPUESTA_SI, RESPUESTA_NO],
                index=None,
                horizontal=True,
                key=CLAVE_RESPUESTA,
            )
            st.form_submit_button("Guardar respuesta", on_click=_al_guardar, args=(supabase,))

        mensaje = st.session_state.pop(CLAVE_MENSAJE, None)
        if mensaje:
            tipo, texto = mensaje
            getattr(st, tipo)(texto)


# ============================================================
# RESULTADOS
# ============================================================

def _resultados(datos: pd.DataFrame):
    if datos.empty:
        st.info("Todavía no hay respuestas registradas: en cuanto guardes la primera aparecerá aquí la fiabilidad.")
        return

    total = len(datos)
    aciertos = int(datos["bool_acierto"].sum())
    resumen = fiabilidad_por_empresa(datos)

    col_global, col_casos, col_empresas = st.columns(3)
    col_global.metric("Fiabilidad global", f"{aciertos / total * 100:.0f} %", help=f"{aciertos} de {total} casos")
    col_casos.metric("Casos registrados", total)
    col_empresas.metric("Empresas evaluadas", len(resumen))

    mostrar_html(
        construir_html_carrusel(
            resumen,
            columna_exitos="aciertos",
            etiqueta_exitos="aciertos",
            descripcion="Fiabilidad de la herramienta por empresa",
        ),
        ALTURA_CARRUSEL,
    )
    st.caption(
        f"Porcentaje de casos en que la empresa adjudicada estaba entre las {NUM_RECOMENDACIONES} primeras "
        "recomendaciones. De la empresa con la que más acierta la herramienta a la que menos: las últimas son los "
        "perfiles en los que conviene afinar las búsquedas. Con pocos casos el porcentaje es poco representativo: "
        "fíjate en «X de Y»."
    )

    with st.expander("Ver los casos registrados"):
        tabla = datos.iloc[::-1].rename(columns={"empresa": "Empresa", "titulo": "Título"})
        tabla["¿Estaba entre las 5 primeras?"] = tabla["bool_acierto"].map({True: RESPUESTA_SI, False: RESPUESTA_NO})
        ancho_completo(
            st.dataframe,
            tabla[["Empresa", "Título", "¿Estaba entre las 5 primeras?"]].reset_index(drop=True),
            hide_index=True,
            height=300,
        )


# ============================================================
# SECCIÓN DE LA PESTAÑA
# ============================================================

def render_fiabilidad(supabase: Client):
    st.markdown("#### Fiabilidad de la herramienta")
    st.caption(
        f"¿Con qué frecuencia la empresa que acaba adjudicada estaba entre las {NUM_RECOMENDACIONES} primeras "
        "recomendaciones de la herramienta? Registra los casos y consulta con qué empresas acierta más y menos."
    )

    try:
        datos = cargar_fiabilidad(supabase)
        empresas = listar_empresas_dossier(supabase)
    except Exception as error:
        if _es_tabla_inexistente(error):
            st.warning(_texto_falta_tabla())
        else:
            st.error(f"No se ha podido cargar la fiabilidad de la herramienta: {error}")
        return

    _formulario(supabase, empresas)
    _resultados(datos)

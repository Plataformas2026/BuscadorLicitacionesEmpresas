"""
fiabilidad.py
-------------
Pestaña 5: Fiabilidad de la herramienta.

QUÉ HACE
--------
Cuando una licitación se adjudica a una empresa, el usuario la registra aquí y
responde a una pregunta: ¿la empresa adjudicada estaba entre las 5 primeras
recomendaciones de la herramienta? Cada respuesta se guarda en la tabla
`fiabilidad_herramienta` (empresa, titulo, bool_acierto, fecha) y con ellas
la pestaña muestra:

1. El formulario para registrar un caso. La FECHA no se pide: se guarda sola,
   la de hoy (zona horaria de Canarias, `ZONA_HORARIA`), en el momento de
   pulsar «Guardar respuesta».
2. Tres cifras (fiabilidad global, casos y empresas evaluadas) y una gráfica
   temporal de líneas con la evolución de la fiabilidad: la acumulada hasta
   cada fecha (cómo va la herramienta en conjunto) y la de cada periodo (cómo
   va en ese día, semana o mes). Se puede agrupar por día, semana o mes.
3. Un carrusel con el % de acierto por empresa, de la que más acierta a la
   que menos, para ver con qué perfiles hay que afinar las búsquedas.
4. Los desplegables «Ver los casos registrados» y «¿Cómo se ha interpretado el
   campo RESULTADO?» (este último, de la pestaña Estadísticas, se muestra
   aquí con `estadisticas.render_interpretacion`).

LA TABLA
--------
Se crea (o se actualiza, si ya tenías la anterior sin fecha) con
`sql/migracion_fiabilidad_herramienta.sql`, que se ejecuta una vez en el editor
SQL de Supabase. (empresa, titulo) es único: volver a registrar el mismo caso
corrige la respuesta anterior en vez de duplicarla, y la fecha pasa a ser la
de la corrección. La app escribe con la clave anónima, igual que ya hace para
marcar licitaciones como vistas o guardadas. Esta pestaña habla de la
herramienta, no de los resultados de las empresas, así que no depende de los
filtros de Estadísticas.

Si la tabla o la columna `fecha` aún no existen, la pestaña explica cómo crearlas.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import altair as alt
import pandas as pd
import streamlit as st
from supabase import Client

from estadisticas_comun import (
    ALTURA_CARRUSEL,
    COLOR_POSITIVO,
    ancho_completo,
    construir_html_carrusel,
    leer_paginado,
    mostrar_html,
)

TABLA_FIABILIDAD = "fiabilidad_herramienta"
NUM_RECOMENDACIONES = 5
MAX_CARACTERES_TITULO = 500
ZONA_HORARIA = "Atlantic/Canary"

RESPUESTA_SI = "Sí"
RESPUESTA_NO = "No"

AUTOMATICO = "Automático"
GRANULARIDADES = {"Día": "D", "Semana": "W-SUN", "Mes": "M"}  # nombre -> frecuencia de pandas
FORMATO_PERIODO = {"Día": "%d/%m/%y", "Semana": "%d/%m/%y", "Mes": "%b %Y"}

COLOR_PERIODO = "#8a93a3"

CLAVE_EMPRESA = "tab5_fiab_empresa"
CLAVE_TITULO = "tab5_fiab_titulo"
CLAVE_RESPUESTA = "tab5_fiab_respuesta"
CLAVE_MENSAJE = "tab5_fiab_mensaje"
CLAVE_GRANULARIDAD = "tab5_fiab_granularidad"


# ============================================================
# DATOS
# ============================================================

def hoy() -> date:
    """Fecha de hoy en Canarias (si el sistema no tiene la base de zonas horarias, en UTC)."""
    try:
        return datetime.now(ZoneInfo(ZONA_HORARIA)).date()
    except ZoneInfoNotFoundError:
        return datetime.now(timezone.utc).date()


@st.cache_data(ttl=300, show_spinner=False)
def cargar_fiabilidad(_supabase: Client) -> pd.DataFrame:
    """Casos registrados, del más antiguo al más reciente. Columnas: empresa, titulo, bool_acierto, fecha (datetime)."""
    filas = leer_paginado(
        lambda: _supabase.table(TABLA_FIABILIDAD).select("empresa, titulo, bool_acierto, fecha").order("id")
    )
    df = pd.DataFrame(filas, columns=["empresa", "titulo", "bool_acierto", "fecha"])
    df["bool_acierto"] = df["bool_acierto"].astype(bool)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
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
    """
    Guarda (o corrige) un caso con la fecha de hoy. Devuelve «guardada» si es nuevo y «actualizada» si ya existía.
    """
    titulo = limpiar_titulo(titulo)
    existente = (
        supabase.table(TABLA_FIABILIDAD).select("id").eq("empresa", empresa).eq("titulo", titulo).limit(1).execute().data
    )
    supabase.table(TABLA_FIABILIDAD).upsert(
        {
            "empresa": empresa,
            "titulo": titulo,
            "bool_acierto": bool(acierto),
            "fecha": hoy().isoformat(),
            "actualizado_en": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="empresa,titulo",
    ).execute()
    return "actualizada" if existente else "guardada"


def _necesita_migracion(error: Exception) -> bool:
    """¿Falta la tabla, o a la tabla antigua le falta la columna `fecha`?"""
    texto = str(error)
    falta_tabla = TABLA_FIABILIDAD in texto and any(
        pista in texto for pista in ("PGRST205", "does not exist", "Could not find", "schema cache", "42P01")
    )
    falta_columna = "fecha" in texto and any(
        pista in texto for pista in ("42703", "PGRST204", "does not exist", "Could not find", "schema cache")
    )
    return falta_tabla or falta_columna


def _texto_falta_migracion() -> str:
    return (
        f"Falta crear o actualizar la tabla `{TABLA_FIABILIDAD}` en Supabase (necesita la columna `fecha`). Ejecuta "
        "una vez el archivo `sql/migracion_fiabilidad_herramienta.sql` en el editor SQL de Supabase y recarga la página."
    )


# ============================================================
# CÁLCULOS (puros: reciben y devuelven DataFrames)
# ============================================================

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


def periodo_por_defecto(datos: pd.DataFrame) -> str:
    """Con pocos días de historia, por día; con unos meses, por semana; con más, por mes."""
    fechas = datos["fecha"].dropna()
    if fechas.empty:
        return "Mes"
    dias = (fechas.max() - fechas.min()).days
    if dias <= 31:
        return "Día"
    if dias <= 180:
        return "Semana"
    return "Mes"


def evolucion_fiabilidad(datos: pd.DataFrame, granularidad: str) -> pd.DataFrame:
    """
    Fiabilidad a lo largo del tiempo, agrupando los casos por día, semana (de lunes a domingo) o mes.
    Una fila por periodo desde el primero hasta el último caso, incluidos los periodos sin casos.
    Columnas: periodo (inicio del periodo), etiqueta, aciertos, total, pct_periodo (NaN si no hubo casos),
    aciertos_acum, total_acum, pct_acumulado.
    """
    columnas = ["periodo", "etiqueta", "aciertos", "total", "pct_periodo", "aciertos_acum", "total_acum", "pct_acumulado"]
    con_fecha = datos.dropna(subset=["fecha"])
    if con_fecha.empty:
        return pd.DataFrame(columns=columnas)

    frecuencia = GRANULARIDADES[granularidad]
    periodos = con_fecha["fecha"].dt.to_period(frecuencia)
    por_periodo = con_fecha.groupby(periodos).agg(
        aciertos=("bool_acierto", "sum"), total=("bool_acierto", "size")
    )
    completo = pd.period_range(periodos.min(), periodos.max(), freq=frecuencia)
    por_periodo = por_periodo.reindex(completo, fill_value=0)

    por_periodo["aciertos"] = por_periodo["aciertos"].astype(int)
    por_periodo["total"] = por_periodo["total"].astype(int)
    por_periodo["pct_periodo"] = (por_periodo["aciertos"] / por_periodo["total"] * 100).where(por_periodo["total"] > 0)
    por_periodo["aciertos_acum"] = por_periodo["aciertos"].cumsum()
    por_periodo["total_acum"] = por_periodo["total"].cumsum()
    por_periodo["pct_acumulado"] = por_periodo["aciertos_acum"] / por_periodo["total_acum"] * 100

    resultado = por_periodo.reset_index(names="p")
    resultado["periodo"] = resultado["p"].dt.start_time
    resultado["etiqueta"] = resultado["periodo"].dt.strftime(FORMATO_PERIODO[granularidad])
    return resultado[columnas]


# ============================================================
# GRÁFICA
# ============================================================

def construir_grafica_evolucion(evolucion: pd.DataFrame, granularidad: str):
    """Dos líneas: fiabilidad acumulada (azul, continua) y fiabilidad de cada periodo (gris, discontinua)."""
    nombre_acumulada = "Acumulada"
    nombre_periodo = {"Día": "Del día", "Semana": "De la semana", "Mes": "Del mes"}[granularidad]

    largo = pd.concat([
        pd.DataFrame({
            "etiqueta": evolucion["etiqueta"], "serie": nombre_acumulada, "pct": evolucion["pct_acumulado"],
            "aciertos": evolucion["aciertos_acum"], "total": evolucion["total_acum"],
        }),
        pd.DataFrame({
            "etiqueta": evolucion["etiqueta"], "serie": nombre_periodo, "pct": evolucion["pct_periodo"],
            "aciertos": evolucion["aciertos"], "total": evolucion["total"],
        }),
    ], ignore_index=True)
    largo = largo.dropna(subset=["pct"])

    orden = list(evolucion["etiqueta"])
    base = alt.Chart(largo).encode(
        x=alt.X("etiqueta:O", sort=orden, title={"Día": "Día", "Semana": "Semana (inicio)", "Mes": "Mes"}[granularidad],
                axis=alt.Axis(labelAngle=0, labelOverlap="parity")),
        y=alt.Y("pct:Q", title="Fiabilidad (%)", scale=alt.Scale(domain=[0, 100]), axis=alt.Axis(values=[0, 25, 50, 75, 100], format="d")),
        color=alt.Color(
            "serie:N",
            scale=alt.Scale(domain=[nombre_acumulada, nombre_periodo], range=[COLOR_POSITIVO, COLOR_PERIODO]),
            legend=alt.Legend(title=None, orient="top", direction="horizontal"),
        ),
        strokeDash=alt.StrokeDash(
            "serie:N", scale=alt.Scale(domain=[nombre_acumulada, nombre_periodo], range=[[1, 0], [5, 4]]), legend=None
        ),
        tooltip=[
            alt.Tooltip("etiqueta:O", title="Periodo"),
            alt.Tooltip("serie:N", title="Serie"),
            alt.Tooltip("pct:Q", title="Fiabilidad (%)", format=".0f"),
            alt.Tooltip("aciertos:Q", title="Aciertos"),
            alt.Tooltip("total:Q", title="Casos"),
        ],
    )
    # Los periodos sin casos no tienen punto en la serie del periodo: la línea los une con el siguiente.
    lineas = base.mark_line(
        strokeWidth=2.5, point=alt.OverlayMarkDef(size=70, filled=True)
    )
    return lineas.properties(height=320, width="container")


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
        if _necesita_migracion(error):
            st.session_state[CLAVE_MENSAJE] = ("error", _texto_falta_migracion())
        else:
            st.session_state[CLAVE_MENSAJE] = ("error", f"No se ha podido guardar: {error}")
        return

    cargar_fiabilidad.clear()
    st.session_state[CLAVE_MENSAJE] = ("success", f"Respuesta {estado}: {empresa} ({hoy():%d/%m/%Y}).")
    st.session_state[CLAVE_EMPRESA] = None
    st.session_state[CLAVE_TITULO] = ""
    st.session_state[CLAVE_RESPUESTA] = None


def _formulario(supabase: Client, empresas: list):
    with st.container(border=True):
        st.markdown("**Registrar un caso**")
        st.caption(
            "Cuando una licitación se adjudique a una empresa, apúntala aquí. La fecha se guarda sola (la de hoy, "
            f"{hoy():%d/%m/%Y}). Si registras otra vez la misma empresa y título, se corrige la respuesta anterior "
            "y la fecha pasa a ser la de hoy."
        )
        with st.form("tab5_fiab_form", clear_on_submit=False):
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

def _evolucion(datos: pd.DataFrame):
    st.markdown("#### Evolución de la fiabilidad")
    col_agrupar, _ = st.columns([1, 3])
    with col_agrupar:
        elegida = st.selectbox("Agrupar por", [AUTOMATICO, *GRANULARIDADES], key=CLAVE_GRANULARIDAD)
    granularidad = periodo_por_defecto(datos) if elegida == AUTOMATICO else elegida

    evolucion = evolucion_fiabilidad(datos, granularidad)
    if evolucion.empty:
        st.info("Los casos registrados no tienen fecha, así que no se puede dibujar la evolución.")
        return

    ancho_completo(st.altair_chart, construir_grafica_evolucion(evolucion, granularidad))
    st.caption(
        "Línea azul: fiabilidad acumulada, es decir, el % de casos acertados desde el primer registro hasta esa fecha; "
        "si sube, la herramienta mejora. Línea gris discontinua: fiabilidad solo de los casos de ese "
        f"{granularidad.lower()}, que sube y baja más con pocos casos (pasa el ratón por los puntos para ver «X de Y»). "
        "Un caso corregido cuenta con su fecha de corrección."
    )


def _casos_registrados(datos: pd.DataFrame):
    with st.expander("Ver los casos registrados"):
        tabla = datos.assign(_orden=range(len(datos))).sort_values(["fecha", "_orden"], ascending=False, na_position="last")
        tabla["Fecha"] = tabla["fecha"].dt.strftime("%d/%m/%Y").fillna("")
        tabla["¿Estaba entre las 5 primeras?"] = tabla["bool_acierto"].map({True: RESPUESTA_SI, False: RESPUESTA_NO})
        tabla = tabla.rename(columns={"empresa": "Empresa", "titulo": "Título"})
        ancho_completo(
            st.dataframe,
            tabla[["Fecha", "Empresa", "Título", "¿Estaba entre las 5 primeras?"]].reset_index(drop=True),
            hide_index=True,
            height=300,
        )


def _interpretacion_resultado(supabase: Client):
    """Desplegable de la pestaña Estadísticas que explica cómo se lee RESULTADO (vive aquí por petición)."""
    import estadisticas  # aquí dentro: este módulo solo lo necesita para este desplegable

    try:
        df, duplicadas_hms = estadisticas.cargar_referencias(supabase)
    except Exception as error:
        with st.expander("¿Cómo se ha interpretado el campo RESULTADO?"):
            st.error(f"No se han podido cargar las referencias de Supabase: {error}")
        return
    estadisticas.render_interpretacion(df, duplicadas_hms)


def _resultados(supabase: Client, datos: pd.DataFrame):
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

    _evolucion(datos)

    st.markdown("#### Fiabilidad por empresa")
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

    _casos_registrados(datos)


# ============================================================
# PESTAÑA
# ============================================================

def render_tab5(supabase: Client):
    st.subheader("Fiabilidad de la herramienta")
    st.caption(
        f"¿Con qué frecuencia la empresa que acaba adjudicada estaba entre las {NUM_RECOMENDACIONES} primeras "
        "recomendaciones de la herramienta? Registra los casos y consulta cómo evoluciona la fiabilidad y con qué "
        "empresas acierta más y menos."
    )

    try:
        datos = cargar_fiabilidad(supabase)
        empresas = listar_empresas_dossier(supabase)
    except Exception as error:
        if _necesita_migracion(error):
            st.warning(_texto_falta_migracion())
        else:
            st.error(f"No se ha podido cargar la fiabilidad de la herramienta: {error}")
        return

    _formulario(supabase, empresas)
    _resultados(supabase, datos)
    _interpretacion_resultado(supabase)

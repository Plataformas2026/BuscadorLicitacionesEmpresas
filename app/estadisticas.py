"""
estadisticas.py
---------------
Pestaña 4: Estadísticas de adjudicación (resultados de licitaciones).

El mismo motor (`render_estadisticas`) dibuja también la pestaña «Estadísticas
de interés» (`estadisticas_interes.py`): cambian los datos, la lectura del
resultado y los textos (`Perfil` de `estadisticas_comun.py`), no las gráficas.

ORIGEN DE LOS DATOS
-------------------
La hoja "REFERENCIAS P BÚSQUEDAS" del Excel de Drive ya llega a Supabase
(tabla `empresas_referencias`, ver ingest/sync_empresas_drive.py), que es de
donde lee también el Directorio y la Coincidencia Inteligente. Esta pestaña
usa ese mismo cliente de Supabase (el que recibe `render_tab4`): no abre
ninguna conexión nueva ni necesita credenciales de Google en la app.

Se usan FECHA, EMPRESA y RESULTADO (más el título, solo para detectar
duplicados). Como RESULTADO y FECHA son texto libre, se interpretan con
`normalizacion_resultados.py` (ahí están las reglas y su justificación), y
NO con el campo `resultado_normalizado` de la tabla, que solo reconoce
"adjudicad..." y por eso dejaría fuera "ganada", "conseguido", etc.

DOS DETALLES DE LOS DATOS REALES
--------------------------------
- La sincronización carga en `empresas_referencias` tanto la hoja
  "REFERENCIAS P BÚSQUEDAS" como la hoja "HMS", y la hoja HMS repite las 39
  filas de HMS que ya están en la primera (con la columna RESULTADO usada
  para describir la tarea). Sin tratamiento, HMS contaría dos veces. Aquí se
  eliminan, SOLO para esa empresa, las filas con el mismo título, fecha e
  igual interpretación de RESULTADO.
- Si una referencia está enlazada a una empresa del dossier (numero_interno)
  se muestra con el nombre del dossier; así "CANAEST" y "CANAEST CONSULTORES"
  cuentan como una sola empresa. Si no está enlazada, con el nombre del Excel.

QUÉ MUESTRA
-----------
1. Gráfica de evolución por año (adjudicadas vs no adjudicadas) con filtros
   de empresa, año y resultado.
2. Carrusel con el % de éxito por empresa (adjudicadas / (adjudicadas + no
   adjudicadas)), de mayor a menor. Respeta los filtros de empresa y año; el
   filtro de resultado no se aplica aquí porque el % necesita ambos.
3. Las 15 palabras más repetidas en los títulos de las licitaciones
   adjudicadas y en las no adjudicadas (`estadisticas_palabras.py`).
4. Porcentaje de éxito por organismo financiador, en barras horizontales
   (`estadisticas_organismos.py`), justo debajo del carrusel de empresas.
5. Palabras clave por empresa (`estadisticas_palabras.py`): desplegable de
   empresa y dos tablas paralelas (adjudicadas / no adjudicadas), debajo de
   las nubes.

La fiabilidad de la herramienta tiene su propia pestaña (`fiabilidad.py`),
que también muestra el desplegable de cómo se ha interpretado RESULTADO
(`render_interpretacion`, definida aquí).

Los elementos compartidos (colores, paginación, carrusel) están en
`estadisticas_comun.py`.
"""
import altair as alt
import pandas as pd
import streamlit as st
from supabase import Client

from estadisticas_comun import (
    ALTURA_CARRUSEL,
    COLOR_NEGATIVO,
    COLOR_POSITIVO,
    NOMBRES_MESES,
    PERFIL_ADJUDICACION,
    Perfil,
    ancho_completo,
    construir_html_carrusel,
    leer_paginado,
    mostrar_html,
)
from estadisticas_organismos import render_organismos
from estadisticas_palabras import render_nubes, render_palabras_por_empresa
from normalizacion_resultados import (
    IGNORADO,
    NEGATIVO,
    POSITIVO,
    clasificar_resultado,
    extraer_anio,
    limpiar_texto,
)

TABLA_REFERENCIAS = "empresas_referencias"
COLUMNAS_REFERENCIAS = "id, numero_interno, nombre_empresa_excel, titulo, fecha, resultado, organismo_financiador"

# Empresas cuya hoja propia del Excel repite filas de "REFERENCIAS P BÚSQUEDAS"
# (clave = nombre tal y como lo normaliza `limpiar_texto`).
EMPRESAS_CON_HOJA_ESPEJO = {"hms"}

ETIQUETA_POSITIVO = PERFIL_ADJUDICACION.positivo
ETIQUETA_NEGATIVO = PERFIL_ADJUDICACION.negativo
ETIQUETAS = {POSITIVO: ETIQUETA_POSITIVO, NEGATIVO: ETIQUETA_NEGATIVO}


def etiquetas_de(perfil: Perfil) -> dict:
    return {POSITIVO: perfil.positivo, NEGATIVO: perfil.negativo}



# ============================================================
# CARGA Y NORMALIZACIÓN (cacheada)
# ============================================================

def _nombres_canonicos(supabase: Client) -> dict:
    filas = leer_paginado(
        lambda: supabase.table("empresas").select("numero_interno, nombre_empresa").order("numero_interno")
    )
    return {f["numero_interno"]: (f.get("nombre_empresa") or "").strip() for f in filas if f.get("numero_interno")}


def construir_dataframe(filas: list, nombres_canonicos: dict) -> tuple:
    """
    Filas de `empresas_referencias` -> (DataFrame con TODAS las referencias ya
    interpretadas, nº de duplicados de la hoja HMS eliminados).

    Columnas: empresa, titulo, anio (Int64, puede ser NA), categoria
    (POSITIVO / NEGATIVO / IGNORADO), regla, resultado_original,
    organismo_original (texto libre del Excel; se unifica en estadisticas_organismos.py).
    """
    columnas = ["empresa", "titulo", "anio", "mes", "categoria", "regla", "resultado_original", "organismo_original"]
    if not filas:
        return pd.DataFrame(columns=columnas), 0

    registros = []
    for fila in filas:
        nombre_excel = (fila.get("nombre_empresa_excel") or "").strip()
        empresa = nombres_canonicos.get(fila.get("numero_interno")) or nombre_excel
        if not empresa:
            continue
        categoria, regla = clasificar_resultado(fila.get("resultado"))
        
        # Extraemos mes y año de la fecha si es posible
        fecha_str = str(fila.get("fecha") or "")
        anio_val = extraer_anio(fecha_str)
        
        # Intentar extraer el mes de la fecha (asumiendo formato estándar YYYY-MM-DD o similar)
        mes_val = None
        try:
            dt_parsed = pd.to_datetime(fila.get("fecha"), errors="coerce")
            if pd.notna(dt_parsed):
                mes_val = int(dt_parsed.month)
        except Exception:
            pass

        registros.append({
            "empresa": empresa,
            "titulo": (fila.get("titulo") or "").strip(),
            "anio": anio_val,
            "mes": mes_val,
            "categoria": categoria,
            "regla": regla,
            "resultado_original": (fila.get("resultado") or "").strip(),
            "organismo_original": (fila.get("organismo_financiador") or "").strip(),
            # solo para detectar duplicados; no se conservan
            "_empresa_excel": limpiar_texto(nombre_excel),
            "_titulo": limpiar_texto(fila.get("titulo")),
            "_fecha": fecha_str,
        })

    df = pd.DataFrame(registros)
    es_espejo = df["_empresa_excel"].isin(EMPRESAS_CON_HOJA_ESPEJO)
    duplicada = es_espejo & df.duplicated(
        subset=["_empresa_excel", "_titulo", "_fecha", "categoria"], keep="first"
    )
    df = df[~duplicada]

    df["anio"] = pd.to_numeric(df["anio"], errors="coerce").astype("Int64")
    if "mes" in df.columns:
        df["mes"] = pd.to_numeric(df["mes"], errors="coerce").astype("Int64")
        
    return df[columnas].reset_index(drop=True), int(duplicada.sum())


@st.cache_data(ttl=1800, show_spinner=False)
def cargar_referencias(_supabase: Client) -> tuple:
    filas = leer_paginado(
        lambda: _supabase.table(TABLA_REFERENCIAS).select(COLUMNAS_REFERENCIAS + ", fecha").order("id")
    )
    return construir_dataframe(filas, _nombres_canonicos(_supabase))


# ============================================================
# CÁLCULOS (puros: reciben y devuelven DataFrames)
# ============================================================

def filtrar(claros: pd.DataFrame, empresas: list, anios: list, meses: list = ()) -> pd.DataFrame:
    """Aplica los filtros de empresa, año y mes (1-12). Vacío = sin filtrar."""
    if empresas:
        claros = claros[claros["empresa"].isin(empresas)]
    if anios:
        claros = claros[claros["anio"].isin(anios)]
    if len(meses) and "mes" in claros.columns:
        claros = claros[claros["mes"].isin(meses)]
    return claros


def serie_por_periodo(
    claros: pd.DataFrame, anios_elegidos: list, categorias: list, etiquetas: dict = ETIQUETAS
) -> tuple[pd.DataFrame, str]:
    """
    Agrupa por mes si hay menos de 3 años (en total o seleccionados),
    o por año si hay 3 o más.
    Devuelve (DataFrame con columnas: periodo, resultado, n, y tipo_eje: 'anio' o 'mes').
    """
    con_datos = claros.dropna(subset=["anio"])
    if con_datos.empty:
        return pd.DataFrame(columns=["periodo", "resultado", "n"]), "anio"

    anios_disponibles = sorted(int(a) for a in con_datos["anio"].unique())
    if anios_elegidos:
        anios = sorted(int(a) for a in anios_elegidos)
    else:
        anios = anios_disponibles

    # CONDICIÓN: Si hay menos de 3 años, agrupamos por mes
    if len(anios) < 3 and "mes" in con_datos.columns:
        if anios_elegidos:
            con_datos = con_datos[con_datos["anio"].isin(anios_elegidos)]
        
        conteo = con_datos.groupby(["anio", "mes", "categoria"]).size()
        filas = []
        for anio in anios:
            for mes in range(1, 13):
                nombre_mes = NOMBRES_MESES.get(mes, str(mes))
                periodo_str = f"{nombre_mes} {anio}"
                for categoria in categorias:
                    filas.append({
                        "periodo": periodo_str,
                        "resultado": etiquetas[categoria],
                        "n": int(conteo.get((anio, mes, categoria), 0)),
                        "_orden": f"{anio}-{mes:02d}",
                    })
        df_res = pd.DataFrame(filas)
        if not df_res.empty:
            df_res = df_res.sort_values("_orden").drop(columns=["_orden"])
        return df_res, "mes"
    
    # Comportamiento por defecto (por año si hay 3 o más)
    if anios_elegidos:
        anios_finales = sorted(int(a) for a in anios_elegidos)
    else:
        anios_finales = list(range(int(con_datos["anio"].min()), int(con_datos["anio"].max()) + 1))

    conteo = con_datos.groupby(["anio", "categoria"]).size()
    filas = []
    for anio in anios_finales:
        for categoria in categorias:
            filas.append({
                "periodo": str(anio),
                "resultado": etiquetas[categoria],
                "n": int(conteo.get((anio, categoria), 0)),
            })
    return pd.DataFrame(filas), "anio"


def exito_por_empresa(claros: pd.DataFrame) -> pd.DataFrame:
    """
    % de éxito = adjudicadas / (adjudicadas + no adjudicadas), de mayor a menor.
    """
    if claros.empty:
        return pd.DataFrame(columns=["empresa", "adjudicadas", "total", "pct"])

    agrupado = claros.groupby("empresa").agg(
        adjudicadas=("categoria", lambda s: int((s == POSITIVO).sum())),
        total=("categoria", "size"),
    ).reset_index()
    agrupado["pct"] = agrupado["adjudicadas"] / agrupado["total"] * 100
    return agrupado.sort_values(["pct", "total", "empresa"], ascending=[False, False, True]).reset_index(drop=True)


def resumen_interpretacion(df: pd.DataFrame, perfil: Perfil = PERFIL_ADJUDICACION) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["Redacción en el Excel", "Se interpreta como", "Regla aplicada", "Nº"])

    tabla = (
        df.assign(texto=df["resultado_original"].str.replace(r"\s+", " ", regex=True).str.strip())
        .groupby(["texto", "categoria", "regla"], dropna=False)
        .size()
        .reset_index(name="Nº")
    )
    nombres = {POSITIVO: perfil.positivo, NEGATIVO: perfil.negativo, IGNORADO: "Se ignora"}
    tabla["Se interpreta como"] = tabla["categoria"].map(nombres)
    tabla["Redacción en el Excel"] = tabla["texto"].map(lambda t: (t[:90] + "…") if len(t) > 90 else (t or "(vacío)"))
    tabla = tabla.rename(columns={"regla": "Regla aplicada"})
    return tabla.sort_values("Nº", ascending=False)[
        ["Redacción en el Excel", "Se interpreta como", "Regla aplicada", "Nº"]
    ].reset_index(drop=True)


# ============================================================
# GRÁFICA
# ============================================================

def construir_grafica(serie: pd.DataFrame, tipo: str, tipo_eje: str, perfil: Perfil = PERFIL_ADJUDICACION):
    codificacion_color = alt.Color(
        "resultado:N",
        scale=alt.Scale(domain=[perfil.positivo, perfil.negativo], range=[COLOR_POSITIVO, COLOR_NEGATIVO]),
        legend=alt.Legend(title=None, orient="top", direction="horizontal"),
    )
    
    titulo_eje_x = "Mes y Año" if tipo_eje == "mes" else "Año"
    rotacion_etiquetas = -45 if tipo_eje == "mes" else 0
    
    # Forzamos el orden cronológico exacto usando sort con los valores únicos del DataFrame
    eje_x = alt.X(
        "periodo:O", 
        title=titulo_eje_x, 
        sort=list(serie["periodo"].unique()), 
        axis=alt.Axis(labelAngle=rotacion_etiquetas)
    )
    eje_y = alt.Y("n:Q", title="Número de resultados", axis=alt.Axis(tickMinStep=1, format="d"))
    
    tooltip = [
        alt.Tooltip("periodo:O", title="Periodo"),
        alt.Tooltip("resultado:N", title="Resultado"),
        alt.Tooltip("n:Q", title="Nº de licitaciones"),
    ]

    grafica = alt.Chart(serie)
    if tipo == "Líneas":
        grafica = grafica.mark_line(point=alt.OverlayMarkDef(size=70, filled=True), strokeWidth=2.5).encode(
            x=eje_x, y=eje_y, color=codificacion_color, tooltip=tooltip
        )
    else:
        grafica = grafica.mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=eje_x, y=eje_y, color=codificacion_color, xOffset="resultado:N", tooltip=tooltip
        )
    return grafica.properties(height=340, width="container")


# ============================================================
# DESPLEGABLE DE INTERPRETACIÓN
# ============================================================

def render_interpretacion(
    df: pd.DataFrame,
    duplicadas_hms: int = 0,
    perfil: Perfil = PERFIL_ADJUDICACION,
    campo: str = "RESULTADO",
    explicacion: str = "",
):
    with st.expander(f"¿Cómo se ha interpretado el campo {campo}?"):
        total = len(df)
        n_pos_total = int((df["categoria"] == POSITIVO).sum())
        n_neg_total = int((df["categoria"] == NEGATIVO).sum())
        st.markdown(
            f"De **{total}** referencias, **{n_pos_total}** se interpretan como {perfil.positivos.lower()}, "
            f"**{n_neg_total}** como {perfil.negativos.lower()} y **{total - n_pos_total - n_neg_total}** se ignoran "
            + (
                "(sin resultado, sin información, pendientes, canceladas o no presentadas, o textos que solo nombran socios)."
                if not explicacion else f"({explicacion})."
            )
        )
        if duplicadas_hms:
            st.caption(
                f"Se han descartado {duplicadas_hms} filas duplicadas de HMS (la hoja HMS repite las de "
                "«REFERENCIAS P BÚSQUEDAS»)."
            )
        ancho_completo(st.dataframe, resumen_interpretacion(df, perfil), hide_index=True, height=360)


# ============================================================
# PESTAÑA (la misma para adjudicación y para interés)
# ============================================================

def render_estadisticas(
    supabase: Client,
    perfil: Perfil,
    cargar,
    titulo: str,
    descripcion: str,
    mensaje_vacio: str,
    con_mes: bool = False,
    pie=None,
):
    st.subheader(titulo)
    st.caption(descripcion)

    try:
        with st.spinner("Cargando estadísticas..."):
            df, extra = cargar(supabase)
    except Exception as error:
        st.error(f"No se han podido cargar los datos de Supabase: {error}")
        return

    claros = df[df["categoria"] != IGNORADO]
    if claros.empty:
        st.info(mensaje_vacio)
        if pie:
            pie(df, extra)
        return

    clave = perfil.clave
    etiquetas = etiquetas_de(perfil)

    # ---------------- Filtros ----------------
    opciones_empresas = sorted(claros["empresa"].unique(), key=str.casefold)
    opciones_anios = sorted((int(a) for a in claros["anio"].dropna().unique()), reverse=True)

    anchuras = [3, 2, 2, 2, 2] if con_mes else [3, 2, 2, 2]
    columnas = st.columns(anchuras)
    col_empresa, col_anio = columnas[0], columnas[1]
    col_mes = columnas[2] if con_mes else None
    col_resultado, col_tipo = columnas[-2], columnas[-1]

    with col_empresa:
        filtro_empresas = st.multiselect("Empresa", opciones_empresas, key=f"{clave}_filtro_empresa", placeholder="Todas")
    with col_anio:
        filtro_anios = st.multiselect("Año", opciones_anios, key=f"{clave}_filtro_anio", placeholder="Todos")
    
    filtro_meses = []
    if con_mes:
        opciones_meses = sorted(int(m) for m in claros["mes"].dropna().unique())
        with col_mes:
            filtro_meses = st.multiselect(
                "Mes", opciones_meses, key=f"{clave}_filtro_mes", placeholder="Todos", format_func=NOMBRES_MESES.get
            )
            
    with col_resultado:
        filtro_resultados = st.multiselect(
            "Resultado", [perfil.positivo, perfil.negativo], key=f"{clave}_filtro_resultado", placeholder="Todos"
        )
    with col_tipo:
        tipo_grafica = st.radio("Tipo de gráfica", ["Barras", "Líneas"], horizontal=True, key=f"{clave}_tipo_grafica")

    base = filtrar(claros, filtro_empresas, filtro_anios, filtro_meses)

    categorias = [c for c, etiqueta in etiquetas.items() if not filtro_resultados or etiqueta in filtro_resultados]
    seleccion = base[base["categoria"].isin(categorias)]

    # ---------------- Gráfica (Dinámica: Año o Meses) ----------------
    serie, tipo_eje = serie_por_periodo(seleccion, filtro_anios, categorias, etiquetas)
    if serie.empty or int(serie["n"].sum()) == 0:
        st.info(f"No hay licitaciones con {perfil.claro} para esta combinación de filtros.")
    else:
        n_pos = int((seleccion["categoria"] == POSITIVO).sum())
        n_neg = int((seleccion["categoria"] == NEGATIVO).sum())
        sin_anio = int(seleccion["anio"].isna().sum())
        texto_resumen = (
            f"{n_pos + n_neg} licitaciones con {perfil.claro}: {n_pos} {perfil.positivos.lower()} · "
            f"{n_neg} {perfil.negativos.lower()}"
        )
        if sin_anio and not filtro_anios:
            texto_resumen += f" ({sin_anio} sin año no aparecen en la gráfica)"
        st.caption(texto_resumen)
        ancho_completo(st.altair_chart, construir_grafica(serie, tipo_grafica, tipo_eje, perfil))

    # ---------------- Carrusel de % de éxito ----------------
    st.markdown("#### Porcentaje de éxito por empresa")
    exito = exito_por_empresa(base)
    if exito.empty:
        st.info(f"No hay empresas con {perfil.claro} para esta combinación de filtros.")
    else:
        mostrar_html(construir_html_carrusel(exito, etiqueta_exitos=perfil.unidad_exito), ALTURA_CARRUSEL)
        st.caption(
            (perfil.definicion + " " if perfil.definicion else "")
            + f"{perfil.formula}, de mayor a menor; "
            "a igual porcentaje, antes la que tiene más licitaciones. Con pocas licitaciones el porcentaje es poco "
            f"representativo: fíjate en «X de Y». Respeta los filtros de {perfil.filtros}."
        )

    # ---------------- Porcentaje de éxito por organismo financiador ----------------
    render_organismos(base, perfil)

    # ---------------- Palabras clave de los títulos, según el estado ----------------
    render_nubes(base, perfil)

    # ---------------- Palabras clave por empresa (tablas) ----------------
    render_palabras_por_empresa(base, perfil)

    if pie:
        pie(df, extra)


def render_tab4(supabase: Client):
    """Pestaña «Estadísticas de adjudicación»: hoja «REFERENCIAS P BÚSQUEDAS» y su campo RESULTADO."""
    render_estadisticas(
        supabase,
        PERFIL_ADJUDICACION,
        cargar_referencias,
        titulo="Estadísticas de adjudicación",
        descripcion=(
            "Resultados de las referencias de la hoja «REFERENCIAS P BÚSQUEDAS» del Excel. "
            "Solo cuentan las licitaciones con un resultado claro (adjudicada o no adjudicada)."
        ),
        mensaje_vacio="Todavía no hay referencias con un resultado claro (adjudicada / no adjudicada) para mostrar.",
    )

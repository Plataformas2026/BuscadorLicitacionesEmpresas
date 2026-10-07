"""
estadisticas.py
---------------
Pestaña 4: Estadísticas de resultados de licitaciones.

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
3. Un desplegable que explica cómo se ha interpretado cada redacción de
   RESULTADO, para poder comprobar la normalización.
"""
import html

import altair as alt
import pandas as pd
import streamlit as st
from supabase import Client

from normalizacion_resultados import (
    IGNORADO,
    NEGATIVO,
    POSITIVO,
    clasificar_resultado,
    extraer_anio,
    limpiar_texto,
)

TABLA_REFERENCIAS = "empresas_referencias"
COLUMNAS_REFERENCIAS = "id, numero_interno, nombre_empresa_excel, titulo, fecha, resultado"

# Empresas cuya hoja propia del Excel repite filas de "REFERENCIAS P BÚSQUEDAS"
# (clave = nombre tal y como lo normaliza `limpiar_texto`).
EMPRESAS_CON_HOJA_ESPEJO = {"hms"}

ETIQUETA_POSITIVO = "Adjudicada"
ETIQUETA_NEGATIVO = "No adjudicada"
ETIQUETAS = {POSITIVO: ETIQUETA_POSITIVO, NEGATIVO: ETIQUETA_NEGATIVO}

# Azul corporativo de la app (styles.py) y naranja: contraste alto, también
# para daltonismo (el verde/rojo habitual no lo tiene).
COLOR_POSITIVO = "#0066cc"
COLOR_NEGATIVO = "#e8590c"

ALTURA_CARRUSEL = 132  # px del iframe del carrusel (compacto)


# ============================================================
# CARGA Y NORMALIZACIÓN (cacheada)
# ============================================================

def _leer_paginado(construir_consulta, tamano_pagina: int = 1000) -> list:
    """
    Supabase devuelve como mucho 1.000 filas por consulta: se pagina hasta
    traerlo todo. `construir_consulta` debe devolver una consulta NUEVA (con
    su .order) en cada llamada. (Mismo patrón que `_consulta_paginada` de
    matching.py; no se importa de allí por ser una función privada.)
    """
    filas, inicio = [], 0
    while True:
        datos = construir_consulta().range(inicio, inicio + tamano_pagina - 1).execute().data or []
        filas.extend(datos)
        if len(datos) < tamano_pagina:
            return filas
        inicio += tamano_pagina


def _nombres_canonicos(supabase: Client) -> dict:
    filas = _leer_paginado(
        lambda: supabase.table("empresas").select("numero_interno, nombre_empresa").order("numero_interno")
    )
    return {f["numero_interno"]: (f.get("nombre_empresa") or "").strip() for f in filas if f.get("numero_interno")}


def construir_dataframe(filas: list, nombres_canonicos: dict) -> tuple:
    """
    Filas de `empresas_referencias` -> (DataFrame con TODAS las referencias ya
    interpretadas, nº de duplicados de la hoja HMS eliminados).

    Columnas: empresa, anio (Int64, puede ser NA), categoria (POSITIVO /
    NEGATIVO / IGNORADO), regla, resultado_original.
    """
    columnas = ["empresa", "anio", "categoria", "regla", "resultado_original"]
    if not filas:
        return pd.DataFrame(columns=columnas), 0

    registros = []
    for fila in filas:
        nombre_excel = (fila.get("nombre_empresa_excel") or "").strip()
        empresa = nombres_canonicos.get(fila.get("numero_interno")) or nombre_excel
        if not empresa:
            continue
        categoria, regla = clasificar_resultado(fila.get("resultado"))
        registros.append({
            "empresa": empresa,
            "anio": extraer_anio(fila.get("fecha")),
            "categoria": categoria,
            "regla": regla,
            "resultado_original": (fila.get("resultado") or "").strip(),
            # solo para detectar duplicados; no se conservan
            "_empresa_excel": limpiar_texto(nombre_excel),
            "_titulo": limpiar_texto(fila.get("titulo")),
            "_fecha": str(fila.get("fecha") or ""),
        })

    df = pd.DataFrame(registros)
    es_espejo = df["_empresa_excel"].isin(EMPRESAS_CON_HOJA_ESPEJO)
    # Se compara la INTERPRETACIÓN y no el texto: la hoja HMS usa la columna RESULTADO
    # para describir la tarea, así que el texto difiere aunque la fila sea la misma.
    duplicada = es_espejo & df.duplicated(
        subset=["_empresa_excel", "_titulo", "_fecha", "categoria"], keep="first"
    )
    df = df[~duplicada]

    df["anio"] = df["anio"].astype("Int64")
    return df[columnas].reset_index(drop=True), int(duplicada.sum())


@st.cache_data(ttl=1800, show_spinner=False)
def cargar_referencias(_supabase: Client) -> tuple:
    filas = _leer_paginado(
        lambda: _supabase.table(TABLA_REFERENCIAS).select(COLUMNAS_REFERENCIAS).order("id")
    )
    return construir_dataframe(filas, _nombres_canonicos(_supabase))


# ============================================================
# CÁLCULOS (puros: reciben y devuelven DataFrames)
# ============================================================

def filtrar(claros: pd.DataFrame, empresas: list, anios: list) -> pd.DataFrame:
    """Aplica los filtros de empresa y año (lista vacía = sin filtrar)."""
    if empresas:
        claros = claros[claros["empresa"].isin(empresas)]
    if anios:
        claros = claros[claros["anio"].isin(anios)]
    return claros


def serie_por_anio(claros: pd.DataFrame, anios_elegidos: list, categorias: list) -> pd.DataFrame:
    """
    Nº de licitaciones por (año, resultado) con ceros donde no hay ninguna,
    para que la gráfica no "salte" años y las barras queden alineadas.
    Columnas: anio (int), resultado (etiqueta), n.
    """
    con_anio = claros.dropna(subset=["anio"])
    if con_anio.empty:
        return pd.DataFrame(columns=["anio", "resultado", "n"])

    if anios_elegidos:
        anios = sorted(int(a) for a in anios_elegidos)
    else:
        anios = list(range(int(con_anio["anio"].min()), int(con_anio["anio"].max()) + 1))

    conteo = con_anio.groupby(["anio", "categoria"]).size()
    filas = []
    for anio in anios:
        for categoria in categorias:
            filas.append({
                "anio": anio,
                "resultado": ETIQUETAS[categoria],
                "n": int(conteo.get((anio, categoria), 0)),
            })
    return pd.DataFrame(filas)


def exito_por_empresa(claros: pd.DataFrame) -> pd.DataFrame:
    """
    % de éxito = adjudicadas / (adjudicadas + no adjudicadas), de mayor a
    menor. Con el mismo % va primero la empresa con más licitaciones (más
    fiable), y después el orden alfabético.
    Columnas: empresa, adjudicadas, total, pct.
    """
    if claros.empty:
        return pd.DataFrame(columns=["empresa", "adjudicadas", "total", "pct"])

    agrupado = claros.groupby("empresa").agg(
        adjudicadas=("categoria", lambda s: int((s == POSITIVO).sum())),
        total=("categoria", "size"),
    ).reset_index()
    agrupado["pct"] = agrupado["adjudicadas"] / agrupado["total"] * 100
    return agrupado.sort_values(["pct", "total", "empresa"], ascending=[False, False, True]).reset_index(drop=True)


def resumen_interpretacion(df: pd.DataFrame) -> pd.DataFrame:
    """Una fila por redacción distinta de RESULTADO: cómo se ha clasificado y cuántas veces aparece."""
    if df.empty:
        return pd.DataFrame(columns=["Redacción en el Excel", "Se interpreta como", "Regla aplicada", "Nº"])

    tabla = (
        df.assign(texto=df["resultado_original"].str.replace(r"\s+", " ", regex=True).str.strip())
        .groupby(["texto", "categoria", "regla"], dropna=False)
        .size()
        .reset_index(name="Nº")
    )
    nombres = {POSITIVO: ETIQUETA_POSITIVO, NEGATIVO: ETIQUETA_NEGATIVO, IGNORADO: "Se ignora"}
    tabla["Se interpreta como"] = tabla["categoria"].map(nombres)
    tabla["Redacción en el Excel"] = tabla["texto"].map(lambda t: (t[:90] + "…") if len(t) > 90 else (t or "(vacío)"))
    tabla = tabla.rename(columns={"regla": "Regla aplicada"})
    return tabla.sort_values("Nº", ascending=False)[
        ["Redacción en el Excel", "Se interpreta como", "Regla aplicada", "Nº"]
    ].reset_index(drop=True)


# ============================================================
# GRÁFICA
# ============================================================

def construir_grafica(serie: pd.DataFrame, tipo: str):
    codificacion_color = alt.Color(
        "resultado:N",
        scale=alt.Scale(domain=[ETIQUETA_POSITIVO, ETIQUETA_NEGATIVO], range=[COLOR_POSITIVO, COLOR_NEGATIVO]),
        legend=alt.Legend(title=None, orient="top", direction="horizontal"),
    )
    eje_x = alt.X("anio:O", title="Año", axis=alt.Axis(labelAngle=0))
    eje_y = alt.Y("n:Q", title="Número de resultados", axis=alt.Axis(tickMinStep=1, format="d"))
    tooltip = [
        alt.Tooltip("anio:O", title="Año"),
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


def _mostrar_html(contenido: str, altura: int):
    """
    Muestra HTML con JavaScript (el carrusel) en un iframe. `st.iframe` es lo
    que usan las versiones nuevas de Streamlit (`st.components.v1.html` está
    deprecado y se retira); en las antiguas no existe y se usa el anterior.
    Solo se le pasa HTML propio: los textos que vienen de la base de datos
    están escapados en `construir_html_carrusel`.
    """
    if hasattr(st, "iframe"):
        st.iframe(contenido, height=altura)
    else:
        import streamlit.components.v1 as components

        components.html(contenido, height=altura)


def _ancho_completo(mostrar, *args, **kwargs):
    """
    Estira un elemento de Streamlit al ancho disponible: `width="stretch"` en las
    versiones nuevas y `use_container_width=True` en las antiguas (las nuevas
    van retirando ese parámetro).
    """
    try:
        return mostrar(*args, width="stretch", **kwargs)
    except Exception:
        return mostrar(*args, use_container_width=True, **kwargs)


# ============================================================
# CARRUSEL
# ============================================================

CSS_CARRUSEL = f"""
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: transparent; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
.wrap {{ position: relative; padding: 4px 0; }}
.track {{
    display: flex; gap: 10px; overflow-x: auto; scroll-snap-type: x mandatory;
    scroll-behavior: smooth; scrollbar-width: none; padding: 4px 2px 6px 2px; outline: none;
}}
.track::-webkit-scrollbar {{ display: none; }}
.card {{
    flex: 0 0 188px; height: 104px; scroll-snap-align: start; padding: 10px 12px 10px 12px;
    background: #ffffff; border: 1px solid #dfe5f1; border-radius: 12px;
    box-shadow: 0 1px 3px rgba(23, 32, 51, 0.07); display: flex; flex-direction: column; justify-content: space-between;
    transition: box-shadow .15s ease, border-color .15s ease, transform .15s ease;
}}
.card:hover {{ border-color: {COLOR_POSITIVO}; box-shadow: 0 4px 10px rgba(0, 102, 204, 0.16); transform: translateY(-1px); }}
.top {{ display: flex; align-items: flex-start; gap: 7px; }}
.rank {{
    flex: 0 0 auto; min-width: 22px; height: 18px; padding: 0 6px; border-radius: 999px; background: #e8f0fe;
    color: #0052a3; font-size: 10.5px; font-weight: 700; display: inline-flex; align-items: center; justify-content: center;
}}
.name {{
    font-size: 12px; font-weight: 600; color: #172033; line-height: 1.25; display: -webkit-box;
    -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; word-break: break-word;
}}
.mid {{ display: flex; align-items: baseline; justify-content: space-between; }}
.pct {{ font-size: 24px; font-weight: 800; color: #172033; letter-spacing: -0.5px; line-height: 1; }}
.pct small {{ font-size: 13px; font-weight: 700; margin-left: 1px; }}
.det {{ font-size: 11px; color: #5b6470; }}
.bar {{ height: 6px; border-radius: 999px; background: {COLOR_NEGATIVO}33; overflow: hidden; }}
.bar > span {{ display: block; height: 100%; background: {COLOR_POSITIVO}; border-radius: 999px; }}
.flecha {{
    position: absolute; top: 50%; transform: translateY(-50%); z-index: 2; width: 30px; height: 30px; border-radius: 50%;
    border: 1px solid #dfe5f1; background: rgba(255, 255, 255, 0.96); color: #172033; font-size: 16px; line-height: 1;
    cursor: pointer; box-shadow: 0 2px 6px rgba(23, 32, 51, 0.18); display: flex; align-items: center; justify-content: center;
}}
.flecha:hover:not(:disabled) {{ background: {COLOR_POSITIVO}; color: #ffffff; border-color: {COLOR_POSITIVO}; }}
.flecha:focus-visible {{ outline: 2px solid {COLOR_POSITIVO}; outline-offset: 2px; }}
.flecha:disabled {{ opacity: 0; pointer-events: none; }}
#prev {{ left: -4px; }}
#next {{ right: -4px; }}
"""

JS_CARRUSEL = """
const track = document.getElementById('track');
const prev = document.getElementById('prev');
const next = document.getElementById('next');
const paso = () => Math.max(track.clientWidth * 0.8, 200);
function actualizar() {
    prev.disabled = track.scrollLeft <= 2;
    next.disabled = track.scrollLeft + track.clientWidth >= track.scrollWidth - 2;
}
prev.addEventListener('click', () => track.scrollBy({ left: -paso(), behavior: 'smooth' }));
next.addEventListener('click', () => track.scrollBy({ left: paso(), behavior: 'smooth' }));
track.addEventListener('scroll', actualizar, { passive: true });
window.addEventListener('resize', actualizar);
actualizar();
"""


def _formatear_pct(valor: float) -> str:
    return f"{valor:.1f}".replace(".", ",")


def construir_html_carrusel(empresas: pd.DataFrame) -> str:
    """HTML autocontenido (CSS + JS) del carrusel; una tarjeta por fila de `empresas`."""
    tarjetas = []
    for posicion, fila in enumerate(empresas.itertuples(index=False), start=1):
        nombre = html.escape(str(fila.empresa))
        pct = float(fila.pct)
        detalle = f"{int(fila.adjudicadas)} de {int(fila.total)} adjudicadas"
        ayuda = html.escape(f"{fila.empresa}: {_formatear_pct(pct)} % ({detalle})", quote=True)
        tarjetas.append(
            f'<div class="card" title="{ayuda}">'
            f'<div class="top"><span class="rank">#{posicion}</span><span class="name">{nombre}</span></div>'
            f'<div class="mid"><span class="pct">{pct:.0f}<small>%</small></span><span class="det">{html.escape(detalle)}</span></div>'
            f'<div class="bar"><span style="width:{pct:.1f}%"></span></div>'
            f"</div>"
        )

    return (
        f"<style>{CSS_CARRUSEL}</style>"
        '<div class="wrap">'
        '<button class="flecha" id="prev" type="button" aria-label="Anterior">&#10094;</button>'
        '<div class="track" id="track" role="region" aria-label="Porcentaje de éxito por empresa" tabindex="0">'
        + "".join(tarjetas)
        + "</div>"
        '<button class="flecha" id="next" type="button" aria-label="Siguiente">&#10095;</button>'
        "</div>"
        f"<script>{JS_CARRUSEL}</script>"
    )


# ============================================================
# PESTAÑA
# ============================================================

def render_tab4(supabase: Client):
    st.subheader("Estadísticas de licitaciones")
    st.caption(
        "Resultados de las referencias de la hoja «REFERENCIAS P BÚSQUEDAS» del Excel. "
        "Solo cuentan las licitaciones con un resultado claro (adjudicada o no adjudicada)."
    )

    try:
        with st.spinner("Cargando estadísticas..."):
            df, duplicadas_hms = cargar_referencias(supabase)
    except Exception as error:
        st.error(f"No se han podido cargar las referencias de Supabase: {error}")
        return

    claros = df[df["categoria"] != IGNORADO]
    if claros.empty:
        st.info("Todavía no hay referencias con un resultado claro (adjudicada / no adjudicada) para mostrar.")
        return

    # ---------------- Filtros ----------------
    opciones_empresas = sorted(claros["empresa"].unique(), key=str.casefold)
    opciones_anios = sorted((int(a) for a in claros["anio"].dropna().unique()), reverse=True)

    col_empresa, col_anio, col_resultado, col_tipo = st.columns([3, 2, 2, 2])
    with col_empresa:
        filtro_empresas = st.multiselect("Empresa", opciones_empresas, key="tab4_filtro_empresa", placeholder="Todas")
    with col_anio:
        filtro_anios = st.multiselect("Año", opciones_anios, key="tab4_filtro_anio", placeholder="Todos")
    with col_resultado:
        filtro_resultados = st.multiselect(
            "Resultado", [ETIQUETA_POSITIVO, ETIQUETA_NEGATIVO], key="tab4_filtro_resultado", placeholder="Todos"
        )
    with col_tipo:
        tipo_grafica = st.radio("Tipo de gráfica", ["Barras", "Líneas"], horizontal=True, key="tab4_tipo_grafica")

    base = filtrar(claros, filtro_empresas, filtro_anios)

    categorias = [c for c, etiqueta in ETIQUETAS.items() if not filtro_resultados or etiqueta in filtro_resultados]
    seleccion = base[base["categoria"].isin(categorias)]

    # ---------------- Gráfica ----------------
    serie = serie_por_anio(seleccion, filtro_anios, categorias)
    if serie.empty or int(serie["n"].sum()) == 0:
        st.info("No hay licitaciones con resultado claro para esta combinación de filtros.")
    else:
        n_pos = int((seleccion["categoria"] == POSITIVO).sum())
        n_neg = int((seleccion["categoria"] == NEGATIVO).sum())
        sin_anio = int(seleccion["anio"].isna().sum())
        texto_resumen = f"{n_pos + n_neg} licitaciones con resultado claro: {n_pos} adjudicadas · {n_neg} no adjudicadas"
        if sin_anio and not filtro_anios:
            texto_resumen += f" ({sin_anio} sin año en FECHA no aparecen en la gráfica)"
        st.caption(texto_resumen)
        _ancho_completo(st.altair_chart, construir_grafica(serie, tipo_grafica))

    # ---------------- Carrusel de % de éxito ----------------
    st.markdown("#### Porcentaje de éxito por empresa")
    exito = exito_por_empresa(base)
    if exito.empty:
        st.info("No hay empresas con resultado claro para esta combinación de filtros.")
    else:
        _mostrar_html(construir_html_carrusel(exito), ALTURA_CARRUSEL)
        st.caption(
            "Adjudicadas / (adjudicadas + no adjudicadas), de mayor a menor; a igual porcentaje, antes la que tiene más "
            "licitaciones. Con pocas licitaciones el porcentaje es poco representativo: fíjate en «X de Y». "
            "Respeta los filtros de empresa y año."
        )

    # ---------------- Cómo se ha interpretado RESULTADO ----------------
    with st.expander("¿Cómo se ha interpretado el campo RESULTADO?"):
        total = len(df)
        n_pos_total = int((df["categoria"] == POSITIVO).sum())
        n_neg_total = int((df["categoria"] == NEGATIVO).sum())
        st.markdown(
            f"De **{total}** referencias, **{n_pos_total}** se interpretan como adjudicadas, **{n_neg_total}** como no "
            f"adjudicadas y **{total - n_pos_total - n_neg_total}** se ignoran (sin resultado, sin información, "
            "pendientes, canceladas o no presentadas, o textos que solo nombran socios)."
        )
        if duplicadas_hms:
            st.caption(
                f"Se han descartado {duplicadas_hms} filas duplicadas de HMS (la hoja HMS repite las de "
                "«REFERENCIAS P BÚSQUEDAS»)."
            )
        _ancho_completo(st.dataframe, resumen_interpretacion(df), hide_index=True, height=360)

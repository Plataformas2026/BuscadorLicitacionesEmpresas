"""
estadisticas_organismos.py
--------------------------
Pestaña Estadísticas, sección "Porcentaje de éxito por organismo financiador".

QUÉ MUESTRA
-----------
Una gráfica de barras horizontales con un organismo financiador por fila,
de mayor a menor % de éxito. Cada barra mide el 100 % de las licitaciones con
resultado claro de ese organismo y se reparte en adjudicadas (azul) y no
adjudicadas (naranja), con los mismos colores y la misma lectura de RESULTADO
que el resto de la pestaña (`normalizacion_resultados.py`). Al final de la
barra aparece «% · X de Y». Respeta los filtros de empresa y año de la pestaña.

DE DÓNDE SALE EL ORGANISMO
--------------------------
De la columna ORGANISMO FINANCIADOR de la hoja «REFERENCIAS P BÚSQUEDAS»
(campo `organismo_financiador` de `empresas_referencias`). Es texto libre y
en el Excel real el mismo organismo está escrito de muchas formas («BID»,
«IDB», «BID\\nBANCO INTERAMERICANO DE DESARROLLO»; «Banco Mundial», «BM», «WB»,
«World Bank»...). Sin unificarlas, cada variante saldría como una barra
distinta con pocos casos. `normalizar_organismo` las junta con las reglas de
`REGLAS_ORGANISMOS`, que puedes ampliar sin tocar nada más: cada regla es
(expresión regular, nombre con el que se muestra). Se aplican sobre el texto
sin tildes ni mayúsculas y gana la que aparece ANTES en el texto (en «EC -
European Commission, AFD» sale la Unión Europea). Lo que no encaja con
ninguna regla se muestra tal cual está escrito (agrupando mayúsculas y
puntuación). Los instrumentos de la UE (Interreg, FEDER, Erasmus+, LIFE,
Horizon...) se cuentan como «Unión Europea».

Las filas sin organismo (la mayoría en el Excel real) o con «N/A», «-»,
«No aplica»... no entran. Para que el porcentaje signifique algo, solo se
muestran los organismos con al menos `MINIMO_POR_DEFECTO` licitaciones con
resultado claro; el mínimo se puede cambiar en la propia sección. Un
desplegable enseña qué textos del Excel se han agrupado bajo cada nombre.
"""
import re
import unicodedata
from collections import Counter, defaultdict

import altair as alt
import pandas as pd
import streamlit as st

from estadisticas_comun import COLOR_NEGATIVO, COLOR_POSITIVO, ancho_completo
from normalizacion_resultados import NEGATIVO, POSITIVO

MINIMO_POR_DEFECTO = 3
ETIQUETA_POSITIVO = "Adjudicada"
ETIQUETA_NEGATIVO = "No adjudicada"

_ISLAS = {
    "gran canaria": "Gran Canaria", "tenerife": "Tenerife", "palma": "La Palma", "gomera": "La Gomera",
    "hierro": "El Hierro", "lanzarote": "Lanzarote", "fuerteventura": "Fuerteventura",
}

BANCO_MUNDIAL = "Banco Mundial"
UNION_EUROPEA = "Unión Europea"
CLIENTE_MIXTO = "Cliente público / privado"

# (expresión regular sobre texto sin tildes y en minúsculas, nombre mostrado). Ver el docstring del módulo.
REGLAS_ORGANISMOS = [
    (r"international finance corporation|\bifc\b", "IFC (Grupo Banco Mundial)"),
    (r"banco mundial|world bank|worl bank|\bbirf\b|\bibrd\b|\besmap\b|\bprobl?ue\b|^wb\b|^bm\b|^ida$|^bird$", BANCO_MUNDIAL),
    (r"^bid\b|^idb$|banco interamericano|inter-american development", "BID"),
    (r"^caf\b|camara andina de fomento|corporacion andina de fomento", "CAF"),
    (r"\bafd\b|agence francaise", "AFD"),
    (r"\bbafd\b|\bafdb\b|banco africano|african development bank", "Banco Africano de Desarrollo"),
    (r"\badb\b|asian development bank|banco asiatico", "Banco Asiático de Desarrollo"),
    (r"\bebrd\b|\bberd\b|european bank for reconstruction", "BERD (EBRD)"),
    (r"\bbcie\b|centroamericano de integracion", "BCIE"),
    (r"\bkfw\b", "KfW"),
    (r"\busaid\b", "USAID"),
    (r"\baecid\b", "AECID"),
    (r"gobierno de canarias", "Gobierno de Canarias"),
    (r"cabildo (?:insular )?de (?:la |el )?(" + "|".join(_ISLAS) + r")\b", None),  # nombre = «Cabildo de <isla>»
    (r"\bproexca\b", "PROEXCA"),
    (r"\bpnud\b|\bundp\b", "PNUD (UNDP)"),
    (r"naciones unidas|\bonu\b|\bnnuu\b|^un$|\bfao\b|\bpma\b|\bunido\b|\boit\b", "Naciones Unidas (ONU)"),
    (r"\bue\b|\beu\b|^ce\b|union europea|comision europea|european commission|interreg|\bfeder\b|erasmus"
     r"|programa life|^life$|\bhorizon\b|digital europe|cordis", UNION_EUROPEA),
    (r"cliente privado|cliente publico", CLIENTE_MIXTO),
]
_REGLAS_COMPILADAS = [(re.compile(patron), nombre) for patron, nombre in REGLAS_ORGANISMOS]

# Textos que significan «no hay organismo».
_SIN_INFORMACION = {"", "-", "n/a", "na", "no aplica", "no especificado", "no especificado en el informe", "sin informacion"}


def _sin_tildes(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFD", texto.casefold())
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def _limpiar(texto) -> str:
    """Una sola línea, sin espacios sobrantes ni puntuación suelta al final."""
    return " ".join(str(texto or "").split()).strip(" .,;:")


def normalizar_organismo(valor):
    """
    Nombre unificado del organismo, o None si la fila no informa ninguno.
    Gana la regla cuya coincidencia aparece antes en el texto (a igualdad, la primera de la lista).
    """
    limpio = _limpiar(valor)
    clave = _sin_tildes(limpio)
    if clave in _SIN_INFORMACION or clave.lower() == "nan":
        return None

    mejor = None
    for orden, (patron, nombre) in enumerate(_REGLAS_COMPILADAS):
        encontrado = patron.search(clave)
        if encontrado and (mejor is None or (encontrado.start(), orden) < mejor[0]):
            if nombre is None:
                nombre = "Cabildo de " + _ISLAS[encontrado.group(1)]
            mejor = ((encontrado.start(), orden), nombre)
    return mejor[1] if mejor else limpio


# ============================================================
# CÁLCULOS (puros)
# ============================================================

def exito_por_organismo(claros: pd.DataFrame, minimo: int = MINIMO_POR_DEFECTO) -> tuple:
    """
    -> (DataFrame, nº de organismos que quedan fuera por tener menos de `minimo` licitaciones).
    Columnas: organismo, adjudicadas, no_adjudicadas, total, pct; de mayor a menor % (a igual %, antes el
    de más licitaciones y después el orden alfabético).
    """
    columnas = ["organismo", "adjudicadas", "no_adjudicadas", "total", "pct"]
    if claros.empty or "organismo_original" not in claros.columns:
        return pd.DataFrame(columns=columnas), 0

    datos = claros.assign(organismo=claros["organismo_original"].map(normalizar_organismo)).dropna(subset=["organismo"])
    if datos.empty:
        return pd.DataFrame(columns=columnas), 0

    agrupado = datos.groupby("organismo").agg(
        adjudicadas=("categoria", lambda s: int((s == POSITIVO).sum())),
        no_adjudicadas=("categoria", lambda s: int((s == NEGATIVO).sum())),
        total=("categoria", "size"),
    ).reset_index()
    agrupado["pct"] = agrupado["adjudicadas"] / agrupado["total"] * 100

    visibles = agrupado[agrupado["total"] >= minimo]
    visibles = visibles.sort_values(["pct", "total", "organismo"], ascending=[False, False, True]).reset_index(drop=True)
    return visibles[columnas], len(agrupado) - len(visibles)


def tabla_de_agrupaciones(claros: pd.DataFrame) -> pd.DataFrame:
    """Para cada nombre mostrado, los textos del Excel que se han agrupado bajo él (y cuántas filas son)."""
    if claros.empty or "organismo_original" not in claros.columns:
        return pd.DataFrame(columns=["Organismo", "Textos en el Excel", "Nº"])

    variantes = defaultdict(Counter)
    for original in claros["organismo_original"]:
        nombre = normalizar_organismo(original)
        if nombre:
            variantes[nombre][_limpiar(original)] += 1

    filas = [
        {
            "Organismo": nombre,
            "Textos en el Excel": " · ".join(f"{t} ({n})" if n > 1 else t for t, n in conteo.most_common()),
            "Nº": sum(conteo.values()),
        }
        for nombre, conteo in variantes.items()
    ]
    return pd.DataFrame(filas).sort_values(["Nº", "Organismo"], ascending=[False, True]).reset_index(drop=True)


# ============================================================
# GRÁFICA
# ============================================================

def construir_grafica_organismos(exito: pd.DataFrame):
    orden = list(exito["organismo"])
    partes = pd.concat([
        pd.DataFrame({"organismo": exito["organismo"], "resultado": ETIQUETA_POSITIVO, "n": exito["adjudicadas"]}),
        pd.DataFrame({"organismo": exito["organismo"], "resultado": ETIQUETA_NEGATIVO, "n": exito["no_adjudicadas"]}),
    ], ignore_index=True)
    partes["total"] = partes["organismo"].map(dict(zip(exito["organismo"], exito["total"])))
    partes["fraccion"] = partes["n"] / partes["total"] * 100

    eje_y = alt.Y("organismo:N", sort=orden, title=None, axis=alt.Axis(labelLimit=260))
    escala_x = alt.Scale(domain=[0, 138])

    barras = alt.Chart(partes).mark_bar().encode(
        y=eje_y,
        x=alt.X(
            "fraccion:Q", title="% de las licitaciones con resultado claro", scale=escala_x,
            axis=alt.Axis(values=[0, 25, 50, 75, 100], format="d", grid=True),
        ),
        order=alt.Order("resultado:N", sort="descending"),
        color=alt.Color(
            "resultado:N",
            scale=alt.Scale(domain=[ETIQUETA_POSITIVO, ETIQUETA_NEGATIVO], range=[COLOR_POSITIVO, COLOR_NEGATIVO]),
            legend=alt.Legend(title=None, orient="top", direction="horizontal"),
        ),
        tooltip=[
            alt.Tooltip("organismo:N", title="Organismo"),
            alt.Tooltip("resultado:N", title="Resultado"),
            alt.Tooltip("n:Q", title="Nº de licitaciones"),
            alt.Tooltip("total:Q", title="Total del organismo"),
        ],
    )

    etiquetas_df = exito.assign(
        limite=100,
        texto=exito.apply(lambda f: f"{f['pct']:.0f} % · {int(f['adjudicadas'])} de {int(f['total'])}", axis=1),
    )
    etiquetas = alt.Chart(etiquetas_df).mark_text(align="left", dx=6, fontSize=12, color="#172033").encode(
        y=eje_y, x=alt.X("limite:Q", scale=escala_x), text="texto:N"
    )
    return (barras + etiquetas).properties(height=max(120, 30 * len(orden) + 60), width="container")


# ============================================================
# SECCIÓN DE LA PESTAÑA
# ============================================================

def render_organismos(base: pd.DataFrame):
    """`base`: referencias con resultado claro ya filtradas por empresa y año (columnas `organismo_original` y `categoria`)."""
    st.markdown("#### Porcentaje de éxito por organismo financiador")

    minimo = int(st.number_input(
        "Mínimo de licitaciones por organismo", min_value=1, max_value=50, value=MINIMO_POR_DEFECTO, step=1,
        key="tab4_organismos_minimo",
        help="Con muy pocas licitaciones el porcentaje no es representativo (1 de 1 sería un 100 %).",
    ))
    exito, descartados = exito_por_organismo(base, minimo)

    if exito.empty:
        st.info(
            "No hay organismos financiadores con suficientes licitaciones de resultado claro para esta combinación "
            "de filtros. Prueba a bajar el mínimo."
        )
        return

    ancho_completo(st.altair_chart, construir_grafica_organismos(exito))

    nota = (
        "Adjudicadas / (adjudicadas + no adjudicadas) según el organismo financiador del Excel, de mayor a menor. "
        "Las filas sin organismo financiador no cuentan. Respeta los filtros de empresa y año."
    )
    if descartados:
        nota += f" Quedan fuera {descartados} organismos con menos de {minimo} licitaciones."
    st.caption(nota)

    with st.expander("¿Cómo se han agrupado los organismos?"):
        st.caption(
            "El organismo es texto libre en el Excel, así que se han unificado las variantes de un mismo organismo "
            "(p. ej. «BM», «WB» y «World Bank» → Banco Mundial; Interreg, FEDER o Erasmus+ → Unión Europea). "
            "Las reglas están en `REGLAS_ORGANISMOS` (estadisticas_organismos.py)."
        )
        ancho_completo(st.dataframe, tabla_de_agrupaciones(base), hide_index=True, height=300)

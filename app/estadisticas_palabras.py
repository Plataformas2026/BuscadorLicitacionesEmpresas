"""
estadisticas_palabras.py
------------------------
Pestaña Estadísticas, sección "Palabras clave según el estado": las 15
palabras más repetidas en los títulos de las licitaciones ADJUDICADAS y en
los de las NO ADJUDICADAS, cada una como una nube de palabras.

CÓMO SE CUENTA
--------------
- Los títulos vienen en español, inglés, portugués y francés, así que las
  palabras vacías (artículos, preposiciones, conjunciones, pronombres...) se
  quitan con las listas de los 4 idiomas (`PALABRAS_VACIAS`). Tampoco cuentan
  los números ni las palabras de menos de 3 letras.
- Cada título DISTINTO cuenta una sola vez por categoría. Una misma
  licitación aparece en varias filas cuando varias empresas la listan (en el
  Excel real, «fotovoltaica antropizada» está 10 veces): sin esto, una sola
  licitación dominaría la nube. El número que acompaña a cada palabra es el
  de títulos distintos en los que aparece.
- Singular y plural se cuentan juntos («proyecto» + «proyectos»,
  «ciudad» + «ciudades»); se muestra la forma más frecuente.
- Para excluir además palabras genéricas del sector (p. ej. «servicio»,
  «proyecto»), añádelas a `PALABRAS_EXCLUIDAS_EXTRA`.

El módulo es puro salvo `render_nubes`: las funciones de conteo y de HTML no
dependen de Streamlit y se pueden probar sueltas.
"""
import hashlib
import html
import re
import unicodedata
from collections import Counter, defaultdict

import pandas as pd
import streamlit as st

from estadisticas_comun import COLOR_NEGATIVO, COLOR_POSITIVO
from normalizacion_resultados import NEGATIVO, POSITIVO, limpiar_texto

NUMERO_DE_PALABRAS = 15
LONGITUD_MINIMA = 3

# Palabras propias de tu sector que no quieras ver en las nubes (en minúsculas).
PALABRAS_EXCLUIDAS_EXTRA = ()


def _sin_tildes(texto: str) -> str:
    texto = unicodedata.normalize("NFD", texto.casefold())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def _conjunto(texto: str) -> frozenset:
    return frozenset(_sin_tildes(p) for p in texto.split())


_ES = _conjunto("""
a al algo algun alguna algunas alguno algunos ambos ante antes aquel aquella aquellas aquello aquellos aqui asi aun
aunque bajo bien cada como con contra cual cuales cuando cuyo de del dentro desde donde dos durante e el ella ellas
ello ellos en entre era eran es esa esas ese eso esos esta estaba estan estar este esto estos fue fueron fuera ha han
hacia hasta hay la las le les lo los mas mediante me mi mis mucho muy nada ni no nos nosotros nuestra nuestras nuestro
nuestros o os otra otras otro otros para pero poco por porque que quien quienes se sea segun ser si sido sin sobre solo
son su sus tambien tanto te tiene tienen todo todas todos tras traves tu tus un una unas uno unos usted via vez vosotros
y ya
""")
_EN = _conjunto("""
a about above after again against all also am an and any are as at be because been before being below between both but
by can did do does doing down during each few for from further had has have having he her here hers him his how i if in
into is it its just me more most my no nor not now of off on once only or other our out over own per same she should so
some such than that the their them then there these they this those through to too towards under until up using very
via was we were what when where which while who whom why will with within without would you your
""")
_FR = _conjunto("""
au aux avec ce ces dans de des du elle elles en entre et eux il ils je la le les leur leurs lui ma mais me meme mes moi
mon ne nos notre nous on ou par pas pour qu que qui sa se ses son sous sur ta te tes toi ton tu un une vers vos votre
vous afin dont est sont ete etre chez c d j l m n s t y
""")
_PT = _conjunto("""
a ao aos as com como da das de dela dele deles depois do dos e ela elas ele eles em entre era essa esse essas esses esta
este eu foi isso isto ja lhe mais mas me mesmo meu minha muito na nas nem no nos nossa nosso num numa o os ou para pela
pelas pelo pelos por qual quando que quem sao se sem ser seu seus so sobre sua suas tambem te tem um uma voce dum duma
""")

PALABRAS_VACIAS = _ES | _EN | _FR | _PT


# ============================================================
# CONTEO
# ============================================================

def tokenizar(titulo) -> list:
    """Palabras significativas de un título (en minúsculas, con sus tildes)."""
    excluidas = {_sin_tildes(p) for p in PALABRAS_EXCLUIDAS_EXTRA}
    palabras = []
    for palabra in re.findall(r"[^\W\d_]+", str(titulo or "").casefold()):
        sin_tildes = _sin_tildes(palabra)
        if len(palabra) < LONGITUD_MINIMA or sin_tildes in PALABRAS_VACIAS or sin_tildes in excluidas:
            continue
        palabras.append(palabra)
    return palabras


def raiz(palabra: str) -> str:
    """Clave para contar juntos singular y plural («ciudades» -> «ciudad», «servicios» -> «servicio»)."""
    p = _sin_tildes(palabra)
    if p.endswith("es") and len(p) > 6:
        return p[:-2]
    if p.endswith("s") and not p.endswith("ss") and len(p) > 3:
        return p[:-1]
    return p


def palabras_mas_repetidas(titulos, n: int = NUMERO_DE_PALABRAS) -> tuple:
    """
    Devuelve ([(palabra, nº de títulos distintos en que aparece), ...], nº de títulos distintos
    analizados), con las `n` palabras más repetidas de mayor a menor (a igual cifra, por orden alfabético).
    """
    vistos = set()
    titulos_por_grupo = Counter()
    formas = defaultdict(Counter)

    for titulo in titulos:
        clave_titulo = limpiar_texto(titulo)
        if not clave_titulo or clave_titulo in vistos:
            continue
        vistos.add(clave_titulo)
        grupos_del_titulo = set()
        for palabra in tokenizar(titulo):
            grupo = raiz(palabra)
            formas[grupo][palabra] += 1
            grupos_del_titulo.add(grupo)
        titulos_por_grupo.update(grupos_del_titulo)

    resultado = []
    for grupo, cantidad in titulos_por_grupo.items():
        forma = sorted(formas[grupo].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        resultado.append((forma.capitalize(), cantidad))
    resultado.sort(key=lambda par: (-par[1], par[0].casefold()))
    return resultado[:n], len(vistos)


# ============================================================
# HTML (sin JavaScript: se pinta con st.markdown)
# ============================================================

PALETA_POSITIVA = ("#0a4a94", "#0066cc", "#2f80d1", "#5a9bd8")
PALETA_NEGATIVA = ("#a8390a", "#e8590c", "#ee7a3a", "#f29a68")
TAMANO_MINIMO, TAMANO_MAXIMO = 15, 36


def _orden_de_nube(palabras: list) -> list:
    """Orden estable pero 'revuelto' (por hash), para que grandes y pequeñas se mezclen como en una nube."""
    return sorted(palabras, key=lambda par: hashlib.md5(par[0].encode("utf-8")).hexdigest())


def construir_html_nube(palabras: list, paleta: tuple) -> str:
    """Una nube: cada palabra con tamaño y color según su frecuencia, y el nº de títulos en pequeño."""
    if not palabras:
        return '<div style="text-align:center;color:#8a93a3;padding:26px 0;font-size:13px">Sin datos para esta selección</div>'

    cifras = [c for _, c in palabras]
    minimo, maximo = min(cifras), max(cifras)
    elementos = []
    for palabra, cantidad in _orden_de_nube(palabras):
        relativo = 0.5 if maximo == minimo else (cantidad - minimo) / (maximo - minimo)
        tamano = round(TAMANO_MINIMO + relativo * (TAMANO_MAXIMO - TAMANO_MINIMO))
        color = paleta[min(int((1 - relativo) * len(paleta)), len(paleta) - 1)]
        peso = 800 if relativo > 0.6 else (700 if relativo > 0.25 else 600)
        texto = html.escape(palabra)
        elementos.append(
            f'<span title="{texto}: {cantidad} títulos" style="display:inline-block;margin:3px 9px;'
            f'font-size:{tamano}px;font-weight:{peso};color:{color};line-height:1.25;white-space:nowrap">'
            f'{texto}<sup style="font-size:10px;font-weight:600;color:#8a93a3;margin-left:2px">{cantidad}</sup></span>'
        )
    return '<div style="text-align:center;padding:6px 4px 2px 4px">' + "".join(elementos) + "</div>"


def _bloque(titulo: str, color: str, n_titulos: int, nube: str) -> str:
    return (
        '<div style="padding:4px 14px 10px 14px">'
        f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">'
        f'<span style="background:{color}1a;color:{color};font-weight:700;font-size:12.5px;border-radius:999px;'
        f'padding:3px 12px;border:1px solid {color}55">{html.escape(titulo)}</span>'
        f'<span style="font-size:11.5px;color:#5b6470">{n_titulos} títulos analizados</span></div>'
        f"{nube}</div>"
    )


def construir_html_panel(positivas: list, n_positivas: int, negativas: list, n_negativas: int) -> str:
    """Panel con las dos nubes lado a lado (una debajo de otra en pantallas estrechas)."""
    return (
        '<div style="background:#ffffff;border:1px solid #dfe5f1;border-radius:14px;padding:14px 8px 8px 8px;'
        'box-shadow:0 2px 8px rgba(23,32,51,0.07);margin:4px 0 6px 0">'
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:6px">'
        + _bloque("Adjudicadas", COLOR_POSITIVO, n_positivas, construir_html_nube(positivas, PALETA_POSITIVA))
        + _bloque("No adjudicadas", COLOR_NEGATIVO, n_negativas, construir_html_nube(negativas, PALETA_NEGATIVA))
        + "</div></div>"
    )


# ============================================================
# SECCIÓN DE LA PESTAÑA
# ============================================================

def render_nubes(base: pd.DataFrame):
    """
    `base`: referencias con resultado claro ya filtradas por empresa y año
    (columnas `titulo` y `categoria`).
    """
    st.markdown(f"#### Las {NUMERO_DE_PALABRAS} palabras clave más repetidas en los títulos, según el estado")

    if base.empty or "titulo" not in base.columns:
        st.info("No hay títulos con resultado claro para esta combinación de filtros.")
        return

    positivas, n_positivas = palabras_mas_repetidas(base.loc[base["categoria"] == POSITIVO, "titulo"])
    negativas, n_negativas = palabras_mas_repetidas(base.loc[base["categoria"] == NEGATIVO, "titulo"])

    # Una sola línea y sin sangrías: el parser de Markdown trataría líneas con 4 espacios como código.
    st.markdown(construir_html_panel(positivas, n_positivas, negativas, n_negativas), unsafe_allow_html=True)
    st.caption(
        "Tamaño y color según el número de licitaciones (títulos distintos) en que aparece cada palabra, que es la cifra "
        "pequeña. Se ignoran artículos, preposiciones y demás palabras vacías en español, inglés, portugués y francés. "
        "Respeta los filtros de empresa y año."
    )

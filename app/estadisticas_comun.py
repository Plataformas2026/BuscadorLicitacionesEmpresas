"""
estadisticas_comun.py
---------------------
Piezas compartidas por las secciones de la pestaña Estadísticas
(`estadisticas.py`, `estadisticas_palabras.py` y `estadisticas_fiabilidad.py`):
colores, lectura paginada de Supabase, ayudas para mostrar elementos de
Streamlit y el carrusel de tarjetas.
"""
import html
from dataclasses import dataclass

import pandas as pd
import streamlit as st

# Azul corporativo de la app (styles.py) y naranja: contraste alto, también
# para daltonismo (el verde/rojo habitual no lo tiene).
COLOR_POSITIVO = "#0066cc"
COLOR_NEGATIVO = "#e8590c"

ALTURA_CARRUSEL = 132  # px del iframe del carrusel (compacto)

NOMBRES_MESES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}


@dataclass(frozen=True)
class Perfil:
    """
    Cómo se llama el «éxito» y el «fracaso» en cada pestaña de estadísticas. Las mismas gráficas y tablas
    (empresas, organismos, palabras) sirven para las dos pestañas y solo cambian estos textos.
    """
    clave: str              # prefijo de las claves de los widgets (tiene que ser distinto en cada pestaña)
    positivo: str           # etiqueta de un caso de éxito (leyenda, filtro, tooltip)
    negativo: str           # etiqueta de un caso de fracaso
    positivos: str          # lo mismo, para titulares de listas («Adjudicadas»)
    negativos: str
    unidad_exito: str       # lo que se cuenta en «X de Y <unidad>» del carrusel
    claro: str              # «resultado claro» / «respuesta clara»
    definicion: str = ""    # definición de éxito que se repite en los textos descriptivos (opcional)
    filtros: str = "empresa y año"  # filtros que respetan las secciones («Respeta los filtros de ...»)
    formula_texto: str = ""         # cómo se expresa el % de éxito en los textos (si no, se deduce de las etiquetas)

    @property
    def formula(self) -> str:
        return self.formula_texto or f"{self.positivos} / ({self.positivos.lower()} + {self.negativos.lower()})"


PERFIL_ADJUDICACION = Perfil(
    clave="tab4",
    positivo="Adjudicada",
    negativo="No adjudicada",
    positivos="Adjudicadas",
    negativos="No adjudicadas",
    unidad_exito="adjudicadas",
    claro="resultado claro",
)

PERFIL_INTERES = Perfil(
    clave="tab_interes",
    positivo="Interés confirmado",
    negativo="Sin interés / no encaja",
    positivos="Interés confirmado",
    negativos="Sin interés / no encaja",
    unidad_exito="con interés",
    claro="respuesta clara",
    filtros="empresa, año y mes",
    formula_texto="Con interés confirmado / (con interés confirmado + sin interés o no encaja)",
    definicion=(
        "Éxito = que se le ha informado a una empresa de la licitación, y esta ha mostrado interés y confirmado "
        "que encaja con su perfil."
    ),
)


def leer_paginado(construir_consulta, tamano_pagina: int = 1000) -> list:
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



def mostrar_html(contenido: str, altura: int):
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


def ancho_completo(mostrar, *args, **kwargs):
    """
    Estira un elemento de Streamlit al ancho disponible: `width="stretch"` en las
    versiones nuevas y `use_container_width=True` en las antiguas (las nuevas
    van retirando ese parámetro).
    """
    try:
        return mostrar(*args, width="stretch", **kwargs)
    except Exception:
        return mostrar(*args, use_container_width=True, **kwargs)



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


def construir_html_carrusel(
    empresas: pd.DataFrame,
    columna_exitos: str = "adjudicadas",
    etiqueta_exitos: str = "adjudicadas",
    descripcion: str = "Porcentaje de éxito por empresa",
) -> str:
    """
    HTML autocontenido (CSS + JS) del carrusel; una tarjeta por fila de `empresas`
    (columnas: empresa, <columna_exitos>, total, pct), en el orden recibido.
    `etiqueta_exitos` es lo que se cuenta en «X de Y <etiqueta>».
    """
    tarjetas = []
    for posicion, fila in enumerate(empresas.itertuples(index=False), start=1):
        nombre = html.escape(str(fila.empresa))
        pct = float(fila.pct)
        detalle = f"{int(getattr(fila, columna_exitos))} de {int(fila.total)} {etiqueta_exitos}"
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
        f'<div class="track" id="track" role="region" aria-label="{html.escape(descripcion, quote=True)}" tabindex="0">'
        + "".join(tarjetas)
        + "</div>"
        '<button class="flecha" id="next" type="button" aria-label="Siguiente">&#10095;</button>'
        "</div>"
        f"<script>{JS_CARRUSEL}</script>"
    )

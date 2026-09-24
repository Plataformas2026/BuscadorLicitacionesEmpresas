# -*- coding: utf-8 -*-
"""
ingesta_bcie.py
----------------
Sincroniza avisos de adquisición del BCIE (Banco Centroamericano de
Integración Económica) contra la tabla `licitaciones_internacionales` de
Supabase, leyendo la tabla real del portal:

    https://www.bcie.org/adquisiciones-en-proyectos/avisos-de-adquisicion

usando Playwright (navegador real, headless, gratuito) -- necesario porque
la tabla se renderiza con JavaScript.

Adaptado de un script de prueba que se ha ejecutado con éxito contra la
página real fuera de este entorno de desarrollo: cada aviso es una fila
`table tbody tr` con al menos 5 celdas (nº de aviso, título con enlace,
país, fecha de publicación, fecha límite, y opcionalmente días
restantes). Único cambio respecto al script de prueba (aparte de quitar
el andamiaje propio de Colab): se usa `playwright.sync_api`, igual que el
resto de scrapers de este proyecto que necesitan un navegador.

AVISO DE FIABILIDAD -- FECHAS Y DESCRIPCIÓN (corregido con HTML real)
--------------------------------------------------------------------------
Las dos versiones anteriores de este script se basaban en suposiciones
sin confirmar (formatos de fecha "a ciegas", luego fragmentos de texto
indexados por un buscador). Esta versión sí se basa en un fragmento de
HTML real de una ficha de aviso, proporcionado directamente por el
usuario -- la fuente más fiable que se ha tenido hasta ahora para esta
fuente. Confirma la estructura exacta: un
<ol class="list-decimal..."> con un <li> por bloque, cada uno con un
<span> de cabecera y uno o más <p> de contenido:
  - <li> "Objetivos Generales de la adquisición:" -> su primer <p> es
    la DESCRIPCIÓN real de la licitación (antes no se extraía en
    absoluto, se guardaba siempre None).
  - <li> "...estará disponible en:" -> contiene "A partir de: ..." y
    "Hasta: ..." (disponibilidad de la documentación).
  - <li> "...se recibirán en:" -> contiene "Fecha: ...", que es la
    fecha límite REAL de presentación de propuestas. Esto corrige una
    suposición equivocada de la versión anterior: se buscaba el
    literal "Fecha de recepción de propuesta", que NO aparece en el
    HTML real -- se mantiene esa búsqueda como red de seguridad
    adicional (por si alguna ficha antigua sí la usa), pero ya no es
    el método principal.
  - El formato de fecha confirmado en el HTML real es "DD-mon-AAAA" en
    minúsculas (p. ej. "16-sep-2026"), consistente con el "DD-Mon-AAAA"
    que ya se había confirmado antes por otra vía -- `parsear_fecha_bcie`
    ya normaliza la capitalización del mes, así que ambas formas
    funcionan sin cambios.
Por eso `ejecutar_sincronizacion()` lee la ficha de CADA aviso
(`obtener_datos_ficha`, con `requests` + BeautifulSoup para recorrer la
estructura, igual que hace ingesta_caf.py) en vez de fiarse solo de la
tabla del listado -- más lento pero mucho más fiable, y con solo 2
páginas de avisos (MAX_PAGINAS) el coste es asumible. Sigue sin poder
confirmarse en vivo en este entorno (sin salida de red hacia bcie.org):
revisa los logs "Avisos sin descripción reconocida" y "Fechas sin
reconocer" en la primera ejecución manual (workflow_dispatch).

SIN VENTANA DE FECHAS
------------------------
A diferencia de BID/UNDP/UNGM (que recorren un histórico y necesitan una
ventana de "últimos N días" para no releer avisos antiguos), el listado
de avisos de adquisición del BCIE parece mostrar únicamente los avisos
ACTUALMENTE abiertos (de ahí la columna "días restantes"). Por eso este
script sincroniza TODO lo que encuentra en el listado, sin filtrar por
fecha -- la comparación contra lo ya existente en Supabase
(`preparar_lote_para_subir`) ya evita regenerar el embedding de los
avisos que no han cambiado desde la última ejecución.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_bcie.py
Ejecucion programada: ver .github/workflows/sincronizar_bcie.yml
   (necesita el paso extra "playwright install --with-deps chromium")
"""
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://www.bcie.org"
LISTADO_URL = BASE_URL + "/adquisiciones-en-proyectos/avisos-de-adquisicion"
FUENTE = "BCIE"
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_publicacion", "fecha_limite")

TIEMPO_ESPERA_CARGA_MS = 45000
TIMEOUT_PETICION = 30
PAUSA_ENTRE_DETALLES_SEGUNDOS = 0.4
MAX_PAGINAS = 2
CAPTURA_DEPURACION = "debug_bcie_tabla.png"

CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CABECERAS_PETICION = {"User-Agent": CABECERAS_USER_AGENT}

MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
PATRON_FECHA_LARGA_ES = re.compile(
    r"(\d{1,2})\s*(?:de\s+)?(" + "|".join(MESES_ES.keys()) + r")\s*(?:de\s+)?(\d{4})",
    re.IGNORECASE,
)
# "10-Jul-2024" -- formato confirmado contra fichas reales del portal (ver
# aviso de fiabilidad más abajo): mes en abreviatura INGLESA de 3 letras,
# aunque el resto de la página esté en español.
PATRON_FECHA_CORTA = re.compile(r"(\d{1,2})[-/\s]([A-Za-z]{3,9})[-/\s](\d{4})")

# Ficha real observada: "Fecha de recepción de propuesta: 10-Jul-2024" es
# la fecha límite; "A partir de: 02-Jan-2025 Hasta: 18-Feb-2025" es el
# rango en el que está disponible la documentación (lo más parecido a una
# fecha de publicación que expone este portal -- ver aviso de fiabilidad).
PATRON_FECHA_RECEPCION = re.compile(
    r"[Ff]echa de recepci[oó]n de propuesta[s]?:?\s*(\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]\d{4})"
)

# JS de extracción tal cual se validó contra la página real.
_JS_EXTRAER_FILAS = """
() => {
    const resultados = [];
    const filas = Array.from(document.querySelectorAll('table tbody tr'));
    for (const fila of filas) {
        const celdas = fila.querySelectorAll('td');
        if (celdas.length < 5) continue;

        const id_aviso = (celdas[0].innerText || '').trim();
        const enlace = celdas[1].querySelector('a');
        const titulo = (celdas[1].innerText || '').trim();
        const href = enlace ? enlace.getAttribute('href') : '';
        const pais = (celdas[2].innerText || '').trim();
        const fecha_pub = (celdas[3].innerText || '').trim();
        const fecha_lim = (celdas[4].innerText || '').trim();

        let dias_restantes = '';
        if (celdas.length >= 6) {
            dias_restantes = (celdas[5].innerText || '').trim();
        }

        if (titulo && href) {
            resultados.push({ id_aviso, titulo, href, pais, fecha_pub, fecha_lim, dias_restantes });
        }
    }
    return resultados;
}
"""


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def parsear_fecha_bcie(texto: str):
    """
    Intenta primero 'DD-Mon-AAAA' con mes en inglés (formato confirmado
    contra fichas reales, ver PATRON_FECHA_CORTA), y como red de
    seguridad adicional otros formatos habituales en español -- ver
    aviso de fiabilidad en el docstring del módulo.
    """
    if not texto:
        return None
    texto = texto.strip()

    coincidencia = PATRON_FECHA_CORTA.search(texto)
    if coincidencia:
        dia, mes_texto, anio = coincidencia.groups()
        for formato_mes in ("%b", "%B"):
            try:
                mes = datetime.strptime(mes_texto[:3].capitalize(), "%b").month
                return date(int(anio), mes, int(dia))
            except ValueError:
                continue

    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue

    coincidencia_larga = PATRON_FECHA_LARGA_ES.search(texto)
    if coincidencia_larga:
        dia, mes_texto, anio = coincidencia_larga.groups()
        mes = MESES_ES.get(mes_texto.lower())
        if mes:
            try:
                return date(int(anio), mes, int(dia))
            except ValueError:
                pass

    return None


def _parrafos_de(li) -> list:
    return [p.get_text(" ", strip=True) for p in li.find_all("p")]


def obtener_datos_ficha(url: str) -> dict:
    resultado = {"descripcion": None, "fecha_publicacion": None, "fecha_limite": None}

    try:
        respuesta = requests.get(url, timeout=TIMEOUT_PETICION, headers=CABECERAS_PETICION)
        respuesta.raise_for_status()
    except Exception as error:
        print(f"     Error descargando la ficha: {error}", flush=True)
        return resultado

    soup = BeautifulSoup(respuesta.text, "html.parser")
    
    # BÚSQUEDA TOLERANTE DE DESCRIPCIÓN: Busca cualquier elemento cuyo texto incluya "objetivos generales"
    elemento_objetivos = soup.find(lambda tag: tag.name in ['span', 'h3', 'p', 'div'] and 'objetivos generales' in tag.get_text().lower())
    if elemento_objetivos:
        # Buscamos el primer párrafo (<p>) que esté a continuación o dentro del mismo contenedor padre (<li> o <div>)
        contenedor = elemento_objetivos.find_parent(['li', 'div', 'section'])
        if contenedor:
            parrafos = [p.get_text(" ", strip=True) for p in contenedor.find_all("p") if p.get_text(strip=True)]
            # Filtramos para asegurarnos de no coger el propio título de objetivos si fuera un párrafo
            parrafos_validos = [p for p in parrafos if 'objetivos generales' not in p.lower()]
            if parrafos_validos:
                resultado["descripcion"] = parrafos_validos[0]
            elif parrafos:
                resultado["descripcion"] = parrafos[0]

    # Si aún así fuera nulo, buscamos cualquier párrafo que hable de la finalidad o adquisición de forma genérica en la ficha
    if not resultado["descripcion"]:
        for p in soup.find_all("p"):
            texto_p = p.get_text(" ", strip=True)
            if any(k in texto_p.lower() for k in ["tiene como finalidad", "acquisition and delivery", "consiste en", "la presente adquisición"]):
                resultado["descripcion"] = texto_p
                break

    # Extracción de fechas robusta basada en la estructura de metadatos o etiquetas <li> / párrafos
    items = soup.find_all("li")
    for li in items:
        span = li.find("span")
        texto_span = (span.get_text(" ", strip=True) if span else "").lower()
        parrafos = _parrafos_de(li)

        if "recibir" in texto_span:   # "...se recibirán en:"
            for texto_p in parrafos:
                coincidencia = re.search(r"fecha:?\s*(\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]\d{4})", texto_p, re.IGNORECASE)
                if coincidencia:
                    resultado["fecha_limite"] = parsear_fecha_bcie(coincidencia.group(1))
            continue

        for texto_p in parrafos:
            coincidencia_partir = re.search(r"a partir de:?\s*(\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]\d{4})", texto_p, re.IGNORECASE)
            if coincidencia_partir:
                resultado["fecha_publicacion"] = parsear_fecha_bcie(coincidencia_partir.group(1))
            
            coincidencia_hasta = re.search(r"hasta:?\s*(\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]\d{4})", texto_p, re.IGNORECASE)
            if coincidencia_hasta and resultado["fecha_limite"] is None:
                resultado["fecha_limite"] = parsear_fecha_bcie(coincidencia_hasta.group(1))

    # Red de seguridad global para fechas si vinieron vacías
    if resultado["fecha_limite"] is None:
        texto_plano = soup.get_text(" ", strip=True)
        coincidencia_recepcion = PATRON_FECHA_RECEPCION.search(texto_plano)
        if coincidencia_recepcion:
            resultado["fecha_limite"] = parsear_fecha_bcie(coincidencia_recepcion.group(1))

    return resultado

def extraer_licitaciones_playwright() -> list:
    """
    Abre el portal y recorre hasta MAX_PAGINAS páginas (vía ?page=N),
    devolviendo TODOS los avisos vistos.
    """
    registros_por_url = {}

    try:
        with sync_playwright() as p:
            navegador = None
            try:
                navegador = p.chromium.launch(headless=True)
                pagina = navegador.new_page(user_agent=CABECERAS_USER_AGENT)

                for indice_pagina in range(1, MAX_PAGINAS + 1):
                    url_pagina = f"{LISTADO_URL}?page={indice_pagina}" if indice_pagina > 1 else LISTADO_URL
                    print(f"--> Cargando {url_pagina}...", flush=True)
                    pagina.goto(url_pagina, timeout=TIEMPO_ESPERA_CARGA_MS, wait_until="domcontentloaded")

                    try:
                        pagina.wait_for_selector("table tbody tr", timeout=15000)
                    except Exception:
                        print(f"    No se encontraron más registros en la página {indice_pagina}.", flush=True)
                        break

                    filas = pagina.evaluate(_JS_EXTRAER_FILAS)
                    print(f"    Filas obtenidas en página {indice_pagina}: {len(filas)}", flush=True)

                    for f in filas:
                        href = f.get("href")
                        if not href:
                            continue
                        url_completa = urljoin(BASE_URL, href)
                        if url_completa in registros_por_url:
                            continue
                        registros_por_url[url_completa] = {
                            "id_aviso": f.get("id_aviso") or None,
                            "titulo": f.get("titulo"),
                            "pais": f.get("pais"),
                            "fecha_pub_raw": f.get("fecha_pub"),
                            "fecha_lim_raw": f.get("fecha_lim"),
                            "dias_restantes_raw": f.get("dias_restantes"),
                            "url_oficial": url_completa,
                        }

            except Exception as error:
                print(f"Error durante la navegación con Playwright: {error}", flush=True)
                try:
                    if "pagina" in locals():
                        pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)
                        print(f"Captura de depuración guardada en {CAPTURA_DEPURACION}.", flush=True)
                except Exception:
                    pass
            finally:
                if navegador is not None:
                    try:
                        navegador.close()
                    except Exception:
                        pass
    except Exception as error:
        print(f"Error inesperado no capturado dentro de Playwright: {error}", flush=True)

    return list(registros_por_url.values())


def construir_registro(item: dict) -> dict:
    # Prioridad: datos de la FICHA (más fiables, ver obtener_datos_ficha)
    # sobre los de la tabla del listado.
    fecha_publicacion = item.get("fecha_publicacion_detalle") or parsear_fecha_bcie(item.get("fecha_pub_raw"))
    fecha_limite = item.get("fecha_limite_detalle") or parsear_fecha_bcie(item.get("fecha_lim_raw"))
    descripcion = item.get("descripcion_detalle")

    # Red de seguridad: si la fecha limite no se pudo parsear pero si hay
    # un numero reconocible de "dias restantes", se calcula a partir de
    # hoy -- ver aviso de fiabilidad en el docstring.
    if fecha_limite is None and item.get("dias_restantes_raw"):
        coincidencia_dias = re.search(r"\d+", item["dias_restantes_raw"])
        if coincidencia_dias:
            fecha_limite = date.today() + timedelta(days=int(coincidencia_dias.group()))

    pais = (item.get("pais") or "").strip() or None
    id_aviso = (item.get("id_aviso") or "").strip()
    slug_base = id_aviso or _generar_slug(item.get("titulo") or item["url_oficial"])

    return {
        "codigo_unico": f"BCIE-{_generar_slug(slug_base)}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": None,
        "titulo": item.get("titulo"),
        "descripcion": descripcion,
        "pais": pais,
        "paises": [pais] if pais else [],
        "organismo": "BCIE",
        "categoria": None,
        "url_oficial": item["url_oficial"],
        "url_documento": None,
        "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
    }


def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
        existente = registros_existentes.get(datos["codigo_unico"])
        texto_completo = (
            f"Titulo: {datos['titulo']}\n{datos.get('descripcion') or ''}\n"
            f"Pais: {datos.get('pais') or 'No especificado'}"
        )

        if existente is None:
            datos["texto_completo"] = texto_completo
            datos["embedding"] = generar_embedding(texto_completo)
            datos["es_novedad"] = True
            datos["es_actualizada"] = False
            a_subir.append(datos)
            continue

        ha_cambiado = any(
            str(existente.get(campo)) != str(datos.get(campo)) for campo in CAMPOS_COMPARABLES
        )
        if not ha_cambiado:
            continue

        datos["texto_completo"] = texto_completo
        datos["embedding"] = generar_embedding(texto_completo)
        datos["es_novedad"] = False
        datos["es_actualizada"] = True
        a_subir.append(datos)

    return a_subir


def ejecutar_sincronizacion():
    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - BCIE", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)

    crudos = extraer_licitaciones_playwright()
    print(f"\nTotal avisos rastreados (todas las páginas): {len(crudos)}", flush=True)

    if not crudos:
        print(
            "No se ha extraído ningún aviso. Revisa el log de arriba y, si existe, "
            f"{CAPTURA_DEPURACION} -- lo más probable es que la estructura real de la tabla "
            "haya cambiado respecto a 'table tbody tr' con 5+ celdas.",
            flush=True,
        )
        return

    print("\nLeyendo la ficha de cada aviso para sacar su descripción y sus fechas (más fiable que la tabla)...", flush=True)
    for indice, item in enumerate(crudos, start=1):
        print(f"  [{indice}/{len(crudos)}] {item['titulo'][:90]}", flush=True)
        datos_ficha = obtener_datos_ficha(item["url_oficial"])
        item["descripcion_detalle"] = datos_ficha["descripcion"]
        item["fecha_publicacion_detalle"] = datos_ficha["fecha_publicacion"]
        item["fecha_limite_detalle"] = datos_ficha["fecha_limite"]
        time.sleep(PAUSA_ENTRE_DETALLES_SEGUNDOS)

    normalizados = [construir_registro(item) for item in crudos]

    sin_descripcion = sum(1 for n in normalizados if not n.get("descripcion"))
    if sin_descripcion:
        print(
            f"Avisos sin descripción reconocida: {sin_descripcion}/{len(normalizados)} -- "
            "revisa si la ficha usa la misma estructura <ol class=\"list-decimal...\"> con "
            "\"Objetivos Generales de la adquisición\".",
            flush=True,
        )

    sin_fecha_limite = sum(1 for n in normalizados if not n.get("fecha_limite"))
    if sin_fecha_limite:
        print(
            f"Fechas sin reconocer (fecha_limite vacía tras el parseo y el respaldo por días "
            f"restantes): {sin_fecha_limite}/{len(normalizados)} -- ver aviso de fiabilidad en el docstring.",
            flush=True,
        )

    supabase = obtener_cliente_supabase()

    print("\nComparando con lo ya existente en Supabase...", flush=True)
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico") + CAMPOS_COMPARABLES,
        claves=[n["codigo_unico"] for n in normalizados],
    )

    lote_final = preparar_lote_para_subir(normalizados, registros_existentes)

    if not lote_final:
        print("No hay avisos nuevos ni cambios que sincronizar.", flush=True)
        return

    subidas = subir_en_lotes(
        supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
    )
    print(f"\nSincronizacion BCIE completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

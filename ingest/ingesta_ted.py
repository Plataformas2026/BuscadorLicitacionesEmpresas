# -*- coding: utf-8 -*-
"""
ingesta_ted.py
----------------
Sincroniza avisos de licitación de TED (Tenders Electronic Daily -- el
diario oficial de contratación pública de la UE) relacionados con GIZ
contra la tabla `licitaciones_internacionales` de Supabase.

INTEGRACIÓN CON PLAYWRIGHT ASÍNCRONO
--------------------------------------------------------------------------
Dado que TED es una aplicación de página única (SPA) desarrollada en Angular,
las peticiones HTTP simples con `requests` devuelven el cascarón HTML sin
los datos renderizados.

En esta versión se utiliza `playwright.async_api` junto con `nest_asyncio`
para ejecutar un navegador Chromium en segundo plano, esperar el renderizado
de la página de detalle (`wait_until="networkidle"`) y extraer de forma precisa
la fecha límite (BT-131) y la descripción (BT-24).

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local:     python ingesta_ted.py
"""
import asyncio
import re
import time
from datetime import date, datetime, timedelta

import nest_asyncio
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

# Permitir bucles de eventos anidados (necesario en entornos interactivos / Jupyter)
nest_asyncio.apply()

URL_API_BUSQUEDA = "https://api.ted.europa.eu/v3/notices/search"
URL_BASE_AVISO = "https://ted.europa.eu/de/notice/-/detail/"
FUENTE = "TED"
CONSULTA = 'FT~"GIZ" SORT BY publication-date DESC'
CAMPOS_SOLICITADOS = [
    "publication-number", "notice-title", "buyer-name", "buyer-country",
    "publication-date", "deadline", "notice-type",
]
ALCANCE = "ACTIVE"
LIMITE_POR_PAGINA = 50
MAX_PAGINAS = 10
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_publicacion", "fecha_limite")
PAUSA_ENTRE_FICHAS_SEGUNDOS = 0.3

CABECERAS_PETICION = {"Content-Type": "application/json", "Accept": "application/json"}

PAISES_ISO3_TED = {
    "AUT": "Austria", "BEL": "Bélgica", "BGR": "Bulgaria", "HRV": "Croacia",
    "CYP": "Chipre", "CZE": "República Checa", "DNK": "Dinamarca", "EST": "Estonia",
    "FIN": "Finlandia", "FRA": "Francia", "DEU": "Alemania", "GRC": "Grecia",
    "HUN": "Hungría", "ISL": "Islandia", "IRL": "Irlanda", "ITA": "Italia",
    "LVA": "Letonia", "LIE": "Liechtenstein", "LTU": "Lituania", "LUX": "Luxemburgo",
    "MLT": "Malta", "NLD": "Países Bajos", "NOR": "Noruega", "POL": "Polonia",
    "PRT": "Portugal", "ROU": "Rumanía", "SVK": "Eslovaquia", "SVN": "Eslovenia",
    "ESP": "España", "SWE": "Suecia", "CHE": "Suiza", "GBR": "Reino Unido",
    "UKR": "Ucrania", "MDA": "Moldavia", "XKX": "Kosovo",
}


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def _valor_multiidioma(valor):
    if valor is None:
        return None
    if isinstance(valor, str):
        return valor.strip() or None
    if isinstance(valor, list):
        return _valor_multiidioma(valor[0]) if valor else None
    if isinstance(valor, dict):
        for idioma in ("eng", "en", "deu", "de"):
            if idioma in valor and valor[idioma]:
                return _valor_multiidioma(valor[idioma])
        for lista in valor.values():
            resultado = _valor_multiidioma(lista)
            if resultado:
                return resultado
    return None


def _limpiar_prefijo_titulo(titulo: str) -> str:
    if not titulo:
        return None

    partes = re.split(r"\s+[–-]\s*|\s*–\s*", titulo)
    texto = partes[-1].strip() if partes else titulo.strip()
    texto = re.sub(r"^\d{7,8}[-–]\s*", "", texto)

    return texto.strip() or None


def parsear_fecha_ted(valor):
    texto = _valor_multiidioma(valor) if isinstance(valor, (dict, list)) else valor
    if not texto:
        return None
    texto = str(texto).strip()[:10]
    try:
        return datetime.strptime(texto, "%Y-%m-%d").date()
    except ValueError:
        return None


def parsear_fecha_ted_detalle(texto: str):
    """Extrae y parsea fechas en formato DD/MM/AAAA o AAAA-MM-DD presentes en el HTML."""
    if not texto:
        return None

    patron = re.search(r"(\d{2}[/.-]\d{2}[/.-]\d{4})|(\d{4}[/.-]\d{2}[/.-]\d{2})", texto)
    if patron:
        fecha_str = patron.group(0)
        for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(fecha_str, fmt).date()
            except ValueError:
                continue
    return None


def _pagina_de_resultados(token_siguiente: str = None) -> dict:
    cuerpo = {
        "query": CONSULTA,
        "fields": CAMPOS_SOLICITADOS,
        "limit": LIMITE_POR_PAGINA,
        "scope": ALCANCE,
        "paginationMode": "ITERATION",
    }
    if token_siguiente:
        cuerpo["iterationNextToken"] = token_siguiente

    respuesta = requests.post(
        URL_API_BUSQUEDA, json=cuerpo, headers=CABECERAS_PETICION, timeout=TIMEOUT_PETICION
    )
    respuesta.raise_for_status()
    return respuesta.json()


async def obtener_datos_ficha_html_async(numero_publicacion: str) -> dict:
    """
    Renderiza la página SPA de TED utilizando Playwright asíncrono para esperar
    la carga completa del DOM vía JavaScript y extraer la descripción (BT-24)
    y la fecha límite (BT-131).
    """
    resultado = {"descripcion": None, "fecha_limite": None}
    if not numero_publicacion:
        return resultado

    url_detalle = f"{URL_BASE_AVISO}{numero_publicacion}"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            await page.goto(url_detalle, wait_until="networkidle")

            try:
                await page.wait_for_selector(".notice-detail, [data-labels-key]", timeout=15000)
            except Exception:
                pass

            html_renderizado = await page.content()
            await browser.close()
    except Exception as error:
        print(f"      Error cargando con Playwright ({numero_publicacion}): {error}", flush=True)
        return resultado

    soup = BeautifulSoup(html_renderizado, "html.parser")

    # 1. BÚSQUEDA DE DESCRIPCIÓN (BT-24)
    desc_elem = soup.find(attrs={"data-labels-key": re.compile(r"BT-24")})
    if desc_elem:
        contenedor = desc_elem.find_parent("div")
        if contenedor:
            span_dato = contenedor.find("span", class_="data") or contenedor
            resultado["descripcion"] = span_dato.get_text(" ", strip=True)

    if not resultado["descripcion"]:
        for div in soup.find_all("div"):
            texto = div.get_text(" ", strip=True)
            if "Beschreibung" in texto or "Description" in texto:
                if len(texto) > 100:
                    resultado["descripcion"] = texto
                    break

    # 2. BÚSQUEDA DE FECHA LÍMITE (BT-131)
    fecha_elem = soup.find(attrs={"data-labels-key": re.compile(r"BT-131")})
    if fecha_elem:
        contenedor = fecha_elem.find_parent("div")
        if contenedor:
            resultado["fecha_limite"] = parsear_fecha_ted_detalle(contenedor.get_text())

    if not resultado["fecha_limite"]:
        texto_busqueda = re.compile(r"Frist für den Eingang|Deadline for receipt", re.IGNORECASE)
        elem_texto = soup.find(string=texto_busqueda)
        if elem_texto:
            contenedor = elem_texto.find_parent("div")
            if contenedor:
                resultado["fecha_limite"] = parsear_fecha_ted_detalle(contenedor.get_text())

    await asyncio.sleep(PAUSA_ENTRE_FICHAS_SEGUNDOS)
    return resultado


def extraer_avisos_api() -> list:
    avisos = []
    token_siguiente = None

    for indice_pagina in range(1, MAX_PAGINAS + 1):
        print(f"--> Consultando la API de TED (página {indice_pagina})...", flush=True)
        try:
            cuerpo_respuesta = _pagina_de_resultados(token_siguiente)
        except Exception as error:
            print(f"    Error consultando la API de TED: {error}", flush=True)
            break

        resultados_pagina = cuerpo_respuesta.get("notices") or cuerpo_respuesta.get("results") or []
        print(f"    Avisos recibidos en esta página: {len(resultados_pagina)}", flush=True)
        avisos.extend(resultados_pagina)

        token_siguiente = cuerpo_respuesta.get("iterationNextToken")
        if not token_siguiente or not resultados_pagina:
            break

        time.sleep(0.3)

    return avisos


async def construir_registro_async(aviso: dict) -> dict:
    numero_publicacion = _valor_multiidioma(aviso.get("publication-number")) or ""

    titulo_raw = _valor_multiidioma(aviso.get("notice-title"))
    titulo = _limpiar_prefijo_titulo(titulo_raw)

    comprador = _valor_multiidioma(aviso.get("buyer-name"))
    codigo_pais = _valor_multiidioma(aviso.get("buyer-country"))
    pais = PAISES_ISO3_TED.get((codigo_pais or "").upper(), codigo_pais) if codigo_pais else None
    tipo_aviso = _valor_multiidioma(aviso.get("notice-type"))

    fecha_publicacion = parsear_fecha_ted(aviso.get("publication-date"))
    fecha_limite_api = parsear_fecha_ted(aviso.get("deadline"))

    # Extracción asíncrona de datos desde la vista renderizada por Playwright SOLO para esta licitación
    datos_ficha = await obtener_datos_ficha_html_async(numero_publicacion)
    fecha_limite = datos_ficha["fecha_limite"] or fecha_limite_api
    descripcion = datos_ficha["descripcion"]

    if not descripcion:
        partes_descripcion = []
        if comprador:
            partes_descripcion.append(f"Organismo comprador: {comprador}.")
        if tipo_aviso:
            partes_descripcion.append(f"Tipo de aviso: {tipo_aviso}.")
        descripcion = " ".join(partes_descripcion) or None

    slug_base = numero_publicacion or _generar_slug(titulo or "sin-titulo")
    url_oficial = f"{URL_BASE_AVISO}{numero_publicacion}" if numero_publicacion else None

    return {
        "codigo_unico": f"TED-{_generar_slug(slug_base)}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": tipo_aviso,
        "titulo": titulo,
        "descripcion": descripcion,
        "pais": pais,
        "paises": [pais] if pais else [],
        "organismo": comprador or "GIZ",
        "categoria": None,
        "url_oficial": url_oficial,
        "url_documento": None,
        "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
    }


def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
        if not datos.get("titulo") or not datos.get("url_oficial"):
            continue

        existente = registros_existentes.get(datos["codigo_unico"])
        texto_completo = (
            f"Titulo: {datos['titulo']}\n{datos.get('descripcion') or ''}\n"
            f"Pais: {datos.get('pais') or 'No especificado'}"
        )

        datos_enviar = dict(datos)

        if existente is None:
            datos_enviar["texto_completo"] = texto_completo
            datos_enviar["embedding"] = generar_embedding(texto_completo)
            datos_enviar["es_novedad"] = True
            datos_enviar["es_actualizada"] = False
            a_subir.append(datos_enviar)
            continue

        ha_cambiado = any(
            str(existente.get(campo)) != str(datos_enviar.get(campo)) for campo in CAMPOS_COMPARABLES
        )
        if not ha_cambiado:
            continue

        datos_enviar["texto_completo"] = texto_completo
        datos_enviar["embedding"] = generar_embedding(texto_completo)
        datos_enviar["es_novedad"] = False
        datos_enviar["es_actualizada"] = True
        a_subir.append(datos_enviar)

    return a_subir


async def ejecutar_sincronizacion_async():
    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - TED (GIZ, via API + Playwright)", flush=True)
    print("=" * 100, flush=True)
    print(f"Consulta: {CONSULTA}  ·  Alcance: {ALCANCE}", flush=True)

    crudos = extraer_avisos_api()
    print(f"\nTotal avisos recibidos de la API (todas las páginas): {len(crudos)}", flush=True)

    if not crudos:
        print("No se ha recibido ningún aviso.", flush=True)
        return

    # --- FILTRO PREVIO POR FECHA (AYER Y HOY) ---
    hoy = date.today()
    ayer = hoy - timedelta(days=1)

    crudos_filtrados = []
    for aviso in crudos:
        fecha_pub = parsear_fecha_ted(aviso.get("publication-date"))
        if fecha_pub and (ayer <= fecha_pub <= hoy):
            crudos_filtrados.append(aviso)

    print(f"Avisos a procesar con Playwright tras filtrar por fecha (Ayer {ayer} - Hoy {hoy}): {len(crudos_filtrados)}", flush=True)

    if not crudos_filtrados:
        print("No hay avisos publicados entre ayer y hoy para procesar.", flush=True)
        return

    # Procesar UNICAMENTE las licitaciones filtradas con Playwright
    normalizados = []
    for aviso in crudos_filtrados:
        num_pub = _valor_multiidioma(aviso.get("publication-number"))
        print(f"   -> Procesando con Playwright licitación: {num_pub}", flush=True)
        registro = await construir_registro_async(aviso)
        normalizados.append(registro)

    supabase = obtener_cliente_supabase()

    print("\nComparando con lo ya existente en Supabase...", flush=True)
    claves_validas = [n["codigo_unico"] for n in normalizados if n.get("titulo") and n.get("url_oficial")]
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico") + CAMPOS_COMPARABLES,
        claves=claves_validas,
    )

    lote_final = preparar_lote_para_subir(normalizados, registros_existentes)

    if not lote_final:
        print("No hay avisos nuevos ni cambios que sincronizar.", flush=True)
        return

    subidas = subir_en_lotes(
        supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
    )
    print(f"\nSincronización TED completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


def ejecutar_sincronizacion():
    asyncio.run(ejecutar_sincronizacion_async())


if __name__ == "__main__":
    ejecutar_sincronizacion()

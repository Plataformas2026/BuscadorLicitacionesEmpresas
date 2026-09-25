# -*- coding: utf-8 -*-
"""
ingesta_service_bund.py
--------------------------
Sincroniza licitaciones del portal de contratación pública alemán
service.bund.de contra la tabla `licitaciones_internacionales` de
Supabase, mediante scraping directo del HTML (sin usar ninguna API) y
traduciendo al inglés el contenido en alemán.

Listado:  https://www.service.bund.de/Content/DE/Ausschreibungen/Suche/Formular.html?view=processForm&nn=9465610
Ficha:    https://www.service.bund.de/IMPORTE/Ausschreibungen/<sistema>/<id>.html
"""

import asyncio
import re
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://www.service.bund.de"
LISTADO_URL = (
    BASE_URL + "/Content/DE/Ausschreibungen/Suche/Formular.html"
    "?view=processForm&nn=9465610&sortOrder=dateOfIssue_dt+desc&resultsPerPage=100"
)
FUENTE = "SERVICE_BUND"
TIMEOUT_PETICION = 30
CONCURRENCIA_MAXIMA = 5   # Límite de peticiones/traducciones simultáneas
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_publicacion", "fecha_limite")
MAX_PAGINAS = 3
CAPTURA_DEPURACION = "debug_service_bund_listado.html"

CABECERAS_PETICION = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

PATRON_URL_DETALLE = re.compile(r"IMPORTE/Ausschreibungen/([^\"'?;]+?/[0-9a-zA-Z-]+)\.html")
PATRON_TITULO_ATTR = re.compile(r"Zur Ausschreibung\s+[\u2018'](.+)[\u2019']$")
PATRON_FECHA_LISTADO = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2})\b")
PATRON_FECHA_DETALLE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
PATRON_BOILERPLATE_HINWEIS = re.compile(r"ist nur die ver(?:[o\u00f6]|oe)ffentlichungsplattform", re.IGNORECASE)


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def _limpiar_texto(texto: str) -> str:
    if not texto:
        return None
    texto = texto.replace("\u00ad", "")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def parsear_fecha_listado_bund(texto: str):
    if not texto:
        return None
    coincidencia = PATRON_FECHA_LISTADO.search(texto)
    if not coincidencia:
        return None
    dia, mes, anio2 = coincidencia.groups()
    try:
        return date(2000 + int(anio2), int(mes), int(dia))
    except ValueError:
        return None


def parsear_fecha_detalle_bund(texto: str):
    if not texto:
        return None
    coincidencia = PATRON_FECHA_DETALLE.search(texto)
    if not coincidencia:
        return None
    dia, mes, anio = coincidencia.groups()
    try:
        return date(int(anio), int(mes), int(dia))
    except ValueError:
        return None


async def _traducir_al_ingles(texto: str) -> str:
    if not texto:
        return None
    try:
        return await asyncio.to_thread(
            lambda: GoogleTranslator(source="de", target="en").translate(texto)
        )
    except Exception as error:
        print(f"      Aviso: fallo al traducir ('{texto[:40]}...'): {error}", flush=True)
        return texto


def extraer_avisos_listado(desde: date, hasta: date) -> list:
    encontrados = {}
    detenerse = False

    for indice_pagina in range(MAX_PAGINAS):
        url_pagina = LISTADO_URL + (f"&page={indice_pagina + 1}" if indice_pagina else "")
        print(f"--> Descargando listado (página {indice_pagina + 1}): {url_pagina}", flush=True)
        try:
            respuesta = requests.get(url_pagina, timeout=TIMEOUT_PETICION, headers=CABECERAS_PETICION)
            respuesta.raise_for_status()
        except Exception as error:
            print(f"    Error descargando el listado: {error}", flush=True)
            break

        if indice_pagina == 0:
            try:
                with open(CAPTURA_DEPURACION, "w", encoding="utf-8") as f:
                    f.write(respuesta.text)
            except Exception:
                pass

        soup = BeautifulSoup(respuesta.text, "html.parser")
        lista = soup.select_one("ul.result-list")
        items = lista.find_all("li", recursive=False) if lista else []
        print(f"    Avisos en la página (<li> de ul.result-list): {len(items)}", flush=True)

        if not items:
            break

        fecha_mas_antigua_de_la_pagina = None
        for li in items:
            enlace = li.find("a", href=True)
            if not enlace:
                continue

            href_absoluto = urljoin(BASE_URL + "/", enlace["href"])
            coincidencia_id = PATRON_URL_DETALLE.search(href_absoluto)
            if not coincidencia_id:
                print(f"    Aviso: no se reconoció el patrón de URL en {href_absoluto[:100]}", flush=True)
                continue

            url_oficial = href_absoluto.split(";jsessionid=")[0]
            if url_oficial in encontrados:
                continue

            div_fecha_pub = enlace.find("div", attrs={"aria-labelledby": "date"})
            fecha_publicacion = parsear_fecha_listado_bund(
                div_fecha_pub.get_text(" ", strip=True) if div_fecha_pub else ""
            )

            div_fecha_lim = enlace.find("div", attrs={"aria-labelledby": "location"})
            fecha_limite_listado = parsear_fecha_listado_bund(
                div_fecha_lim.get_text(" ", strip=True) if div_fecha_lim else ""
            )

            if fecha_mas_antigua_de_la_pagina is None or (
                fecha_publicacion and fecha_publicacion < fecha_mas_antigua_de_la_pagina
            ):
                fecha_mas_antigua_de_la_pagina = fecha_publicacion

            if fecha_publicacion is not None and not (desde <= fecha_publicacion <= hasta):
                continue

            titulo_attr = enlace.get("title", "")
            coincidencia_titulo = PATRON_TITULO_ATTR.search(titulo_attr)
            titulo_listado = coincidencia_titulo.group(1) if coincidencia_titulo else None
            if not titulo_listado:
                h3 = enlace.find("h3")
                if h3:
                    titulo_listado = _limpiar_texto(h3.get_text(" ", strip=True)).removeprefix("Ausschreibung").strip()

            encontrados[url_oficial] = {
                "titulo_listado": _limpiar_texto(titulo_listado),
                "url_oficial": url_oficial,
                "slug_base": coincidencia_id.group(1),
                "fecha_publicacion_listado": fecha_publicacion,
                "fecha_limite_listado": fecha_limite_listado,
            }

        if fecha_mas_antigua_de_la_pagina and fecha_mas_antigua_de_la_pagina < desde:
            detenerse = True

        if detenerse:
            break

    return list(encontrados.values())


def obtener_datos_ficha(url: str) -> dict:
    resultado = {
        "titulo": None, "organismo": None, "descripcion": None,
        "tipo_aviso": None, "fecha_limite": None,
    }

    try:
        respuesta = requests.get(url, timeout=TIMEOUT_PETICION, headers=CABECERAS_PETICION)
        respuesta.raise_for_status()
    except Exception as error:
        print(f"      Error descargando la ficha: {error}", flush=True)
        return resultado

    soup = BeautifulSoup(respuesta.text, "html.parser")

    etiqueta_titulo = soup.find("title")
    if etiqueta_titulo:
        partes = etiqueta_titulo.get_text().split(" - ")
        resultado["titulo"] = _limpiar_texto(partes[-1]) if partes else None

    def _valor_por_dt(clave: str):
        for dt in soup.find_all("dt"):
            if dt.get_text(strip=True).lower() == clave.lower():
                dd = dt.find_next_sibling("dd")
                if dd:
                    return _limpiar_texto(dd.get_text(" ", strip=True))
        return None

    resultado["organismo"] = _valor_por_dt("Vergabestelle")
    resultado["descripcion"] = _valor_por_dt("Leistungen und Erzeugnisse")
    resultado["tipo_aviso"] = _valor_por_dt("Vergabeart")

    texto_deadline = _valor_por_dt("Angebotsfrist")
    resultado["fecha_limite"] = parsear_fecha_detalle_bund(texto_deadline)

    parrafo_largo = None
    for p in soup.find_all("p"):
        texto_p = _limpiar_texto(p.get_text(" ", strip=True))
        if not texto_p or len(texto_p) < 40:
            continue
        if PATRON_BOILERPLATE_HINWEIS.search(texto_p):
            continue
        parrafo_largo = texto_p
        break

    if not resultado["descripcion"]:
        resultado["descripcion"] = parrafo_largo
    elif parrafo_largo:
        resultado["descripcion"] = f"{resultado['descripcion']}. {parrafo_largo}"

    return resultado


async def _procesar_aviso(item: dict, semaforo: asyncio.Semaphore) -> dict:
    async with semaforo:
        datos_ficha = await asyncio.to_thread(obtener_datos_ficha, item["url_oficial"])

    titulo_de = datos_ficha.get("titulo") or item.get("titulo_listado")
    organismo_de = datos_ficha.get("organismo")
    descripcion_de = datos_ficha.get("descripcion")
    tipo_aviso_de = datos_ficha.get("tipo_aviso")

    # Traducción de campos en paralelo
    titulo_en, organismo_en, descripcion_en, tipo_aviso_en = await asyncio.gather(
        _traducir_al_ingles(titulo_de),
        _traducir_al_ingles(organismo_de),
        _traducir_al_ingles(descripcion_de),
        _traducir_al_ingles(tipo_aviso_de),
    )

    fecha_publicacion = item.get("fecha_publicacion_listado")
    fecha_limite = datos_ficha.get("fecha_limite") or item.get("fecha_limite_listado")

    slug_base = item.get("slug_base") or _generar_slug(titulo_en or titulo_de or item["url_oficial"])

    return {
        "codigo_unico": f"BUND-{_generar_slug(slug_base)}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": tipo_aviso_en or "Tender",
        "titulo": titulo_en or titulo_de,
        "descripcion": descripcion_en or descripcion_de,
        "pais": "Germany",
        "paises": ["Germany"],
        "organismo": organismo_en or organismo_de or "Federal Republic of Germany",
        "categoria": None,
        "url_oficial": item["url_oficial"],
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


async def ejecutar_sincronizacion_async():
    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - SERVICE.BUND.DE", flush=True)
    print("=" * 100, flush=True)

    hoy = date.today()
    ayer = hoy - timedelta(days=1)
    print(f"Ventana de fecha de publicación objetivo: {ayer} .. {hoy}", flush=True)

    crudos = await asyncio.to_thread(extraer_avisos_listado, ayer, hoy)
    print(f"\nAvisos detectados en el listado dentro de la ventana: {len(crudos)}", flush=True)

    if not crudos:
        print(
            "No se ha detectado ningún aviso en el listado dentro de la ventana de fechas. "
            f"Revisa el log de arriba y, si existe, {CAPTURA_DEPURACION}.",
            flush=True,
        )
        return

    print("\nConsultando la ficha de cada aviso y traduciendo (de forma concurrente)...", flush=True)
    semaforo = asyncio.Semaphore(CONCURRENCIA_MAXIMA)
    normalizados = await asyncio.gather(*(_procesar_aviso(item, semaforo) for item in crudos))

    sin_fecha_limite = sum(1 for n in normalizados if not n.get("fecha_limite"))
    if sin_fecha_limite:
        print(
            f"Avisos sin fecha límite reconocida: {sin_fecha_limite}/{len(normalizados)}.",
            flush=True,
        )

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
    print(f"\nSincronizacion service.bund.de completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


def ejecutar_sincronizacion():
    asyncio.run(ejecutar_sincronizacion_async())


if __name__ == "__main__":
    ejecutar_sincronizacion()

# -*- coding: utf-8 -*-
"""
ingesta_service_bund.py
--------------------------
Sincroniza licitaciones del portal de contratación pública alemán
service.bund.de contra la tabla `licitaciones_internacionales` de
Supabase, mediante scraping directo del HTML (sin usar ninguna API).

    Listado:  https://www.service.bund.de/Content/DE/Ausschreibungen/Suche/Formular.html
    Ficha:    https://www.service.bund.de/IMPORTE/Ausschreibungen/<sistema>/<id>.html
"""
import asyncio
import re
import time
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://www.service.bund.de"
LISTADO_ACTION_URL = BASE_URL + "/Content/DE/Ausschreibungen/Suche/Formular.html"

PARAMS_BASE_LISTADO = {
    "nn": "4641482",
    "resourceId": "4641464",
    "input_": "4641482",
    "pageLocale": "de",
    "resultsPerPage": "100",
    "gts": "4642258_list=dateOfIssue_dt+desc",
}

PREFIJO_LISTA = "4642258"
FUENTE = "SERVICE_BUND"
TIMEOUT_CONEXION = 10
TIMEOUT_LECTURA = 30
MAX_REINTENTOS_PETICION = 3
CONCURRENCIA_MAXIMA = 6
LOTE_ENVIO_SUPABASE = 15
MAX_PAGINAS = 15
CAPTURA_DEPURACION = "debug_service_bund_listado.html"

CABECERAS_PETICION = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

PATRON_URL_DETALLE = re.compile(r"IMPORTE/Ausschreibungen/([^\"'?;]+?/[0-9a-zA-Z-]+)\.html")
PATRON_TITULO_ATTR = re.compile(r"Zur Ausschreibung\s+[\u2018'](.+)[\u2019']$")
PATRON_FECHA_LISTADO = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2})\b")
PATRON_FECHA_DETALLE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
PATRON_BOILERPLATE_HINWEIS = re.compile(r"ist nur die ver(?:[o\u00f6]|oe)ffentlichungsplattform", re.IGNORECASE)


def _crear_sesion_http() -> requests.Session:
    sesion = requests.Session()
    estrategia_reintento = Retry(
        total=2,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adaptador = HTTPAdapter(max_retries=estrategia_reintento, pool_maxsize=CONCURRENCIA_MAXIMA + 2)
    sesion.mount("https://", adaptador)
    sesion.mount("http://", adaptador)
    sesion.headers.update(CABECERAS_PETICION)
    return sesion


SESION_HTTP = _crear_sesion_http()


def _peticion_con_reintentos(url: str, params: dict = None, max_intentos: int = MAX_REINTENTOS_PETICION):
    for intento in range(1, max_intentos + 1):
        try:
            respuesta = SESION_HTTP.get(url, params=params, timeout=(TIMEOUT_CONEXION, TIMEOUT_LECTURA))
            respuesta.raise_for_status()
            return respuesta
        except requests.exceptions.RequestException as error:
            if intento < max_intentos:
                espera = intento * 3
                print(
                    f"    Aviso: fallo de red (intento {intento}/{max_intentos}) en "
                    f"{url[:90]}: {error} -- reintentando en {espera}s...",
                    flush=True,
                )
                time.sleep(espera)
            else:
                print(f"    Error definitivo tras {max_intentos} intentos en {url[:90]}: {error}", flush=True)
    return None


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


def extraer_avisos_listado(desde: date, hasta: date) -> list:
    encontrados = {}

    for indice_pagina in range(1, MAX_PAGINAS + 1):
        parametros = dict(PARAMS_BASE_LISTADO)
        if indice_pagina > 1:
            parametros["gtp"] = f"{PREFIJO_LISTA}_list={indice_pagina}"

        print(f"--> Descargando listado (página {indice_pagina})...", flush=True)
        respuesta = _peticion_con_reintentos(LISTADO_ACTION_URL, params=parametros)
        if respuesta is None:
            print(f"    No se pudo descargar la página {indice_pagina} tras varios intentos, se detiene el listado.", flush=True)
            break

        if indice_pagina == 1:
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
            print("    No se encontraron más elementos en el listado HTML.", flush=True)
            break

        fecha_mas_antigua_de_la_pagina = None
        nuevos_en_esta_pagina = 0

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

            if url_oficial in encontrados:
                continue

            titulo_attr = enlace.get("title", "")
            coincidencia_titulo = PATRON_TITULO_ATTR.search(titulo_attr)
            titulo_listado = coincidencia_titulo.group(1) if coincidencia_titulo else None
            if not titulo_listado:
                h3 = enlace.find("h3")
                if h3:
                    titulo_listado = _limpiar_texto(h3.get_text(" ", strip=True))
                    if titulo_listado:
                        titulo_listado = titulo_listado.removeprefix("Ausschreibung").strip()

            slug_base = coincidencia_id.group(1)
            encontrados[url_oficial] = {
                "titulo_listado": _limpiar_texto(titulo_listado),
                "url_oficial": url_oficial,
                "codigo_unico": f"BUND-{_generar_slug(slug_base)}"[:150],
                "fecha_publicacion_listado": fecha_publicacion,
                "fecha_limite_listado": fecha_limite_listado,
            }
            nuevos_en_esta_pagina += 1

        print(f"    Avisos procesados en esta página dentro de la ventana: {nuevos_en_esta_pagina}", flush=True)

        # Si tras procesar toda la página el aviso más antiguo de la misma ya es anterior a 'desde',
        # no tiene sentido pedir la siguiente página porque el listado está ordenado descendente.
        if fecha_mas_antigua_de_la_pagina and fecha_mas_antigua_de_la_pagina < desde:
            print(f"    Alcanzada fecha anterior a la ventana objetivo ({fecha_mas_antigua_de_la_pagina} < {desde}). Finalizando paginación.", flush=True)
            break

    return list(encontrados.values())


def obtener_datos_ficha(url: str) -> dict:
    resultado = {
        "titulo": None, "organismo": None, "descripcion": None,
        "tipo_aviso": None, "fecha_limite": None,
    }

    respuesta = _peticion_con_reintentos(url)
    if respuesta is None:
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
    resultado["fecha_limite"] = parsear_fecha_detalle_bund(_valor_por_dt("Angebotsfrist"))

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


async def _obtener_ficha_de_aviso(item: dict, semaforo: asyncio.Semaphore) -> dict:
    async with semaforo:
        datos_ficha = await asyncio.to_thread(obtener_datos_ficha, item["url_oficial"])
    return {**item, "datos_ficha": datos_ficha}


def construir_registro(item: dict) -> dict:
    datos_ficha = item.get("datos_ficha", {})
    titulo = datos_ficha.get("titulo") or item.get("titulo_listado")
    organismo = datos_ficha.get("organismo")
    descripcion = datos_ficha.get("descripcion")
    tipo_aviso = datos_ficha.get("tipo_aviso")

    fecha_publicacion = item.get("fecha_publicacion_listado")
    fecha_limite = datos_ficha.get("fecha_limite") or item.get("fecha_limite_listado")

    return {
        "codigo_unico": item["codigo_unico"],
        "fuente_origen": FUENTE,
        "tipo_aviso": tipo_aviso or "Ausschreibung",
        "titulo": titulo,
        "descripcion": descripcion,
        "pais": "Germany",
        "paises": ["Germany"],
        "organismo": organismo or "Bundesrepublik Deutschland",
        "categoria": None,
        "url_oficial": item["url_oficial"],
        "url_documento": None,
        "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
    }


def _finalizar_para_subir(registros: list) -> list:
    finales = []
    for datos in registros:
        if not datos.get("titulo") or not datos.get("url_oficial"):
            continue
        texto_completo = (
            f"Titulo: {datos['titulo']}\n{datos.get('descripcion') or ''}\n"
            f"Pais: {datos.get('pais') or 'No especificado'}"
        )
        datos["texto_completo"] = texto_completo
        datos["embedding"] = generar_embedding(texto_completo)
        datos["es_novedad"] = True
        datos["es_actualizada"] = False
        finales.append(datos)
    return finales


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

    supabase = obtener_cliente_supabase()

    print("\nComprobando en Supabase cuáles avisos ya existen (ANTES de descargar ninguna ficha)...", flush=True)
    codigos_del_listado = [item["codigo_unico"] for item in crudos]
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico"),
        claves=codigos_del_listado,
    )

    avisos_nuevos = [item for item in crudos if item["codigo_unico"] not in registros_existentes]
    print(
        f"Avisos ya existentes en Supabase (se omite su ficha): {len(crudos) - len(avisos_nuevos)}/{len(crudos)}",
        flush=True,
    )
    print(f"Avisos NUEVOS a procesar (se descarga su ficha): {len(avisos_nuevos)}", flush=True)

    if not avisos_nuevos:
        print("No hay avisos nuevos que procesar.", flush=True)
        return

    print(f"\nDescargando la ficha de cada aviso nuevo (hasta {CONCURRENCIA_MAXIMA} a la vez)...", flush=True)
    semaforo = asyncio.Semaphore(CONCURRENCIA_MAXIMA)
    resultados_ficha = await asyncio.gather(
        *(_obtener_ficha_de_aviso(item, semaforo) for item in avisos_nuevos),
        return_exceptions=True,
    )

    items_con_ficha = []
    for item_original, resultado in zip(avisos_nuevos, resultados_ficha):
        if isinstance(resultado, Exception):
            print(
                f"    Aviso: fallo inesperado procesando {item_original.get('url_oficial')}: "
                f"{resultado} -- se omite este aviso, se sigue con el resto.",
                flush=True,
            )
            continue
        items_con_ficha.append(resultado)

    if not items_con_ficha:
        print("Ningún aviso nuevo pudo procesarse correctamente.", flush=True)
        return

    normalizados = [construir_registro(item) for item in items_con_ficha]

    sin_fecha_limite = sum(1 for n in normalizados if not n.get("fecha_limite"))
    if sin_fecha_limite:
        print(f"Avisos sin fecha límite reconocida: {sin_fecha_limite}/{len(normalizados)}.", flush=True)

    lote_final = _finalizar_para_subir(normalizados)

    if not lote_final:
        print("No hay avisos válidos que subir.", flush=True)
        return

    subidas = subir_en_lotes(
        supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
    )
    print(f"\nSincronizacion service.bund.de completada: {subidas}/{len(lote_final)} registros subidos (todos nuevos).", flush=True)


def ejecutar_sincronizacion():
    asyncio.run(ejecutar_sincronizacion_async())


if __name__ == "__main__":
    ejecutar_sincronizacion()

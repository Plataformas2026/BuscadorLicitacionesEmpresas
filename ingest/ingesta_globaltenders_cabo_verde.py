# -*- coding: utf-8 -*-
"""
ingesta_globaltenders_cabo_verde.py
-----------------------------------
Sincroniza las licitaciones VIVAS de Cabo Verde publicadas en Global Tenders
contra la tabla `licitaciones_internacionales` de Supabase, mediante scraping
directo del HTML (requests + BeautifulSoup, sin Playwright).

    Listado:  https://www.globaltenders.com/gov-tenders/es-licitaciones-del-gobierno-cabo-verde
    Ficha:    https://www.globaltenders.com/tender-detail/<slug>-<id>

ESTRUCTURA DEL PORTAL
--------------------------------------------------------------------------
- LISTADO: cada licitación es una tarjeta con título, país, fecha de
  publicación ("06 Oct 2026"), fecha límite ("19 Oct 2026") y un enlace
  "View Detail" a la ficha. Algunas palabras del título aparecen resaltadas
  en negrita (<b>/<strong>): son las PALABRAS CLAVE / etiquetas del aviso.
- FICHA: frase de cabecera ("<Organismo> has Released a tender for <resumen>
  in <Sector>. The tender was released on <fecha>."), seguida de pares
  "Etiqueta - valor": Country, Summary, Deadline, GT reference number,
  Product classification (categoría), Organization Details (Address, Contact
  details, Tender notice no., Document Type) y "Notice Details and Documents"
  con la Description. El pliego está tras login, así que `url_documento` es
  NULL.

MAPEO
--------------------------------------------------------------------------
- `descripcion` = "Palabras clave: a, b, c." + texto COMPLETO del apartado
  Description de la ficha (si no hay palabras clave, solo el texto; si no hay
  Description, se usa el Summary). Se elimina únicamente la frase comercial
  que el portal añade a TODAS las fichas ("Global Tenders is not only
  confined to tenders...").
- `categoria` = Product classification + Sector de la cabecera (sin repetir).
- Código único: `GT-CV-<id>`, siendo <id> el identificador final de la URL de
  la ficha (estable aunque cambie el slug del título).

Estrategia (igual que AFD / Enabel): se recorre el listado, se compara con lo
ya existente en Supabase y SOLO se descarga la ficha de las licitaciones
nuevas o con cambios visibles en el listado (título o fecha límite) o sin
categoría todavía. Solo se generan embeddings / suben las NUEVAS o las que han
CAMBIADO (`es_actualizada=True`).

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local o programada: python ingesta_globaltenders_cabo_verde.py
"""
import asyncio
import hashlib
import re
import time
from datetime import date
from urllib.parse import urljoin, urlparse

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

BASE_URL = "https://www.globaltenders.com"
LISTADO_URL = BASE_URL + "/gov-tenders/es-licitaciones-del-gobierno-cabo-verde"

FUENTE = "Cabo Verde - Global Tenders"
PAIS = "Cape Verde"
ORGANISMO_POR_DEFECTO = "Cape Verde (Global Tenders)"
TIPO_AVISO_POR_DEFECTO = "Tender Notices"

MAX_PAGINAS_SEGURIDAD = 10
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.5
TIMEOUT_CONEXION = 10
TIMEOUT_LECTURA = 30
MAX_REINTENTOS_PETICION = 3
CONCURRENCIA_MAXIMA = 4
LOTE_ENVIO_SUPABASE = 15
CAPTURA_DEPURACION_LISTADO = "debug_globaltenders_cabo_verde_listado.html"
CAPTURA_DEPURACION_FICHA = "debug_globaltenders_cabo_verde_ficha.html"

CABECERAS_PETICION = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

MESES_INGLES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

PATRON_URL_DETALLE = re.compile(r"/tender-detail/", re.IGNORECASE)
PATRON_ID_FICHA = re.compile(r"-([A-Za-z0-9]{10,24})/?$")
# "06 Oct 2026" (listado) y "Oct 12, 2026" (ficha).
PATRON_FECHA_DMY = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})")
PATRON_FECHA_MDY = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})")

# Etiquetas "Etiqueta - valor" de la ficha (solo con guion: "Description:" aparece
# dentro del propio texto de la descripción y no debe cortar el valor).
ETIQUETAS_GUION = {
    "country": "pais",
    "summary": "resumen",
    "deadline": "limite",
    "gt reference number": "ref_gt",
    "product classification": "clasificacion",
    "address": "direccion",
    "contact details": "contacto",
    "tender notice no.": "num_aviso",
    "gt ref id": "ref_gt_2",
    "document type": "tipo_documento",
    "description": "descripcion",
    "keywords": "palabras_clave",
    "tags": "palabras_clave",
    "sector": "sector",
    "category": "categoria",
}
PATRON_ETIQUETA = re.compile(
    r"(?<![\w-])(?:(?P<guion>" + "|".join(re.escape(e) for e in ETIQUETAS_GUION) + r")\s*[-\u2013]\s+"
    r"|(?P<separador>Organization Details|Notice Details and Documents)\s*:)",
    re.IGNORECASE,
)
PATRON_CABECERA = re.compile(
    r"^(?P<organismo>.+?)\s+has Released a tender for\s+(?P<resumen>.+)\s+in\s+(?P<sector>[^.]+?)\.\s*"
    r"The tender was released on\s+(?P<fecha>.+?)\.?$",
    re.IGNORECASE,
)
# Frase comercial que el portal añade a todas las fichas tras el texto del aviso.
PATRON_BOILERPLATE = re.compile(r"Global Tenders is not only confined to tenders.*$", re.IGNORECASE)
MARCADORES_FIN_FICHA = ("View more tenders for", "Similar Tenders")


# ============================================================
# HTTP
# ============================================================

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
    """Devuelve la respuesta, o None si falla. Un 404 no se reintenta."""
    for intento in range(1, max_intentos + 1):
        try:
            respuesta = SESION_HTTP.get(url, params=params, timeout=(TIMEOUT_CONEXION, TIMEOUT_LECTURA))
            if respuesta.status_code == 404:
                return None
            respuesta.raise_for_status()
            respuesta.encoding = "utf-8"  # el portal sirve UTF-8 (slugs con tildes: "formação")
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


# ============================================================
# UTILIDADES DE TEXTO / FECHAS
# ============================================================

def _limpiar_texto(texto: str):
    if not texto:
        return None
    texto = texto.replace("\u00ad", "").replace("\xa0", " ")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def _pegar_puntuacion(texto: str):
    """'Cape Verde .' -> 'Cape Verde.' (las negritas del portal dejan huecos antes de la puntuación)."""
    if not texto:
        return texto
    return re.sub(r"\s+([.,;:!?)])", r"\1", texto)


def parsear_fecha(texto: str):
    """'06 Oct 2026' o 'Oct 12, 2026' -> date. Devuelve None si no se reconoce."""
    if not texto:
        return None
    coincidencia = PATRON_FECHA_DMY.search(texto)
    if coincidencia:
        dia, nombre_mes, anio = coincidencia.groups()
    else:
        coincidencia = PATRON_FECHA_MDY.search(texto)
        if not coincidencia:
            return None
        nombre_mes, dia, anio = coincidencia.groups()
    mes = MESES_INGLES.get(nombre_mes.lower())
    if not mes:
        return None
    try:
        return date(int(anio), mes, int(dia))
    except ValueError:
        return None


def _codigo_unico_desde_url(url: str) -> str:
    ruta = urlparse(url).path.rstrip("/")
    coincidencia = PATRON_ID_FICHA.search(ruta)
    if coincidencia:
        return f"GT-CV-{coincidencia.group(1)}"[:150]
    huella = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return f"GT-CV-H{huella}"


def _unir_sin_repetir(valores: list, separador: str = "; ") -> str:
    vistos = set()
    resultado = []
    for valor in valores:
        valor = _limpiar_texto(valor)
        if not valor or valor.lower() in vistos:
            continue
        vistos.add(valor.lower())
        resultado.append(valor)
    return separador.join(resultado) if resultado else None


def componer_descripcion(palabras_clave: list, texto: str):
    """Palabras clave al INICIO de la descripción, seguidas del texto completo del apartado."""
    partes = []
    if palabras_clave:
        partes.append("Palabras clave: " + ", ".join(palabras_clave) + ".")
    if texto:
        partes.append(texto)
    return " ".join(partes) if partes else None


# ============================================================
# LISTADO
# ============================================================

def _contenedor_tarjeta(enlace):
    """Sube desde el enlace 'View Detail' hasta la tarjeta que contiene SOLO esa licitación."""
    nodo = enlace
    while nodo.parent is not None and nodo.parent.name not in ("body", "html", "[document]"):
        padre = nodo.parent
        hrefs = {a["href"] for a in padre.find_all("a", href=PATRON_URL_DETALLE)}
        if len(hrefs) > 1 or len(padre.get_text(" ")) > 1200:
            break
        nodo = padre
    return nodo


def _extraer_palabras_clave_tarjeta(tarjeta, pais: str) -> list:
    """Palabras resaltadas (negrita / marca) dentro de la tarjeta del listado."""
    candidatos = list(tarjeta.find_all(["b", "strong", "em", "mark"]))
    candidatos += tarjeta.select('[class*="highlight"], [class*="keyword"]')
    palabras = []
    for nodo in candidatos:
        texto = _limpiar_texto(nodo.get_text(" ", strip=True))
        if not texto or len(texto) > 60:
            continue
        if texto.lower() in ("view detail", (pais or "").lower()) or PATRON_FECHA_DMY.search(texto):
            continue
        palabras.append(texto)
    return [p for p in (_unir_sin_repetir(palabras, "\n") or "").split("\n") if p]


def parsear_tarjeta(enlace, url_pagina: str):
    """Convierte el bloque de una licitación del listado en un dict. None si no se puede leer."""
    url_oficial = urljoin(url_pagina, enlace["href"]).split("#")[0]
    tarjeta = _contenedor_tarjeta(enlace)
    texto_tarjeta = _limpiar_texto(tarjeta.get_text(" ")) or ""

    fechas = [m for m in PATRON_FECHA_DMY.finditer(texto_tarjeta)]
    if fechas:
        cabeza = texto_tarjeta[: fechas[0].start()]
    else:
        cabeza = re.sub(r"View Detail", "", texto_tarjeta, flags=re.IGNORECASE)
    cabeza = re.sub(r"\s*" + re.escape(PAIS) + r"\s*$", "", cabeza, flags=re.IGNORECASE)
    titulo = _limpiar_texto(_pegar_puntuacion(cabeza))

    if not titulo:
        atributo = (enlace.get("title") or "").strip()
        if atributo and atributo.lower() != "view detail":
            titulo = _limpiar_texto(atributo)
    if not titulo:
        return None

    fecha_publicacion = parsear_fecha(fechas[0].group(0)) if len(fechas) >= 1 else None
    fecha_limite = parsear_fecha(fechas[1].group(0)) if len(fechas) >= 2 else None

    return {
        "titulo": titulo,
        "url_oficial": url_oficial,
        "codigo_unico": _codigo_unico_desde_url(url_oficial),
        "fecha_publicacion": fecha_publicacion,
        "fecha_limite": fecha_limite,
        "palabras_clave": _extraer_palabras_clave_tarjeta(tarjeta, PAIS),
    }


def extraer_tarjetas(html: str, url_pagina: str):
    """Devuelve (lista de licitaciones del listado, soup)."""
    soup = BeautifulSoup(html, "html.parser")
    vistos = set()
    resultado = []
    for enlace in soup.find_all("a", href=PATRON_URL_DETALLE):
        href_absoluto = urljoin(url_pagina, enlace["href"]).split("#")[0]
        if href_absoluto in vistos:
            continue
        vistos.add(href_absoluto)
        datos = parsear_tarjeta(enlace, url_pagina)
        if datos:
            resultado.append(datos)
    return resultado, soup


def _buscar_url_siguiente(soup, url_actual: str):
    enlace = soup.select_one('a[rel~="next"]')
    if enlace is None:
        for a in soup.find_all("a", href=True):
            if a.get_text(" ", strip=True).lower() in ("next", "siguiente", "\u00bb", "\u203a", ">", "next \u00bb"):
                enlace = a
                break
    if enlace is None:
        return None
    href = (enlace.get("href") or "").strip()
    if not href or href.startswith("#") or href.lower().startswith("javascript"):
        return None
    return urljoin(url_actual, href)


def extraer_licitaciones_vivas(hoy: date) -> list:
    encontradas = {}
    url = LISTADO_URL
    visitadas = set()

    for numero_pagina in range(1, MAX_PAGINAS_SEGURIDAD + 1):
        if url in visitadas:
            break
        visitadas.add(url)
        print(f"--> Descargando listado de Global Tenders - Cabo Verde (página {numero_pagina})...", flush=True)

        respuesta = _peticion_con_reintentos(url)
        if respuesta is None:
            print(f"    Sin respuesta para la página {numero_pagina}. Se detiene.", flush=True)
            break

        if numero_pagina == 1:
            try:
                with open(CAPTURA_DEPURACION_LISTADO, "w", encoding="utf-8") as f:
                    f.write(respuesta.text)
            except Exception:
                pass

        tarjetas, soup = extraer_tarjetas(respuesta.text, url)
        print(f"    Licitaciones en la página: {len(tarjetas)}", flush=True)
        if not tarjetas:
            print("    No se encontraron más licitaciones. Fin del listado.", flush=True)
            break

        nuevas_en_pagina = 0
        for datos in tarjetas:
            if datos["fecha_limite"] and datos["fecha_limite"] < hoy:
                continue  # ya vencida
            if datos["codigo_unico"] in encontradas:
                continue
            encontradas[datos["codigo_unico"]] = datos
            nuevas_en_pagina += 1
        print(f"    Vigentes nuevas en el recorrido: {nuevas_en_pagina}", flush=True)

        url_siguiente = _buscar_url_siguiente(soup, url)
        if not url_siguiente:
            break
        url = url_siguiente
        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    return list(encontradas.values())


# ============================================================
# FICHA
# ============================================================

def parsear_ficha(html: str) -> dict:
    """
    Extrae de la ficha: organismo, sector, resumen, fecha límite, categoría
    (Product classification), tipo de aviso, descripción y palabras clave
    (solo si la ficha trae un apartado Keywords/Tags).
    """
    resultado = {
        "titulo": None, "organismo": None, "sector": None, "resumen": None,
        "clasificacion": None, "tipo_aviso": None, "descripcion": None,
        "palabras_clave": [], "fecha_limite": None, "fecha_publicacion": None,
        "categorias_enlaces": [],
    }

    soup = BeautifulSoup(html, "html.parser")
    for etiqueta in soup(["script", "style", "noscript"]):
        etiqueta.decompose()

    h1 = soup.find("h1")
    resultado["titulo"] = _pegar_puntuacion(_limpiar_texto(h1.get_text(" ", strip=True))) if h1 else None

    # Categorías de respaldo: enlaces "View more tenders for" con filtro CPV.
    for a in soup.find_all("a", href=True):
        if "cpv=" in a["href"] and "/gtsearch" in a["href"]:
            texto = _limpiar_texto(re.sub(r"\s+Tenders\s*$", "", a.get_text(" ", strip=True)))
            if texto:
                resultado["categorias_enlaces"].append(texto)

    texto_pagina = _limpiar_texto(soup.get_text(" ")) or ""

    inicio = 0
    if resultado["titulo"]:
        posicion = texto_pagina.find(h1.get_text(" ", strip=True) if h1 else resultado["titulo"])
        if posicion >= 0:
            inicio = posicion
    fin = len(texto_pagina)
    for marcador in MARCADORES_FIN_FICHA:
        posicion = texto_pagina.find(marcador, inicio)
        if posicion >= 0:
            fin = min(fin, posicion)
    region = texto_pagina[inicio:fin]

    coincidencias = list(PATRON_ETIQUETA.finditer(region))

    # Frase de cabecera: lo que hay entre el título y la primera etiqueta.
    cabecera = region[: coincidencias[0].start()] if coincidencias else region
    if resultado["titulo"] and cabecera.startswith(h1.get_text(" ", strip=True) if h1 else ""):
        cabecera = cabecera[len(h1.get_text(" ", strip=True)):]
    cabecera = _limpiar_texto(cabecera) or ""
    m_cabecera = PATRON_CABECERA.match(cabecera)
    if m_cabecera:
        organismo = re.sub(r"\s*" + re.escape(PAIS) + r"\s*$", "", m_cabecera.group("organismo"), flags=re.IGNORECASE)
        resultado["organismo"] = _limpiar_texto(organismo)
        resultado["sector"] = _limpiar_texto(m_cabecera.group("sector"))
        resultado["fecha_publicacion"] = parsear_fecha(m_cabecera.group("fecha"))

    valores = {}
    for indice, coincidencia in enumerate(coincidencias):
        if coincidencia.group("separador"):
            continue
        clave = ETIQUETAS_GUION[coincidencia.group("guion").lower()]
        fin_valor = coincidencias[indice + 1].start() if indice + 1 < len(coincidencias) else len(region)
        valor = _limpiar_texto(region[coincidencia.end():fin_valor])
        if clave not in valores and valor:
            valores[clave] = valor

    def _sin_login(valor):
        valor = re.sub(r"\bLogin\b", " ", valor or "")
        return _limpiar_texto(valor)

    resultado["resumen"] = _sin_login(valores.get("resumen"))
    resultado["clasificacion"] = _sin_login(valores.get("clasificacion") or valores.get("categoria"))
    resultado["tipo_aviso"] = _sin_login(valores.get("tipo_documento"))
    resultado["fecha_limite"] = parsear_fecha(valores.get("limite"))
    if not resultado["sector"]:
        resultado["sector"] = _sin_login(valores.get("sector"))

    descripcion = _sin_login(valores.get("descripcion"))
    if descripcion:
        descripcion = PATRON_BOILERPLATE.sub("", descripcion).strip()
        descripcion = re.sub(r"^Description\s*:\s*", "", descripcion, flags=re.IGNORECASE)
        descripcion = _limpiar_texto(descripcion)
    resultado["descripcion"] = descripcion

    claves = _sin_login(valores.get("palabras_clave"))
    if claves:
        resultado["palabras_clave"] = [c.strip() for c in re.split(r"[,;|]", claves) if c.strip()]

    return resultado


def obtener_datos_ficha(url: str, guardar_captura: bool = False) -> dict:
    respuesta = _peticion_con_reintentos(url)
    if respuesta is None:
        return {}
    if guardar_captura:
        try:
            with open(CAPTURA_DEPURACION_FICHA, "w", encoding="utf-8") as f:
                f.write(respuesta.text)
        except Exception:
            pass
    return parsear_ficha(respuesta.text)


async def _obtener_ficha_de_aviso(item: dict, semaforo: asyncio.Semaphore, guardar_captura: bool) -> dict:
    async with semaforo:
        datos_ficha = await asyncio.to_thread(obtener_datos_ficha, item["url_oficial"], guardar_captura)
    return {**item, "datos_ficha": datos_ficha}


# ============================================================
# CAMPOS COMPARABLES
# ============================================================

CAMPOS_COMPARABLES = (
    "titulo",
    "descripcion",
    "categoria",
    "organismo",
    "pais",
    "fecha_limite",
)


# ============================================================
# CONSTRUIR REGISTRO
# ============================================================

def construir_registro(item: dict, hoy: date) -> dict:
    ficha = item.get("datos_ficha") or {}

    # Palabras clave: las del listado (texto resaltado) + las de la ficha si trae un apartado propio.
    palabras_clave = []
    vistas = set()
    for palabra in list(item.get("palabras_clave") or []) + list(ficha.get("palabras_clave") or []):
        if palabra.lower() not in vistas:
            vistas.add(palabra.lower())
            palabras_clave.append(palabra)

    texto_descripcion = ficha.get("descripcion") or ficha.get("resumen")
    descripcion = componer_descripcion(palabras_clave, texto_descripcion)

    categoria = _unir_sin_repetir(
        [ficha.get("clasificacion"), ficha.get("sector")]
        if (ficha.get("clasificacion") or ficha.get("sector"))
        else list(ficha.get("categorias_enlaces") or [])
    )

    fecha_publicacion = item.get("fecha_publicacion") or ficha.get("fecha_publicacion") or hoy
    fecha_limite = item.get("fecha_limite") or ficha.get("fecha_limite")

    return {
        "codigo_unico": item["codigo_unico"],
        "fuente_origen": FUENTE,
        "tipo_aviso": ficha.get("tipo_aviso") or TIPO_AVISO_POR_DEFECTO,
        "titulo": item["titulo"] or ficha.get("titulo"),
        "descripcion": descripcion,
        "pais": PAIS,
        "paises": [PAIS],
        "organismo": ficha.get("organismo") or ORGANISMO_POR_DEFECTO,
        "categoria": categoria,
        "url_oficial": item["url_oficial"],
        "url_documento": None,  # el pliego está tras login en Global Tenders
        "fecha_publicacion": fecha_publicacion.isoformat(),
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
    }


# ============================================================
# DECIDIR QUE SUBIR
# ============================================================

def necesita_ficha(item: dict, existente) -> bool:
    """La ficha solo se descarga si la licitación es nueva, ha cambiado en el listado o aún no tiene categoría."""
    if existente is None:
        return True
    if str(existente.get("titulo")) != str(item["titulo"]):
        return True
    limite_listado = item["fecha_limite"].isoformat() if item.get("fecha_limite") else None
    if str(existente.get("fecha_limite")) != str(limite_listado):
        return True
    if not existente.get("categoria"):
        return True
    return False


def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []

    for datos in normalizados:
        existente = registros_existentes.get(datos["codigo_unico"])

        texto_completo = (
            f"Titulo: {datos['titulo']}\n"
            f"{datos.get('descripcion') or ''}\n"
            f"Pais: {datos.get('pais') or 'No especificado'}\n"
            f"Categoria: {datos.get('categoria') or 'No especificada'}"
        )

        if existente is None:
            datos["texto_completo"] = texto_completo
            datos["embedding"] = generar_embedding(texto_completo)
            datos["es_novedad"] = True
            datos["es_actualizada"] = False
            a_subir.append(datos)
            continue

        ha_cambiado = any(
            str(existente.get(campo)) != str(datos.get(campo))
            for campo in CAMPOS_COMPARABLES
        )

        if not ha_cambiado:
            continue

        datos["texto_completo"] = texto_completo
        datos["embedding"] = generar_embedding(texto_completo)
        datos["es_novedad"] = False
        datos["es_actualizada"] = True
        a_subir.append(datos)

    return a_subir


# ============================================================
# EJECUCION PRINCIPAL
# ============================================================

async def ejecutar_sincronizacion_async():
    hoy = date.today()

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - CABO VERDE (GLOBAL TENDERS)", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)
    print(f"Solo licitaciones vivas con cierre >= {hoy}", flush=True)

    crudas = await asyncio.to_thread(extraer_licitaciones_vivas, hoy)
    print(f"\nLicitaciones vivas detectadas en el listado: {len(crudas)}", flush=True)

    if not crudas:
        print(
            "No se ha detectado ninguna licitación viva. "
            f"Revisa el log de arriba y, si existe, {CAPTURA_DEPURACION_LISTADO}.",
            flush=True,
        )
        return

    supabase = obtener_cliente_supabase()

    print("\nComparando con lo ya existente en Supabase (ANTES de descargar ninguna ficha)...", flush=True)
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico") + CAMPOS_COMPARABLES,
        claves=[c["codigo_unico"] for c in crudas],
    )
    print(f"Ya existentes en Supabase: {len(registros_existentes)}/{len(crudas)}", flush=True)

    pendientes = [c for c in crudas if necesita_ficha(c, registros_existentes.get(c["codigo_unico"]))]
    print(f"Fichas a descargar (nuevas o con cambios en el listado): {len(pendientes)}", flush=True)

    if not pendientes:
        print("No hay licitaciones nuevas ni cambios que sincronizar.", flush=True)
        return

    semaforo = asyncio.Semaphore(CONCURRENCIA_MAXIMA)
    resultados = await asyncio.gather(
        *(
            _obtener_ficha_de_aviso(item, semaforo, guardar_captura=(indice == 0))
            for indice, item in enumerate(pendientes)
        ),
        return_exceptions=True,
    )

    con_ficha = []
    for item_original, resultado in zip(pendientes, resultados):
        if isinstance(resultado, Exception):
            print(f"    Aviso: error procesando {item_original.get('url_oficial')}: {resultado}", flush=True)
            continue
        if not resultado.get("datos_ficha"):
            print(f"    Aviso: ficha vacía o no descargada para {item_original['codigo_unico']}; se usan solo datos del listado.", flush=True)
        con_ficha.append(resultado)

    sin_categoria = sum(1 for r in con_ficha if not (r.get("datos_ficha") or {}).get("clasificacion"))
    sin_descripcion = sum(1 for r in con_ficha if not (r.get("datos_ficha") or {}).get("descripcion"))
    con_claves = sum(1 for r in con_ficha if r.get("palabras_clave"))
    print(
        f"Fichas procesadas: {len(con_ficha)} | sin categoría: {sin_categoria} | "
        f"sin descripción: {sin_descripcion} | con palabras clave: {con_claves}",
        flush=True,
    )
    if con_ficha and (sin_categoria == len(con_ficha) or sin_descripcion == len(con_ficha)):
        print(
            "    ATENCIÓN: ninguna ficha ha devuelto categoría o descripción. Es probable que el HTML del portal "
            f"haya cambiado; revisa {CAPTURA_DEPURACION_FICHA}.",
            flush=True,
        )

    normalizados = [construir_registro(r, hoy) for r in con_ficha]
    normalizados = [n for n in normalizados if n.get("titulo") and n.get("url_oficial")]

    lote_final = await asyncio.to_thread(preparar_lote_para_subir, normalizados, registros_existentes)

    if not lote_final:
        print("No hay licitaciones nuevas ni cambios que sincronizar.", flush=True)
        return

    nuevas = sum(1 for d in lote_final if d["es_novedad"])
    print(f"A subir: {len(lote_final)} ({nuevas} nuevas, {len(lote_final) - nuevas} actualizadas)", flush=True)

    subidas = await asyncio.to_thread(
        subir_en_lotes,
        supabase,
        "licitaciones_internacionales",
        "codigo_unico",
        lote_final,
        tamano_lote=LOTE_ENVIO_SUPABASE,
    )

    print(f"\nSincronización Cabo Verde (Global Tenders) completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


def ejecutar_sincronizacion():
    asyncio.run(ejecutar_sincronizacion_async())


if __name__ == "__main__":
    ejecutar_sincronizacion()

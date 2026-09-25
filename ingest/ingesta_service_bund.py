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

TERCERA VUELTA -- LENTITUD Y TIMEOUTS: REDISEÑO DE LA TRADUCCIÓN
--------------------------------------------------------------------------
El script llegó a funcionar (100/100 avisos detectados en el listado
real, ver aviso de fiabilidad más abajo), pero en producción resultó
lento y con errores "Read timed out" frecuentes. La versión anterior ya
añadía reintentos HTTP y un backoff ante límite de peticiones de
Google, pero seguía traduciendo UN AVISO A LA VEZ (`CONCURRENCIA_MAXIMA
= 1`, con 2-4s de pausa ENTRE CADA AVISO) -- para 100 avisos, eso son
minutos solo en pausas de cortesía, antes de contar ningún reintento
por límite de peticiones (cuyo backoff llegaba a 5-20 MINUTOS por
intento). La causa de fondo no era la falta de reintentos, sino hacer
demasiadas peticiones de traducción por separado. Dos cambios lo
resuelven:

  1. TRADUCIR CADA TEXTO DISTINTO UNA SOLA VEZ: "Vergabestelle"
     (organismo) se repite mucho entre avisos del mismo licitador (en
     el listado real de 100 avisos, solo 78 organismos son distintos),
     y sobre todo "Leistungen und Erzeugnisse" -- de donde sale
     "descripcion" -- es sistemáticamente uno de solo ~18 categorías
     fijas en todo el portal (confirmado por los propios filtros de
     categoría del buscador: "Bauleistungen", "Dienstleistungen",
     "Lieferleistungen"...), igual que "Vergabeart" (tipo_aviso) tiene
     un puñado fijo de valores posibles (Offenes Verfahren, Öffentliche
     Ausschreibung...). Traducir cada aviso por separado traduce el
     MISMO texto una y otra vez. Ahora se recopilan primero TODOS los
     textos en alemán de TODOS los avisos, se descartan los repetidos,
     y solo se traduce cada texto ÚNICO una vez.
  2. AGRUPAR VARIOS TEXTOS POR PETICIÓN: además de no repetir textos,
     `traducir_textos_unicos` agrupa varios textos únicos en una sola
     petición a Google Translate (unidos por saltos de línea, hasta un
     límite de caracteres seguro por lote), en vez de una peticion por
     texto. Para un día normal de avisos, esto puede reducir cientos de
     peticiones posibles a un puñado de lotes -- menos peticiones
     totales significa menos tiempo Y menos probabilidad de toparse con
     el límite de peticiones de Google. Si un lote no conserva el
     número de líneas tras traducir (riesgo real de unir textos con
     saltos de línea), se cae a traducir ese lote texto a texto como
     respaldo, sin perder la deduplicación ya hecha.

Además, las peticiones a las FICHAS de detalle (que no tienen nada que
ver con el límite de Google) ahora se hacen con concurrencia moderada
(CONCURRENCIA_DETALLE) en vez de una a una, ya que el cuello de botella
de Google Translate no tiene por qué frenar también las descargas del
propio service.bund.de.

ROBUSTEZ HTTP Y ANTE FALLOS -- para que el script nunca se cuelgue
--------------------------------------------------------------------------
  - Sesión de requests con reintentos automáticos y backoff exponencial
    ante 429/500/502/503/504 y errores de conexión/lectura
    (`_crear_sesion_http`, con `urllib3.Retry` -- respeta la cabecera
    `Retry-After` si el servidor la envía).
  - Cada descarga (listado o ficha) está en su propio try/except: un
    aviso que falle se registra y se omite, nunca tira abajo el resto
    del lote.
  - `asyncio.gather(..., return_exceptions=True)` en todos los puntos
    donde se procesan varios avisos a la vez -- sin esto, UNA sola
    excepción inesperada en un aviso cancela silenciosamente el resto
    de tareas concurrentes y se pierde todo el lote; con esto, se
    registra el fallo puntual y se sigue con los demás.
  - El backoff por límite de peticiones de Google ya no espera 5-20
    MINUTOS por intento (heredado de una versión anterior, pensado
    para un bloqueo prolongado real) -- ahora son unos segundos
    crecientes, más acorde con "que vaya rápido"; si el límite persiste
    tras varios intentos, se desactiva la traducción para el resto de
    ESA ejecución concreta (`BLOQUEADO_POR_GOOGLE`) y los textos
    restantes se guardan en alemán en vez de bloquear todo el proceso.

AVISO DE FIABILIDAD -- LISTADO CONFIRMADO CONTRA HTML REAL (100/100)
--------------------------------------------------------------------------
La extracción del LISTADO se confirmó contra un `debug_service_bund_listado.html`
real de 100 resultados:
    <ul class="result-list">
      <li><a href="IMPORTE/Ausschreibungen/<sistema>/<año>/<mes>/<id>.html;jsessionid=..."
             title="Zur Ausschreibung 'Título real'">
        <div aria-labelledby="date"><p><em>Veröffentlicht</em> DD.MM.AA</p></div>
        <div aria-labelledby="location"><p><em>Angebotsfrist</em> DD.MM.AA</p></div>
      </a></li>
      ...
    </ul>
Hay AL MENOS 7 sistemas de origen distintos tras "/IMPORTE/Ausschreibungen/"
(editor/, obb/, asp/, eVergabe/, healyhudson/, subreport/, subreport15/,
abc/...), con identificadores numéricos, UUID, o alfanuméricos mixtos --
el patrón de URL cubre los tres formatos. `codigo_unico` usa el PATH
COMPLETO tras "Ausschreibungen/" (no solo el identificador final):
"subreport/.../E82212934.html" y "subreport15/.../E82212934.html" son
avisos DISTINTOS con el mismo identificador final, confirmado en el
HTML real.

La FICHA DE DETALLE sigue sin confirmarse contra HTML real completo
(solo fragmentos de texto indexados por un buscador) -- revisa el
resultado de la primera ejecución real con atención. "Leistungen und
Erzeugnisse" (el campo `descripcion` primario) es una ETIQUETA DE
CATEGORÍA corta, no una frase descriptiva de la licitación -- ver el
mismo aviso en versiones anteriores de este docstring si hace falta
más contexto; se mantiene el respaldo con el párrafo más largo de la
página que no sea el aviso legal repetido ("Hinweis: service.bund.de
ist nur die Veröffentlichungsplattform...").

DOS FORMATOS DE FECHA DISTINTOS -- confirmado, no es un error:
  - Listado: "Veröffentlicht"/"Angebotsfrist" DD.MM.AA (año en 2 dígitos).
  - Ficha:   "Angebotsfrist" DD.MM.AAAA (año en 4 dígitos).

LÍMITE DE FONDO DE deep-translator/Google Translate
--------------------------------------------------------------------------
`deep-translator` usa el endpoint GRATUITO/no oficial de Google
Translate (el mismo que translate.google.com), pensado para uso
puntual, no para cientos de peticiones diarias -- por diseño es
propenso a bloqueos temporales de IP con volumen. Deduplicar y agrupar
(ver más arriba) reduce muchísimo el riesgo, pero no lo elimina del
todo. Si esto sigue dando problemas de forma persistente, la solución
de fondo sería una cuenta de pago de Google Cloud Translation API (con
credenciales y límites mucho más altos) en vez del endpoint gratuito --
fuera del alcance de este cambio, pero conviene saberlo si el problema
persiste.

No se ha podido instalar ni probar `deep-translator` en este entorno de
desarrollo (bloqueado específicamente por el acceso de red del entorno,
a diferencia de paquetes normales) -- el resto del pipeline sí se ha
probado exhaustivamente con la traducción simulada.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local:      python ingesta_service_bund.py
Ejecución programada: ver .github/workflows/sincronizar_service_bund.yml
"""
import asyncio
import random
import re
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
CONCURRENCIA_DETALLE = 6   # peticiones concurrentes a las FICHAS (no tiene relacion con el limite de Google)
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_publicacion", "fecha_limite")
MAX_PAGINAS = 3   # red de seguridad -- ver aviso de fiabilidad sobre la paginacion
CAPTURA_DEPURACION = "debug_service_bund_listado.html"

DELIMITADOR_LOTE_TRADUCCION = "\n"
LIMITE_CARACTERES_POR_LOTE_TRADUCCION = 3500   # margen de seguridad bajo el limite tipico del endpoint gratuito
MAX_REINTENTOS_TRADUCCION = 4
PAUSA_ENTRE_LOTES_TRADUCCION = (1.0, 2.5)   # segundos, aleatorio -- cortesia ENTRE LOTES, no por texto

# Si el limite de peticiones de Google persiste tras varios reintentos
# en un mismo lote, se desactiva la traduccion para el RESTO de esta
# ejecucion (no tiene sentido seguir intentando) -- los textos que
# queden se guardan en aleman en vez de bloquear todo el proceso.
BLOQUEADO_POR_GOOGLE = False

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
    """
    Sesión de requests con reintentos automáticos y backoff exponencial
    ante fallos transitorios (incluye errores de lectura/conexión, no
    solo códigos de estado) -- para que un "Read timed out" puntual no
    tumbe la ejecución entera.
    """
    sesion = requests.Session()
    estrategia_reintento = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adaptador = HTTPAdapter(max_retries=estrategia_reintento, pool_maxsize=CONCURRENCIA_DETALLE + 2)
    sesion.mount("https://", adaptador)
    sesion.mount("http://", adaptador)
    sesion.headers.update(CABECERAS_PETICION)
    return sesion


SESION_HTTP = _crear_sesion_http()


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def _limpiar_texto(texto: str) -> str:
    """Quita guiones suaves (U+00AD) y normaliza espacios (incluido el
    espacio de no separación U+00A0, que \\s ya reconoce)."""
    if not texto:
        return None
    texto = texto.replace("\u00ad", "")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def parsear_fecha_listado_bund(texto: str):
    """'DD.MM.AA' -- año en 2 dígitos, tal y como aparece en el
    LISTADO (distinto del formato de la ficha)."""
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
    """'DD.MM.AAAA' -- año en 4 dígitos, tal y como aparece en la
    FICHA de detalle (campo Angebotsfrist)."""
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
    """
    Descarga la página de resultados y devuelve, para cada aviso cuya
    fecha de publicación (Veröffentlicht) esté entre `desde` y `hasta`
    (ambos incluidos), un dict con lo mínimo fiable ahí -- ver aviso de
    fiabilidad del docstring del módulo para la estructura confirmada.
    """
    encontrados = {}
    detenerse = False

    for indice_pagina in range(MAX_PAGINAS):
        url_pagina = LISTADO_URL + (f"&page={indice_pagina + 1}" if indice_pagina else "")
        print(f"--> Descargando listado (página {indice_pagina + 1}): {url_pagina}", flush=True)
        try:
            respuesta = SESION_HTTP.get(url_pagina, timeout=TIMEOUT_PETICION)
            respuesta.raise_for_status()
        except requests.exceptions.RequestException as error:
            print(f"    Error descargando el listado (página {indice_pagina + 1}): {error}", flush=True)
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
                    titulo_listado = _limpiar_texto(h3.get_text(" ", strip=True))
                    if titulo_listado:
                        titulo_listado = titulo_listado.removeprefix("Ausschreibung").strip()

            encontrados[url_oficial] = {
                "titulo_listado": _limpiar_texto(titulo_listado),
                "url_oficial": url_oficial,
                "slug_base": coincidencia_id.group(1),
                "fecha_publicacion_listado": fecha_publicacion,
                "fecha_limite_listado": fecha_limite_listado,
            }

        # Si el aviso mas antiguo visto en esta pagina ya es anterior a
        # `desde`, dado el orden descendente, las paginas siguientes
        # solo traerian avisos aun mas antiguos -- se para aqui.
        if fecha_mas_antigua_de_la_pagina and fecha_mas_antigua_de_la_pagina < desde:
            detenerse = True

        if detenerse:
            break

    return list(encontrados.values())


def obtener_datos_ficha(url: str) -> dict:
    """
    Descarga la ficha de detalle y extrae organismo, descripción, tipo
    de aviso y fecha límite -- ver aviso de fiabilidad del docstring
    del módulo. Nunca lanza una excepción hacia el llamador: cualquier
    fallo de red devuelve el dict vacío, para que un aviso problemático
    no tumbe el resto del lote.
    """
    resultado = {
        "titulo": None, "organismo": None, "descripcion": None,
        "tipo_aviso": None, "fecha_limite": None,
    }

    try:
        respuesta = SESION_HTTP.get(url, timeout=TIMEOUT_PETICION)
        respuesta.raise_for_status()
    except requests.exceptions.RequestException as error:
        print(f"      Error descargando la ficha ({url[:80]}...): {error}", flush=True)
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


def _es_error_limite_peticiones(error: Exception) -> bool:
    texto_error = str(error).lower()
    return "429" in texto_error or "too many requests" in texto_error


async def _traducir_texto_individual(texto: str, reintentos: int = MAX_REINTENTOS_TRADUCCION) -> str:
    """Traduce UN texto (puede ser varios unidos por saltos de línea),
    con reintentos y backoff ante el límite de peticiones de Google.
    Si falla, o si el límite persiste, devuelve el texto original en
    alemán -- nunca se pierde el dato por un fallo de traducción."""
    global BLOQUEADO_POR_GOOGLE
    if not texto:
        return texto
    if BLOQUEADO_POR_GOOGLE:
        return texto

    for intento in range(reintentos):
        try:
            return await asyncio.to_thread(
                lambda: GoogleTranslator(source="de", target="en").translate(texto)
            )
        except Exception as error:
            if _es_error_limite_peticiones(error):
                espera = (5 * (2 ** intento)) + random.uniform(1, 4)
                if intento < reintentos - 1:
                    print(
                        f"      [429] Límite de Google alcanzado (intento {intento + 1}/{reintentos}), "
                        f"esperando {espera:.0f}s...",
                        flush=True,
                    )
                    await asyncio.sleep(espera)
                else:
                    print(
                        "      [429] El límite persiste tras varios intentos -- se desactiva la "
                        "traducción para el resto de esta ejecución (los textos restantes se "
                        "guardan en alemán).",
                        flush=True,
                    )
                    BLOQUEADO_POR_GOOGLE = True
                    return texto
            else:
                print(f"      Aviso: fallo puntual al traducir: {error}", flush=True)
                return texto
    return texto


def _agrupar_textos_en_lotes(textos: list, limite_caracteres: int) -> list:
    """Agrupa una lista de textos en lotes que no superen
    `limite_caracteres` cada uno (contando el delimitador), para poder
    traducir varios textos distintos en una sola petición."""
    lotes = []
    lote_actual, longitud_actual = [], 0
    for texto in textos:
        longitud_texto = len(texto) + len(DELIMITADOR_LOTE_TRADUCCION)
        if lote_actual and longitud_actual + longitud_texto > limite_caracteres:
            lotes.append(lote_actual)
            lote_actual, longitud_actual = [], 0
        lote_actual.append(texto)
        longitud_actual += longitud_texto
    if lote_actual:
        lotes.append(lote_actual)
    return lotes


async def _traducir_lote(lote: list) -> dict:
    """
    Traduce un lote de textos distintos en UNA sola petición (unidos
    por saltos de línea). Si la traducción no conserva el mismo número
    de líneas (riesgo real al unir varios textos), cae a traducir ese
    lote concreto texto a texto como respaldo -- sin perder la
    deduplicación ya hecha en `traducir_textos_unicos`.
    """
    if len(lote) == 1:
        return {lote[0]: await _traducir_texto_individual(lote[0]) or lote[0]}

    texto_unido = DELIMITADOR_LOTE_TRADUCCION.join(lote)
    traduccion = await _traducir_texto_individual(texto_unido)
    partes = traduccion.split(DELIMITADOR_LOTE_TRADUCCION) if traduccion else []

    if len(partes) == len(lote):
        return {original: (traducido.strip() or original) for original, traducido in zip(lote, partes)}

    print(
        f"      Aviso: el lote de {len(lote)} textos no conservó el número de líneas al traducir "
        f"({len(partes)} recibidas) -- se traduce texto a texto como respaldo.",
        flush=True,
    )
    resultado = {}
    for texto in lote:
        resultado[texto] = await _traducir_texto_individual(texto) or texto
    return resultado


async def traducir_textos_unicos(textos: set) -> dict:
    """
    Traduce cada texto en alemán UNA SOLA VEZ, agrupando varios textos
    distintos por cada petición HTTP -- ver "TERCERA VUELTA" en el
    docstring del módulo para el razonamiento completo. Devuelve un
    diccionario {texto_aleman: texto_ingles} para consultar al
    construir cada registro.
    """
    textos_unicos = sorted(t for t in textos if t)
    if not textos_unicos:
        return {}

    lotes = _agrupar_textos_en_lotes(textos_unicos, LIMITE_CARACTERES_POR_LOTE_TRADUCCION)
    print(
        f"    {len(textos_unicos)} textos distintos a traducir, agrupados en {len(lotes)} lote(s) "
        f"(en vez de hasta {len(textos_unicos)} peticiones por separado)",
        flush=True,
    )

    cache = {}
    for indice, lote in enumerate(lotes, start=1):
        if BLOQUEADO_POR_GOOGLE:
            for texto in lote:
                cache[texto] = texto
            continue
        print(f"    Traduciendo lote {indice}/{len(lotes)} ({len(lote)} textos)...", flush=True)
        cache.update(await _traducir_lote(lote))
        if indice < len(lotes) and not BLOQUEADO_POR_GOOGLE:
            await asyncio.sleep(random.uniform(*PAUSA_ENTRE_LOTES_TRADUCCION))

    return cache


def construir_registro(item: dict, cache_traduccion: dict) -> dict:
    datos_ficha = item.get("datos_ficha", {})
    titulo_de = datos_ficha.get("titulo") or item.get("titulo_listado")
    organismo_de = datos_ficha.get("organismo")
    descripcion_de = datos_ficha.get("descripcion")
    tipo_aviso_de = datos_ficha.get("tipo_aviso")

    titulo_en = cache_traduccion.get(titulo_de, titulo_de) if titulo_de else None
    organismo_en = cache_traduccion.get(organismo_de, organismo_de) if organismo_de else None
    descripcion_en = cache_traduccion.get(descripcion_de, descripcion_de) if descripcion_de else None
    tipo_aviso_en = cache_traduccion.get(tipo_aviso_de, tipo_aviso_de) if tipo_aviso_de else None

    fecha_publicacion = item.get("fecha_publicacion_listado")
    # Prioridad a la fecha límite de la FICHA (formato de año sin
    # ambigüedad); si no está, se usa la del propio listado.
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
    global BLOQUEADO_POR_GOOGLE
    BLOQUEADO_POR_GOOGLE = False

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

    print(
        f"\nConsultando la ficha de cada aviso (hasta {CONCURRENCIA_DETALLE} a la vez)...",
        flush=True,
    )
    semaforo_detalle = asyncio.Semaphore(CONCURRENCIA_DETALLE)
    resultados_ficha = await asyncio.gather(
        *(_obtener_ficha_de_aviso(item, semaforo_detalle) for item in crudos),
        return_exceptions=True,
    )

    items_con_ficha = []
    for item_original, resultado in zip(crudos, resultados_ficha):
        if isinstance(resultado, Exception):
            print(
                f"    Aviso: fallo inesperado procesando {item_original.get('url_oficial')}: "
                f"{resultado} -- se omite este aviso, se sigue con el resto.",
                flush=True,
            )
            continue
        items_con_ficha.append(resultado)

    if not items_con_ficha:
        print("Ningún aviso pudo procesarse correctamente.", flush=True)
        return

    textos_a_traducir = set()
    for item in items_con_ficha:
        datos_ficha = item["datos_ficha"]
        titulo_de = datos_ficha.get("titulo") or item.get("titulo_listado")
        for campo in (titulo_de, datos_ficha.get("organismo"), datos_ficha.get("descripcion"), datos_ficha.get("tipo_aviso")):
            if campo:
                textos_a_traducir.add(campo)

    print(f"\nTraduciendo al inglés...", flush=True)
    cache_traduccion = await traducir_textos_unicos(textos_a_traducir)

    normalizados = [construir_registro(item, cache_traduccion) for item in items_con_ficha]

    sin_fecha_limite = sum(1 for n in normalizados if not n.get("fecha_limite"))
    if sin_fecha_limite:
        print(f"Avisos sin fecha límite reconocida: {sin_fecha_limite}/{len(normalizados)}.", flush=True)

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

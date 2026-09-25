# -*- coding: utf-8 -*-
"""
ingesta_service_bund.py
--------------------------
Sincroniza licitaciones del portal de contratación pública alemán
service.bund.de contra la tabla `licitaciones_internacionales` de
Supabase, mediante scraping directo del HTML (sin usar ninguna API).

    Listado:  https://www.service.bund.de/Content/DE/Ausschreibungen/Suche/Formular.html?view=processForm&nn=9465610
    Ficha:    https://www.service.bund.de/IMPORTE/Ausschreibungen/<sistema>/<id>.html

CUARTA VUELTA -- SE ELIMINA LA TRADUCCIÓN; SE COMPRUEBA SUPABASE ANTES
DE DESCARGAR NINGUNA FICHA; RED DE SEGURIDAD ADICIONAL ANTE TIMEOUTS
--------------------------------------------------------------------------
Tres cambios de fondo sobre la versión anterior (que traducía al inglés
y comparaba contra Supabase DESPUÉS de descargar todas las fichas):

  1. SIN TRADUCCIÓN: se elimina por completo `deep-translator` y toda
     la lógica de traducción por lotes que se había añadido -- ya no
     hay ninguna dependencia de un servicio externo de traducción, ni
     el riesgo de bloqueo/429 que traía consigo. `titulo`,
     `descripcion`, `organismo` y `tipo_aviso` se guardan tal cual se
     extraen de la página, en alemán. Esto por sí solo ya hace el
     proceso mucho más rápido y elimina una fuente entera de fallos.

  2. SUPABASE ANTES QUE LA FICHA: antes se descargaban las fichas de
     TODOS los avisos del listado y solo al final se comparaba con
     Supabase para decidir qué subir -- pidiendo la ficha incluso de
     avisos que ya existían de una ejecución anterior. Ahora
     `extraer_avisos_listado` ya calcula el `codigo_unico` de cada
     aviso (a partir del slug de su URL, sin necesidad de la ficha), y
     `ejecutar_sincronizacion_async` consulta Supabase con esos
     códigos INMEDIATAMENTE después del listado -- antes de descargar
     ninguna ficha. Solo se piden las fichas de los avisos cuyo
     `codigo_unico` NO existe todavía. Si el listado trae, por
     ejemplo, 100 avisos y 80 ya están en Supabase de ayer, ahora se
     hacen ~20 peticiones a fichas en vez de 100.

     CONTRAPARTIDA A TENER EN CUENTA: como ya no se descarga la ficha
     de los avisos que ya existen, tampoco se puede detectar si algo
     de esos avisos cambió (p. ej. una prórroga de la fecha límite) --
     `es_actualizada` deja de tener sentido con este diseño y ya no se
     usa; todo lo que se sube es siempre `es_novedad=True`. Es la
     contrapartida directa de evitar esas peticiones HTTP, tal y como
     se pidió; si en algún momento hiciera falta detectar
     actualizaciones de avisos ya conocidos, habría que volver a
     descargar su ficha periódicamente (fuera del alcance de este
     cambio).

  3. RED DE SEGURIDAD ADICIONAL ANTE "Read timed out": la sesión de
     requests ya reintentaba automáticamente vía `urllib3.Retry`, pero
     el error seguía apareciendo -- un timeout de LECTURA concreto no
     siempre lo intercepta `Retry` según la versión/circunstancia
     exacta de urllib3 (es un problema conocido: `Retry` está pensado
     sobre todo para reintentar por código de estado HTTP o por fallo
     de CONEXIÓN, no siempre por fallo de LECTURA tras una conexión ya
     establecida). Por eso ahora hay una SEGUNDA capa de reintentos,
     explícita y propia (`_peticion_con_reintentos`), que envuelve cada
     llamada de red y reintenta ante CUALQUIER
     `requests.exceptions.RequestException` (de conexión, de lectura,
     o de otro tipo), con espera creciente entre intentos. Además el
     timeout ahora se pasa como tupla `(conexión, lectura)` en vez de
     un único valor -- permite fallar rápido si el servidor ni
     siquiera responde a la conexión, sin acortar el tiempo que se da
     a que termine de enviar una página más pesada.

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
CATEGORÍA corta ("Dienstleistungen", "Bauleistungen"...), no una frase
descriptiva de la licitación -- se mantiene el respaldo con el párrafo
más largo de la página que no sea el aviso legal repetido ("Hinweis:
service.bund.de ist nur die Veröffentlichungsplattform...").

DOS FORMATOS DE FECHA DISTINTOS -- confirmado, no es un error:
  - Listado: "Veröffentlicht"/"Angebotsfrist" DD.MM.AA (año en 2 dígitos).
  - Ficha:   "Angebotsfrist" DD.MM.AAAA (año en 4 dígitos).

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local:      python ingesta_service_bund.py
Ejecución programada: ver .github/workflows/sincronizar_service_bund.yml
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
LISTADO_URL = (
    BASE_URL + "/Content/DE/Ausschreibungen/Suche/Formular.html"
    "?view=processForm&nn=9465610&sortOrder=dateOfIssue_dt+desc&resultsPerPage=100"
)
FUENTE = "SERVICE_BUND"
TIMEOUT_CONEXION = 10        # falla rapido si el servidor ni responde a la conexion
TIMEOUT_LECTURA = 30         # tiempo que se da a que termine de enviar la pagina
MAX_REINTENTOS_PETICION = 3  # capa de reintentos EXPLICITA, ver aviso de fiabilidad en el docstring
CONCURRENCIA_MAXIMA = 6      # peticiones concurrentes a las FICHAS
LOTE_ENVIO_SUPABASE = 15
MAX_PAGINAS = 3   # red de seguridad -- ver aviso de fiabilidad sobre la paginacion
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
    """
    Sesión de requests con reintentos automáticos vía urllib3.Retry
    (primera capa: sobre todo eficaz ante códigos de estado 429/5xx y
    fallos de conexión). Se combina con `_peticion_con_reintentos`
    (segunda capa, explícita) para cubrir también los "Read timed out"
    que Retry no siempre intercepta -- ver aviso de fiabilidad en el
    docstring del módulo.
    """
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


def _peticion_con_reintentos(url: str, max_intentos: int = MAX_REINTENTOS_PETICION):
    """
    Descarga una URL con una capa de reintentos EXPLÍCITA, por encima
    de la que ya hace `SESION_HTTP` vía urllib3.Retry -- ver aviso de
    fiabilidad en el docstring del módulo sobre por qué hace falta esta
    segunda capa (un "Read timed out" concreto no siempre lo
    intercepta Retry). Captura cualquier
    `requests.exceptions.RequestException` (timeout de conexión, de
    lectura, error de conexión...) y reintenta con espera creciente.
    Nunca lanza una excepción hacia el llamador: devuelve None si todos
    los intentos fallan, para que quien la use decida cómo degradar
    (nunca debe tumbar el resto del lote por un aviso problemático).
    """
    for intento in range(1, max_intentos + 1):
        try:
            respuesta = SESION_HTTP.get(url, timeout=(TIMEOUT_CONEXION, TIMEOUT_LECTURA))
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
    (ambos incluidos), un dict con lo mínimo fiable ahí -- incluido ya
    su `codigo_unico`, calculado aquí mismo a partir del slug de la
    URL, para poder consultar Supabase ANTES de descargar ninguna
    ficha (ver "CUARTA VUELTA" en el docstring del módulo). El resto de
    campos se sacan de la propia ficha de detalle, pero solo para los
    avisos que resulten ser nuevos.
    """
    encontrados = {}
    detenerse = False

    for indice_pagina in range(MAX_PAGINAS):
        url_pagina = LISTADO_URL + (f"&page={indice_pagina + 1}" if indice_pagina else "")
        print(f"--> Descargando listado (página {indice_pagina + 1}): {url_pagina}", flush=True)
        respuesta = _peticion_con_reintentos(url_pagina)
        if respuesta is None:
            print(f"    No se pudo descargar la página {indice_pagina + 1} tras varios intentos, se detiene el listado.", flush=True)
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

            slug_base = coincidencia_id.group(1)
            encontrados[url_oficial] = {
                "titulo_listado": _limpiar_texto(titulo_listado),
                "url_oficial": url_oficial,
                "codigo_unico": f"BUND-{_generar_slug(slug_base)}"[:150],
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
    """Construye el registro final -- todos los campos de texto se
    guardan tal cual se extraen, en alemán (sin traducción, ver
    "CUARTA VUELTA" en el docstring del módulo)."""
    datos_ficha = item.get("datos_ficha", {})
    titulo = datos_ficha.get("titulo") or item.get("titulo_listado")
    organismo = datos_ficha.get("organismo")
    descripcion = datos_ficha.get("descripcion")
    tipo_aviso = datos_ficha.get("tipo_aviso")

    fecha_publicacion = item.get("fecha_publicacion_listado")
    # Prioridad a la fecha límite de la FICHA (formato de año sin
    # ambigüedad); si no está, se usa la del propio listado.
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
    """
    Añade `texto_completo`, `embedding`, `es_novedad` y
    `es_actualizada` a cada registro. Como estos avisos ya se
    filtraron ANTES de llegar aquí (solo se procesan los que no
    existían todavía en Supabase, ver `ejecutar_sincronizacion_async`),
    todos son siempre novedades -- ya no hace falta comparar campo a
    campo contra un registro existente.
    """
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

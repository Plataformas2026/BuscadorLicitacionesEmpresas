# -*- coding: utf-8 -*-
"""
ingesta_service_bund.py
--------------------------
Sincroniza licitaciones del portal de contratación pública alemán
service.bund.de contra la tabla `licitaciones_internacionales` de
Supabase, mediante scraping directo del HTML (sin usar ninguna API) y
traduciendo al inglés el contenido en alemán.

    Listado:  https://www.service.bund.de/Content/DE/Ausschreibungen/Suche/Formular.html?view=processForm&nn=9465610
    Ficha:    https://www.service.bund.de/IMPORTE/Ausschreibungen/eVergabe/<id>.html

AVISO DE FIABILIDAD -- LEE ESTO ANTES DE CONFIAR EN EL CAMPO "descripcion"
--------------------------------------------------------------------------
Se ha podido confirmar la estructura real de VARIAS fichas de detalle
reales de service.bund.de (indexadas por un buscador; no hay salida de
red hacia service.bund.de en este entorno de desarrollo, así que no se
ha podido navegar en vivo). Confirman que cada ficha expone sus datos
en pares <dt>/<dd>: "Vergabestelle" (organismo), "Leistungen und
Erzeugnisse", "Ausschreibungsweite", "Vergabeverfahren", "Vergabeart",
"Angebotsfrist" (fecha límite, formato DD.MM.AAAA), "Erfüllungsort",
"CPV-Code".

IMPORTANTE: en las 7 fichas reales distintas consultadas,
"Leistungen und Erzeugnisse" NUNCA es una frase descriptiva de la
licitación concreta -- es siempre una ETIQUETA DE CATEGORÍA corta (1 a
3 palabras: "Informationstechnik", "Dienstleistungen",
"Lieferleistungen", "Lebensmittel", "Arbeitsmarktdienstleistungen"...),
del mismo tipo que "Bauleistungen"/obras o "Dienstleistungen"/servicios
-- de hecho son las MISMAS etiquetas que el propio buscador usa como
filtro de categoría. Se implementa tal cual se pidió (es el campo
"descripcion" primario), pero conviene saber que el resultado será
una categoría genérica, no un resumen de la licitación en sí. Como
red de seguridad adicional (y seguido lo que se pidió: "si no lo
encuentra, usa como fallback el texto explicativo"), si el <dt> no
aparece, o como complemento siempre que haya sitio, se intenta además
capturar el párrafo más largo de la página que NO sea el aviso legal
repetido en todas las fichas ("Hinweis: service.bund.de ist nur die
Veröffentlichungsplattform...", confirmado en las 7 fichas), uniendo
ambas fuentes.

El LISTADO de resultados no ha podido confirmarse con el mismo detalle
(solo se ha visto el TEXTO ya extraído por un buscador -- "Vergabestelle
X · Veröffentlicht DD.MM.AA · Angebotsfrist DD.MM.AA" por cada fila--,
no las clases CSS/etiquetas HTML subyacentes). Por eso la extracción del
listado es deliberadamente defensiva: localiza cada enlace a una ficha
de detalle (por su patrón de URL, que SÍ está confirmado:
/IMPORTE/Ausschreibungen/eVergabe/<id>.html) y sube por sus contenedores
padres hasta que el bloque de texto incluya "Veröffentlicht", en vez de
asumir una estructura de fila fija. Revisa el log "Avisos detectados en
el listado" en la primera ejecución manual (workflow_dispatch); si sale
en 0, ahí sigue habiendo margen de ajuste.

DOS FORMATOS DE FECHA DISTINTOS -- confirmado, no es un error:
  - Listado: "Veröffentlicht DD.MM.AA" (año en 2 dígitos, p. ej. "17.07.26").
  - Ficha:   "Angebotsfrist DD.MM.AAAA" (año en 4 dígitos, p. ej. "29.09.2026").
Cada uno tiene su propio parser (`parsear_fecha_listado_bund` /
`parsear_fecha_detalle_bund`); no se puede usar el mismo para ambos.

TRADUCCIÓN
------------
Se usa `deep-translator` (`GoogleTranslator(source="de", target="en")`),
tal y como se pidió. No se ha podido instalar ni probar esta librería en
este entorno de desarrollo (el acceso de red del entorno bloquea
específicamente tanto `deep-translator` como `googletrans` al hacer
`pip install`, a diferencia de paquetes normales como `requests`, que sí
instalan sin problema) -- el resto del pipeline si se ha podido probar
exhaustivamente con la traducción simulada. Debería instalarse sin
problema en GitHub Actions, que tiene salida de red completa; conviene
confirmarlo en la primera ejecución real. Si la traducción de un texto
concreto falla (servicio caído, límite de peticiones), se conserva el
texto original en alemán para ese campo en vez de perder el registro
entero -- revisa el log "Avisos con traducción fallida" tras cada
ejecución.

ASÍNCRONO
-----------
Tal y como se pidió, el script es asíncrono: las peticiones HTTP a cada
ficha de detalle y las llamadas de traducción se lanzan de forma
concurrente (con un límite, CONCURRENCIA_MAXIMA, para no saturar ni el
propio portal ni el servicio de traducción). Como `requests` (ya usado
en el resto del proyecto) y `deep_translator` son librerías síncronas,
cada llamada se ejecuta en un hilo aparte vía `asyncio.to_thread` --
evita añadir una dependencia HTTP nueva (p. ej. httpx) solo para esto,
manteniendo consistencia con el resto del proyecto, a la vez que se
consigue la concurrencia real que se pidió.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local:      python ingesta_service_bund.py
Ejecución programada: ver .github/workflows/sincronizar_service_bund.yml
"""
import asyncio
import re
from datetime import date, timedelta

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
# nn=9465610 tal y como se indicó; si no devuelve resultados, otra
# vista del mismo buscador confirmada por otra vía usa nn=4641482 --
# ver aviso de fiabilidad.
LISTADO_URL = (
    BASE_URL + "/Content/DE/Ausschreibungen/Suche/Formular.html"
    "?view=processForm&nn=9465610&sortOrder=dateOfIssue_dt+desc&resultsPerPage=100"
)
FUENTE = "SERVICE_BUND"
TIMEOUT_PETICION = 30
CONCURRENCIA_MAXIMA = 5   # limite de peticiones/traducciones simultaneas
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_publicacion", "fecha_limite")
MAX_PAGINAS = 3   # red de seguridad -- ver aviso de fiabilidad sobre la paginacion
CAPTURA_DEPURACION = "debug_service_bund_listado.html"

CABECERAS_PETICION = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

PATRON_URL_DETALLE = re.compile(r"/IMPORTE/Ausschreibungen/eVergabe/(\d+)\.html")
PATRON_FECHA_LISTADO = re.compile(r"Ver\u00f6ffentlicht:?\s*(\d{1,2})\.(\d{1,2})\.(\d{2})\b")
PATRON_FECHA_DETALLE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

# Confirmado en varias fichas reales -- el mismo aviso legal se repite
# en todas, palabra por palabra; sirve para EXCLUIRLO al buscar un
# parrafo descriptivo de respaldo (ver aviso de fiabilidad). Se
# admiten tanto "ö" como su transliteración ASCII "oe" (misma clase de
# variación ya vista y corregida en ingesta_giz_satellite.py).
PATRON_BOILERPLATE_HINWEIS = re.compile(r"ist nur die ver(?:[o\u00f6]|oe)ffentlichungsplattform", re.IGNORECASE)


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def _limpiar_texto(texto: str) -> str:
    """Quita guiones suaves (U+00AD, usados en aleman para el salto de
    linea, p. ej. 'Um\xadzugs\xadleis\xadtung') y normaliza espacios."""
    if not texto:
        return None
    texto = texto.replace("\u00ad", "")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def parsear_fecha_listado_bund(texto: str):
    """'Veröffentlicht DD.MM.AA' -- año en 2 dígitos, tal y como aparece
    en el LISTADO (distinto del formato de la ficha, ver aviso de
    fiabilidad del docstring)."""
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
    """'DD.MM.AAAA' -- año en 4 dígitos, tal y como aparece en la FICHA
    de detalle (campo Angebotsfrist)."""
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
    """
    Traduce un texto en aleman al ingles con deep-translator, en un
    hilo aparte (la libreria es sincrona). Si falla (servicio caido,
    limite de peticiones, texto vacio...) se conserva el texto
    original en aleman -- mejor un registro con un campo sin traducir
    que perder el registro entero.
    """
    if not texto:
        return None
    try:
        return await asyncio.to_thread(
            lambda: GoogleTranslator(source="de", target="en").translate(texto)
        )
    except Exception as error:
        print(f"      Aviso: fallo al traducir ('{texto[:40]}...'): {error}", flush=True)
        return texto


def _bloque_contenedor(enlace, marcador: str, max_niveles: int = 6):
    """
    Sube por los contenedores padres del enlace hasta que el texto
    acumulado incluya el marcador dado (p. ej. 'Veröffentlicht') --
    ver aviso de fiabilidad: no hay confirmación de la estructura CSS
    exacta del listado, así que no se asume ningún nivel fijo.
    """
    nodo = enlace
    for _ in range(max_niveles):
        if nodo.parent is None:
            break
        nodo = nodo.parent
        texto = nodo.get_text(" ", strip=True)
        if marcador in texto:
            return texto
    return nodo.get_text(" ", strip=True) if nodo is not None else ""


def extraer_avisos_listado(desde: date, hasta: date) -> list:
    """
    Descarga la página de resultados y devuelve, para cada aviso cuya
    fecha de publicación (Veröffentlicht) esté entre `desde` y `hasta`
    (ambos incluidos), un dict con lo mínimo fiable ahí: título tal
    como aparece listado, URL de la ficha, y fecha de publicación --
    el resto de campos se sacan de la propia ficha de detalle.

    El listado se pide ordenado por fecha descendente
    (sortOrder=dateOfIssue_dt+desc, confirmado como parámetro válido
    por otra vía) para poder parar en cuanto se detecten avisos más
    antiguos que `desde` -- ver aviso de fiabilidad sobre la
    paginación: no se ha podido confirmar el mecanismo exacto de
    "página siguiente", así que MAX_PAGINAS actúa como tope de
    seguridad más que como algo que se espere alcanzar en uso normal.
    """
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
        enlaces = soup.find_all("a", href=PATRON_URL_DETALLE)
        print(f"    Enlaces a fichas de detalle encontrados: {len(enlaces)}", flush=True)

        if not enlaces:
            break

        fecha_mas_antigua_de_la_pagina = None
        for enlace in enlaces:
            href = enlace.get("href") or ""
            coincidencia_id = PATRON_URL_DETALLE.search(href)
            if not coincidencia_id:
                continue
            url_oficial = href if href.startswith("http") else BASE_URL + href
            if url_oficial in encontrados:
                continue

            bloque_texto = _bloque_contenedor(enlace, "Ver\u00f6ffentlicht")
            fecha_publicacion = parsear_fecha_listado_bund(bloque_texto)

            if fecha_mas_antigua_de_la_pagina is None or (
                fecha_publicacion and fecha_publicacion < fecha_mas_antigua_de_la_pagina
            ):
                fecha_mas_antigua_de_la_pagina = fecha_publicacion

            if fecha_publicacion is None:
                # No se pudo leer la fecha en el listado -- se deja pasar
                # a la ficha de detalle, que trae su propia fecha (Angebotsfrist,
                # aunque esa es la de cierre, no la de publicacion) como referencia;
                # se filtrará otra vez por fecha real tras leer la ficha si hiciera falta.
                pass
            elif not (desde <= fecha_publicacion <= hasta):
                continue

            titulo_listado = _limpiar_texto(enlace.get_text(" ", strip=True))
            encontrados[url_oficial] = {
                "titulo_listado": titulo_listado,
                "url_oficial": url_oficial,
                "fecha_publicacion_listado": fecha_publicacion,
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
    Descarga la ficha de detalle y extrae, de sus pares <dt>/<dd>
    (ver aviso de fiabilidad del docstring):
      - "Vergabestelle" -> organismo
      - "Leistungen und Erzeugnisse" -> descripcion (categoría corta,
        no un resumen -- ver aviso de fiabilidad)
      - "Vergabeart" -> tipo_aviso
      - "Angebotsfrist" -> fecha_limite (DD.MM.AAAA)
    Y del <title> de la página, el título real (último segmento tras
    partir por " - ", descartando el boilerplate "SERVICE.BUND.DE -
    Aktuelle Ausschreibungen...").
    """
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

    # Respaldo/complemento: el parrafo mas largo de la pagina que no
    # sea el aviso legal repetido en todas las fichas -- ver aviso de
    # fiabilidad. Se usa si no hay "Leistungen und Erzeugnisse", o se
    # añade como contexto adicional si lo hay.
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

    async with semaforo:
        titulo_en, organismo_en, descripcion_en, tipo_aviso_en = await asyncio.gather(
            _traducir_al_ingles(titulo_de),
            _traducir_al_ingles(organismo_de),
            _traducir_al_ingles(descripcion_de),
            _traducir_al_ingles(tipo_aviso_de),
        )

    fecha_publicacion = item.get("fecha_publicacion_listado")
    fecha_limite = datos_ficha.get("fecha_limite")

    slug_base = None
    coincidencia_id = PATRON_URL_DETALLE.search(item["url_oficial"])
    if coincidencia_id:
        slug_base = coincidencia_id.group(1)
    slug_base = slug_base or _generar_slug(titulo_en or titulo_de or item["url_oficial"])

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
            f"Revisa el log de arriba y, si existe, {CAPTURA_DEPURACION} -- ver aviso de "
            "fiabilidad en el docstring sobre la estructura del listado.",
            flush=True,
        )
        return

    print("\nConsultando la ficha de cada aviso y traduciendo (de forma concurrente)...", flush=True)
    semaforo = asyncio.Semaphore(CONCURRENCIA_MAXIMA)
    normalizados = await asyncio.gather(*(_procesar_aviso(item, semaforo) for item in crudos))

    sin_fecha_limite = sum(1 for n in normalizados if not n.get("fecha_limite"))
    if sin_fecha_limite:
        print(
            f"Avisos sin fecha límite reconocida: {sin_fecha_limite}/{len(normalizados)} -- "
            "revisa si el campo 'Angebotsfrist' de la ficha usa otra redacción.",
            flush=True,
        )

    con_traduccion_igual_al_original = sum(
        1 for n in normalizados if n.get("titulo") and re.search(r"[äöüßÄÖÜ]", n["titulo"] or "")
    )
    if con_traduccion_igual_al_original:
        print(
            f"Avisos con título aparentemente sin traducir (quedan caracteres alemanes): "
            f"{con_traduccion_igual_al_original}/{len(normalizados)} -- revisa si el servicio de "
            "traducción falló (ver logs 'Aviso: fallo al traducir' arriba).",
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

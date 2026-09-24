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

AVISO DE FIABILIDAD -- FECHAS Y DESCRIPCIÓN (corregido con HTML real completo)
--------------------------------------------------------------------------
Tercera vuelta sobre esta misma fuente, cada una con más evidencia real
que la anterior. Esta versión se basa en una página de detalle COMPLETA
(no un fragmento) proporcionada por el usuario, que reveló dos fallos
reales de la versión anterior:

  1. DESCRIPCIÓN: el <span>"Objetivos Generales de la adquisición:"</span>
     NO cuelga directamente del primer <ol> de la página, como se había
     asumido -- la página real anida un <ol> dentro de otro (el <li>
     "Presentación del Proceso" del <ol> exterior contiene un <ol>
     interior, y ahí es donde vive). Por eso ahora se busca ese <span>
     por su TEXTO en todo el documento, a cualquier profundidad
     (`_span_por_texto`), y se toma el <p> que sea su hermano
     inmediatamente siguiente -- ver `find_next_sibling`, independiente
     de en qué nivel de anidación esté.
  2. FECHAS: la misma página reveló un bloque de metadatos <dt>/<dd>
     limpio cerca de la cabecera ("País", "Fecha de publicación",
     "Fecha de cierre", "Línea"), mucho más fiable que rebuscar "A
     partir de"/"Hasta"/"Fecha:" en párrafos sueltos -- ahora es la
     fuente PRINCIPAL (`_valor_dt_dd`); el rebusque anterior por <li>
     se mantiene como respaldo si el bloque dt/dd no trae alguna fecha.
  3. BUG ADICIONAL encontrado al probar con esta página real: el mes
     abreviado puede venir en ESPAÑOL ("28-ago-2026") o en INGLÉS
     ("10-Jul-2024") indistintamente. La versión anterior usaba
     `strptime("%b")`, que solo reconoce abreviaturas en inglés y
     además depende del locale del sistema -- "oct"/"Jul" coinciden
     por casualidad entre ambos idiomas y colaban el bug sin dar error,
     pero "ago" no es una abreviatura inglesa válida ("Aug" sí lo es),
     así que fallaba en silencio y `fecha_publicacion` se quedaba
     vacía. Ahora se resuelve con MESES_ABREVIADOS, un diccionario
     propio que cubre ambos idiomas sin depender del locale.

Por eso `ejecutar_sincronizacion()` lee la ficha de CADA aviso
(`obtener_datos_ficha`, con `requests` + BeautifulSoup, igual que hace
ingesta_caf.py) en vez de fiarse solo de la tabla del listado -- más
lento pero mucho más fiable, y con solo 2 páginas de avisos
(MAX_PAGINAS) el coste es asumible. Sigue sin poder confirmarse en vivo
en este entorno (sin salida de red hacia bcie.org): revisa los logs
"Avisos sin descripción reconocida" y "Fechas sin reconocer" en la
primera ejecución manual (workflow_dispatch).

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
# "28-ago-2026" / "10-Jul-2024" / "12-oct-2026" -- formato confirmado
# contra fichas reales (HTML completo proporcionado por el usuario):
# DD-<mes abreviado>-AAAA, pero el mes puede venir abreviado en ESPAÑOL
# ("ago", "ene", "abr", "dic"...) o en INGLÉS ("Jul", "Aug", "Jan"...)
# -- se ha visto research indistintamente en la misma página. Por eso
# se resuelve con este diccionario propio en vez de `strptime("%b")`
# (que solo reconoce abreviaturas EN INGLÉS, y además depende del
# locale del sistema donde se ejecute -- nada fiable): "ago" no es una
# abreviatura inglesa válida ("Aug" sí lo es), así que strptime lo
# fallaba en silencio y `fecha_publicacion` se quedaba vacía.
MESES_ABREVIADOS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
    "jan": 1, "apr": 4, "aug": 8, "dec": 12,   # los que difieren del español
}
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
    Intenta primero 'DD-Mon-AAAA' (mes abreviado en español o en
    inglés, ver MESES_ABREVIADOS -- formato confirmado contra fichas
    reales), y como red de seguridad adicional otros formatos
    habituales en español -- ver aviso de fiabilidad en el docstring
    del módulo.
    """
    if not texto:
        return None
    texto = texto.strip()

    coincidencia = PATRON_FECHA_CORTA.search(texto)
    if coincidencia:
        dia, mes_texto, anio = coincidencia.groups()
        mes = MESES_ABREVIADOS.get(mes_texto[:3].lower())
        if mes:
            try:
                return date(int(anio), mes, int(dia))
            except ValueError:
                pass

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


def _valor_dt_dd(soup, etiqueta_buscada: str):
    """Busca, en el bloque de metadatos de la ficha (los pares
    <dt>/<dd> tipo 'País: Belice', 'Fecha de publicación: 28-ago-2026',
    'Fecha de cierre: 12-oct-2026'), el <dd> del <dt> cuyo texto
    contenga la etiqueta dada."""
    etiqueta_norm = etiqueta_buscada.lower()
    for dt in soup.find_all("dt"):
        if etiqueta_norm in dt.get_text(" ", strip=True).lower():
            dd = dt.find_next_sibling("dd")
            if dd:
                return dd.get_text(" ", strip=True)
    return None


def _span_por_texto(soup, fragmento_texto: str):
    """Busca, en TODO el documento y a CUALQUIER profundidad de
    anidación de <ol>/<li>, el primer <span> cuyo texto contenga el
    fragmento dado. Ver aviso de fiabilidad: esto reemplaza a una
    versión anterior que asumía que el <li> con "Objetivos Generales"
    colgaba directamente del primer <ol> de la página -- en la ficha
    real proporcionada por el usuario, ese <li> vive dentro de un <ol>
    ANIDADO dentro de otro <ol> (el de nivel superior solo tiene
    "Fuente de Recursos" / "Organismo Ejecutor..." / "Presentación del
    Proceso" como sus 3 <li> directos), así que buscar por texto en
    todo el árbol, sin asumir ningún nivel fijo, es la única forma
    robusta de encontrarlo."""
    fragmento_norm = fragmento_texto.lower()
    for span in soup.find_all("span"):
        if fragmento_norm in span.get_text(" ", strip=True).lower():
            return span
    return None


# Etiquetas alternativas de "Objetivos Generales de la adquisición" --
# no todas las fichas usan exactamente esa redacción (ver aviso de
# fiabilidad más abajo: en una ficha real en inglés no apareció NINGUNA
# de estas).
ETIQUETAS_DESCRIPCION = (
    "objetivos generales", "objetivo general", "objetivos de la adquisición",
    "general objectives", "general objective",
)

# Fragmentos distintivos del párrafo de "Fuente de Recursos" -- el
# mismo texto legal repetido en TODAS las fichas (confirmado en español
# Y en inglés) que no dice nada específico de la licitación concreta.
# Se usa para EXCLUIRLO del respaldo por párrafos largos: como ese
# bloque suele ser el PRIMER <li> de la página (ver ficha completa de
# Belice, turno anterior), un respaldo que simplemente tomara "los N
# párrafos más largos" sin filtrar se lo encontraría con frecuencia.
FRAGMENTOS_BOILERPLATE_FUENTE_RECURSOS = (
    "servicios que brinda a sus países socios",   # español
    "services it provides to its beneficiary",      # inglés
)


def _es_boilerplate_fuente_recursos(texto: str) -> bool:
    texto_lower = texto.lower()
    return any(frag.lower() in texto_lower for frag in FRAGMENTOS_BOILERPLATE_FUENTE_RECURSOS)


def _descripcion_por_parrafos_largos(soup, longitud_minima: int = 40, maximo_parrafos: int = 2):
    """
    Respaldo para cuando ninguna ETIQUETAS_DESCRIPCION aparece en la
    página (ver aviso de fiabilidad en el docstring de
    obtener_datos_ficha): se toman, en el orden en que aparecen en la
    página, los primeros párrafos suficientemente largos que NO sean
    el boilerplate de "Fuente de Recursos" -- confirmado contra una
    ficha real que esto suele bastar (el nombre del programa general,
    seguido del alcance específico del contrato).
    """
    candidatos = []
    for p in soup.find_all("p"):
        texto = p.get_text(" ", strip=True)
        if len(texto) < longitud_minima:
            continue
        if _es_boilerplate_fuente_recursos(texto):
            continue
        candidatos.append(texto)
        if len(candidatos) >= maximo_parrafos:
            break
    return " ".join(candidatos) if candidatos else None


def obtener_datos_ficha(url: str) -> dict:
    """
    Lee la ficha del aviso para sacar su descripción y sus fechas de
    forma más fiable que la tabla del listado -- ver aviso de
    fiabilidad en el docstring del módulo.

    Dos fuentes, confirmadas contra una página real COMPLETA (no solo
    un fragmento) proporcionada por el usuario:

    1. FECHAS -- bloque de metadatos <dt>/<dd> cerca de la cabecera de
       la ficha: "Fecha de publicación" y "Fecha de cierre" ya vienen
       ahí, limpias y etiquetadas sin ambigüedad -- ya no hace falta
       rebuscar "A partir de"/"Hasta"/"Fecha:" dentro de los <li>
       anidados para esto (aunque se conserva como respaldo, ver más
       abajo). Se ha confirmado además que "Fecha de cierre" coincide
       exactamente con el "Fecha:" que aparece bajo "...se recibirán
       en:", así que son la misma fecha vista desde dos sitios.

    2. DESCRIPCIÓN -- el <span> de alguna ETIQUETAS_DESCRIPCION
       (normalmente "Objetivos Generales de la adquisición:") seguido
       de su <p> hermano. Este <span> NO vive directamente en el
       primer <ol> de la página: la estructura real anida un <ol>
       dentro de otro, así que se busca por el propio texto del
       <span>, a cualquier profundidad -- ver _span_por_texto. Si
       NINGUNA etiqueta aparece (confirmado con una ficha real en
       inglés de un contrato de obra en Costa Rica, donde ni siquiera
       las variantes en inglés aparecían), se cae a
       _descripcion_por_parrafos_largos: los primeros párrafos largos
       de la página que no sean el boilerplate repetido de "Fuente de
       Recursos".
    """
    resultado = {"descripcion": None, "fecha_publicacion": None, "fecha_limite": None}

    try:
        respuesta = requests.get(url, timeout=TIMEOUT_PETICION, headers=CABECERAS_PETICION)
        respuesta.raise_for_status()
    except Exception as error:
        print(f"      Error descargando la ficha: {error}", flush=True)
        return resultado

    soup = BeautifulSoup(respuesta.text, "html.parser")

    # 1. Fechas: bloque de metadatos dt/dd, fuente principal.
    texto_fecha_pub = _valor_dt_dd(soup, "fecha de publicación") or _valor_dt_dd(soup, "fecha de publicacion")
    if texto_fecha_pub:
        resultado["fecha_publicacion"] = parsear_fecha_bcie(texto_fecha_pub)

    texto_fecha_lim = _valor_dt_dd(soup, "fecha de cierre")
    if texto_fecha_lim:
        resultado["fecha_limite"] = parsear_fecha_bcie(texto_fecha_lim)

    # 2. Descripción: span "Objetivos Generales..." (o alguna variante)
    # + su <p> hermano, a cualquier profundidad de anidación.
    span_objetivos = None
    for etiqueta in ETIQUETAS_DESCRIPCION:
        span_objetivos = _span_por_texto(soup, etiqueta)
        if span_objetivos:
            break
    if span_objetivos:
        parrafo = span_objetivos.find_next_sibling("p")
        if parrafo:
            resultado["descripcion"] = parrafo.get_text(" ", strip=True)

    if resultado["descripcion"] is None:
        resultado["descripcion"] = _descripcion_por_parrafos_largos(soup)

    # Respaldos, solo si el bloque dt/dd no trajo alguna de las fechas
    # (ficha con otro formato) -- mismo mecanismo que la versión
    # anterior, por si acaso.
    if resultado["fecha_limite"] is None:
        span_recibiran = _span_por_texto(soup, "se recibirán en") or _span_por_texto(soup, "se recibiran en")
        if span_recibiran:
            parrafo = span_recibiran.find_next_sibling("p")
            if parrafo:
                coincidencia = re.match(r"fecha:?\s*(.+)", parrafo.get_text(" ", strip=True), re.IGNORECASE)
                if coincidencia:
                    resultado["fecha_limite"] = parsear_fecha_bcie(coincidencia.group(1))

    texto_plano = None
    if resultado["fecha_publicacion"] is None or resultado["fecha_limite"] is None:
        texto_plano = soup.get_text(" ", strip=True)

    if resultado["fecha_publicacion"] is None:
        coincidencia_partir = re.search(
            r"[Aa] partir de:?\s*(\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]\d{4})", texto_plano
        )
        if coincidencia_partir:
            resultado["fecha_publicacion"] = parsear_fecha_bcie(coincidencia_partir.group(1))

    if resultado["fecha_limite"] is None:
        coincidencia_hasta = re.search(r"[Hh]asta:?\s*(\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]\d{4})", texto_plano)
        if coincidencia_hasta:
            resultado["fecha_limite"] = parsear_fecha_bcie(coincidencia_hasta.group(1))
        else:
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

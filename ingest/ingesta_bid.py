# -*- coding: utf-8 -*-
"""
ingesta_bid.py
----------------
Sincroniza avisos de adquisiciones del Banco Interamericano de Desarrollo
(BID/IADB) contra la tabla `licitaciones_internacionales` de Supabase,
leyendo la tabla real del portal:

    https://beo-procurement.iadb.org/home

usando Playwright (navegador real, headless, gratuito) -- necesario porque
la tabla se renderiza con JavaScript.

ESTA VERSIÓN REEMPLAZA POR COMPLETO EL ENFOQUE ANTERIOR (Power BI)
--------------------------------------------------------------------------
La versión anterior de este script apuntaba a una URL distinta
(iadb.org/.../avisos-de-adquisiciones) asumiendo que el listado vivía
dentro de un informe de Power BI embebido, con selectores ARIA
(`role="row"`, `role="gridcell"`...) -- una suposición nunca confirmada
contra la página real (el entorno de desarrollo no tenía salida de red
hacia iadb.org, según su propio docstring).

Se sustituye entera por la lógica de este docstring, adaptada de un script
de prueba que SÍ se ha ejecutado con éxito contra la página real
(beo-procurement.iadb.org/home) fuera de este entorno de desarrollo: la
tabla es HTML normal, con cada aviso en una fila `tr.master-row` seguida
de su fila de detalle `tr.detail-row` (país, sub-sector y fecha de
publicación viven en esa fila de detalle, identificados por su
`.detail-label`) -- NO hace falta desplegar nada a mano ni hacer una
petición aparte por aviso: la fila de detalle ya está en el DOM, y
`extraer_avisos_de_pagina()` la lee junto con su `master-row` en la misma
pasada.

Único cambio respecto al script de prueba (aparte de quitar el andamiaje
propio de Colab -- `nest_asyncio`, `async_playwright`, el `await` a nivel
de módulo): se usa `playwright.sync_api`, igual que el resto de scrapers
de este proyecto que necesitan un navegador (ver ingesta_afdb.py) -- así
`ejecutar_sincronizacion()` sigue siendo una función síncrona normal,
invocable con `python ingesta_bid.py` sin ningún event loop de por medio,
que es como se ejecuta en producción (ver
.github/workflows/sincronizar_bid.yml).

VENTANA DE FECHAS
------------------
Igual que en el script de prueba: solo se suben avisos cuya fecha de
publicación ("Notice Publication Date") caiga dentro de los últimos
DIAS_ATRAS días. A diferencia de CAF/UGPE (que usan una tabla auxiliar
porque su ficha completa exige una petición aparte, cara de repetir), aquí
NO hace falta: toda la información ya llega en la misma pasada del
listado, así que una ventana de fechas simple -- igual que hacía la
versión anterior de este mismo script -- es la estrategia más sencilla
que cubre el caso de uso (sincronización diaria).

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_bid.py
Ejecucion programada: ver .github/workflows/sincronizar_bid.yml
   (necesita el paso extra "playwright install --with-deps chromium")
"""
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://beo-procurement.iadb.org/home"
FUENTE = "BID/IADB"
DIAS_ATRAS = 2   # el script de prueba validado exigía fecha_publicacion en {hoy, ayer}
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_publicacion", "fecha_limite")

TIEMPO_ESPERA_CARGA_MS = 60000
MAX_PAGINAS = 5
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 2
CAPTURA_DEPURACION = "debug_bid_tabla.png"

CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# JS de extracción tal cual se validó: empareja cada 'tr.master-row' (nº de
# selección, título, fecha límite) con su 'tr.detail-row' inmediatamente
# siguiente (país, sub-sector, fecha de publicación), identificando estos
# últimos por el texto de su '.detail-label'.
_JS_EXTRAER_FILAS_IADB = """
() => {
    const resultados = [];
    const masterRows = document.querySelectorAll('tr.master-row');

    masterRows.forEach(master => {
        const enlaceEl = master.querySelector('td:nth-child(2) a');
        const selectionNo = enlaceEl ? enlaceEl.innerText.trim() : '';
        const href = enlaceEl ? enlaceEl.getAttribute('href') : '';

        const tituloEl = master.querySelector('td:nth-child(3)');
        const titulo = tituloEl ? tituloEl.innerText.trim() : '';

        const deadlineEl = master.querySelector('td.oei-date');
        const deadline = deadlineEl ? deadlineEl.innerText.trim() : '';

        let detailRow = master.nextElementSibling;
        while (detailRow && !detailRow.classList.contains('detail-row')) {
            detailRow = detailRow.nextElementSibling;
        }

        let publicationDate = '';
        let country = '';
        let subSector = '';

        if (detailRow) {
            const labels = detailRow.querySelectorAll('.detail-label');
            labels.forEach(label => {
                const textLabel = label.innerText.trim().toLowerCase();
                const nodePadre = label.parentNode;

                if (textLabel.includes('notice publication date')) {
                    publicationDate = nodePadre.innerText.replace(/Notice Publication Date:/i, '').split('\\n')[0].trim();
                } else if (textLabel.includes('operation country')) {
                    country = nodePadre.innerText.replace(/Operation Country:/i, '').split('\\n')[0].trim();
                } else if (textLabel.includes('sub-sector')) {
                    subSector = nodePadre.innerText.replace(/Sub-Sector:/i, '').split('\\n')[0].trim();
                }
            });
        }

        resultados.push({
            selection_no: selectionNo,
            titulo: titulo,
            pais: country,
            sub_sector: subSector,
            href: href,
            publication_date_raw: publicationDate,
            deadline_raw: deadline
        });
    });

    return resultados;
}
"""


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


# Las etiquetas de la fila de detalle (ver _JS_EXTRAER_FILAS_IADB) no
# siempre quedan separadas por salto de línea en nodePadre.innerText: se
# ha observado en producción que el país capturado arrastra la etiqueta
# SIGUIENTE pegada sin espacio, p. ej. "PerúFunding Source:
# ATN/PS-22383-PE" en vez de "Perú". Se limpia aquí (en vez de en el JS)
# porque es mucho más fácil de probar sin necesitar un navegador real:
# se corta cualquier cosa a partir de la primera etiqueta de detalle
# conocida que aparezca (la que sea, no solo "Funding Source" -- las
# demás podrían arrastrarse igual si su orden en el DOM cambiara).
ETIQUETAS_DETALLE_BID = (
    "Funding Source", "Operation Country", "Sub-Sector",
    "Notice Publication Date", "Deadline",
)
PATRON_ETIQUETA_SIGUIENTE_BID = re.compile(
    r"\s*(?:" + "|".join(re.escape(e) for e in ETIQUETAS_DETALLE_BID) + r")\s*:.*$",
    re.IGNORECASE | re.DOTALL,
)


def _limpiar_valor_detalle_bid(texto: str):
    """Quita del final del texto cualquier etiqueta de detalle que se
    haya arrastrado pegada (ver ETIQUETAS_DETALLE_BID)."""
    if not texto:
        return None
    limpio = PATRON_ETIQUETA_SIGUIENTE_BID.sub("", texto).strip()
    return limpio or None


def parsear_fecha_iadb(cadena_fecha: str):
    """Convierte cadenas del tipo '21-Sept-2026' / '21-Sep-2026' a date."""
    if not cadena_fecha:
        return None

    cadena_fecha = cadena_fecha.strip()
    cadena_fecha = re.sub(r"Sept", "Sep", cadena_fecha, flags=re.IGNORECASE)

    coincidencia = re.search(r"(\d{1,2})[-/\s]([A-Za-z]{3,9})[-/\s](\d{4})", cadena_fecha)
    if not coincidencia:
        return None

    dia, mes_texto, anio = coincidencia.groups()
    cadena_estandar = f"{int(dia):02d}-{mes_texto.capitalize()}-{anio}"

    for formato in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(cadena_estandar, formato).date()
        except ValueError:
            continue
    return None


def extraer_licitaciones_playwright() -> list:
    """
    Abre el portal, recorre hasta MAX_PAGINAS páginas de la tabla (avanzando
    con el botón "siguiente" si existe) y devuelve TODOS los avisos vistos,
    sin filtrar todavía por fecha -- ver ejecutar_sincronizacion().
    """
    registros_por_url = {}
    navegador = None

    try:
        with sync_playwright() as p:
            try:
                navegador = p.chromium.launch(headless=True)
                contexto = navegador.new_context(
                    user_agent=CABECERAS_USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                )
                pagina = contexto.new_page()

                print(f"--> Cargando portal de licitaciones BID (IADB): {BASE_URL}...", flush=True)
                pagina.goto(BASE_URL, timeout=TIEMPO_ESPERA_CARGA_MS, wait_until="domcontentloaded")
                pagina.wait_for_selector("tr.master-row", timeout=TIEMPO_ESPERA_CARGA_MS)

                for indice_pagina in range(1, MAX_PAGINAS + 1):
                    filas = pagina.evaluate(_JS_EXTRAER_FILAS_IADB)

                    for item in filas:
                        href = item.get("href")
                        url_completa = urljoin(BASE_URL, href) if href else None
                        if not url_completa or url_completa in registros_por_url:
                            continue
                        registros_por_url[url_completa] = {
                            "selection_no": item.get("selection_no"),
                            "titulo": item.get("titulo"),
                            "pais": item.get("pais"),
                            "sub_sector": item.get("sub_sector"),
                            "publication_date_raw": item.get("publication_date_raw"),
                            "deadline_raw": item.get("deadline_raw"),
                            "url_oficial": url_completa,
                        }

                    print(
                        f"    Página {indice_pagina}/{MAX_PAGINAS} -> avisos acumulados: {len(registros_por_url)}",
                        flush=True,
                    )

                    boton_siguiente = pagina.locator(".pager-next a, li.next a, button.next")
                    if boton_siguiente.count() > 0 and boton_siguiente.first.is_visible():
                        boton_siguiente.first.click()
                        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)
                    else:
                        break

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
        # Red de seguridad final -- ver mismo patrón en ingesta_afdb.py: si
        # incluso el arranque de Playwright (chromium.launch) fallara, esto
        # evita que la excepción se escape sin loggear nada útil.
        print(f"Error inesperado no capturado dentro de Playwright: {error}", flush=True)

    return list(registros_por_url.values())


def construir_registro(item: dict) -> dict:
    fecha_publicacion = parsear_fecha_iadb(item.get("publication_date_raw"))
    fecha_limite = parsear_fecha_iadb(item.get("deadline_raw"))

    pais = _limpiar_valor_detalle_bid(item.get("pais"))
    sub_sector = _limpiar_valor_detalle_bid(item.get("sub_sector"))

    partes_descripcion = []
    if sub_sector:
        partes_descripcion.append(f"Sub-sector: {sub_sector}.")
    if item.get("deadline_raw"):
        partes_descripcion.append(f"Plazo (REOI deadline): {item['deadline_raw']}.")
    descripcion = " ".join(partes_descripcion) or None

    selection_no = (item.get("selection_no") or "").strip()
    slug_base = selection_no or _generar_slug(item.get("titulo") or item["url_oficial"])

    return {
        "codigo_unico": f"BID-{_generar_slug(slug_base)}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": "REOI" if item.get("deadline_raw") else None,
        "titulo": item.get("titulo"),
        "descripcion": descripcion,
        "pais": pais,
        "paises": [pais] if pais else [],
        "organismo": "BID",
        # Se guarda el sub-sector del proyecto aquí (clasificación temática),
        # no en tipo_aviso (que en el resto de fuentes indica el TIPO de
        # aviso -- obras/servicios/convocatoria -- no el sector).
        "categoria": sub_sector,
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
    hoy = date.today()
    desde = hoy - timedelta(days=DIAS_ATRAS - 1)

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - BID (tabla real vía Playwright)", flush=True)
    print("=" * 100, flush=True)
    print(f"Ventana de publicación: {desde} .. {hoy}", flush=True)

    crudos = extraer_licitaciones_playwright()
    print(f"\nTotal avisos rastreados (todas las páginas, sin filtrar por fecha): {len(crudos)}", flush=True)

    if not crudos:
        print(
            "No se ha extraído ningún aviso. Revisa el log de arriba y, si existe, "
            f"{CAPTURA_DEPURACION} -- lo más probable es que la estructura real de la tabla "
            "haya cambiado respecto a 'tr.master-row'/'tr.detail-row'.",
            flush=True,
        )
        return

    normalizados = [construir_registro(item) for item in crudos]

    en_ventana = [
        n for n in normalizados
        if n.get("fecha_publicacion") and date.fromisoformat(n["fecha_publicacion"]) >= desde
    ]
    print(f"Dentro de la ventana de {DIAS_ATRAS} días (por fecha de publicación reconocida): {len(en_ventana)}", flush=True)

    if not en_ventana:
        print("No hay avisos dentro de la ventana de días configurada.", flush=True)
        return

    supabase = obtener_cliente_supabase()

    print("\nComparando con lo ya existente en Supabase...", flush=True)
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico") + CAMPOS_COMPARABLES,
        claves=[n["codigo_unico"] for n in en_ventana],
    )

    lote_final = preparar_lote_para_subir(en_ventana, registros_existentes)

    if not lote_final:
        print("No hay avisos nuevos ni cambios que sincronizar.", flush=True)
        return

    subidas = subir_en_lotes(
        supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
    )
    print(f"\nSincronizacion BID completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

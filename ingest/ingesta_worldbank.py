# -*- coding: utf-8 -*-
"""
ingesta_worldbank.py
----------------------
Sincroniza avisos de adquisición CORPORATIVA/ADMINISTRATIVA del Banco
Mundial (compras para el propio funcionamiento del Banco -- no avisos de
proyectos de desarrollo en países prestatarios, que es un portal
distinto) contra la tabla `licitaciones_internacionales` de Supabase,
leyendo la tabla real del portal:

    https://www.worldbank.org/en/about/corporate-procurement/business-opportunities/administrative-procurement

usando Playwright (navegador real, headless, gratuito) -- necesario porque
la tabla se renderiza con JavaScript.

Adaptado de un script de prueba que se ha ejecutado con éxito contra la
página real fuera de este entorno de desarrollo: cada aviso es una fila
de tabla genérica con 4+ celdas (título con enlace, número de
solicitación, fecha de publicación, fecha de cierre). Único cambio
respecto al script de prueba (aparte de quitar el andamiaje propio de
Colab): se usa `playwright.sync_api`, igual que el resto de scrapers de
este proyecto que necesitan un navegador.

SOBRE EL CAMPO "PAÍS"
------------------------
Al ser procurement CORPORATIVO (bienes/servicios para el propio Banco:
TI, instalaciones, consultoría interna...), no todos los avisos están
ligados a un país concreto -- el script de prueba tampoco capturaba este
campo. Se deja `pais`/`paises` vacíos salvo que el propio título mencione
un país reconocible (mismo mecanismo best-effort que ingesta_ungm.py,
ver PAISES_ONU_PARCIAL) -- no se fuerza un valor solo para rellenar el
campo.

VENTANA DE FECHAS
------------------
Igual que en el script de prueba: solo se suben avisos cuya fecha de
publicación (issue date) caiga dentro de los últimos DIAS_ATRAS días
(15, igual que el script validado). No usa tabla auxiliar por el mismo
motivo que BID/UNDP: toda la información llega ya en la misma pasada del
listado.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:     python ingesta_worldbank.py
Ejecucion programada: ver .github/workflows/sincronizar_worldbank.yml
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

BASE_URL = (
    "https://www.worldbank.org/en/about/corporate-procurement/"
    "business-opportunities/administrative-procurement"
)
FUENTE = "World Bank"
DIAS_ATRAS = 15    # igual que el script de prueba validado
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "fecha_publicacion", "fecha_limite")

TIEMPO_ESPERA_CARGA_MS = 60000
MAX_PAGINAS = 5
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 2
CAPTURA_DEPURACION = "debug_worldbank_tabla.png"

CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MESES = {
    "enero": 1, "january": 1, "jan": 1,
    "febrero": 2, "february": 2, "feb": 2,
    "marzo": 3, "march": 3, "mar": 3,
    "abril": 4, "april": 4, "apr": 4,
    "mayo": 5, "may": 5,
    "junio": 6, "june": 6, "jun": 6,
    "julio": 7, "july": 7, "jul": 7,
    "agosto": 8, "august": 8, "aug": 8,
    "septiembre": 9, "september": 9, "sep": 9, "sept": 9,
    "octubre": 10, "october": 10, "oct": 10,
    "noviembre": 11, "november": 11, "nov": 11,
    "diciembre": 12, "december": 12, "dec": 12,
}
PATRON_FECHA_COMA = re.compile(r"([a-z]+)\s+(\d{1,2})\s*,\s*(\d{4})")
PATRON_FECHA_GUION = re.compile(r"(\d{1,2})[\s\-/]+(?:de\s+)?([a-z]+)[\s\-/]+(?:de\s+)?(\d{4})")

# Best-effort, igual que en ingesta_ungm.py -- ver "SOBRE EL CAMPO PAÍS"
# en el docstring: nunca se fuerza, solo se usa si aparece tal cual.
PAISES_ONU_PARCIAL = [
    "Afghanistan", "Argentina", "Bangladesh", "Bolivia", "Brazil", "Cambodia",
    "Cameroon", "Chile", "China", "Colombia", "Congo", "Egypt", "Ethiopia",
    "Ghana", "Guatemala", "Haiti", "Honduras", "India", "Indonesia", "Iraq",
    "Jordan", "Kenya", "Liberia", "Madagascar", "Malawi", "Mali",
    "Mexico", "Morocco", "Mozambique", "Myanmar", "Nepal", "Nicaragua",
    "Niger", "Nigeria", "Pakistan", "Paraguay", "Peru", "Philippines",
    "Rwanda", "Senegal", "Somalia", "South Africa", "South Sudan", "Sudan",
    "Tanzania", "Thailand", "Tunisia", "Turkey", "Uganda", "Ukraine",
    "Vietnam", "Yemen", "Zambia", "Zimbabwe",
]

# JS de extracción tal cual se validó: tabla generica, se descarta la
# fila de cabecera comprobando que la 1a celda no diga "Solicitation Title".
_JS_EXTRAER_FILAS_WB = """
() => {
    const resultados = [];
    const rows = document.querySelectorAll('table tbody tr, table tr');

    rows.forEach(row => {
        const cols = row.querySelectorAll('td');
        if (cols.length >= 4) {
            const titleEl = cols[0].querySelector('a');
            const titulo = titleEl ? titleEl.innerText.trim() : cols[0].innerText.trim();
            const href = titleEl ? titleEl.getAttribute('href') : '';

            const number = cols[1] ? cols[1].innerText.trim() : '';
            const issueDate = cols[2] ? cols[2].innerText.trim() : '';
            const closingDate = cols[3] ? cols[3].innerText.trim() : '';

            if (titulo && !titulo.toLowerCase().includes('solicitation title')) {
                resultados.push({
                    number: number,
                    titulo: titulo,
                    href: href,
                    issue_date_raw: issueDate,
                    closing_date_raw: closingDate
                });
            }
        }
    });

    return resultados;
}
"""


def _limpiar_titulo(titulo: str) -> str:
    """Limpia el título removiendo prefijos tipo 'RFxNow 1234567 - ' y comillas."""
    if not titulo:
        return ""
    # Remueve 'RFxNow <numero> - ' del inicio si existe
    titulo_limpio = re.sub(r"^RFxNow\s+\d+\s*-\s*", "", titulo, flags=re.IGNORECASE)
    # Remueve comillas dobles/simples circundantes o internas sobrantes
    titulo_limpio = titulo_limpio.strip(' "\'“’')
    return titulo_limpio


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def parsear_fecha_wb(cadena_fecha: str):
    """Convierte 'September 17,2026' / 'September 17, 2026' / '17-Sep-2026' a date."""
    if not cadena_fecha:
        return None
    cadena_fecha = cadena_fecha.strip().lower()

    coincidencia = PATRON_FECHA_COMA.search(cadena_fecha)
    if coincidencia:
        mes_texto, dia, anio = coincidencia.groups()
        mes = MESES.get(mes_texto)
        if mes:
            try:
                return datetime(int(anio), mes, int(dia)).date()
            except ValueError:
                pass

    coincidencia = PATRON_FECHA_GUION.search(cadena_fecha)
    if coincidencia:
        dia, mes_texto, anio = coincidencia.groups()
        mes = MESES.get(mes_texto)
        if mes:
            try:
                return datetime(int(anio), mes, int(dia)).date()
            except ValueError:
                pass

    return None


def _detectar_pais(texto: str):
    if not texto:
        return None
    texto_lower = texto.lower()
    for pais in PAISES_ONU_PARCIAL:
        if re.search(rf"\b{re.escape(pais.lower())}\b", texto_lower):
            return pais
    return None


def extraer_licitaciones_playwright() -> list:
    """
    Abre el portal y recorre hasta MAX_PAGINAS páginas (avanzando con el
    botón "siguiente" si existe), devolviendo TODOS los avisos vistos,
    sin filtrar todavía por fecha -- ver ejecutar_sincronizacion().
    """
    registros_por_url = {}

    try:
        with sync_playwright() as p:
            navegador = None
            try:
                navegador = p.chromium.launch(headless=True)
                contexto = navegador.new_context(
                    user_agent=CABECERAS_USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                )
                pagina = contexto.new_page()

                print(f"--> Cargando portal de adquisiciones administrativas del World Bank: {BASE_URL}...", flush=True)
                pagina.goto(BASE_URL, timeout=TIEMPO_ESPERA_CARGA_MS, wait_until="domcontentloaded")
                pagina.wait_for_selector("table", timeout=TIEMPO_ESPERA_CARGA_MS)

                for indice_pagina in range(1, MAX_PAGINAS + 1):
                    filas = pagina.evaluate(_JS_EXTRAER_FILAS_WB)

                    for item in filas:
                        href = item.get("href")
                        url_completa = urljoin(BASE_URL, href) if href else None
                        if not url_completa or url_completa in registros_por_url:
                            continue
                        registros_por_url[url_completa] = {
                            "number": item.get("number"),
                            "titulo": item.get("titulo"),
                            "issue_date_raw": item.get("issue_date_raw"),
                            "closing_date_raw": item.get("closing_date_raw"),
                            "url_oficial": url_completa,
                        }

                    print(
                        f"    Página {indice_pagina}/{MAX_PAGINAS} -> avisos acumulados: {len(registros_por_url)}",
                        flush=True,
                    )

                    boton_siguiente = pagina.locator(".pagination .next a, a.next, li.next-page a")
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
        print(f"Error inesperado no capturado dentro de Playwright: {error}", flush=True)

    return list(registros_por_url.values())


def extraer_detalles_avisos(avisos: list) -> list:
    """Navega a la URL de detalle de cada aviso para extraer la descripción completa."""
    if not avisos:
        return avisos

    print(f"\n--> Extrayendo detalles para {len(avisos)} avisos en la ventana de fechas...", flush=True)
    try:
        with sync_playwright() as p:
            navegador = p.chromium.launch(headless=True)
            contexto = navegador.new_context(
                user_agent=CABECERAS_USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            pagina = contexto.new_page()

            for i, aviso in enumerate(avisos, 1):
                url = aviso.get("url_oficial")
                if not url:
                    continue

                try:
                    print(f"   [{i}/{len(avisos)}] Cargando detalle: {url}", flush=True)
                    pagina.goto(url, timeout=30000, wait_until="domcontentloaded")
                    
                    # Extraer el texto completo presente en div.procurement_detail.section
                    selector_detalle = "div.procurement_detail.section, .procurement_detail"
                    if pagina.locator(selector_detalle).count() > 0:
                        texto_detalle = pagina.locator(selector_detalle).first.inner_text()
                        # Limpiar espacios en blanco innecesarios
                        lineas = [linea.strip() for linea in texto_detalle.splitlines() if linea.strip()]
                        aviso["descripcion"] = "\n".join(lineas)
                    else:
                        print("     Warning: No se encontró la sección div.procurement_detail", flush=True)
                except Exception as err:
                    print(f"     Error al cargar detalle de {url}: {err}", flush=True)

            navegador.close()
    except Exception as e:
        print(f"Error al abrir navegador para extraer detalles: {e}", flush=True)

    return avisos


def construir_registro(item: dict) -> dict:
    fecha_publicacion = parsear_fecha_wb(item.get("issue_date_raw"))
    fecha_limite = parsear_fecha_wb(item.get("closing_date_raw"))

    titulo_limpio = _limpiar_titulo(item.get("titulo"))
    pais = _detectar_pais(titulo_limpio)

    descripcion = item.get("descripcion")
    if not descripcion and item.get("number"):
        descripcion = f"Nº de solicitación: {item['number']}."

    number = (item.get("number") or "").strip()
    slug_base = number or _generar_slug(titulo_limpio or item["url_oficial"])

    return {
        "codigo_unico": f"WB-{_generar_slug(slug_base)}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": "Administrative Procurement",
        "titulo": titulo_limpio,
        "descripcion": descripcion,
        "pais": pais,
        "paises": [pais] if pais else [],
        "organismo": "World Bank",
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
    hoy = date.today()
    desde = hoy - timedelta(days=DIAS_ATRAS)

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - WORLD BANK (administrative procurement)", flush=True)
    print("=" * 100, flush=True)
    print(f"Ventana de publicación: {desde} .. {hoy}", flush=True)

    crudos = extraer_licitaciones_playwright()
    print(f"\nTotal avisos rastreados (todas las páginas, sin filtrar por fecha): {len(crudos)}", flush=True)

    if not crudos:
        print(
            "No se ha extraído ningún aviso. Revisa el log de arriba y, si existe, "
            f"{CAPTURA_DEPURACION} -- lo más probable es que la estructura real de la tabla "
            "haya cambiado.",
            flush=True,
        )
        return

    en_ventana = [
        item for item in crudos
        if item.get("issue_date_raw") and parsear_fecha_wb(item["issue_date_raw"]) and desde <= parsear_fecha_wb(item["issue_date_raw"]) <= hoy
    ]
    print(f"Dentro de la ventana de {DIAS_ATRAS} días (por fecha de publicación reconocida): {len(en_ventana)}", flush=True)

    if not en_ventana:
        print("No hay avisos dentro de la ventana de días configurada.", flush=True)
        return

    # Extraer la descripción desde cada página de detalle solo para los avisos en ventana
    en_ventana_con_detalle = extraer_detalles_avisos(en_ventana)

    normalizados = [construir_registro(item) for item in en_ventana_con_detalle]

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
    print(f"\nSincronizacion World Bank completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

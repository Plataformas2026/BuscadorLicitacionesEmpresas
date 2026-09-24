# -*- coding: utf-8 -*-
"""
ingesta_ungm.py
----------------
Sincroniza avisos de adquisiciones de UNGM (United Nations Global
Marketplace) contra la tabla `licitaciones_internacionales` de Supabase.

CAMBIO DE ARQUITECTURA -- FICHA POR AVISO, NO FILAS DEL LISTADO
--------------------------------------------------------------------------
Versiones anteriores intentaban leer fecha de publicación, fecha límite
y país directamente de las filas del listado (`#tblNotices`/
`.ungm-list-item`), con selectores `.row .label`/`.value` -- pero esa
estructura de "fila con etiqueta y valor" NO vive en el listado: vive en
la FICHA de cada aviso individual (`/Public/Notice/<id>`), confirmado
contra una página de detalle real proporcionada por el usuario. Por eso
las fechas salían vacías o mal, aunque los selectores en sí fueran
correctos -- se estaban aplicando sobre el documento equivocado.

Ahora el listado (`extraer_licitaciones_playwright`, con Playwright,
scroll infinito) SOLO se usa para descubrir título + URL de cada aviso
-- lo mínimo que sí es fiable ahí. Para cada aviso descubierto,
`obtener_datos_ficha` visita su propia página con `requests` (la ficha
es HTML servido por el servidor, no hace falta navegador para leerla,
igual que en ingesta_bcie.py/ingesta_caf.py) y extrae de ahí, con
selectores CSS estructurales sobre pares `<div class="row">
<span class="label">Etiqueta:</span><span class="value">Valor</span>
</div>`:
  - "Published on" -> fecha_publicacion
  - "Deadline on" -> fecha_limite (puede traer hora y zona horaria detrás,
    p. ej. "26-Sep-2026 12:00 (GMT 2.00)"; el propio patrón de fecha ya
    ignora ese sobrante)
  - "Beneficiary countries or territories" -> país (se intenta primero
    tal cual sobre PAISES_ONU antes de descartar por no encontrarlo)
  - El panel cuyo título es literalmente "Description" -> la
    descripción real del aviso (antes era un texto sintético a partir
    de fragmentos sueltos de la fila del listado)

Con MAX_SCROLLS=150 el listado puede descubrir muchos avisos; visitar
la ficha de cada uno con `requests` es rápido (sin arrancar navegador
por aviso) pero sigue siendo una petición HTTP por aviso -- 
MAX_FICHAS_A_CONSULTAR pone un tope de seguridad razonable para no
disparar peticiones sin límite si el listado devolviera muchos más
avisos de los esperados para una ventana de 2 días.

AVISO DE FIABILIDAD
------------------------
No hay salida de red hacia ungm.org en este entorno de desarrollo. El
listado (selectores de descubrimiento de título+URL) sigue sin
confirmarse en vivo -- revisa "Total avisos rastreados" en la primera
ejecución manual (workflow_dispatch); si sale en 0, ahí sigue habiendo
margen de ajuste. La FICHA (fechas, país, descripción) sí se ha
validado contra una página de detalle real completa.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local:     python ingesta_ungm.py
Ejecución programada: ver .github/workflows/sincronizar_ungm.yml
"""
import re
import time
import unicodedata
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

BASE_URL = "https://www.ungm.org"
LISTADO_URL = BASE_URL + "/Public/Notice"
FUENTE = "UNGM"
DIAS_ATRAS = 2   # Coge avisos con fecha de publicación de hoy y ayer
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_publicacion", "fecha_limite")

TIEMPO_ESPERA_CARGA_MS = 60000
TIMEOUT_PETICION = 30
PAUSA_ENTRE_FICHAS_SEGUNDOS = 0.3
MAX_FICHAS_A_CONSULTAR = 400   # red de seguridad -- ver aviso de fiabilidad
MAX_SCROLLS = 150
PAUSA_ENTRE_SCROLLS_SEGUNDOS = 2.0
CAPTURA_DEPURACION = "debug_ungm_tabla.png"

CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CABECERAS_PETICION = {"User-Agent": CABECERAS_USER_AGENT}

# JS simplificado: el listado SOLO se usa para descubrir título + URL de
# cada aviso -- ver aviso de fiabilidad más abajo sobre por qué ya no se
# intentan sacar fechas/país de las filas del listado.
_JS_EXTRAER_FILAS = """
() => {
    const resultados = [];
    const filas = document.querySelectorAll('#tblNotices tr, #tblNotices .tableRow, .ungm-list-item');

    filas.forEach(fila => {
        const enlace = fila.querySelector('a[href*="/Public/Notice/"]');
        let href = enlace ? enlace.getAttribute('href') : null;

        if (!href) {
            const btn = fila.querySelector('[data-noticeid], [data-notice-id]');
            if (btn) {
                const noticeId = btn.getAttribute('data-noticeid') || btn.getAttribute('data-notice-id');
                if (noticeId) {
                    href = '/Public/Notice/' + noticeId;
                }
            }
        }

        if (!href) return;

        let titulo = '';
        const elTitulo = fila.querySelector('.title');
        if (elTitulo) {
            titulo = elTitulo.innerText.trim();
        } else if (enlace) {
            titulo = (enlace.innerText || enlace.getAttribute('title') || '').trim();
        }

        resultados.push({ titulo, href });
    });

    return resultados;
}
"""

PATRON_FECHA = re.compile(r"\b(\d{1,2})[-/\s]([A-Za-z]{3,9})[-/\s](\d{4})\b")
PATRON_ID_NOTICE = re.compile(r"/Public/Notice/(\d+)")

PAISES_ONU = [
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Antigua and Barbuda",
    "Argentina", "Armenia", "Australia", "Austria", "Azerbaijan", "Bahamas", "Bahrain",
    "Bangladesh", "Barbados", "Belarus", "Belgium", "Belize", "Benin", "Bhutan",
    "Bolivia", "Bosnia and Herzegovina", "Botswana", "Brazil", "Brunei", "Bulgaria",
    "Burkina Faso", "Burundi", "Cabo Verde", "Cambodia", "Cameroon", "Canada",
    "Central African Republic", "Chad", "Chile", "China", "Colombia", "Comoros",
    "Congo", "Costa Rica", "Croatia", "Cuba", "Cyprus", "Czechia", "Czech Republic",
    "Denmark", "Djibouti", "Dominica", "Dominican Republic", "Ecuador", "Egypt",
    "El Salvador", "Equatorial Guinea", "Eritrea", "Estonia", "Eswatini", "Ethiopia",
    "Fiji", "Finland", "France", "Gabon", "Gambia", "Georgia", "Germany", "Ghana",
    "Greece", "Grenada", "Guatemala", "Guinea", "Guinea-Bissau", "Guyana", "Haiti",
    "Honduras", "Hungary", "Iceland", "India", "Indonesia", "Iran", "Iraq", "Ireland",
    "Israel", "Italy", "Ivory Coast", "Jamaica", "Japan", "Jordan", "Kazakhstan",
    "Kenya", "Kiribati", "Kosovo", "Kuwait", "Kyrgyzstan", "Laos", "Latvia", "Lebanon",
    "Lesotho", "Liberia", "Libya", "Liechtenstein", "Lithuania", "Luxembourg",
    "Madagascar", "Malawi", "Malaysia", "Maldives", "Mali", "Malta",
    "Marshall Islands", "Mauritania", "Mauritius", "Mexico", "Micronesia", "Moldova",
    "Monaco", "Mongolia", "Montenegro", "Morocco", "Mozambique", "Myanmar", "Namibia",
    "Nauru", "Nepal", "Netherlands", "New Zealand", "Nicaragua", "Niger", "Nigeria",
    "North Korea", "North Macedonia", "Norway", "Oman", "Pakistan", "Palau",
    "Palestine", "Panama", "Papua New Guinea", "Paraguay", "Peru", "Philippines",
    "Poland", "Portugal", "Qatar", "Romania", "Russia", "Rwanda",
    "Saint Kitts and Nevis", "Saint Lucia", "Saint Vincent and the Grenadines",
    "Samoa", "San Marino", "Sao Tome and Principe", "Saudi Arabia", "Senegal",
    "Serbia", "Seychelles", "Sierra Leone", "Singapore", "Slovakia", "Slovenia",
    "Solomon Islands", "Somalia", "South Africa", "South Korea", "South Sudan",
    "Spain", "Sri Lanka", "Sudan", "Suriname", "Sweden", "Switzerland", "Syria",
    "Tajikistan", "Tanzania", "Thailand", "Timor-Leste", "Togo", "Tonga",
    "Trinidad and Tobago", "Tunisia", "Turkey", "Turkmenistan", "Tuvalu", "Uganda",
    "Ukraine", "United Arab Emirates", "United Kingdom", "United States", "Uruguay",
    "Uzbekistan", "Vanuatu", "Venezuela", "Vietnam", "Yemen", "Zambia", "Zimbabwe",
    "Democratic Republic of the Congo", "Republic of the Congo",
]


def _normalizar_texto(texto: str) -> str:
    texto = (texto or "").lower()
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


_PAISES_NORMALIZADOS = sorted(
    ((_normalizar_texto(p), p) for p in PAISES_ONU), key=lambda par: len(par[0]), reverse=True
)


def _detectar_pais(texto: str):
    if not texto:
        return None
    texto_norm = _normalizar_texto(texto)
    for pais_norm, pais_original in _PAISES_NORMALIZADOS:
        if re.search(rf"\b{re.escape(pais_norm)}\b", texto_norm):
            return pais_original
    return None


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def _id_o_slug(url: str) -> str:
    coincidencia = PATRON_ID_NOTICE.search(url or "")
    if coincidencia:
        return coincidencia.group(1)
    return _generar_slug(url)


def parsear_fecha_string(cadena_fecha: str):
    if not cadena_fecha:
        return None
    coincidencia = PATRON_FECHA.search(cadena_fecha)
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


def obtener_datos_ficha(url: str) -> dict:
    """
    Visita la ficha propia del aviso y extrae, de sus pares
    <div class="row"><span class="label">Etiqueta:</span>
    <span class="value">Valor</span></div>, los datos fiables -- ver
    aviso de fiabilidad en el docstring del módulo: esta estructura SÍ
    se ha confirmado contra una página de detalle real completa
    (a diferencia de las filas del listado).
    """
    resultado = {
        "fecha_publicacion": None, "fecha_limite": None,
        "pais": None, "descripcion": None, "referencia": None,
    }

    try:
        respuesta = requests.get(url, timeout=TIMEOUT_PETICION, headers=CABECERAS_PETICION)
        respuesta.raise_for_status()
    except Exception as error:
        print(f"      Error descargando la ficha: {error}", flush=True)
        return resultado

    soup = BeautifulSoup(respuesta.text, "html.parser")

    datos_html = {}
    for fila in soup.select(".row"):
        label = fila.select_one(".label")
        value = fila.select_one(".value")
        if label and value:
            clave = label.get_text(strip=True).rstrip(":")
            datos_html[clave] = value.get_text(strip=True)

    if datos_html.get("Published on"):
        resultado["fecha_publicacion"] = parsear_fecha_string(datos_html["Published on"])
    if datos_html.get("Deadline on"):
        resultado["fecha_limite"] = parsear_fecha_string(datos_html["Deadline on"])
    if datos_html.get("Reference"):
        resultado["referencia"] = datos_html["Reference"]

    pais_raw = datos_html.get("Beneficiary countries or territories")
    if pais_raw:
        resultado["pais"] = _detectar_pais(pais_raw)

    # Panel "Description": un <div class="ungm-list-item ..."> cuyo
    # <div class="title"> dice literalmente "Description", seguido de
    # un <div> hermano con el texto real del aviso.
    for panel in soup.select(".ungm-list-item"):
        titulo_panel = panel.select_one(".title")
        if titulo_panel and titulo_panel.get_text(strip=True).lower() == "description":
            hermano = titulo_panel.find_next_sibling("div")
            if hermano:
                resultado["descripcion"] = hermano.get_text(" ", strip=True)
            break

    if resultado["pais"] is None:
        resultado["pais"] = _detectar_pais(soup.get_text(" ", strip=True))

    return resultado


def extraer_licitaciones_playwright() -> list:
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

                print(f"--> Cargando portal de adquisiciones UNGM: {LISTADO_URL}...", flush=True)
                pagina.goto(LISTADO_URL, timeout=TIEMPO_ESPERA_CARGA_MS, wait_until="domcontentloaded")
                pagina.wait_for_selector("#tblNotices", timeout=TIEMPO_ESPERA_CARGA_MS)
                time.sleep(2)

                for indice in range(1, MAX_SCROLLS + 1):
                    filas = pagina.evaluate(_JS_EXTRAER_FILAS)

                    for item in filas:
                        href = item.get("href")
                        url_completa = urljoin(BASE_URL, href) if href else None
                        if not url_completa or url_completa in registros_por_url:
                            continue

                        registros_por_url[url_completa] = {
                            "titulo": item.get("titulo"),
                            "url_oficial": url_completa,
                        }

                    print(
                        f"    Scroll {indice}/{MAX_SCROLLS} -> avisos acumulados: {len(registros_por_url)}",
                        flush=True,
                    )

                    pagina.evaluate("window.scrollBy(0, 1800);")
                    time.sleep(PAUSA_ENTRE_SCROLLS_SEGUNDOS)

                    boton_cargar = pagina.locator(
                        "button:has-text('Show more'), a:has-text('Show more'), #btnMoreNotices"
                    )
                    if boton_cargar.count() > 0 and boton_cargar.first.is_visible():
                        boton_cargar.first.click()
                        time.sleep(2)

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


def construir_registro(item: dict, datos_ficha: dict) -> dict:
    titulo = item.get("titulo") or "Aviso de UNGM sin título reconocido"

    descripcion = datos_ficha.get("descripcion")
    if not descripcion and datos_ficha.get("referencia"):
        descripcion = f"Ref: {datos_ficha['referencia']}."

    return {
        "codigo_unico": f"UNGM-{_id_o_slug(item['url_oficial'])}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": None,
        "titulo": titulo,
        "descripcion": descripcion,
        "pais": datos_ficha.get("pais"),
        "paises": [datos_ficha["pais"]] if datos_ficha.get("pais") else [],
        "organismo": "UNGM",
        "categoria": None,
        "url_oficial": item["url_oficial"],
        "url_documento": None,
        "fecha_publicacion": datos_ficha["fecha_publicacion"].isoformat() if datos_ficha.get("fecha_publicacion") else None,
        "fecha_limite": datos_ficha["fecha_limite"].isoformat() if datos_ficha.get("fecha_limite") else None,
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
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - UNGM", flush=True)
    print("=" * 100, flush=True)
    print(f"Ventana de publicación objetivo: {desde} .. {hoy}", flush=True)

    crudos = extraer_licitaciones_playwright()
    print(f"\nTotal avisos rastreados: {len(crudos)}", flush=True)

    if not crudos:
        print(
            "No se ha extraído ningún aviso. Revisa el log de arriba y, si existe, "
            f"{CAPTURA_DEPURACION}.",
            flush=True,
        )
        return

    if len(crudos) > MAX_FICHAS_A_CONSULTAR:
        print(
            f"Aviso: se han descubierto {len(crudos)} avisos, por encima del tope de seguridad "
            f"({MAX_FICHAS_A_CONSULTAR}) -- se consultará la ficha solo de los primeros "
            f"{MAX_FICHAS_A_CONSULTAR}.",
            flush=True,
        )
        crudos = crudos[:MAX_FICHAS_A_CONSULTAR]

    print("\nConsultando la ficha de cada aviso para sacar fechas, país y descripción...", flush=True)
    normalizados = []
    for indice, item in enumerate(crudos, start=1):
        print(f"  [{indice}/{len(crudos)}] {(item.get('titulo') or '')[:90]}", flush=True)
        datos_ficha = obtener_datos_ficha(item["url_oficial"])
        normalizados.append(construir_registro(item, datos_ficha))
        time.sleep(PAUSA_ENTRE_FICHAS_SEGUNDOS)

    con_pais = sum(1 for n in normalizados if n.get("pais"))
    print(
        f"Avisos con país reconocido: {con_pais}/{len(normalizados)}",
        flush=True,
    )

    en_ventana = [
        n for n in normalizados
        if not n.get("fecha_publicacion") or date.fromisoformat(n["fecha_publicacion"]) >= desde
    ]
    print(f"Dentro de la ventana de {DIAS_ATRAS} días (por fecha de publicación reconocida o desconocida): {len(en_ventana)}", flush=True)

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
    print(f"\nSincronizacion UNGM completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

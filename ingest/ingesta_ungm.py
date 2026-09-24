# -*- coding: utf-8 -*-
"""
ingesta_ungm.py
----------------
Sincroniza avisos de adquisiciones de UNGM (United Nations Global
Marketplace) contra la tabla `licitaciones_internacionales` de Supabase,
leyendo la tabla real del portal:

    https://www.ungm.org/Public/Notice

usando Playwright (navegador real, headless, gratuito) -- necesario porque
la tabla se renderiza con JavaScript y se carga progresivamente con scroll
("Show more").

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local:     python ingesta_ungm.py
Ejecución programada: ver .github/workflows/sincronizar_ungm.yml
"""
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

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
CAMPOS_COMPARABLES = ("titulo", "pais", "fecha_publicacion", "fecha_limite")

TIEMPO_ESPERA_CARGA_MS = 60000
MAX_SCROLLS = 150  # Realizará los 150 scrolls completos
PAUSA_ENTRE_SCROLLS_SEGUNDOS = 2.0
CAPTURA_DEPURACION = "debug_ungm_tabla.png"

CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_JS_EXTRAER_FILAS = """
() => {
    const resultados = [];
    const filas = document.querySelectorAll('#tblNotices tr, #tblNotices .tableRow');

    filas.forEach(fila => {
        const enlace = fila.querySelector('a[href*="/Public/Notice/"]');
        if (!enlace) return;

        let titulo = (enlace.innerText || '').trim();
        if (!titulo) {
            titulo = (enlace.getAttribute('title') || enlace.getAttribute('aria-label') || '').trim();
        }
        if (!titulo) {
            const celdaEnlace = enlace.closest('td, div.tableCell');
            if (celdaEnlace) {
                titulo = (celdaEnlace.innerText || '').trim();
            }
        }
        if (!titulo) {
            const primeraCelda = fila.querySelector('td, div.tableCell');
            if (primeraCelda) {
                titulo = (primeraCelda.innerText || '').trim();
            }
        }

        const href = enlace.getAttribute('href');
        const celdas = Array.from(fila.querySelectorAll('td, div.tableCell')).map(c => c.innerText.trim());

        resultados.push({
            titulo: titulo,
            href: href,
            celdas: celdas,
            texto_completo: (fila.innerText || '').replace(/\\s+/g, ' ')
        });
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


def evaluar_licitacion(item: dict):
    texto = item.get("texto_completo", "")
    coincidencias = PATRON_FECHA.findall(texto)

    fechas = []
    for dia, mes_texto, anio in coincidencias:
        cadena = f"{int(dia):02d}-{mes_texto.capitalize()}-{anio}"
        fecha = parsear_fecha_string(cadena)
        if fecha:
            fechas.append((cadena, fecha))

    if not fechas:
        return None, None

    deadline_str, _ = fechas[0]
    if len(fechas) >= 2:
        pub_str, _ = fechas[1]
    else:
        pub_str, _ = fechas[0]

    return pub_str, deadline_str


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
                            "texto_completo": item.get("texto_completo"),
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


def _mejor_titulo_disponible(item: dict) -> str:
    titulo = (item.get("titulo") or "").strip()
    if titulo:
        return titulo

    candidatas = [c.strip() for c in (item.get("celdas") or []) if c and c.strip()]
    candidatas = [c for c in candidatas if len(c) > 15 and not PATRON_FECHA.fullmatch(c)]
    if candidatas:
        return max(candidatas, key=len)

    return "Aviso de UNGM sin título reconocido (revisar extracción)"


def construir_registro(item: dict) -> dict:
    fecha_publicacion_str, fecha_limite_str = evaluar_licitacion(item)
    fecha_publicacion = parsear_fecha_string(fecha_publicacion_str) if fecha_publicacion_str else None
    fecha_limite = parsear_fecha_string(fecha_limite_str) if fecha_limite_str else None

    pais = _detectar_pais(item.get("texto_completo"))
    titulo = _mejor_titulo_disponible(item)

    partes_descripcion = []
    if fecha_limite_str:
        partes_descripcion.append(f"Plazo: {fecha_limite_str}.")
    texto_extra = (item.get("texto_completo") or "").replace(titulo, "", 1).strip()
    if texto_extra:
        partes_descripcion.append(texto_extra[:300])
    descripcion = " ".join(partes_descripcion) or None

    return {
        "codigo_unico": f"UNGM-{_id_o_slug(item['url_oficial'])}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": None,
        "titulo": titulo,
        "descripcion": descripcion,
        "pais": pais,
        "paises": [pais] if pais else [],
        "organismo": "UNGM",
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
            f"{CAPTURA_DEPURACION} -- lo más probable es que la estructura real de la tabla "
            "haya cambiado respecto a '#tblNotices'.",
            flush=True,
        )
        return

    normalizados = [construir_registro(item) for item in crudos]

    con_pais = sum(1 for n in normalizados if n.get("pais"))
    print(
        f"Avisos con país reconocido: {con_pais}/{len(normalizados)}",
        flush=True,
    )
    sin_titulo = sum(1 for n in normalizados if "sin título reconocido" in (n.get("titulo") or ""))
    if sin_titulo:
        print(
            f"Aviso: {sin_titulo}/{len(normalizados)} avisos se han quedado sin título tras todos "
            "los respaldos.",
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

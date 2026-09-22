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

Adaptado de un script de prueba que se ha ejecutado con éxito contra la
página real fuera de este entorno de desarrollo. Único cambio respecto al
script de prueba (aparte de quitar el andamiaje propio de Colab): se usa
`playwright.sync_api`, igual que el resto de scrapers de este proyecto que
necesitan un navegador.

DETECCIÓN DE FECHAS -- HEURÍSTICA POSICIONAL, TAL CUAL SE VALIDÓ
--------------------------------------------------------------------------
Cada fila no separa sus fechas por etiqueta (a diferencia de BID/UNDP):
`evaluar_licitacion()` extrae TODAS las fechas del texto completo de la
fila por regex, y asume la 1ª = fecha límite (Deadline) y la 2ª = fecha
de publicación (Published) -- ese orden es el que confirmó el script de
prueba contra la página real. Solo se suben avisos cuya fecha de
publicación caiga en los últimos DIAS_ATRAS días.

AVISO DE FIABILIDAD -- PAÍS/UBICACIÓN NO VALIDADO
--------------------------------------------------------------------------
El script de prueba que se ha ejecutado con éxito NO extraía país (su
propia lista de columnas de salida no lo incluía). Una búsqueda aparte
confirma que el buscador de UNGM sí expone un facet "Beneficiary country
or territory" en la página, pero no se ha podido confirmar en qué celda
exacta de cada fila aparece ese valor (no hay salida de red hacia
ungm.org en este entorno de desarrollo). Para no adivinar una posición
de celda a ciegas -- con el riesgo de capturar silenciosamente el campo
equivocado (p. ej. la referencia o el organismo en vez del país) -- este
script usa el mismo mecanismo YA VALIDADO en ingesta_caf.py: comprobar si
el texto completo de la fila contiene el nombre de algún país conocido
(ver PAISES_ONU). Es deliberadamente conservador: si el país no aparece
tal cual en el texto de la fila (p. ej. "Multiple destinations", como
usa UNGM para avisos multipaís), `pais` queda en None en vez de forzar
un valor probablemente erróneo.

**Revisa el log "Avisos con país reconocido" tras la primera ejecución
manual (workflow_dispatch); si sale muy bajo, es señal de que el país sí
vive en una celda de la tabla y conviene inspeccionar el HTML real para
extraerlo de forma más precisa en vez de por coincidencia de texto.**

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_ungm.py
Ejecucion programada: ver .github/workflows/sincronizar_ungm.yml
   (necesita el paso extra "playwright install --with-deps chromium")
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
DIAS_ATRAS = 2   # el script de prueba validado exigía fecha de publicacion en {hoy, ayer}
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "pais", "fecha_publicacion", "fecha_limite")

TIEMPO_ESPERA_CARGA_MS = 60000
MAX_SCROLLS = 50
PAUSA_ENTRE_SCROLLS_SEGUNDOS = 2.0
CAPTURA_DEPURACION = "debug_ungm_tabla.png"

CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# JS de extracción tal cual se validó: cada fila de '#tblNotices' con un
# enlace a '/Public/Notice/' es un aviso. Se guardan tanto las celdas
# estructuradas como el texto completo de la fila (las fechas y -- ver
# aviso de fiabilidad -- el país se detectan después, en Python, por
# regex/lista de nombres, no por posición de celda).
_JS_EXTRAER_FILAS = """
() => {
    const resultados = [];
    const filas = document.querySelectorAll('#tblNotices tr, #tblNotices .tableRow');

    filas.forEach(fila => {
        const enlace = fila.querySelector('a[href*="/Public/Notice/"]');
        if (!enlace) return;

        const titulo = (enlace.innerText || '').trim();
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

# Lista de países best-effort para la detección de "pais" -- ver aviso de
# fiabilidad en el docstring del módulo. Nombres en inglés (idioma del
# portal). No pretende ser exhaustiva letra por letra (territorios,
# variantes ortográficas) pero cubre los ~195 estados miembro/observadores
# de la ONU con su forma corta habitual en inglés.
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
    """Best-effort: primer país conocido que aparezca literalmente en el
    texto -- ver aviso de fiabilidad en el docstring del módulo."""
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
    """El ID numerico de '/Public/Notice/223083' si se reconoce (mucho
    mas limpio que trocear toda la URL); si no, la URL entera troceada
    como respaldo."""
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
    """
    Analiza el texto completo de la fila: la 1ª fecha reconocida es el
    Deadline, la 2ª es la fecha de publicación (Published) -- orden
    confirmado en el script de prueba validado contra la página real.
    Devuelve (fecha_publicacion_str, deadline_str) o (None, None) si no
    hay ninguna fecha reconocible.
    """
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
    """
    Abre el portal y va haciendo scroll / pulsando "Show more" hasta
    MAX_SCROLLS veces, devolviendo TODOS los avisos vistos, sin filtrar
    todavía por fecha -- ver ejecutar_sincronizacion().
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


def construir_registro(item: dict) -> dict:
    fecha_publicacion_str, fecha_limite_str = evaluar_licitacion(item)
    fecha_publicacion = parsear_fecha_string(fecha_publicacion_str) if fecha_publicacion_str else None
    fecha_limite = parsear_fecha_string(fecha_limite_str) if fecha_limite_str else None

    pais = _detectar_pais(item.get("texto_completo"))

    partes_descripcion = []
    if fecha_limite_str:
        partes_descripcion.append(f"Plazo: {fecha_limite_str}.")
    descripcion = " ".join(partes_descripcion) or None

    return {
        "codigo_unico": f"UNGM-{_id_o_slug(item['url_oficial'])}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": None,
        "titulo": item.get("titulo"),
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
    print(f"Ventana de publicación: {desde} .. {hoy}", flush=True)

    crudos = extraer_licitaciones_playwright()
    print(f"\nTotal avisos rastreados (todos los scrolls, sin filtrar por fecha): {len(crudos)}", flush=True)

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
        f"Avisos con país reconocido (best-effort, ver aviso de fiabilidad en el docstring): "
        f"{con_pais}/{len(normalizados)}",
        flush=True,
    )

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
    print(f"\nSincronizacion UNGM completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

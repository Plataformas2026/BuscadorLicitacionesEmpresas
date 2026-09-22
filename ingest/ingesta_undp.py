# -*- coding: utf-8 -*-
"""
ingesta_undp.py
----------------
Sincroniza avisos de adquisiciones del PNUD/UNDP contra la tabla
`licitaciones_internacionales` de Supabase, leyendo la tabla real del
portal:

    https://procurement-notices.undp.org/

usando Playwright (navegador real, headless, gratuito) -- necesario porque
la tabla se renderiza con JavaScript.

Adaptado de un script de prueba que se ha ejecutado con éxito contra la
página real fuera de este entorno de desarrollo (el mismo patrón que
ingesta_bid.py: cada aviso vive en un `a.vacanciesTable__row`, y sus
campos se leen por pares etiqueta/valor dentro de cada
`.vacanciesTable__cell` -- `.vacanciesTable__cell__label` + `span`). Único
cambio respecto al script de prueba: se usa `playwright.sync_api` en vez
de `async_playwright`/`nest_asyncio` (andamiaje propio de Colab), igual
que el resto de scrapers de este proyecto que necesitan un navegador --
así `ejecutar_sincronizacion()` sigue siendo una función síncrona normal,
sin ningún event loop de por medio, invocable con `python ingesta_undp.py`
tal cual se ejecuta en producción.

TODA la información necesaria (título, referencia, país/oficina, tipo de
proceso, fechas) llega ya en la misma pasada del listado -- no hace falta
leer una ficha aparte por aviso.

VENTANA DE FECHAS
------------------
Igual que en ingesta_bid.py: solo se suben avisos cuya fecha "Posted"
caiga dentro de los últimos DIAS_ATRAS días (el script de prueba validado
exigía fecha en {hoy, ayer}). No usa tabla auxiliar por el mismo motivo
que BID: no hay ficha cara que evitar releer.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_undp.py
Ejecucion programada: ver .github/workflows/sincronizar_undp.yml
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

BASE_URL = "https://procurement-notices.undp.org/"
FUENTE = "UNDP"
DIAS_ATRAS = 2   # el script de prueba validado exigía fecha 'Posted' en {hoy, ayer}
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_publicacion", "fecha_limite")

TIEMPO_ESPERA_CARGA_MS = 60000
MAX_PAGINAS = 10
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 2
CAPTURA_DEPURACION = "debug_undp_tabla.png"

CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# JS de extracción tal cual se validó contra la página real: cada aviso es
# un 'a.vacanciesTable__row', y cada campo vive en un
# '.vacanciesTable__cell' con su propia etiqueta
# ('.vacanciesTable__cell__label') y su valor ('span').
_JS_EXTRAER_FILAS_UNDP = """
() => {
    const resultados = [];
    const filas = document.querySelectorAll('a.vacanciesTable__row');

    filas.forEach(fila => {
        const href = fila.getAttribute('href');
        let titulo = '';
        let refNo = '';
        let pais = '';
        let proceso = '';
        let deadline = '';
        let posted = '';

        const celdas = fila.querySelectorAll('.vacanciesTable__cell');
        celdas.forEach(celda => {
            const labelEl = celda.querySelector('.vacanciesTable__cell__label');
            const spanEl = celda.querySelector('span');

            if (labelEl && spanEl) {
                const labelText = labelEl.innerText.trim().toLowerCase();
                const valorText = spanEl.innerText.trim();

                if (labelText.includes('title')) {
                    titulo = valorText;
                } else if (labelText.includes('ref no')) {
                    refNo = valorText;
                } else if (labelText.includes('office') || labelText.includes('country')) {
                    pais = valorText;
                } else if (labelText.includes('process')) {
                    proceso = valorText;
                } else if (labelText.includes('deadline')) {
                    deadline = valorText;
                } else if (labelText.includes('posted')) {
                    posted = valorText;
                }
            }
        });

        resultados.push({
            ref_no: refNo,
            titulo: titulo,
            pais: pais,
            proceso: proceso,
            href: href,
            posted_raw: posted,
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


def parsear_fecha_undp(cadena_fecha: str):
    """Convierte fechas tipo '22-Sep-26' o '22-Sep-2026' a date (año de 2 dígitos -> 20XX)."""
    if not cadena_fecha:
        return None

    coincidencia = re.search(r"(\d{1,2})[-/\s]([A-Za-z]{3,9})[-/\s](\d{2,4})", cadena_fecha.strip())
    if not coincidencia:
        return None

    dia, mes_texto, anio = coincidencia.groups()
    if len(anio) == 2:
        anio = f"20{anio}"

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

                print(f"--> Cargando portal de adquisiciones UNDP: {BASE_URL}...", flush=True)
                pagina.goto(BASE_URL, timeout=TIEMPO_ESPERA_CARGA_MS, wait_until="domcontentloaded")
                pagina.wait_for_selector("a.vacanciesTable__row", timeout=TIEMPO_ESPERA_CARGA_MS)

                for indice_pagina in range(1, MAX_PAGINAS + 1):
                    filas = pagina.evaluate(_JS_EXTRAER_FILAS_UNDP)

                    for item in filas:
                        href = item.get("href")
                        url_completa = urljoin(BASE_URL, href) if href else None
                        if not url_completa or url_completa in registros_por_url:
                            continue
                        registros_por_url[url_completa] = {
                            "ref_no": item.get("ref_no"),
                            "titulo": item.get("titulo"),
                            "pais": item.get("pais"),
                            "proceso": item.get("proceso"),
                            "posted_raw": item.get("posted_raw"),
                            "deadline_raw": item.get("deadline_raw"),
                            "url_oficial": url_completa,
                        }

                    print(
                        f"    Página {indice_pagina}/{MAX_PAGINAS} -> avisos acumulados: {len(registros_por_url)}",
                        flush=True,
                    )

                    boton_siguiente = pagina.locator(".pagination .next:not(.disabled) a, a.next, button.next-page")
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


def construir_registro(item: dict) -> dict:
    fecha_publicacion = parsear_fecha_undp(item.get("posted_raw"))
    fecha_limite = parsear_fecha_undp(item.get("deadline_raw"))

    pais = (item.get("pais") or "").strip() or None
    proceso = (item.get("proceso") or "").strip() or None

    partes_descripcion = []
    if proceso:
        partes_descripcion.append(f"Tipo de proceso: {proceso}.")
    if item.get("deadline_raw"):
        partes_descripcion.append(f"Plazo: {item['deadline_raw']}.")
    descripcion = " ".join(partes_descripcion) or None

    ref_no = (item.get("ref_no") or "").strip()
    slug_base = ref_no or _generar_slug(item.get("titulo") or item["url_oficial"])

    return {
        "codigo_unico": f"UNDP-{_generar_slug(slug_base)}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": proceso,
        "titulo": item.get("titulo"),
        "descripcion": descripcion,
        "pais": pais,
        "paises": [pais] if pais else [],
        "organismo": "UNDP",
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
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - UNDP", flush=True)
    print("=" * 100, flush=True)
    print(f"Ventana de publicación: {desde} .. {hoy}", flush=True)

    crudos = extraer_licitaciones_playwright()
    print(f"\nTotal avisos rastreados (todas las páginas, sin filtrar por fecha): {len(crudos)}", flush=True)

    if not crudos:
        print(
            "No se ha extraído ningún aviso. Revisa el log de arriba y, si existe, "
            f"{CAPTURA_DEPURACION} -- lo más probable es que la estructura real de la tabla "
            "haya cambiado respecto a 'a.vacanciesTable__row'.",
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
    print(f"\nSincronizacion UNDP completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

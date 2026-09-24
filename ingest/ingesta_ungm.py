import time
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

# Configuración / Constantes
BASE_URL = "https://www.ungm.org"
LISTADO_URL = "https://www.ungm.org/Public/Notice"
CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/115.0.0.0 Safari/537.36"
)
TIEMPO_ESPERA_CARGA_MS = 30000
MAX_SCROLLS = 50
PAUSA_ENTRE_SCROLLS_SEGUNDOS = 2.5
CAPTURA_DEPURACION = "depuracion_error.png"

# Script JS para extraer las filas del DOM
_JS_EXTRAER_FILAS = """
() => {
    const filas = Array.from(document.querySelectorAll('#tblNotices tbody tr'));
    return filas.map(tr => {
        const enlace = tr.querySelector('a');
        const celdas = Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim());
        return {
            href: enlace ? enlace.getAttribute('href') : null,
            titulo: enlace ? enlace.innerText.trim() : '',
            texto_completo: tr.innerText.trim(),
            fecha_pub_texto: celdas.length > 0 ? celdas[celdas.length - 1] : '',
            celdas: celdas
        };
    });
}
"""


def asegurar_orden_publicacion_descendente(pagina):
    """Intenta ordenar por fecha si la interfaz lo requiere."""
    try:
        columna_fecha = pagina.locator("th:has-text('Date'), th:has-text('Published')")
        if columna_fecha.count() > 0:
            columna_fecha.first.click()
            time.sleep(1)
    except Exception:
        pass


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

                print(
                    f"--> Cargando portal de adquisiciones UNGM: {LISTADO_URL}...",
                    flush=True,
                )
                pagina.goto(
                    LISTADO_URL,
                    timeout=TIEMPO_ESPERA_CARGA_MS,
                    wait_until="domcontentloaded",
                )
                pagina.wait_for_selector(
                    "#tblNotices", timeout=TIEMPO_ESPERA_CARGA_MS
                )
                time.sleep(2)

                asegurar_orden_publicacion_descendente(pagina)

                sin_nuevos_registros = 0

                for indice in range(1, MAX_SCROLLS + 1):
                    conteo_anterior = len(registros_por_url)
                    filas = pagina.evaluate(_JS_EXTRAER_FILAS)

                    for item in filas:
                        href = item.get("href")
                        url_completa = urljoin(BASE_URL, href) if href else None
                        if not url_completa or url_completa in registros_por_url:
                            continue

                        registros_por_url[url_completa] = {
                            "titulo": item.get("titulo"),
                            "texto_completo": item.get("texto_completo"),
                            "fecha_pub_texto": item.get("fecha_pub_texto"),
                            "celdas": item.get("celdas"),
                            "url_oficial": url_completa,
                        }

                    total_actual = len(registros_por_url)
                    print(
                        f"    Iteración {indice}/{MAX_SCROLLS} -> avisos acumulados: {total_actual}",
                        flush=True,
                    )

                    # Si el número total de registros no cambia, sumamos al contador de control
                    if total_actual == conteo_anterior:
                        sin_nuevos_registros += 1
                    else:
                        sin_nuevos_registros = 0

                    # Si tras 3 iteraciones seguidas no hay registros nuevos, termina el raspado
                    if sin_nuevos_registros >= 3:
                        print(
                            "--> Fin del listado: no se detectaron nuevos avisos tras 3 intentos seguidos.",
                            flush=True,
                        )
                        break

                    # 1. Scroll al final para activar la carga dinámica (Infinite Scroll)
                    pagina.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(PAUSA_ENTRE_SCROLLS_SEGUNDOS)

                    # 2. Intento de clic en caso de que exista un botón de apoyo "Show More"
                    boton_cargar = pagina.locator(
                        "button:has-text('Show more'), a:has-text('Show more'), #btnMoreNotices"
                    )
                    if boton_cargar.count() > 0:
                        try:
                            if boton_cargar.first.is_visible():
                                boton_cargar.first.click(timeout=1000)
                                time.sleep(2)
                        except Exception:
                            pass

            except Exception as error:
                print(f"Error durante la navegación con Playwright: {error}", flush=True)
                try:
                    if "pagina" in locals():
                        pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)
                        print(
                            f"Captura de depuración guardada en {CAPTURA_DEPURACION}.",
                            flush=True,
                        )
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


if __name__ == "__main__":
    licitaciones = extraer_licitaciones_playwright()
    print(f"\nProceso finalizado. Total de avisos extraídos: {len(licitaciones)}")

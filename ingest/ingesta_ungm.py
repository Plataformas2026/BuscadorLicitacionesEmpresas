import time
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

# Configuración / Constantes
BASE_URL = "https://www.ungm.org"
LISTADO_URL = "https://www.ungm.org/Public/Notice"
CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
TIEMPO_ESPERA_CARGA_MS = 30000
MAX_SCROLLS = 50
PAUSA_ENTRE_SCROLLS_SEGUNDOS = 3.0
CAPTURA_DEPURACION = "depuracion_error.png"

# Script JS más amplio para probar múltiples selectores comunes en UNGM
_JS_EXTRAER_FILAS = """
() => {
    // Probar selectores típicos de UNGM (Tablas de datos o Listados de avisos)
    let filas = Array.from(document.querySelectorAll('#tblNotices tbody tr, .tblNotices tbody tr, div.table-row, div.dataRow'));
    
    // Si no encuentra filas por tabla, busca enlaces que contengan '/Public/Notice/'
    if (filas.length === 0) {
        const enlaces = Array.from(document.querySelectorAll("a[href*='/Public/Notice/']"));
        return enlaces.map(a => ({
            href: a.getAttribute('href'),
            titulo: a.innerText.trim(),
            texto_completo: a.closest('tr, div')?.innerText.trim() || a.innerText.trim(),
            fecha_pub_texto: '',
            celdas: []
        }));
    }

    return filas.map(tr => {
        const enlace = tr.querySelector("a[href*='/Public/Notice/']") || tr.querySelector('a');
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


def extraer_licitaciones_playwright() -> list:
    registros_por_url = {}

    try:
        with sync_playwright() as p:
            navegador = None
            try:
                # Se desactiva la bandera de automatización para evitar detecciones simples
                navegador = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                contexto = navegador.new_context(
                    user_agent=CABECERAS_USER_AGENT,
                    viewport={"width": 1440, "height": 900},
                )
                pagina = contexto.new_page()

                print(f"--> Cargando portal de adquisiciones UNGM: {LISTADO_URL}...", flush=True)
                pagina.goto(
                    LISTADO_URL,
                    timeout=TIEMPO_ESPERA_CARGA_MS,
                    wait_until="networkidle",  # Esperar a que la red esté inactiva (AJAX completo)
                )

                # Intentar esperar explícitamente a que aparezca al menos un enlace de aviso
                try:
                    pagina.wait_for_selector("a[href*='/Public/Notice/']", timeout=10000)
                except Exception:
                    print("⚠️ No se encontró 'a[href*=/Public/Notice/]' tras 10s. Guardando captura...", flush=True)
                    pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)

                time.sleep(2)

                sin_nuevos_registros = 0

                for indice in range(1, MAX_SCROLLS + 1):
                    conteo_anterior = len(registros_por_url)
                    filas = pagina.evaluate(_JS_EXTRAER_FILAS)

                    for item in filas:
                        href = item.get("href")
                        if not href or href == "#" or "javascript:" in href:
                            continue
                        
                        url_completa = urljoin(BASE_URL, href)
                        if url_completa in registros_por_url:
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

                    if total_actual == conteo_anterior:
                        sin_nuevos_registros += 1
                    else:
                        sin_nuevos_registros = 0

                    if sin_nuevos_registros >= 3:
                        print(
                            "--> Fin del listado: no se detectaron nuevos avisos tras 3 intentos seguidos.",
                            flush=True,
                        )
                        break

                    # Scroll progresivo hacia el fondo de la página
                    pagina.evaluate("window.scrollBy(0, 1500);")
                    time.sleep(PAUSA_ENTRE_SCROLLS_SEGUNDOS)

            except Exception as error:
                print(f"Error durante la navegación con Playwright: {error}", flush=True)
                if "pagina" in locals():
                    pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)
            finally:
                if navegador is not None:
                    navegador.close()
    except Exception as error:
        print(f"Error inesperado: {error}", flush=True)

    return list(registros_por_url.values())


if __name__ == "__main__":
    licitaciones = extraer_licitaciones_playwright()
    print(f"\nProceso finalizado. Total de avisos extraídos: {len(licitaciones)}")

"""
ingesta_bid_powerbi.py
--------------------------
Sincroniza los avisos de licitación del Banco Interamericano de Desarrollo (BID / IADB)
extraídos mediante Playwright/PowerBI contra la tabla `licitaciones_internacionales` de Supabase.
"""

import asyncio
import os
import re
import sys
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# --- Rutas de importación: funciona si common.py está en /ingest o en /app ---
BASE_DIR = Path(__file__).resolve().parent
for _ruta in (BASE_DIR, BASE_DIR.parent / "app"):
    if str(_ruta) not in sys.path:
        sys.path.append(str(_ruta))

from common import (  # noqa: E402
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

URL_IADB = "https://www.iadb.org/es/como-trabajar-juntos/adquisiciones/adquisiciones-para-proyectos/avisos-de-adquisiciones"
FUENTE = "BID"
LOTE_ENVIO_SUPABASE = 15

DEBUG_DIR = BASE_DIR / "debug"
CAPTURA_DEPURACION = DEBUG_DIR / "powerbi_tabla_extraida.png"

# HEADLESS=false solo si quieres depurar con Xvfb (xvfb-run). Por defecto, igual que en Colab.
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

SELECTOR_IFRAME = (
    'iframe[src*="powerbi"], iframe[data-src*="powerbi"], iframe[title*="power bi" i]'
)

CAMPOS_COMPARABLES = ("titulo", "fecha_limite", "pais", "organismo", "descripcion")


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def parsear_fecha(texto: str):
    if not texto:
        return None

    coincidencia_iso = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", texto)
    if coincidencia_iso:
        anio, mes, dia = coincidencia_iso.groups()
        try:
            return date(int(anio), int(mes), int(dia))
        except ValueError:
            pass

    coincidencia_lat = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", texto)
    if coincidencia_lat:
        dia, mes, anio = coincidencia_lat.groups()
        try:
            return date(int(anio), int(mes), int(dia))
        except ValueError:
            pass

    return None


# ==============================================================================
# DIAGNÓSTICO
# ==============================================================================
async def volcar_diagnostico(page, etiqueta: str, respuesta_status=None):
    """Guarda captura, HTML y lista de frames/iframes para depurar fallos en CI."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{etiqueta}.png"), full_page=True)
    except Exception as e:
        print(f"    (no se pudo guardar captura: {e})", flush=True)
    try:
        html = await page.content()
        (DEBUG_DIR / f"{etiqueta}.html").write_text(html, encoding="utf-8")
    except Exception as e:
        print(f"    (no se pudo guardar HTML: {e})", flush=True)

    lineas = [f"HTTP status de la navegación: {respuesta_status}", f"URL actual: {page.url}", "", "FRAMES:"]
    lineas += [f"  - {f.url}" for f in page.frames]
    try:
        iframes = await page.eval_on_selector_all(
            "iframe",
            "els => els.map(e => ({src: e.src, dataSrc: e.getAttribute('data-src'), title: e.title}))",
        )
        lineas += ["", "ELEMENTOS <iframe>:"] + [f"  - {i}" for i in iframes]
    except Exception:
        pass
    (DEBUG_DIR / f"{etiqueta}_frames.txt").write_text("\n".join(lineas), encoding="utf-8")
    print("\n".join(lineas), flush=True)


async def localizar_frame_powerbi(page, intentos: int = 30):
    """Busca el frame de PowerBI forzando la carga diferida del iframe."""
    iframes = page.locator(SELECTOR_IFRAME)
    for intento in range(1, intentos + 1):
        for frame in page.frames:
            if "powerbi.com" in frame.url and frame.url != "about:blank":
                return frame

        try:
            if await iframes.count() > 0:
                await iframes.first.scroll_into_view_if_needed(timeout=3000)
            else:
                await page.mouse.wheel(0, 400)
        except Exception:
            pass

        if intento % 5 == 0:
            print(f"    ... iframe de PowerBI aún no disponible (intento {intento}/{intentos})", flush=True)
        await page.wait_for_timeout(2000)
    return None


# ==============================================================================
# EXTRACCIÓN
# ==============================================================================
async def extraer_licitaciones_iadb() -> list:
    licitaciones_raw = []
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
            locale="es-ES",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        try:
            print(f"--> [1/5] Cargando la página del BID: {URL_IADB}...", flush=True)
            respuesta = await page.goto(URL_IADB, wait_until="domcontentloaded", timeout=90000)
            status = respuesta.status if respuesta else None
            print(f"    HTTP status: {status}", flush=True)
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                print("    (networkidle no alcanzado, se continúa)", flush=True)

            # Cierre de cookies
            try:
                btn_cookie = page.locator('#onetrust-accept-btn-handler, button:has-text("Aceptar")').first
                if await btn_cookie.is_visible(timeout=5000):
                    await btn_cookie.click()
                    print("    ✔ Banner de cookies aceptado.", flush=True)
                    await page.wait_for_timeout(2000)
            except Exception:
                pass

            print("--> Descendiendo en la página para activar la carga del reporte...", flush=True)
            await page.evaluate("window.scrollBy(0, 500)")
            await page.wait_for_timeout(3000)

            print("--> Buscando iframe de PowerBI...", flush=True)
            frame_powerbi = await localizar_frame_powerbi(page)
            if frame_powerbi is None:
                await volcar_diagnostico(page, "sin_iframe_powerbi", status)
                raise RuntimeError("No se encontró el iframe de PowerBI (ver carpeta debug/).")
            target_context = frame_powerbi
            print(f"    ✔ Frame activo: {target_context.url}", flush=True)

            print("--> Esperando renderizado de celdas en PowerBI...", flush=True)
            try:
                await target_context.wait_for_selector('.pivotTable, [role="gridcell"]', timeout=90000)
                await page.wait_for_timeout(3000)
                print("    ✔ Celdas localizadas exitosamente.", flush=True)
            except Exception as e:
                await volcar_diagnostico(page, "sin_celdas_powerbi", status)
                raise RuntimeError(f"PowerBI cargó pero no renderizó celdas: {e}")

            try:
                celda = target_context.locator('[role="gridcell"], .pivotTableCellWrap').first
                await celda.click(force=True)
                await page.wait_for_timeout(1000)
                print("    ✔ Foco fijado en la primera celda.", flush=True)
            except Exception as e:
                print(f"⚠️ No se pudo fijar el foco: {e}", flush=True)

            # ------------------------------------------------------------------
            # ESCANEO SECUENCIAL SUAVE (igual que en Colab)
            # ------------------------------------------------------------------
            print("--> Escaneando datos progresivamente (evitando saltos)...", flush=True)

            TOTAL_PASADAS = 80
            pasadas_sin_cambios = 0

            for i in range(1, TOTAL_PASADAS + 1):
                frame_html = await target_context.content()
                soup = BeautifulSoup(frame_html, "html.parser")
                celdas = soup.select('.pivotTableCellWrap, [role="gridcell"], .rowText, .cell-interactive')

                nuevos_elementos = 0
                for c in celdas:
                    texto = c.get_text(strip=True)
                    if texto and texto not in licitaciones_raw:
                        licitaciones_raw.append(texto)
                        nuevos_elementos += 1

                print(
                    f"    Pasada {i}/{TOTAL_PASADAS}: {nuevos_elementos} campos nuevos "
                    f"(Total acumulado: {len(licitaciones_raw)})",
                    flush=True,
                )

                if nuevos_elementos == 0 and len(licitaciones_raw) > 0:
                    pasadas_sin_cambios += 1
                    if pasadas_sin_cambios >= 8:
                        print("\n    ✔ Final del reporte alcanzado correctamente.", flush=True)
                        break
                else:
                    pasadas_sin_cambios = 0

                for _ in range(8):
                    await page.keyboard.press("ArrowDown")

                await target_context.evaluate(
                    """
                    () => {
                        const contenedores = document.querySelectorAll('.scrollWrapper, .viewport, [role="grid"], .pivotTable');
                        contenedores.forEach(c => c.scrollTop += 250);
                    }
                    """
                )
                await page.wait_for_timeout(3000)

            print("\n" + "=" * 80, flush=True)
            print(f"TOTAL DE REGISTROS EXTRAÍDOS SINCRO: {len(licitaciones_raw)}", flush=True)
            print("=" * 80, flush=True)
            for idx, item in enumerate(licitaciones_raw, 1):
                print(f"[{idx}] {item}", flush=True)

            await page.screenshot(path=str(CAPTURA_DEPURACION), full_page=True)

        finally:
            await browser.close()

    return licitaciones_raw


# ==============================================================================
# ESTRUCTURACIÓN Y SUBIDA A SUPABASE (sin cambios)
# ==============================================================================
def agrupar_y_normalizar(elementos_raw: list) -> list:
    normalizados = []

    TAMAÑO_BLOQUE = 5
    bloques = [elementos_raw[i:i + TAMAÑO_BLOQUE] for i in range(0, len(elementos_raw), TAMAÑO_BLOQUE)]

    for bloque in bloques:
        cadena_texto = " - ".join(bloque)
        titulo = bloque[0] if len(bloque) > 0 else "Aviso BID"
        pais = bloque[1] if len(bloque) > 1 else "Internacional"
        organismo = "BID - Banco Interamericano de Desarrollo"

        fecha_pub = None
        fecha_lim = None
        for item in bloque:
            f = parsear_fecha(item)
            if f:
                if not fecha_lim:
                    fecha_lim = f
                else:
                    fecha_pub = f

        slug_base = _generar_slug(f"{pais}-{titulo}")
        codigo_unico = f"BIDPBI-{slug_base}"[:150]

        normalizados.append({
            "codigo_unico": codigo_unico,
            "fuente_origen": FUENTE,
            "tipo_aviso": "Licitación / Adquisición",
            "titulo": titulo[:500],
            "descripcion": f"Detalle extraído de PowerBI: {cadena_texto}"[:5000],
            "pais": pais[:100],
            "paises": [pais[:100]],
            "organismo": organismo,
            "categoria": None,
            "url_oficial": URL_IADB,
            "url_documento": None,
            "fecha_publicacion": fecha_pub.isoformat() if fecha_pub else None,
            "fecha_limite": fecha_lim.isoformat() if fecha_lim else None,
        })

    return normalizados


def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
        if not datos.get("titulo") or not datos.get("codigo_unico"):
            continue

        existente = registros_existentes.get(datos["codigo_unico"])
        linea_categoria = f"Categoria: {datos['categoria']}\n" if datos.get("categoria") else ""
        texto_completo = (
            f"Titulo: {datos['titulo']}\n{datos.get('descripcion') or ''}\n"
            f"{linea_categoria}"
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


async def ejecutar_sincronizacion():
    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - BID (PowerBI)", flush=True)
    print("=" * 100, flush=True)

    crudos = await extraer_licitaciones_iadb()
    print(f"\n--> Total celdas/campos extraídos: {len(crudos)}", flush=True)

    if not crudos:
        raise RuntimeError(f"No se extrajo información. Revisa {DEBUG_DIR}")

    normalizados = agrupar_y_normalizar(crudos)

    normalizados_unicos = {}
    for item in normalizados:
        normalizados_unicos[item["codigo_unico"]] = item
    normalizados = list(normalizados_unicos.values())

    print(f"--> Registros consolidados: {len(normalizados)}", flush=True)

    print("\n--> Consultando registros previos en Supabase...", flush=True)
    supabase = obtener_cliente_supabase()
    codigos = [reg["codigo_unico"] for reg in normalizados]
    existentes = obtener_registros_existentes(supabase, codigos)

    a_subir = preparar_lote_para_subir(normalizados, existentes)
    print(f"--> Registros a subir/actualizar a Supabase: {len(a_subir)}", flush=True)

    if a_subir:
        subir_en_lotes(supabase, a_subir, tamaño_lote=LOTE_ENVIO_SUPABASE)

    print("\n" + "=" * 100, flush=True)
    print("PROCESO DE INGESTA BID FINALIZADO CON ÉXITO", flush=True)
    print("=" * 100, flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(ejecutar_sincronizacion())
    except Exception as exc:
        print(f"\n❌ ERROR: {exc}", flush=True)
        sys.exit(1)

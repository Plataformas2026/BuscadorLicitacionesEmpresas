"""
ingesta_bid_powerbi.py
--------------------------
Sincroniza los avisos de licitación del Banco Interamericano de Desarrollo (BID / IADB)
extraídos mediante Playwright/PowerBI contra la tabla `licitaciones_internacionales` de Supabase.
"""

import asyncio
import re
from datetime import date
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

URL_IADB = "https://www.iadb.org/es/como-trabajar-juntos/adquisiciones/adquisiciones-para-proyectos/avisos-de-adquisiciones"
FUENTE = "BID"
LOTE_ENVIO_SUPABASE = 15
CAPTURA_DEPURACION = "powerbi_tabla_extraida.png"

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
# SCRIPT CON SCROLL CONTROLADO PARA EVITAR DESORDEN O SALTOS DE TIEMPO (ORIGINAL)
# ==============================================================================
async def extraer_licitaciones_iadb() -> list:
    licitaciones_raw = []

    async with async_playwright() as p:
        # Configuración de Chromium con soporte de aceleración gráfica para Canvas de PowerBI
        browser = await p.chromium.launch(
            headless=False,  # Se ejecuta bajo Xvfb en la pantalla virtual
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--ignore-certificate-errors",
                "--enable-features=Vulkan,UseSkiaRenderer",
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1600, "height": 1000}
        )
        page = await context.new_page()

        print(f"--> [1/5] Cargando la página del BID: {URL_IADB}...", flush=True)
        await page.goto(URL_IADB, wait_until="networkidle", timeout=60000)

        # Cierre de cookies
        try:
            btn_cookie = page.locator('#onetrust-accept-btn-handler, button:has-text("Aceptar")').first
            if await btn_cookie.is_visible(timeout=5000):
                await btn_cookie.click()
                print("    ✔ Banner de cookies aceptado.", flush=True)
                await page.wait_for_timeout(2000)
        except Exception:
            pass

        # Bajar hacia el marco
        print("--> Descendiendo en la página para activar la carga del reporte...", flush=True)
        await page.evaluate("window.scrollBy(0, 500)")
        await page.wait_for_timeout(3000)

        # Localizar iframe
        print("--> Buscando iframe de PowerBI...", flush=True)
        frame_powerbi = None
        for intento in range(15):
            for frame in page.frames:
                if ("powerbi.com" in frame.url or "app.powerbi" in frame.url) and frame.url != "about:blank":
                    frame_powerbi = frame
                    break
            if frame_powerbi:
                break
            await page.wait_for_timeout(2000)

        target_context = frame_powerbi if frame_powerbi else page
        print(f"    ✔ Frame activo: {target_context.url}", flush=True)

        # Esperar contenido
        print("--> Esperando renderizado de celdas en PowerBI...", flush=True)
        try:
            await target_context.wait_for_selector('.pivotTable, [role="gridcell"]', timeout=45000)
            await page.wait_for_timeout(3000)
            print("    ✔ Celdas localizadas exitosamente.", flush=True)
        except Exception as e:
            print(f"⚠️ Alerta esperando selectores: {e}", flush=True)

        # Enfocar la tabla sin alterar el orden
        try:
            celda = target_context.locator('[role="gridcell"], .pivotTableCellWrap').first
            await celda.click(force=True)
            await page.wait_for_timeout(1000)
            print("    ✔ Foco fijado en la primera celda.", flush=True)
        except Exception as e:
            print(f"⚠️ No se pudo fijar el foco: {e}", flush=True)

        # ----------------------------------------------------------------------
        # ESCANEO SECUENCIAL SUAVE (MICRO-SCROLL DE FLECHAS ABAJO)
        # ----------------------------------------------------------------------
        print("--> Escaneando datos progresivamente (evitando saltos)...", flush=True)
        
        TOTAL_PASADAS = 80
        pasadas_sin_cambios = 0

        for i in range(1, TOTAL_PASADAS + 1):
            frame_html = await target_context.content()
            soup = BeautifulSoup(frame_html, 'html.parser')
            celdas = soup.select('.pivotTableCellWrap, [role="gridcell"], .rowText, .cell-interactive')

            nuevos_elementos = 0
            for c in celdas:
                texto = c.get_text(strip=True)
                if texto and texto not in licitaciones_raw:
                    licitaciones_raw.append(texto)
                    nuevos_elementos += 1

            print(f"    Pasada {i}/{TOTAL_PASADAS}: {nuevos_elementos} campos nuevos (Total acumulado: {len(licitaciones_raw)})", flush=True)

            if nuevos_elementos == 0 and len(licitaciones_raw) > 0:
                pasadas_sin_cambios += 1
                if pasadas_sin_cambios >= 8:
                    print("\n    ✔ Final del reporte alcanzado correctamente.", flush=True)
                    break
            else:
                pasadas_sin_cambios = 0

            # En lugar de PageDown (salto brusco), impulsamos con pulsaciones controladas de ArrowDown y Scroll progresivo
            for _ in range(8):
                await page.keyboard.press("ArrowDown")
            
            await target_context.evaluate("""
                () => {
                    const contenedores = document.querySelectorAll('.scrollWrapper, .viewport, [role="grid"], .pivotTable');
                    contenedores.forEach(c => c.scrollTop += 250);
                }
            """)

            # Pausa de 3 segundos para sincronización de red con Azure/PowerBI
            await page.wait_for_timeout(3000)

        # Impresión final
        print("\n" + "=" * 80, flush=True)
        print(f"TOTAL DE REGISTROS EXTRAÍDOS SINCRO: {len(licitaciones_raw)}", flush=True)
        print("=" * 80, flush=True)
        for idx, item in enumerate(licitaciones_raw, 1):
            print(f"[{idx}] {item}", flush=True)

        await page.screenshot(path=CAPTURA_DEPURACION, full_page=True)
        await browser.close()

    return licitaciones_raw


# ==============================================================================
# ESTRUCTURACIÓN Y SUBIDA A SUPABASE
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
        print(f"No se extrajo información. Verifica {CAPTURA_DEPURACION}", flush=True)
        return

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
    asyncio.run(ejecutar_sincronizacion())

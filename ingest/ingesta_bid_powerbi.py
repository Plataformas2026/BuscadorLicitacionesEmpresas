"""
ingesta_bid_powerbi.py
--------------------------
Sincroniza los avisos de licitación del Banco Interamericano de Desarrollo (BID / IADB)
incrustados en el reporte PowerBI contra la tabla `licitaciones_internacionales` de Supabase.

URL: https://www.iadb.org/es/como-trabajar-juntos/adquisiciones/adquisiciones-para-proyectos/avisos-de-adquisiciones

MECANISMO DE EXTRACCIÓN:
- Automatización mediante Playwright asíncrono para navegar al iframe de PowerBI.
- Micro-scroll mediante emulación de teclado (ArrowDown) y actualización del scrollTop
  para renderizar celdas de forma virtualizada sin perder registros.
- Parseo por filas/matriz utilizando BeautifulSoup.
- Integración con Supabase, cálculo de embeddings y deduplicación basada en `codigo_unico`.
"""

import asyncio
import re
from datetime import date, timedelta
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://www.iadb.org"
URL_IADB = f"{BASE_URL}/es/como-trabajar-juntos/adquisiciones/adquisiciones-para-proyectos/avisos-de-adquisiciones"
FUENTE = "BID-PowerBI"

TIEMPO_ESPERA_CARGA_MS = 60000
LOTE_ENVIO_SUPABASE = 15

CAMPOS_LISTADO = ("titulo", "fecha_limite", "pais", "organismo", "descripcion")
CAMPOS_COMPARABLES = CAMPOS_LISTADO

CAPTURA_DEPURACION = "powerbi_tabla_extraida.png"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def parsear_fecha(texto: str):
    if not texto:
        return None
    
    # Formato ISO / Estándar (YYYY-MM-DD)
    coincidencia_iso = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", texto)
    if coincidencia_iso:
        anio, mes, dia = coincidencia_iso.groups()
        try:
            return date(int(anio), int(mes), int(dia))
        except ValueError:
            pass

    # Formato Latam/Europa (DD/MM/YYYY)
    coincidencia_lat = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", texto)
    if coincidencia_lat:
        dia, mes, anio = coincidencia_lat.groups()
        try:
            return date(int(anio), int(mes), int(dia))
        except ValueError:
            pass

    return None


async def extraer_licitaciones_iadb() -> list:
    filas_extraidas = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1400, "height": 900}
        )
        page = await context.new_page()

        print(f"--> [1/4] Cargando la página del BID: {URL_IADB}...", flush=True)
        try:
            await page.goto(URL_IADB, wait_until="networkidle", timeout=TIEMPO_ESPERA_CARGA_MS)
        except Exception as e:
            print(f"⚠️ Aviso al cargar URL principal: {e}", flush=True)

        # Cierre de banner de cookies
        try:
            btn_cookie = page.locator('#onetrust-accept-btn-handler, button:has-text("Aceptar")').first
            if await btn_cookie.is_visible(timeout=5000):
                await btn_cookie.click()
                await page.wait_for_timeout(2000)
        except Exception:
            pass

        # Desplazamiento inicial para enfocar el marco
        await page.evaluate("window.scrollBy(0, 500)")
        await page.wait_for_timeout(3000)

        # Localizar iframe de PowerBI
        frame_powerbi = None
        for _ in range(10):
            for frame in page.frames:
                if ("powerbi.com" in frame.url or "app.powerbi" in frame.url) and frame.url != "about:blank":
                    frame_powerbi = frame
                    break
            if frame_powerbi:
                break
            await page.wait_for_timeout(2000)

        target_context = frame_powerbi if frame_powerbi else page
        print(f"    ✔ Frame activo localizado: {target_context.url}", flush=True)

        # Esperar contenido de la tabla
        try:
            await target_context.wait_for_selector('.pivotTable, [role="gridcell"], .rowText', timeout=45000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"⚠️ Alerta esperando elementos de la tabla: {e}", flush=True)

        # Fijar foco en celda inicial
        try:
            celda = target_context.locator('[role="gridcell"], .pivotTableCellWrap').first
            await celda.click(force=True)
            await page.wait_for_timeout(1000)
            print("    ✔ Foco fijado en la matriz de datos.", flush=True)
        except Exception as e:
            print(f"⚠️ No se pudo fijar el foco directamente: {e}", flush=True)

        # ----------------------------------------------------------------------
        # ESCANEO SECUENCIAL CON MICRO-SCROLL
        # ----------------------------------------------------------------------
        print("--> [2/4] Escaneando tabla de PowerBI progresivamente...", flush=True)
        
        TOTAL_PASADAS = 80
        pasadas_sin_cambios = 0
        registros_vistas = set()

        for i in range(1, TOTAL_PASADAS + 1):
            frame_html = await target_context.content()
            soup = BeautifulSoup(frame_html, 'html.parser')
            
            # Estrategia de extracción por filas de cuadrícula / tabla pivotada
            filas_html = soup.select('[role="row"], .pivotTable .row')
            nuevos_elementos = 0

            if filas_html:
                for fila in filas_html:
                    celdas = [c.get_text(strip=True) for c in fila.select('[role="gridcell"], .pivotTableCellWrap, .cell-interactive') if c.get_text(strip=True)]
                    if celdas:
                        clave_fila = " | ".join(celdas)
                        if clave_fila not in registros_vistas:
                            registros_vistas.add(clave_fila)
                            filas_extraidas.append(celdas)
                            nuevos_elementos += 1
            else:
                # Fallback: captura por celdas agrupadas si no hay marcado explícito de filas
                celdas = soup.select('.pivotTableCellWrap, [role="gridcell"], .rowText, .cell-interactive')
                textos = [c.get_text(strip=True) for c in celdas if c.get_text(strip=True)]
                if textos:
                    clave_bloque = " | ".join(textos[:10])
                    if clave_bloque not in registros_vistas:
                        registros_vistas.add(clave_bloque)
                        filas_extraidas.append(textos)
                        nuevos_elementos += 1

            print(f"    Pasada {i}/{TOTAL_PASADAS}: {nuevos_elementos} bloques/filas nuevos (Total agrupaciones: {len(filas_extraidas)})", flush=True)

            if nuevos_elementos == 0 and len(filas_extraidas) > 0:
                pasadas_sin_cambios += 1
                if pasadas_sin_cambios >= 8:
                    print("\n    ✔ Final del reporte alcanzado correctamente.", flush=True)
                    break
            else:
                pasadas_sin_cambios = 0

            # Desplazamiento progresivo por teclado y DOM
            for _ in range(8):
                await page.keyboard.press("ArrowDown")
            
            await target_context.evaluate("""
                () => {
                    const contenedores = document.querySelectorAll('.scrollWrapper, .viewport, [role="grid"], .pivotTable');
                    contenedores.forEach(c => c.scrollTop += 250);
                }
            """)

            await page.wait_for_timeout(3000)

        await page.screenshot(path=CAPTURA_DEPURACION, full_page=True)
        await browser.close()

    return filas_extraidas


def construir_registro(datos_raw: list) -> dict:
    """
    Normaliza el bloque o arreglo de celdas extraído de PowerBI a la estructura
    de Supabase.
    """
    cadena_texto = " - ".join(datos_raw) if isinstance(datos_raw, list) else str(datos_raw)
    
    # Asignación de campos defensiva basada en posición o patrón
    titulo = datos_raw[0] if len(datos_raw) > 0 else "Aviso BID sin título"
    pais = datos_raw[1] if len(datos_raw) > 1 else "Internacional"
    organismo = "BID - Banco Interamericano de Desarrollo"
    
    fecha_pub = None
    fecha_lim = None
    
    for item in datos_raw:
        f = parsear_fecha(item)
        if f:
            if not fecha_lim:
                fecha_lim = f
            else:
                fecha_pub = f

    slug_base = _generar_slug(f"{pais}-{titulo}")
    codigo_unico = f"BIDPBI-{slug_base}"[:150]

    return {
        "codigo_unico": codigo_unico,
        "fuente_origen": FUENTE,
        "tipo_aviso": "Licitación / Adquisición",
        "titulo": titulo[:500],
        "descripcion": f"Detalle extraído de PowerBI: {cadena_texto}"[:MAX_LONGITUD_DESCRIPCION] if 'MAX_LONGITUD_DESCRIPCION' in globals() else cadena_texto[:5000],
        "pais": pais[:100],
        "paises": [pais[:100]],
        "organismo": organismo,
        "categoria": None,
        "url_oficial": URL_IADB,
        "url_documento": None,
        "fecha_publicacion": fecha_pub.isoformat() if fecha_pub else None,
        "fecha_limite": fecha_lim.isoformat() if fecha_lim else None,
    }


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
    print(f"\n--> [3/4] Total bloques/filas procesados desde PowerBI: {len(crudos)}", flush=True)

    if not crudos:
        print(f"No se ha extraído ningún registro. Revisa la captura guardada ({CAPTURA_DEPURACION}).", flush=True)
        return

    normalizados = [construir_registro(c) for c in crudos]

    # Filtrar posibles duplicados generados durante las pasadas
    normalizados_unicos = {}
    for item in normalizados:
        normalizados_unicos[item["codigo_unico"]] = item
    normalizados = list(normalizados_unicos.values())

    print(f"--> Registros únicos consolidados: {len(normalizados)}", flush=True)

    print("\n--> [4/4] Consultando registros previos en Supabase...", flush=True)
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

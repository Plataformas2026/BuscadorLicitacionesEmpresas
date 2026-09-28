"""
ingesta_bid_powerbi.py
----------------------

Sincroniza los avisos de licitación del Banco Interamericano de Desarrollo
(BID / IADB) extraídos mediante Playwright/PowerBI contra la tabla
`licitaciones_internacionales` de Supabase.
"""

import asyncio
import os
import re
import sys
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================================
# RUTAS DEL PROYECTO
# ============================================================================

# Estructura esperada:
#
# BuscadorLicitacionesEmpresas/
# ├── app/
# │   └── common.py
# ├── ingest/
# │   └── ingesta_bid_powerbi.py
# └── .github/
#     └── workflows/
#
ROOT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = ROOT_DIR / "app"

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


# ============================================================================
# IMPORTS DEL PROYECTO
# ============================================================================

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)


URL_IADB = (
    "https://www.iadb.org/es/como-trabajar-juntos/"
    "adquisiciones/adquisiciones-para-proyectos/"
    "avisos-de-adquisiciones"
)

FUENTE = "BID"
LOTE_ENVIO_SUPABASE = 15

CAPTURA_DEPURACION = ROOT_DIR / "powerbi_tabla_extraida.png"

CAMPOS_COMPARABLES = (
    "titulo",
    "fecha_limite",
    "pais",
    "organismo",
    "descripcion",
)



def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()

    # Normalización sencilla para generar un identificador estable.
    texto_norm = (
        texto_norm
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ñ", "n")
    )

    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")

    return (slug or "sin-referencia")[:120]


def parsear_fecha(texto: str):
    """
    Intenta convertir fechas ISO o numéricas a date.

    Power BI puede entregar fechas en distintos formatos.
    """
    if not texto:
        return None

    coincidencia_iso = re.search(
        r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})",
        texto,
    )

    if coincidencia_iso:
        anio, mes, dia = coincidencia_iso.groups()

        try:
            return date(
                int(anio),
                int(mes),
                int(dia),
            )
        except ValueError:
            pass

    coincidencia_lat = re.search(
        r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})",
        texto,
    )

    if coincidencia_lat:
        dia, mes, anio = coincidencia_lat.groups()

        try:
            return date(
                int(anio),
                int(mes),
                int(dia),
            )
        except ValueError:
            pass

    return None


async def esperar_powerbi(page, frame, timeout_ms=120000):
    """
    Espera de forma tolerante a que Power BI termine de construir la tabla.

    No dependemos exclusivamente de `.pivotTable`, porque Power BI puede
    cambiar su estructura DOM entre ejecuciones/versiones.
    """

    print("--> Esperando renderizado de Power BI...", flush=True)

    selectores = [
        '[role="gridcell"]',
        ".pivotTableCellWrap",
        ".rowText",
        ".cell-interactive",
        '[role="grid"]',
        ".pivotTable",
    ]

    # Primero esperamos a que el documento del iframe tenga actividad.
    try:
        await frame.wait_for_load_state(
            "domcontentloaded",
            timeout=30000,
        )
    except Exception:
        pass

    # Damos tiempo a que Power BI inicialice sus componentes.
    await page.wait_for_timeout(5000)

    # Buscamos cualquiera de los selectores conocidos.
    for selector in selectores:
        try:
            await frame.locator(selector).first.wait_for(
                state="visible",
                timeout=15000,
            )

            print(
                f"    ✔ Power BI renderizado. Selector detectado: {selector}",
                flush=True,
            )

            return True

        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue

    print(
        "⚠️ Power BI no presentó una celda conocida dentro del tiempo esperado.",
        flush=True,
    )

    return False


async def obtener_frame_powerbi(page):
    """
    Localiza el iframe de Power BI.

    Se comprueba repetidamente porque el iframe puede aparecer antes de que
    Power BI haya terminado de navegar a su URL definitiva.
    """

    print("--> Buscando iframe de Power BI...", flush=True)

    for intento in range(30):

        frames = page.frames

        for frame in frames:
            url = frame.url or ""

            if (
                "powerbi.com" in url
                or "app.powerbi" in url
            ) and url != "about:blank":

                print(
                    f"    ✔ Frame activo: {url}",
                    flush=True,
                )

                return frame

        if intento in (0, 5, 10, 20):
            print(
                f"    ... Power BI todavía no está disponible "
                f"(intento {intento + 1}/30)",
                flush=True,
            )

        await page.wait_for_timeout(2000)

    return None


async def extraer_licitaciones_iadb() -> list:
    """
    Abre el portal del BID y extrae progresivamente los elementos visibles
    de la tabla Power BI.
    """

    licitaciones_raw = []

    async with async_playwright() as p:

        print("--> Iniciando Chromium...", flush=True)

        browser = await p.chromium.launch(
            # En GitHub Actions no necesitamos Xvfb.
            # Headless real suele ser más estable en CI.
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",

                # GitHub Actions dispone de un /dev/shm pequeño.
                "--disable-dev-shm-usage",

                # Evita algunos problemas de aceleración gráfica en
                # entornos virtualizados.
                "--disable-gpu",
                "--disable-software-rasterizer",

                # Power BI puede abrir múltiples procesos/iframes.
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",

                # Evita problemas con sandbox en runners Linux.
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={
                "width": 1600,
                "height": 1000,
            },
            device_scale_factor=1,
            locale="es-ES",
            timezone_id="Europe/Madrid",
            ignore_https_errors=True,
        )

        page = await context.new_page()

        # Evita que una petición individual quede esperando indefinidamente.
        page.set_default_timeout(30000)
        page.set_default_navigation_timeout(90000)

        try:
            print(
                f"--> [1/5] Cargando la página del BID: {URL_IADB}...",
                flush=True,
            )

            await page.goto(
                URL_IADB,
                wait_until="domcontentloaded",
                timeout=90000,
            )

            print(
                f"    ✔ Página cargada: {page.url}",
                flush=True,
            )

            # --------------------------------------------------------------
            # COOKIES
            # --------------------------------------------------------------
            try:
                btn_cookie = page.locator(
                    "#onetrust-accept-btn-handler, "
                    "button:has-text('Aceptar'), "
                    "button:has-text('Accept')"
                ).first

                await btn_cookie.wait_for(
                    state="visible",
                    timeout=8000,
                )

                await btn_cookie.click()

                print(
                    "    ✔ Banner de cookies aceptado.",
                    flush=True,
                )

                await page.wait_for_timeout(2000)

            except Exception:
                print(
                    "    - No fue necesario aceptar cookies.",
                    flush=True,
                )

            # --------------------------------------------------------------
            # ACTIVAR LA ZONA DEL POWER BI
            # --------------------------------------------------------------
            print(
                "--> Descendiendo en la página para activar "
                "la carga del reporte...",
                flush=True,
            )

            try:
                await page.evaluate(
                    """
                    window.scrollTo({
                        top: Math.min(
                            700,
                            document.body.scrollHeight
                        ),
                        behavior: "instant"
                    });
                    """
                )
            except Exception:
                pass

            await page.wait_for_timeout(3000)

            # --------------------------------------------------------------
            # POWER BI
            # --------------------------------------------------------------
            print(
                "--> [2/5] Localizando Power BI...",
                flush=True,
            )

            frame_powerbi = await obtener_frame_powerbi(page)

            if frame_powerbi is None:
                print(
                    "⚠️ No se encontró el iframe de Power BI.",
                    flush=True,
                )

                await page.screenshot(
                    path=str(CAPTURA_DEPURACION),
                    full_page=True,
                )

                raise RuntimeError(
                    "Power BI no apareció en el DOM dentro del tiempo esperado."
                )

            # --------------------------------------------------------------
            # ESPERAR RENDERIZADO
            # --------------------------------------------------------------
            print(
                "--> [3/5] Esperando renderizado de celdas en Power BI...",
                flush=True,
            )

            powerbi_listo = await esperar_powerbi(
                page,
                frame_powerbi,
                timeout_ms=120000,
            )

            if not powerbi_listo:
                # Captura inmediatamente el estado problemático.
                await page.screenshot(
                    path=str(CAPTURA_DEPURACION),
                    full_page=True,
                )

                # No abortamos todavía: en algunas ejecuciones Power BI
                # tarda más y termina renderizando durante el primer scroll.
                print(
                    "    ⚠️ Continuaremos con el escaneo progresivo.",
                    flush=True,
                )

            # --------------------------------------------------------------
            # ENFOCAR TABLA
            # --------------------------------------------------------------
            print(
                "--> [4/5] Intentando fijar el foco de Power BI...",
                flush=True,
            )

            selectores_foco = [
                '[role="gridcell"]',
                ".pivotTableCellWrap",
                ".rowText",
                ".cell-interactive",
                '[role="grid"]',
            ]

            foco_fijado = False

            for selector in selectores_foco:
                try:
                    locator = frame_powerbi.locator(selector).first

                    await locator.wait_for(
                        state="visible",
                        timeout=5000,
                    )

                    await locator.click(
                        force=True,
                        timeout=5000,
                    )

                    print(
                        f"    ✔ Foco fijado usando: {selector}",
                        flush=True,
                    )

                    foco_fijado = True
                    break

                except Exception:
                    continue

            if not foco_fijado:
                print(
                    "    ⚠️ No se pudo fijar el foco. "
                    "Se continuará mediante scroll DOM.",
                    flush=True,
                )

            # --------------------------------------------------------------
            # ESCANEO
            # --------------------------------------------------------------
            print(
                "--> [5/5] Escaneando datos progresivamente...",
                flush=True,
            )

            TOTAL_PASADAS = 80
            pasadas_sin_cambios = 0

            for i in range(1, TOTAL_PASADAS + 1):

                frame_html = await frame_powerbi.content()

                soup = BeautifulSoup(
                    frame_html,
                    "html.parser",
                )

                celdas = soup.select(
                    ".pivotTableCellWrap, "
                    '[role="gridcell"], '
                    ".rowText, "
                    ".cell-interactive"
                )

                nuevos_elementos = 0

                for celda in celdas:
                    texto = celda.get_text(
                        " ",
                        strip=True,
                    )

                    if (
                        texto
                        and texto not in licitaciones_raw
                    ):
                        licitaciones_raw.append(texto)
                        nuevos_elementos += 1

                print(
                    f"    Pasada {i}/{TOTAL_PASADAS}: "
                    f"{nuevos_elementos} campos nuevos "
                    f"(Total acumulado: {len(licitaciones_raw)})",
                    flush=True,
                )

                # Solo detenemos el proceso después de varias pasadas
                # consecutivas sin cambios.
                if nuevos_elementos == 0:

                    pasadas_sin_cambios += 1

                    if (
                        len(licitaciones_raw) > 0
                        and pasadas_sin_cambios >= 8
                    ):
                        print(
                            "\n    ✔ Final del reporte alcanzado.",
                            flush=True,
                        )
                        break

                else:
                    pasadas_sin_cambios = 0

                # ----------------------------------------------------------
                # SCROLL CONTROLADO
                # ----------------------------------------------------------

                # Primero intentamos mantener el foco en Power BI.
                if foco_fijado:
                    for _ in range(4):
                        try:
                            await page.keyboard.press(
                                "ArrowDown"
                            )
                        except Exception:
                            break

                # Después desplazamos los posibles contenedores internos.
                try:
                    await frame_powerbi.evaluate(
                        """
                        () => {
                            const selectores = [
                                '.scrollWrapper',
                                '.viewport',
                                '[role="grid"]',
                                '.pivotTable'
                            ];

                            const elementos = [];

                            for (const selector of selectores) {
                                document
                                    .querySelectorAll(selector)
                                    .forEach(el => elementos.push(el));
                            }

                            for (const elemento of elementos) {
                                if (
                                    elemento.scrollHeight >
                                    elemento.clientHeight
                                ) {
                                    elemento.scrollTop += 300;
                                }
                            }
                        }
                        """
                    )
                except Exception:
                    pass

                # Power BI/Azure puede necesitar tiempo para traer
                # la siguiente ventana de datos.
                await page.wait_for_timeout(2500)

            # --------------------------------------------------------------
            # RESULTADO
            # --------------------------------------------------------------
            print(
                "\n" + "=" * 80,
                flush=True,
            )

            print(
                "TOTAL DE CAMPOS EXTRAÍDOS: "
                f"{len(licitaciones_raw)}",
                flush=True,
            )

            print(
                "=" * 80,
                flush=True,
            )

            if not licitaciones_raw:
                # Evidencia de depuración.
                await page.screenshot(
                    path=str(CAPTURA_DEPURACION),
                    full_page=True,
                )

                raise RuntimeError(
                    "Power BI cargó pero no se pudieron extraer celdas."
                )

            # Captura final para GitHub Actions.
            await page.screenshot(
                path=str(CAPTURA_DEPURACION),
                full_page=True,
            )

            return licitaciones_raw

        finally:
            await context.close()
            await browser.close()


# ==============================================================================
# ESTRUCTURACIÓN Y SUBIDA A SUPABASE
# ==============================================================================

def agrupar_y_normalizar(elementos_raw: list) -> list:
    """
    Convierte los campos extraídos en registros.

    NOTA:
    El algoritmo original utiliza bloques de cinco elementos. Se mantiene
    aquí para no modificar la lógica de negocio durante la migración.
    """
    normalizados = []

    TAMAÑO_BLOQUE = 5

    bloques = [
        elementos_raw[i:i + TAMAÑO_BLOQUE]
        for i in range(
            0,
            len(elementos_raw),
            TAMAÑO_BLOQUE,
        )
    ]

    for bloque in bloques:

        cadena_texto = " - ".join(bloque)

        titulo = (
            bloque[0]
            if len(bloque) > 0
            else "Aviso BID"
        )

        pais = (
            bloque[1]
            if len(bloque) > 1
            else "Internacional"
        )

        organismo = (
            "BID - Banco Interamericano de Desarrollo"
        )

        fecha_pub = None
        fecha_lim = None

        for item in bloque:

            f = parsear_fecha(item)

            if f:
                if not fecha_lim:
                    fecha_lim = f
                else:
                    fecha_pub = f

        slug_base = _generar_slug(
            f"{pais}-{titulo}"
        )

        codigo_unico = (
            f"BIDPBI-{slug_base}"
        )[:150]

        normalizados.append(
            {
                "codigo_unico": codigo_unico,
                "fuente_origen": FUENTE,
                "tipo_aviso": "Licitación / Adquisición",
                "titulo": titulo[:500],
                "descripcion": (
                    f"Detalle extraído de PowerBI: "
                    f"{cadena_texto}"
                )[:5000],
                "pais": pais[:100],
                "paises": [pais[:100]],
                "organismo": organismo,
                "categoria": None,
                "url_oficial": URL_IADB,
                "url_documento": None,
                "fecha_publicacion": (
                    fecha_pub.isoformat()
                    if fecha_pub
                    else None
                ),
                "fecha_limite": (
                    fecha_lim.isoformat()
                    if fecha_lim
                    else None
                ),
            }
        )

    return normalizados


def preparar_lote_para_subir(
    normalizados: list,
    registros_existentes: dict,
) -> list:

    a_subir = []

    for datos in normalizados:

        if (
            not datos.get("titulo")
            or not datos.get("codigo_unico")
        ):
            continue

        existente = registros_existentes.get(
            datos["codigo_unico"]
        )

        linea_categoria = (
            f"Categoria: {datos['categoria']}\n"
            if datos.get("categoria")
            else ""
        )

        texto_completo = (
            f"Titulo: {datos['titulo']}\n"
            f"{datos.get('descripcion') or ''}\n"
            f"{linea_categoria}"
            f"Pais: "
            f"{datos.get('pais') or 'No especificado'}"
        )

        if existente is None:

            datos["texto_completo"] = texto_completo
            datos["embedding"] = generar_embedding(
                texto_completo
            )
            datos["es_novedad"] = True
            datos["es_actualizada"] = False

            a_subir.append(datos)

            continue

        ha_cambiado = any(
            str(existente.get(campo))
            != str(datos.get(campo))
            for campo in CAMPOS_COMPARABLES
        )

        if not ha_cambiado:
            continue

        datos["texto_completo"] = texto_completo
        datos["embedding"] = generar_embedding(
            texto_completo
        )
        datos["es_novedad"] = False
        datos["es_actualizada"] = True

        a_subir.append(datos)

    return a_subir


async def ejecutar_sincronizacion():

    print(
        "=" * 100,
        flush=True,
    )

    print(
        "SINCRONIZACION DE LICITACIONES "
        "INTERNACIONALES - BID (PowerBI)",
        flush=True,
    )

    print(
        "=" * 100,
        flush=True,
    )

    crudos = await extraer_licitaciones_iadb()

    print(
        f"\n--> Total celdas/campos extraídos: "
        f"{len(crudos)}",
        flush=True,
    )

    if not crudos:

        print(
            f"No se extrajo información. "
            f"Verifica {CAPTURA_DEPURACION}",
            flush=True,
        )

        return

    normalizados = agrupar_y_normalizar(
        crudos
    )

    normalizados_unicos = {}

    for item in normalizados:
        normalizados_unicos[
            item["codigo_unico"]
        ] = item

    normalizados = list(
        normalizados_unicos.values()
    )

    print(
        f"--> Registros consolidados: "
        f"{len(normalizados)}",
        flush=True,
    )

    print(
        "\n--> Consultando registros previos "
        "en Supabase...",
        flush=True,
    )

    supabase = obtener_cliente_supabase()

    codigos = [
        reg["codigo_unico"]
        for reg in normalizados
    ]

    existentes = obtener_registros_existentes(
        supabase,
        codigos,
    )

    a_subir = preparar_lote_para_subir(
        normalizados,
        existentes,
    )

    print(
        f"--> Registros a subir/actualizar "
        f"a Supabase: {len(a_subir)}",
        flush=True,
    )

    if a_subir:
        subir_en_lotes(
            supabase,
            a_subir,
            tamaño_lote=LOTE_ENVIO_SUPABASE,
        )

    print(
        "\n" + "=" * 100,
        flush=True,
    )

    print(
        "PROCESO DE INGESTA BID FINALIZADO "
        "CON ÉXITO",
        flush=True,
    )

    print(
        "=" * 100,
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(
        ejecutar_sincronizacion()
    )

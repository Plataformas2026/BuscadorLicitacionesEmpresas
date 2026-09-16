# -*- coding: utf-8 -*-
"""
ingesta_bid.py
----------------
Sincroniza avisos de licitacion del Banco Interamericano de Desarrollo
(BID/IADB) leyendo directamente el informe de Power BI incrustado.
"""
import re
import time
from datetime import date, datetime, timedelta

from playwright.sync_api import sync_playwright

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

URL_OFICIAL_BID = "https://www.iadb.org/es/como-trabajar-juntos/adquisiciones/adquisiciones-para-proyectos/avisos-de-adquisiciones"
FUENTE = "BID"
DIAS_ATRAS = 3
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "tipo_aviso")

TIEMPO_ESPERA_CARGA_MS = 45000
MAX_INTENTOS_SCROLL = 3  # Reducido para traer solo lo reciente de arriba
PAUSA_ENTRE_SCROLLS_SEGUNDOS = 0.7
INTENTOS_SIN_NOVEDAD_PARA_PARAR = 2
CAPTURA_DEPURACION = "debug_bid_powerbi.png"

SELECTOR_FILA = '[role="row"]'
SELECTOR_CABECERA = '[role="columnheader"]'
SELECTOR_CELDA = '[role="gridcell"], [role="cell"]'

CANDIDATOS_COLUMNA = {
    "titulo": ["notice", "title", "titulo", "subject", "asunto", "descripcion", "description"],
    "pais": ["country", "pais", "país"],
    "fecha_publicacion": ["date", "fecha", "published", "publication", "divulgacion", "divulgación"],
    "tipo_aviso": ["type", "tipo", "category", "categoria"],
}


def _generar_slug(texto: str) -> str:
    texto_norm = texto.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def _parsear_fecha_flexible(texto: str):
    if not texto:
        return None
    texto = texto.strip()
    for patron in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(texto, patron).date()
        except ValueError:
            continue
    coincidencia = re.search(r"(\d{4})-(\d{2})-(\d{2})", texto)
    if coincidencia:
        try:
            return date(int(coincidencia.group(1)), int(coincidencia.group(2)), int(coincidencia.group(3)))
        except ValueError:
            pass
    return None


def obtener_frame_powerbi(page):
    print(f"Frames presentes en la pagina: {len(page.frames)}", flush=True)
    candidato_generico = None
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        url_frame = (frame.url or "").lower()
        print(f"  - {frame.url}", flush=True)
        if "powerbi" in url_frame:
            return frame
        if candidato_generico is None:
            candidato_generico = frame
    return candidato_generico


def intentar_pestana_abiertas(page):
    try:
        boton = page.get_by_text("Abierto para licitación ahora", exact=False).first
        if boton.count() > 0:
            boton.click(timeout=5000)
            print("Se ha hecho clic en la pestaña 'Abierto para licitación ahora'.", flush=True)
            time.sleep(2)
    except Exception as error:
        print(f"Aviso: no se pudo hacer clic en la pestaña de avisos abiertos ({error}); se continua igualmente.", flush=True)


def extraer_filas_powerbi(frame) -> tuple:
    filas_vistas = {}
    cabeceras = None
    sin_novedad_seguidas = 0

    for intento in range(MAX_INTENTOS_SCROLL):
        filas_dom = frame.locator(SELECTOR_FILA)
        total_filas = filas_dom.count()
        nuevas_en_este_intento = 0

        for indice in range(total_filas):
            fila = filas_dom.nth(indice)
            es_cabecera = fila.locator(SELECTOR_CABECERA).count() > 0

            if es_cabecera:
                if cabeceras is None:
                    celdas_cabecera = fila.locator(SELECTOR_CABECERA)
                    cabeceras = [celdas_cabecera.nth(i).inner_text().strip() for i in range(celdas_cabecera.count())]
                    print(f"    Cabeceras de columna reconocidas: {cabeceras}", flush=True)
                continue

            celdas = fila.locator(SELECTOR_CELDA)
            total_celdas = celdas.count()
            if total_celdas == 0:
                continue

            try:
                textos_celda = [celdas.nth(i).inner_text().strip() for i in range(total_celdas)]
            except Exception:
                continue

            if not any(textos_celda):
                continue

            clave = " | ".join(textos_celda)
            if clave not in filas_vistas:
                filas_vistas[clave] = textos_celda
                nuevas_en_este_intento += 1

        print(
            f"    Scroll {intento + 1}/{MAX_INTENTOS_SCROLL}: {len(filas_vistas)} filas unicas acumuladas "
            f"({nuevas_en_este_intento} nuevas en esta vuelta)",
            flush=True,
        )

        if nuevas_en_este_intento == 0:
            sin_novedad_seguidas += 1
            if sin_novedad_seguidas >= INTENTOS_SIN_NOVEDAD_PARA_PARAR:
                break
        else:
            sin_novedad_seguidas = 0

        try:
            if total_filas > 0:
                filas_dom.nth(total_filas - 1).hover(timeout=2000)
            frame.page.mouse.wheel(0, 700)
        except Exception:
            pass
        time.sleep(PAUSA_ENTRE_SCROLLS_SEGUNDOS)

    return cabeceras, list(filas_vistas.values())


def emparejar_columnas(cabeceras: list) -> dict:
    if not cabeceras:
        return {}
    mapeo = {}
    for concepto, candidatos in CANDIDATOS_COLUMNA.items():
        for indice, cabecera in enumerate(cabeceras):
            if any(c in cabecera.lower() for c in candidatos):
                mapeo[concepto] = indice
                break
    return mapeo


def extraer_licitaciones_playwright() -> list:
    candidatos = []

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        contexto = navegador.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        pagina = contexto.new_page()

        try:
            print(f"Navegando a {URL_OFICIAL_BID}...", flush=True)
            pagina.goto(URL_OFICIAL_BID, timeout=TIEMPO_ESPERA_CARGA_MS, wait_until="networkidle")

            intentar_pestana_abiertas(pagina)

            frame = obtener_frame_powerbi(pagina)
            if frame is None:
                print("No se ha encontrado ningun iframe. Abortando.", flush=True)
                pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)
                return []

            print(f"Esperando a que aparezca al menos una fila ({SELECTOR_FILA}) en el informe...", flush=True)
            try:
                frame.wait_for_selector(SELECTOR_FILA, timeout=TIEMPO_ESPERA_CARGA_MS)
            except Exception as error:
                print(f"No aparecio ninguna fila reconocible a tiempo: {error}", flush=True)
                pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)
                return []

            cabeceras, filas = extraer_filas_powerbi(frame)

            if not filas:
                print("No se ha reconocido ninguna fila de datos.", flush=True)
                pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)
                return []

            mapeo = emparejar_columnas(cabeceras)
            print(f"Emparejamiento de columnas: {mapeo}", flush=True)

            indice_titulo = mapeo.get("titulo")

            for fila in filas:
                if indice_titulo is not None and indice_titulo < len(fila):
                    titulo = fila[indice_titulo]
                else:
                    titulo = max(fila, key=len) if fila else ""
                titulo = (titulo or "").strip()
                if len(titulo) < 8:
                    continue

                indice_pais = mapeo.get("pais")
                pais = fila[indice_pais].strip() if indice_pais is not None and indice_pais < len(fila) else None

                indice_fecha = mapeo.get("fecha_publicacion")
                fecha_publicacion = (
                    _parsear_fecha_flexible(fila[indice_fecha]) if indice_fecha is not None and indice_fecha < len(fila) else None
                )

                indice_tipo = mapeo.get("tipo_aviso")
                tipo_aviso = fila[indice_tipo].strip() if indice_tipo is not None and indice_tipo < len(fila) else None

                candidatos.append({
                    "titulo": titulo,
                    "descripcion": " | ".join(c for c in fila if c and c != titulo) or None,
                    "pais": pais or None,
                    "organismo": "Banco Interamericano de Desarrollo",
                    "tipo_aviso": tipo_aviso,
                    "url_oficial": URL_OFICIAL_BID,
                    "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
                })

            print(f"Filas convertidas en avisos candidatos: {len(candidatos)}", flush=True)

        except Exception as error:
            print(f"Error durante la ejecucion de Playwright: {error}", flush=True)
            try:
                pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)
            except Exception:
                pass
        finally:
            navegador.close()

    unicos = {c["titulo"]: c for c in candidatos}.values()
    return list(unicos)


def preparar_lote_para_subir(candidatos: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in candidatos:
        datos["codigo_unico"] = f"BID-{_generar_slug(datos['titulo'])}"[:150]
        datos["fuente_origen"] = FUENTE
        datos["url_documento"] = None
        datos["categoria"] = None
        datos["fecha_limite"] = None

        existente = registros_existentes.get(datos["codigo_unico"])
        texto_completo = (
            f"Titulo: {datos['titulo']}\n"
            f"{datos.get('descripcion') or ''}\n"
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
    desde = hoy - timedelta(days=DIAS_ATRAS)

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - BID (Power BI via Playwright)", flush=True)
    print("=" * 100, flush=True)
    print(f"Ventana de publicacion: {desde} .. {hoy}", flush=True)

    candidatos = extraer_licitaciones_playwright()
    print(f"\nTotal avisos candidatos extraidos: {len(candidatos)}", flush=True)

    if not candidatos:
        print("No se ha extraido ningun aviso.", flush=True)
        return

    en_ventana = [
        c for c in candidatos
        if not c.get("fecha_publicacion") or date.fromisoformat(c["fecha_publicacion"]) >= desde
    ]
    print(f"Dentro de la ventana de {DIAS_ATRAS} dias (o sin fecha reconocida): {len(en_ventana)}", flush=True)

    if not en_ventana:
        print("No hay avisos dentro de la ventana de dias configurada.", flush=True)
        return

    supabase = obtener_cliente_supabase()

    print("\nComparando con lo ya existente en Supabase (en lotes)...", flush=True)
    claves_a_buscar = [f"BID-{_generar_slug(c['titulo'])}"[:150] for c in en_ventana]
    
    # Consulta troceada en bloques de 50 para evitar errores 400 de PostgREST
    registros_existentes = {}
    for i in range(0, len(claves_a_buscar), 50):
        bloque = claves_a_buscar[i:i+50]
        parcial = obtener_registros_existentes(
            supabase,
            tabla="licitaciones_internacionales",
            columna_clave="codigo_unico",
            columnas=("id", "codigo_unico") + CAMPOS_COMPARABLES,
            claves=bloque,
        )
        if parcial:
            registros_existentes.update(parcial)

    lote_final = preparar_lote_para_subir(en_ventana, registros_existentes)

    if not lote_final:
        print("No hay avisos nuevos ni cambios que sincronizar.", flush=True)
        return

    subidas = subir_en_lotes(
        supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
    )
    print(f"\nSincronizacion BID completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

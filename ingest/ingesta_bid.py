# -*- coding: utf-8 -*-
"""
ingesta_bid.py
----------------
Sincroniza avisos de licitacion del Banco Interamericano de Desarrollo
(BID/IADB) utilizando Playwright para leer los datos renderizados por
el informe de Power BI en la pagina oficial, asegurando compatibilidad
con Supabase y manteniendo el esquema existente.
"""
import os
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


def _generar_slug(texto: str) -> str:
    texto_norm = texto.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def extraer_licitaciones_playwright() -> list:
    """
    Utiliza Playwright en modo headless para navegar a la web del BID,
    hacer scroll para activar el contenido y extraer las filas del informe de Power BI.
    """
    print("Iniciando navegador Playwright para extraer datos de Power BI...", flush=True)
    candidatos = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Usar un User-Agent realista para evitar bloqueos por bot
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()
        
        try:
            print(f"Navegando a {URL_OFICIAL_BID}...", flush=True)
            page.goto(URL_OFICIAL_BID, timeout=60000, wait_until="networkidle")
            
            # Hacer scroll hacia abajo para forzar la carga de elementos multimedia y iframes
            print("Realizando scroll para activar componentes visuales...", flush=True)
            for _ in range(3):
                page.mouse.wheel(0, 800)
                time.sleep(2)

            # Esperar a que cualquier iframe esté presente en el DOM (sin exigir visibilidad estricta)
            print("Buscando iframes en el DOM...", flush=True)
            page.wait_for_selector("iframe", state="attached", timeout=30000)
            
            # Dar un margen adicional para que Power BI pinte los datos dentro del marco
            print("Esperando a que el informe de Power BI procese los datos...", flush=True)
            time.sleep(15)

            # Recorrer los iframes y el contexto principal para extraer textos y enlaces
            frames = page.frames
            print(frames)
            print(f"Frames detectados: {len(frames)}", flush=True)
            
            for frame in frames:
                try:
                    # Extraer todos los enlaces visibles en el frame actual
                    enlaces = frame.locator("a").evaluate_all(
                        "nodes => nodes.map(n => ({text: n.innerText, href: n.href}))"
                    )
                    for item in enlaces:
                        titulo = item.get("text", "").strip()
                        url = item.get("href", "").strip()
                        # Filtrar títulos válidos que parezcan licitaciones o avisos
                        if len(titulo) > 15:
                            candidatos.append({
                                "titulo": titulo,
                                "descripcion": "Extraido desde informe Power BI del BID",
                                "pais": "No especificado",
                                "organismo": "Banco Interamericano de Desarrollo",
                                "tipo_aviso": "GENERAL",
                                "url_oficial": url if url.startswith("http") else URL_OFICIAL_BID,
                                "fecha_publicacion": date.today().isoformat()
                            })
                except Exception as frame_err:
                    # Algunos frames externos de analítica o publicidad pueden denegar acceso, se omiten de forma segura
                    continue

            print(f"Enlaces útiles encontrados en los frames: {len(candidatos)}", flush=True)

        except Exception as error:
            print(f"Error durante la ejecucion de Playwright: {error}", flush=True)
        finally:
            browser.close()

    # Eliminar duplicados por título
    unicos = {c["titulo"]: c for c in candidatos}.values()
    return list(unicos)

def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
        codigo_unico = f"BID-{_generar_slug(datos['titulo']) }"
        datos["codigo_unico"] = codigo_unico[:150]
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
    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - BID (PLAYWRIGHT POWERBI)", flush=True)
    print("=" * 100, flush=True)

    candidatos = extraer_licitaciones_playwright()
    print(f"\nTotal avisos candidatos extraidos: {len(candidatos)}", flush=True)

    if not candidatos:
        print("No se encontraron avisos en la extraccion visual.", flush=True)
        return

    supabase = obtener_cliente_supabase()

    print("\nComparando con lo ya existente en Supabase...", flush=True)
    claves_a_buscar = [f"BID-{_generar_slug(c['titulo'])}"[:150] for c in candidatos]
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico") + CAMPOS_COMPARABLES,
        claves=claves_a_buscar,
    )

    lote_final = preparar_lote_para_subir(candidatos, registros_existentes)

    if not lote_final:
        print("No hay avisos nuevos ni cambios que sincronizar.", flush=True)
        return

    subidas = subir_en_lotes(
        supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
    )
    print(f"\nSincronizacion BID completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

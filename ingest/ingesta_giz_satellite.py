# -*- coding: utf-8 -*-
"""
ingesta_giz_satellite.py
--------------------------
Sincroniza los avisos de licitación publicados en el portal de adquisiciones 
de la GIZ (Vergabemarktplatz GIZ, plataforma cosinex/DTVP) contra la tabla 
`licitaciones_internacionales` de Supabase.

URL: https://ausschreibungen.giz.de/Satellite/company/welcome.do?method=showTable&fromSearch=1
"""

import re
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://ausschreibungen.giz.de"
LISTADO_URL = BASE_URL + "/Satellite/company/welcome.do?method=showTable&fromSearch=1"
FUENTE = "GIZ-Satellite"
TIEMPO_ESPERA_CARGA_MS = 45000
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "fecha_limite")
CAPTURA_DEPURACION = "debug_giz_satellite_tabla.png"

CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PATRON_REFERENCIA_TITULO = re.compile(r"^\s*(\d{4,10})\s*[-–]\s*(.+)$")

_JS_EXTRAER_FILAS = """
() => {
    const resultados = [];
    const filas = Array.from(document.querySelectorAll('table tbody tr'));

    for (const fila of filas) {
        const celdas = fila.querySelectorAll('td');
        if (celdas.length < 3) continue;

        const fechaPub = (celdas[0]?.innerText || '').trim();
        const fechaLimite = (celdas[1]?.innerText || '').trim();
        
        const enlaceEl = celdas[2]?.querySelector('a') || fila.querySelector('a');
        const titulo = (celdas[2]?.innerText || enlaceEl?.innerText || '').trim();
        const href = enlaceEl ? enlaceEl.getAttribute('href') : '';
        const tipo = celdas[3] ? (celdas[3].innerText || '').trim() : '';

        if (titulo) {
            resultados.push({
                titulo: titulo,
                href: href,
                fecha_pub_raw: fechaPub,
                fecha_limite_raw: fechaLimite,
                tipo_procedimiento: tipo
            });
        }
    }
    return resultados;
}
"""


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def parsear_fecha_alemana(texto: str):
    if not texto:
        return None
    coincidencia = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", texto)
    if not coincidencia:
        return None
    dia, mes, anio = coincidencia.groups()
    try:
        return date(int(anio), int(mes), int(dia))
    except ValueError:
        return None


def extraer_avisos_playwright() -> list:
    todos_los_avisos = []
    
    # Rango de fechas permitido: Hoy y Ayer
    hoy = date.today()
    ayer = hoy - timedelta(days=1)

    try:
        with sync_playwright() as p:
            navegador = None
            try:
                navegador = p.chromium.launch(headless=True)
                pagina = navegador.new_page(user_agent=CABECERAS_USER_AGENT)

                print(f"--> Cargando el portal de licitaciones de la GIZ: {LISTADO_URL}...", flush=True)
                pagina.goto(LISTADO_URL, timeout=TIEMPO_ESPERA_CARGA_MS, wait_until="domcontentloaded")

                try:
                    pagina.wait_for_selector('table tbody tr', timeout=TIEMPO_ESPERA_CARGA_MS)
                except Exception as error:
                    print(f"    No apareció la tabla de avisos a tiempo: {error}", flush=True)
                    pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)
                    print(f"    Captura de depuración guardada en {CAPTURA_DEPURACION}.", flush=True)
                    return []

                pagina_actual = 1
                alcanzado_limite_fecha = False

                while True:
                    avisos_pagina = pagina.evaluate(_JS_EXTRAER_FILAS)
                    print(f"    Procesando página {pagina_actual} ({len(avisos_pagina)} avisos)...", flush=True)

                    for aviso in avisos_pagina:
                        fecha_pub = parsear_fecha_alemana(aviso.get("fecha_pub_raw"))
                        
                        if fecha_pub:
                            # Si es de hoy o ayer, lo conservamos
                            if fecha_pub >= ayer:
                                todos_los_avisos.append(aviso)
                            # Como la tabla viene ordenada por fecha descendente,
                            # si encontramos una fecha anterior a ayer, podemos parar.
                            elif fecha_pub < ayer:
                                alcanzado_limite_fecha = True
                        else:
                            # Si por algún motivo no se puede parsear la fecha, conservamos por seguridad
                            todos_los_avisos.append(aviso)

                    if alcanzado_limite_fecha:
                        print(f"--> Alcanzados avisos con fecha anterior a ayer ({ayer.strftime('%d.%m.%Y')}). Deteniendo paginación.", flush=True)
                        break

                    # Buscar botón para pasar a la siguiente página
                    boton_siguiente = pagina.query_selector('a.next-page, a[title*="Nächste"], a[title*="weiter"]')
                    if not boton_siguiente or not boton_siguiente.is_visible():
                        break

                    pagina_actual += 1
                    print(f"--> Avanzando a la página {pagina_actual}...", flush=True)
                    boton_siguiente.click()
                    pagina.wait_for_timeout(2000)
                    pagina.wait_for_selector('table tbody tr', timeout=TIEMPO_ESPERA_CARGA_MS)

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

    return todos_los_avisos


def construir_registro(aviso: dict) -> dict:
    titulo_crudo = (aviso.get("titulo") or "").strip()
    href = aviso.get("href") or ""
    fecha_pub_raw = aviso.get("fecha_pub_raw") or ""
    fecha_limite_raw = aviso.get("fecha_limite_raw") or ""
    tipo_procedimiento = aviso.get("tipo_procedimiento") or None

    coincidencia_ref = PATRON_REFERENCIA_TITULO.match(titulo_crudo)
    if coincidencia_ref:
        referencia, titulo = coincidencia_ref.groups()
    else:
        referencia, titulo = None, titulo_crudo

    fecha_publicacion = parsear_fecha_alemana(fecha_pub_raw)
    fecha_limite = parsear_fecha_alemana(fecha_limite_raw)

    url_oficial = f"{BASE_URL}{href}" if href.startswith("/") else (href or LISTADO_URL)
    slug_base = referencia or _generar_slug(titulo or href)

    return {
        "codigo_unico": f"GIZSAT-{_generar_slug(slug_base)}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": tipo_procedimiento,
        "titulo": titulo or titulo_crudo or None,
        "descripcion": f"Referencia GIZ: {referencia}." if referencia else None,
        "pais": "Alemania",
        "paises": ["Alemania"],
        "organismo": "GIZ",
        "categoria": None,
        "url_oficial": url_oficial,
        "url_documento": None,
        "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
    }


def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
        if not datos.get("titulo") or not datos.get("url_oficial"):
            continue

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
    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - GIZ SATELLITE (portal propio)", flush=True)
    print("=" * 100, flush=True)

    crudos = extraer_avisos_playwright()
    print(f"\nTotal avisos rastreados de hoy y ayer: {len(crudos)}", flush=True)

    if not crudos:
        print(
            "No se ha extraído ningún aviso reciente. Revisa el log de arriba y la captura de depuración "
            f"({CAPTURA_DEPURACION}).",
            flush=True,
        )
        return

    normalizados = [construir_registro(a) for a in crudos]

    sin_fecha_limite = sum(1 for n in normalizados if not n.get("fecha_limite"))
    if sin_fecha_limite:
        print(
            f"Avisos sin fecha límite reconocida o concluidos (p. ej. 'AV'): {sin_fecha_limite}/{len(normalizados)}",
            flush=True,
        )

    supabase = obtener_cliente_supabase()

    print("\nComparando con lo ya existente en Supabase...", flush=True)
    claves_validas = [n["codigo_unico"] for n in normalizados if n.get("titulo") and n.get("url_oficial")]
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico") + CAMPOS_COMPARABLES,
        claves=claves_validas,
    )

    lote_final = preparar_lote_para_subir(normalizados, registros_existentes)

    if not lote_final:
        print("No hay avisos nuevos ni cambios que sincronizar.", flush=True)
        return

    subidas = subir_en_lotes(
        supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
    )
    print(f"\nSincronizacion GIZ-Satellite completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

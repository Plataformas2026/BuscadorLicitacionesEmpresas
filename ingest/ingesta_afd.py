# -*- coding: utf-8 -*-
"""
ingesta_afd.py
----------------
Sincroniza avisos de contratación de la Agencia Française de Développement (AFD)
a través de dgMarket directamente contra la tabla `licitaciones_internacionales` de Supabase.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local:      python ingesta_afd.py
"""

import re
import time
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)


# ============================================================
# CONFIGURACION
# ============================================================

BASE_URL = "https://afd.dgmarket.com"
LISTADO_URL = BASE_URL + "/tenders/brandedNoticeList.do"

FUENTE = "AFD"

DIAS_ATRAS = 1

MAX_PAGINAS_SEGURIDAD = 60

PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.5
PAUSA_ENTRE_DETALLES_SEGUNDOS = 0.4

TIMEOUT_PETICION = 30

LOTE_ENVIO_SUPABASE = 15

CABECERAS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

MESES_INGLES = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Sept": 9, "Oct": 10, "Nov": 11, "Dec": 12
}


# ============================================================
# CAMPOS COMPARABLES
# ============================================================

CAMPOS_COMPARABLES = (
    "titulo",
    "descripcion",
    "pais",
    "categoria",
    "url_documento",
    "fecha_limite",
)


# ============================================================
# DESCARGA DEL LISTADO
# ============================================================

def obtener_pagina(offset: int) -> str:
    print(
        f"--> Descargando listado de AFD (offset {offset})...",
        flush=True
    )
    respuesta = requests.get(
        LISTADO_URL,
        params={"offset": offset},
        timeout=TIMEOUT_PETICION,
        headers=CABECERAS,
    )
    print(f"    HTTP: {respuesta.status_code}", flush=True)
    respuesta.raise_for_status()
    return respuesta.text


# ============================================================
# FECHAS
# ============================================================

def _parsear_fecha_dgmarket(texto_fecha: str):
    if not texto_fecha:
        return None
    try:
        limpio = texto_fecha.replace(",", "").strip()
        partes = limpio.split()
        if len(partes) == 3:
            mes_str, dia_str, anio_str = partes[0], partes[1], partes[2]
            mes = MESES_INGLES.get(mes_str.capitalize())
            if mes:
                return date(int(anio_str), mes, int(dia_str))
    except Exception:
        pass
    
    for fmt in ["%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d-%b-%Y"]:
        try:
            return datetime.strptime(texto_fecha.strip(), fmt).date()
        except ValueError:
            continue
    return None


# ============================================================
# EXTRAER AVISOS DE UNA PAGINA
# ============================================================

def extraer_avisos_de_pagina(html: str):
    soup = BeautifulSoup(html, "html.parser")
    tabla = soup.find("table", id="notice") or soup.find("table", class_="simple")
    if not tabla:
        return []

    avisos = []
    vistos = set()
    filas = tabla.find("tbody").find_all("tr") if tabla.find("tbody") else tabla.find_all("tr")

    for fila in filas:
        try:
            td_pais = fila.find("td", class_="country")
            pais = td_pais.get_text(strip=True) if td_pais else None

            enlace = fila.find("a", href=True)
            if not enlace:
                continue
            titulo = enlace.get_text(strip=True)
            href = enlace["href"]
            if not titulo or not href:
                continue

            url_oficial = href if href.startswith("http") else BASE_URL + href
            if url_oficial in vistos:
                continue
            vistos.add(url_oficial)

            td_pub = fila.find("td", class_="published")
            fecha_publicacion = _parsear_fecha_dgmarket(td_pub.get_text(strip=True)) if td_pub else None

            td_deadline = fila.find("td", class_="deadline")
            fecha_limite = _parsear_fecha_dgmarket(td_deadline.get_text(strip=True)) if td_deadline else None

            avisos.append({
                "titulo": titulo,
                "pais": pais,
                "fecha_publicacion": fecha_publicacion,
                "fecha_limite": fecha_limite,
                "url_oficial": url_oficial,
            })
        except Exception:
            continue

    return avisos


# ============================================================
# FICHA DEL AVISO Y DOCUMENTO
# ============================================================

def extraer_descripcion_y_categoria(soup: BeautifulSoup):
    """
    Extrae la descripción desde la fila 'Eligibilité des Soumissionaires' 
    y la categoría desde la sección 'Missions'.
    """
    descripcion = None
    categoria = None

    # 1. Extracción de la descripción exacta a partir de "Eligibilité des Soumissionaires"
    for fila in soup.find_all("tr"):
        celdas = fila.find_all("td")
        if len(celdas) >= 2:
            texto_etiqueta = celdas[0].get_text(strip=True)
            if "eligibilit" in texto_etiqueta.lower():
                # Obtenemos el texto conservando los saltos de línea internos (<br>)
                descripcion = celdas[1].get_text(separator="\n", strip=True)
                break

    # Si no se encontró por la etiqueta exacta, buscamos en el texto general del bloque principal
    if not descripcion:
        contenedor = soup.find("main") or soup.find(id="content") or soup
        texto_completo_pagina = contenedor.get_text(separator="\n")
        match_eligibilidad = re.search(
            r"(Eligibilit[ée]\s+des\s+Soumissionaires[^:\n]*:.*?)(?=\n\s*\n[A-ZÀ-ÖØ-Þ]|\Z)",
            texto_completo_pagina,
            re.IGNORECASE | re.DOTALL
        )
        if match_eligibilidad:
            descripcion = match_eligibilidad.group(1).strip()

    # 2. Extracción de Categoría basada en la sección "Missions"
    h3_elements = soup.find_all("h3")
    for h3 in h3_elements:
        if "missions" in h3.get_text(strip=True).lower():
            # Buscar el siguiente contenedor o lista ul cercana
            siguiente_tr = h3.find_parent("tr")
            if siguiente_tr:
                siguiente_fila = siguiente_tr.find_next_sibling("tr")
                if siguiente_fila:
                    ul = siguiente_fila.find("ul")
                    if ul:
                        categoria = ul.get_text(separator=" ", strip=True)
                        break

    # Fallback para categoría si no se halló mediante la estructura de tabla anterior
    if not categoria:
        for h3 in h3_elements:
            if "missions" in h3.get_text(strip=True).lower():
                padre = h3.find_parent()
                if padre:
                    ul = padre.find_next("ul")
                    if ul:
                        categoria = ul.get_text(separator=" ", strip=True)
                        break

    return descripcion, categoria


def extraer_url_documento(soup: BeautifulSoup):
    contenedor = soup.find("main") or soup.find(id="content") or soup
    for enlace in contenedor.find_all("a", href=True):
        href = enlace["href"]
        if href.lower().endswith((".pdf", ".docx", ".doc")):
            return href if href.startswith("http") else BASE_URL + href
    
    # Búsqueda específica en la zona de documentos adjuntos vista en el HTML
    for a in soup.find_all("a", href=True):
        if "biddingDocumentsList.do" in a["href"] or "download" in a["href"].lower():
            href = a["href"]
            return href if href.startswith("http") else BASE_URL + href

    return None


def obtener_detalle_aviso(url: str) -> dict:
    try:
        respuesta = requests.get(
            url,
            timeout=TIMEOUT_PETICION,
            headers=CABECERAS,
        )
        respuesta.raise_for_status()
    except Exception as error:
        print(f"     Error descargando la ficha: {error}", flush=True)
        return {
            "descripcion": None,
            "categoria": None,
            "url_documento": None,
        }

    soup = BeautifulSoup(respuesta.text, "html.parser")
    descripcion, categoria = extraer_descripcion_y_categoria(soup)
    url_documento = extraer_url_documento(soup)

    return {
        "descripcion": descripcion,
        "categoria": categoria,
        "url_documento": url_documento,
    }


# ============================================================
# CONSTRUIR REGISTRO
# ============================================================

def construir_registro(aviso: dict) -> dict:
    match_slug = re.search(r"/tender/(\d+)", aviso["url_oficial"])
    slug = match_slug.group(1) if match_slug else aviso["url_oficial"].rstrip("/").split("/")[-1]

    fecha_publicacion = aviso.get("fecha_publicacion")
    fecha_limite = aviso.get("fecha_limite")

    return {
        "codigo_unico": f"AFD-{slug}",
        "fuente_origen": FUENTE,
        "tipo_aviso": "Licitación",
        "titulo": aviso["titulo"],
        "descripcion": aviso.get("descripcion"),
        "pais": aviso.get("pais"),
        "organismo": "Agence Française de Développement",
        "categoria": aviso.get("categoria"),
        "url_oficial": aviso["url_oficial"],
        "url_documento": aviso.get("url_documento"),
        "fecha_publicacion": (
            fecha_publicacion.isoformat()
            if fecha_publicacion
            else None
        ),
        "fecha_limite": (
            fecha_limite.isoformat()
            if fecha_limite
            else None
        ),
    }


# ============================================================
# DECIDIR QUE SUBIR
# ============================================================

def preparar_lote_para_subir(
    normalizados: list,
    registros_existentes: dict
) -> list:
    a_subir = []

    for datos in normalizados:
        if datos is None:
            continue

        existente = registros_existentes.get(datos["codigo_unico"])

        texto_completo = (
            f"Titulo: {datos['titulo']}\n"
            f"{datos.get('descripcion') or ''}\n"
            f"Pais: {datos.get('pais') or 'No especificado'}\n"
            f"Categoria: {datos.get('categoria') or 'No especificada'}"
        )

        if existente is None:
            datos["texto_completo"] = texto_completo
            datos["embedding"] = generar_embedding(texto_completo)
            datos["es_novedad"] = True
            datos["es_actualizada"] = False
            a_subir.append(datos)
            continue

        ha_cambiado = any(
            str(existente.get(campo)) != str(datos.get(campo))
            for campo in CAMPOS_COMPARABLES
        )

        if not ha_cambiado:
            continue

        datos["texto_completo"] = texto_completo
        datos["embedding"] = generar_embedding(texto_completo)
        datos["es_novedad"] = False
        datos["es_actualizada"] = True
        a_subir.append(datos)

    return a_subir


# ============================================================
# EJECUCION PRINCIPAL
# ============================================================

def ejecutar_sincronizacion():
    hoy = date.today()
    desde = hoy - timedelta(days=DIAS_ATRAS)

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - AFD", flush=True)
    print("=" * 100, flush=True)
    print(f"Ventana: {desde} .. {hoy}", flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)

    candidatos = []
    offset = 0
    detener = False
    incremento_offset = 20

    while offset < (MAX_PAGINAS_SEGURIDAD * incremento_offset) and not detener:
        try:
            html = obtener_pagina(offset)
        except Exception as error:
            print(f"    Error descargando la página con offset {offset}: {error}", flush=True)
            break

        avisos = extraer_avisos_de_pagina(html)
        if not avisos:
            print("    No se encontraron avisos en esta página. Fin.", flush=True)
            break

        for aviso in avisos:
            fecha = aviso.get("fecha_publicacion")
            if fecha and fecha < desde:
                print(f"    Llegamos a {fecha}, anterior a {desde}. Fin del escaneo.", flush=True)
                detener = True
                break
            if fecha and fecha >= desde:
                candidatos.append(aviso)

        if detener:
            break

        offset += incremento_offset
        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    print(f"\nAvisos candidatos en la ventana: {len(candidatos)}", flush=True)

    if not candidatos:
        return

    print("\nDescargando ficha de cada candidato (descripción + categoría + documento adjunto)...", flush=True)

    normalizados = []
    for indice, aviso in enumerate(candidatos, start=1):
        print(f"  [{indice}/{len(candidatos)}] {aviso['titulo'][:90]}", flush=True)

        detalle = obtener_detalle_aviso(aviso["url_oficial"])
        aviso["descripcion"] = detalle["descripcion"]
        aviso["categoria"] = detalle["categoria"]
        aviso["url_documento"] = detalle["url_documento"]

        registro = construir_registro(aviso)
        if registro is not None:
            normalizados.append(registro)

        time.sleep(PAUSA_ENTRE_DETALLES_SEGUNDOS)

    normalizados = list(
        {n["codigo_unico"]: n for n in normalizados}.values()
    )

    supabase = obtener_cliente_supabase()

    print("\nComparando con lo ya existente en Supabase...", flush=True)

    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico") + CAMPOS_COMPARABLES,
        claves=[n["codigo_unico"] for n in normalizados],
    )

    lote_final = preparar_lote_para_subir(normalizados, registros_existentes)

    if not lote_final:
        print("No hay avisos nuevos ni cambios que sincronizar.", flush=True)
        return

    subidas = subir_en_lotes(
        supabase,
        "licitaciones_internacionales",
        "codigo_unico",
        lote_final,
        tamano_lote=LOTE_ENVIO_SUPABASE,
    )

    print(f"\nSincronización AFD completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

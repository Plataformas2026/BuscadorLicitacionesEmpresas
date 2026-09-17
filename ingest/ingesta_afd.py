# -*- coding: utf-8 -*-
"""
ingesta_afd_dgmarket.py
-------------------------
Sincroniza avisos de contratación de la Agencia Francesa de Desarrollo (AFD)
desde su portal en dgMarket directamente contra la tabla `licitaciones_internacionales` de Supabase.

Estructura de la fuente (dgMarket AFD):
    https://afd.dgmarket.com/tender/search.do?offset=N

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local:     python ingesta_afd_dgmarket.py
Ejecución programada: ver .github/workflows/sincronizar_afd.yml
"""

import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import unquote

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
LISTADO_URL = BASE_URL + "/tender/search.do"

FUENTE = "AFD"

DIAS_ATRAS = 3

MAX_PAGINAS_SEGURIDAD = 60

PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.8
PAUSA_ENTRE_DETALLES_SEGUNDOS = 0.4

TIMEOUT_PETICION = 30

LOTE_ENVIO_SUPABASE = 15

CABECERAS = {
    "User-Agent": "Mozilla/5.0 (compatible; LicitacionesEmpresasBot/1.0)"
}


# ============================================================
# PATRONES
# ============================================================

# Formato de fecha en dgMarket: "Sept 17, 2026" o similares
PATRON_FECHA_LISTADO = re.compile(
    r"[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}"
)


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
        f"--> Descargando offset {offset} del listado de AFD (dgMarket)...",
        flush=True
    )

    respuesta = requests.get(
        LISTADO_URL,
        params={"offset": offset},
        timeout=TIMEOUT_PETICION,
        headers=CABECERAS,
    )

    print(
        f"    HTTP: {respuesta.status_code}",
        flush=True
    )

    respuesta.raise_for_status()

    return respuesta.text


# ============================================================
# FECHAS
# ============================================================

def _parsear_fecha_dgmarket(texto_fecha: str):
    if not texto_fecha:
        return None
    
    texto_limpio = texto_fecha.strip()
    formatos = ["%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d-%b-%Y"]
    
    for fmt in formatos:
        try:
            return datetime.strptime(texto_limpio, fmt).date()
        except ValueError:
            continue
            
    return None


# ============================================================
# EXTRAER AVISOS DE UNA PAGINA
# ============================================================

def extraer_avisos_de_pagina(html: str):
    """
    Extrae avisos de la tabla de resultados de dgMarket.
    Estructura típica:
    <table id="notice" ...>
      <tr class="...">
        <td class="country"> Côte d'Ivoire </td>
        <td> <a href="/tender/114841109">Título...</a> </td>
        <td class="published"> Sept 17, 2026 </td>
        <td class="deadline"> Sept 30, 2026 </td>
      </tr>
    </table>
    """
    soup = BeautifulSoup(html, "html.parser")
    tabla = soup.find("table", id="notice") or soup.find("table", class_="simple")
    
    if not tabla:
        return []

    avisos = []
    vistos = set()

    filas = tabla.find("tbody").find_all("tr") if tabla.find("tbody") else tabla.find_all("tr")

    for fila in filas:
        try:
            # 1. País
            td_pais = fila.find("td", class_="country")
            pais = td_pais.get_text(strip=True) if td_pais else None

            # 2. Título y URL oficial
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

            # 3. Fecha de Publicación
            td_pub = fila.find("td", class_="published")
            fecha_publicacion = None
            if td_pub:
                fecha_publicacion = _parsear_fecha_dgmarket(td_pub.get_text(strip=True))

            # 4. Fecha Límite
            td_deadline = fila.find("td", class_="deadline")
            fecha_limite = None
            if td_deadline:
                fecha_limite = _parsear_fecha_dgmarket(td_deadline.get_text(strip=True))

            avisos.append({
                "titulo": titulo,
                "pais": pais,
                "fecha_publicacion": fecha_publicacion,
                "fecha_limite": fecha_limite,
                "url_oficial": url_oficial,
            })

        except Exception as e:
            print(f"    Error procesando fila de dgMarket: {e}", flush=True)
            continue

    return avisos


# ============================================================
# FICHA DEL AVISO (DETALLE)
# ============================================================

def extraer_detalle_aviso(url: str) -> dict:
    try:
        respuesta = requests.get(
            url,
            timeout=TIMEOUT_PETICION,
            headers=CABECERAS,
        )
        respuesta.raise_for_status()
    except Exception as error:
        print(
            f"      Error descargando la ficha: {error}",
            flush=True
        )
        return {
            "descripcion": None,
            "categoria": None,
            "url_documento": None,
        }

    soup = BeautifulSoup(respuesta.text, "html.parser")

    # Extracción de la descripción / elegibilidad de los soumissionnaires
    descripcion = None
    texto_elegibilidad = None
    
    # Buscar bloques de texto relevantes en el detalle de dgMarket
    for elem in soup.find_all(["p", "div", "td"]):
        texto = elem.get_text(strip=True)
        if "Eligibilité des Soumissionaires" in texto or "Eligibility of Bidders" in texto:
            texto_elegibilidad = texto.replace("Eligibilité des Soumissionaires:", "").replace("Eligibility of Bidders:", "").strip()
            break

    if not texto_elegibilidad:
        # Si no encuentra un bloque específico, buscar párrafos descriptivos largos
        parrafos = []
        for p in soup.find_all("p"):
            t = p.get_text(strip=True)
            if len(t) > 60 and "dgMarket" not in t:
                parrafos.append(t)
        if parrafos:
            descripcion = "\n\n".join(parrafos[:3])
    else:
        descripcion = texto_elegibilidad

    # Extracción de categoría (Missions / Sector)
    categoria = None
    for tr in soup.find_all("tr"):
        texto_tr = tr.get_text(separator=" ", strip=True)
        if "Sector" in texto_tr or "Missions" in texto_tr or "Category" in texto_tr:
            tds = tr.find_all("td")
            if len(tds) > 1:
                categoria = tds[1].get_text(strip=True)
                break

    # Extracción de URL de documento adjunto si existe
    url_documento = None
    for enlace in soup.find_all("a", href=True):
        href = enlace["href"]
        if href.lower().endswith((".pdf", ".docx", ".doc", ".zip")):
            url_documento = href if href.startswith("http") else BASE_URL + href
            break

    return {
        "descripcion": descripcion,
        "categoria": categoria,
        "url_documento": url_documento,
    }


# ============================================================
# CONSTRUIR REGISTRO
# ============================================================

def construir_registro(aviso: dict) -> dict:
    match_id = re.search(r"/tender/(\d+)", aviso["url_oficial"])
    tender_id = match_id.group(1) if match_id else abs(hash(aviso["url_oficial"]))

    fecha_publicacion = aviso.get("fecha_publicacion")
    fecha_limite = aviso.get("fecha_limite")

    return {
        "codigo_unico": f"AFD-{tender_id}",
        "fuente_origen": FUENTE,
        "tipo_aviso": "Avis de Marché / Appel d'Offres",
        "titulo": aviso["titulo"],
        "descripcion": aviso.get("descripcion"),
        "pais": aviso.get("pais"),
        "organismo": "Agence Française de Développement (AFD)",
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

        existente = registros_existentes.get(
            datos["codigo_unico"]
        )

        texto_completo = (
            f"Titulo: {datos['titulo']}\n"
            f"Descripcion: {datos.get('descripcion') or ''}\n"
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
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - AFD (dgMarket)", flush=True)
    print("=" * 100, flush=True)
    print(f"Ventana: {desde} .. {hoy}", flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)

    candidatos = []
    offset = 0
    detener = False
    incremento_offset = 20  # dgMarket suele paginar de 20 en 20 o similar

    while (
        offset < (MAX_PAGINAS_SEGURIDAD * incremento_offset)
        and not detener
    ):
        try:
            html = obtener_pagina(offset)
        except Exception as error:
            print(f"    Error descargando el offset {offset}: {error}", flush=True)
            break

        avisos = extraer_avisos_de_pagina(html)

        if not avisos:
            print("    No se han reconocido avisos en este offset. Fin del listado.", flush=True)
            break

        for aviso in avisos:
            fecha = aviso.get("fecha_publicacion")

            if fecha and fecha < desde:
                print(f"    Llegamos a {fecha}, anterior a {desde}. Fin del escaneo.", flush=True)
                detener = True
                continue

            candidatos.append(aviso)

        if detener:
            break

        offset += incremento_offset
        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    print(f"\nAvisos candidatos en la ventana: {len(candidatos)}", flush=True)

    if not candidatos:
        return

    print("\nDescargando ficha de cada candidato (detalle, descripción y documentos)...", flush=True)

    normalizados = []

    for indice, aviso in enumerate(candidatos, start=1):
        print(f"  [{indice}/{len(candidatos)}] {aviso['titulo'][:90]}", flush=True)

        detalle = extraer_detalle_aviso(aviso["url_oficial"])

        aviso["descripcion"] = detalle["descripcion"]
        aviso["categoria"] = detalle["categoria"]
        aviso["url_documento"] = detalle["url_documento"]

        registro = construir_registro(aviso)

        if registro is not None:
            normalizados.append(registro)

        time.sleep(PAUSA_ENTRE_DETALLES_SEGUNDOS)

    # Deduplicar
    normalizados = list(
        {
            n["codigo_unico"]: n
            for n in normalizados
        }.values()
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

    lote_final = preparar_lote_para_subir(
        normalizados,
        registros_existentes
    )

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

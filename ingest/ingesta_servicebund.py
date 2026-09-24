# -*- coding: utf-8 -*-
"""
ingesta_servicebund.py
------------------------
Sincroniza licitaciones (Ausschreibungen) relacionadas con GIZ del
buscador de licitaciones de la administración pública alemana
(service.bund.de -- Bund, Länder y municipios) contra la tabla
`licitaciones_internacionales` de Supabase.

CAMBIO DE ARQUITECTURA DELIBERADO -- FEED RSS OFICIAL EN VEZ DE PLAYWRIGHT
--------------------------------------------------------------------------
service.bund.de ofrece sus resultados de búsqueda como feed RSS nativo
(enlace "Suchergebnis als RSS-Feed" en la propia página de resultados).
Un RSS es XML estructurado y estable -- ni arrastra el ruido visual del
HTML de la tabla, ni depende de clases CSS que puedan cambiar, así que
es la opción más fiable para esta fuente, igual que se decidió usar la
API oficial en vez de Playwright para TED (ver ingesta_ted.py). Por eso
este script tampoco usa Playwright -- solo `requests` y el `xml.etree`
de la librería estándar de Python (sin añadir ninguna dependencia
nueva).

AVISO DE FIABILIDAD -- URL DEL FEED RECONSTRUIDA, NO VERIFICADA EN VIVO
--------------------------------------------------------------------------
No hay salida de red hacia service.bund.de en este entorno de
desarrollo. La URL y sus parámetros de abajo (URL_FEED_RSS) se han
reconstruido a partir de varios feeds RSS reales de service.bund.de ya
en uso (uno de ellos enlazado desde la propia web de la Allianz für
Cyber-Sicherheit alemana), y el formato de cada aviso ("Vergabestelle:
...", "Angebotsfrist: DD.MM.AAAA HH:MM") a partir de fragmentos de texto
real de esos mismos feeds -- son fuentes consistentes entre sí, pero
conviene confirmarlo con una ejecución manual (workflow_dispatch) antes
de fiarse del cron automático.

NOTA sobre el parámetro `nn`: el formulario de búsqueda que se dio como
punto de partida usa `nn=9465610`, pero los feeds RSS reales encontrados
usan `nn=4641482` -- se ha usado este último por tener evidencia directa
de que así se genera un feed RSS real y funcional; si `nn=9465610` es en
realidad el nodo correcto para esta sección concreta, ajústalo aquí.

QUERY
-------
`templateQueryString=GIZ`, igual que el resto de fuentes de este bloque
(TED también busca específicamente avisos de GIZ) -- una búsqueda sin
palabra clave devolvería TODAS las licitaciones públicas alemanas
(cientos), fuera del alcance de este proyecto.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_servicebund.py
Ejecucion programada: ver .github/workflows/sincronizar_servicebund.yml
   (NO necesita Playwright/navegador -- solo requests)
"""
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://www.service.bund.de"
NODO_RSS = "4641482"   # ver aviso de fiabilidad -- distinto del nn=9465610 del formulario de partida
PALABRA_CLAVE = "GIZ"
URL_FEED_RSS = (
    f"{BASE_URL}/Content/DE/Ausschreibungen/Suche/Formular.html"
    f"?nn={NODO_RSS}&sortOrder=dateOfIssue_dt+desc&type=0&resultsPerPage=100"
    f"&templateQueryString={quote(PALABRA_CLAVE)}&jobsrss=true"
)
FUENTE = "Service-Bund"
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "pais", "fecha_publicacion", "fecha_limite")

CABECERAS_PETICION = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# "Angebotsfrist: 27.08.2026 10:30" / "Veröffentlichungsende: 27.08.2026 10:30"
PATRON_VERGABESTELLE = re.compile(r"Vergabestelle:?\s*([^\n\r]+?)(?:\s{2,}|Angebotsfrist|Veröffentlichungsende|$)")
PATRON_ANGEBOTSFRIST = re.compile(r"Angebotsfrist:?\s*(\d{1,2}\.\d{1,2}\.\d{4})(?:\s+\d{1,2}:\d{2})?")


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def _slug_desde_url(url: str) -> str:
    """El último segmento de la URL (normalmente ya es un slug con fecha,
    p. ej. '2026-09-22-giz-beratung.html') en vez de trocear la URL
    entera -- mucho más limpio y sigue siendo único."""
    if not url:
        return "sin-referencia"
    ultimo_segmento = url.rstrip("/").split("/")[-1]
    ultimo_segmento = re.sub(r"\.html?$", "", ultimo_segmento, flags=re.IGNORECASE)
    return _generar_slug(ultimo_segmento)


def parsear_fecha_alemana(texto: str):
    """'27.08.2026' (o con hora detrás, que se ignora) -> date."""
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


def parsear_pubdate_rss(texto: str):
    """<pubDate> de un RSS 2.0 estandar, formato RFC 822."""
    if not texto:
        return None
    try:
        return parsedate_to_datetime(texto).date()
    except (TypeError, ValueError):
        return None


def obtener_items_rss() -> list:
    try:
        respuesta = requests.get(URL_FEED_RSS, timeout=TIMEOUT_PETICION, headers=CABECERAS_PETICION)
        respuesta.raise_for_status()
    except Exception as error:
        print(f"Error descargando el feed RSS: {error}", flush=True)
        return []

    try:
        raiz = ET.fromstring(respuesta.content)
    except ET.ParseError as error:
        print(f"Error interpretando el XML del feed RSS: {error}", flush=True)
        return []

    items = []
    for item in raiz.findall(".//item"):
        items.append({
            "titulo": (item.findtext("title") or "").strip(),
            "url_oficial": (item.findtext("link") or "").strip(),
            "descripcion_raw": item.findtext("description") or "",
            "pub_date_raw": item.findtext("pubDate"),
        })
    return items


def construir_registro(item: dict) -> dict:
    descripcion_raw = item.get("descripcion_raw") or ""

    vergabestelle = None
    coincidencia_vergabestelle = PATRON_VERGABESTELLE.search(descripcion_raw)
    if coincidencia_vergabestelle:
        vergabestelle = coincidencia_vergabestelle.group(1).strip().rstrip(",").strip()

    fecha_limite = None
    coincidencia_frist = PATRON_ANGEBOTSFRIST.search(descripcion_raw)
    if coincidencia_frist:
        fecha_limite = parsear_fecha_alemana(coincidencia_frist.group(1))

    fecha_publicacion = parsear_pubdate_rss(item.get("pub_date_raw"))

    # Alemania es fija para esta fuente (buscador de licitaciones de la
    # administracion PUBLICA alemana) -- no hace falta detectarla.
    pais = "Alemania"

    partes_descripcion = []
    if vergabestelle:
        partes_descripcion.append(f"Vergabestelle (organismo): {vergabestelle}.")
    texto_extra = re.sub(r"\s+", " ", descripcion_raw).strip()
    if texto_extra:
        partes_descripcion.append(texto_extra[:300])
    descripcion = " ".join(partes_descripcion) or None

    return {
        "codigo_unico": f"SERVICEBUND-{_slug_desde_url(item.get('url_oficial')) or _generar_slug(item.get('titulo'))}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": None,
        "titulo": item.get("titulo"),
        "descripcion": descripcion,
        "pais": pais,
        "paises": [pais],
        "organismo": vergabestelle or "Service-Bund",
        "categoria": None,
        "url_oficial": item.get("url_oficial") or None,
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
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - SERVICE-BUND (GIZ, via feed RSS)", flush=True)
    print("=" * 100, flush=True)
    print(f"Feed: {URL_FEED_RSS}", flush=True)

    items = obtener_items_rss()
    print(f"\nTotal avisos recibidos en el feed RSS: {len(items)}", flush=True)

    if not items:
        print(
            "No se ha recibido ningún aviso. Revisa el log de arriba -- lo más probable es que la "
            "URL del feed haya cambiado (ver aviso de fiabilidad en el docstring sobre el parámetro 'nn').",
            flush=True,
        )
        return

    normalizados = [construir_registro(item) for item in items]

    sin_fecha_limite = sum(1 for n in normalizados if not n.get("fecha_limite"))
    if sin_fecha_limite:
        print(
            f"Avisos sin 'Angebotsfrist' reconocible: {sin_fecha_limite}/{len(normalizados)} "
            "-- ver aviso de fiabilidad en el docstring.",
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
    print(f"\nSincronizacion Service-Bund completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

# -*- coding: utf-8 -*-
"""
ingesta_afdb.py
----------------
Sincroniza avisos de contratación del Banco Africano de Desarrollo (AfDB)
directamente contra la tabla `licitaciones_internacionales` de Supabase.

CORRECCIÓN IMPORTANTE respecto a la versión anterior (que solo subía 1
registro): la fuente estaba mal. Antes se scrapeaba
`/en/documents/category/specific-procurement-notices`, filtrando solo
títulos que empezaran por "SPN -"/"GPN -". La página real de referencia
es otra bien distinta:

    https://www.afdb.org/en/projects-and-operations/procurement?page=N

que es un listado MUCHO más grande (>12.000 documentos) con avisos de
tipos muy variados -- AMI (Appel à Manifestation d'Intérêt), AAO (Appel
d'Offres Ouvert), IFB (Invitation For Bids), EOI (Expression Of
Interest), PPM, SPN, GPN... casi ninguno empezaba literalmente por "SPN
-", así que el filtro anterior descartaba casi todo. Aquí se reconoce
CUALQUIER prefijo de 2 a 6 letras mayúsculas seguido de " - país - ..."
(ver PATRON_TIPO_PAIS), y solo se descartan explícitamente los avisos de
RESULTADO/ADJUDICACIÓN ("Attribution de..."/"Contract Award...": ya no
son oportunidades abiertas a las que presentarse).

FECHA DE CIERRE (deadline) -- LÉELO ANTES DE CONFIAR CIEGAMENTE EN EL DATO
-----------------------------------------------------------------------------
El listado y la propia ficha HTML de cada aviso NO traen la fecha de
cierre en ningún campo estructurado -- se comprobó expresamente
descargando una ficha real. La fecha de cierre solo aparece dentro del
PDF (u ocasionalmente DOCX) adjunto a cada aviso, como parte del texto
libre de la convocatoria (p. ej. "...must be submitted by e-mail no
later than 13 March 2026..." o, en francés, "...au plus tard le...").

Por eso, para cada aviso dentro de la ventana de sincronización, este
script:
  1. Descarga la ficha HTML del aviso (para sacar la descripción y la
     URL del documento adjunto).
  2. Si hay un PDF adjunto, lo descarga y busca, en sus primeras
     páginas, alguna de varias frases habituales que anuncian el plazo
     (en inglés, francés y portugués) y, si encuentra una, intenta leer
     una fecha justo después con `dateutil.parser` (que reconoce muchos
     formatos sin tener que listarlos todos a mano).
  3. Si no encuentra nada fiable (o la fecha "encontrada" no tiene
     sentido -- pasada, o a más de 3 años vista), `fecha_limite` se deja
     a NULL en vez de arriesgarse a guardar un dato inventado.

Es una heurística de MEJOR ESFUERZO sobre texto libre en varios idiomas
y formatos de PDF muy distintos entre sí -- no va a acertar siempre.
Se ha probado contra los patrones de frase reales encontrados al buscar
avisos de AfDB ya publicados (ver el módulo de pruebas que se entrega
aparte), pero no hay ninguna garantía de cobertura al 100%.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local:      python ingesta_afdb.py
Ejecución programada: ver .github/workflows/sincronizar_afdb.yml
"""
import io
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://www.afdb.org"
LISTADO_URL = BASE_URL + "/en/projects-and-operations/procurement"

FUENTE = "AfDB"
DIAS_ATRAS = 3                        # "últimos 3 días", tal como se pidió
MAX_PAGINAS_SEGURIDAD = 30             # red de seguridad de paginación
MAX_DETALLES_POR_EJECUCION = 80        # tope de fichas+PDF a procesar por ejecución
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.8
PAUSA_ENTRE_DETALLES_SEGUNDOS = 0.5
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15

CABECERAS = {"User-Agent": "Mozilla/5.0 (compatible; LicitacionesEmpresasBot/1.0)"}

PATRON_FECHA_LISTADO = re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{4}")
# Prefijo de 2 a 6 letras mayúsculas (AMI, AAO, IFB, EOI, PPM, SPN, GPN...)
# seguido de " - país - resto del título".
PATRON_TIPO_PAIS = re.compile(r"^([A-ZÀ-ÖØ-Þ]{2,6})\s*-\s*([^-]+?)\s*-\s*(.+)$")
# Avisos de resultado/adjudicación: ya no son oportunidades abiertas.
PATRON_EXCLUIR = re.compile(r"\b(attribution|contract award|award of contract)\b", re.IGNORECASE)

CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_limite", "url_documento")

# Frases que habitualmente preceden la fecha límite de presentación, en
# los tres idiomas más comunes en avisos de AfDB.
FRASES_DISPARADORAS_FECHA_CIERRE = [
    r"no later than", r"not later than", r"deadline for submission",
    r"submission deadline", r"must be submitted (?:by|no later than)",
    r"closing date", r"deadline:",
    r"au plus tard le", r"date limite de d[eé]p[oô]t", r"date limite",
    r"avant le",
    r"o mais tardar", r"data limite",
]
PATRON_DISPARADOR_FECHA_CIERRE = re.compile("|".join(FRASES_DISPARADORAS_FECHA_CIERRE), re.IGNORECASE)

# dateutil.parser solo reconoce nombres de mes en inglés por defecto (se
# comprobó: "20 novembre 2026" falla con "bad month number 20"). Se
# traducen los meses en francés/portugués -- los otros dos idiomas
# habituales en avisos de AfDB -- antes de intentar el parseo.
MESES_FR_A_EN = {
    "janvier": "January", "février": "February", "fevrier": "February", "mars": "March",
    "avril": "April", "mai": "May", "juin": "June", "juillet": "July",
    "août": "August", "aout": "August", "septembre": "September", "octobre": "October",
    "novembre": "November", "décembre": "December", "decembre": "December",
}
MESES_PT_A_EN = {
    "janeiro": "January", "fevereiro": "February", "março": "March", "marco": "March",
    "abril": "April", "maio": "May", "junho": "June", "julho": "July", "agosto": "August",
    "setembro": "September", "outubro": "October", "novembro": "November", "dezembro": "December",
}


def _traducir_meses(texto: str) -> str:
    for mapa in (MESES_FR_A_EN, MESES_PT_A_EN):
        for mes_local, mes_en in mapa.items():
            texto = re.sub(rf"\b{mes_local}\b", mes_en, texto, flags=re.IGNORECASE)
    return texto


# ------------------------------------------------------------------
# Descarga del listado
# ------------------------------------------------------------------
def obtener_pagina(pagina: int) -> str:
    print(f"--> Descargando página {pagina} del listado de AfDB...", flush=True)
    respuesta = requests.get(
        LISTADO_URL, params={"page": pagina}, timeout=TIMEOUT_PETICION, headers=CABECERAS
    )
    print(f"    HTTP: {respuesta.status_code}", flush=True)
    respuesta.raise_for_status()
    return respuesta.text


def _parsear_fecha_listado(texto_fecha: str):
    try:
        return datetime.strptime(texto_fecha, "%d-%b-%Y").date()
    except ValueError:
        return None


def extraer_avisos_de_pagina(html: str) -> list:
    """
    Para cada enlace real a un aviso (href bajo /en/documents/, que no sea
    un enlace de categoría), busca hacia atrás en el DOM -- no en un
    texto ya aplanado -- el nodo de texto más cercano con forma de fecha
    "DD-Mon-YYYY". Se prefiere esta navegación por el árbol a un regex
    sobre texto plano porque es inmune a que el ÚLTIMO aviso de la
    página "se coma" texto de paginación/pie que venga justo después en
    el flujo de texto (bug real detectado al probar contra HTML
    reconstruido: el último título de cada página perdía el enlace
    porque el texto capturado ya no coincidía con el del <a>).
    """
    soup = BeautifulSoup(html, "html.parser")
    contenedor = soup.find("main") or soup.find(id="content") or soup

    avisos = []
    vistos = set()

    for enlace in contenedor.find_all("a", href=True):
        titulo = enlace.get_text(strip=True)
        href = enlace["href"]

        if not titulo or not href.startswith("/en/documents/") or "/category/" in href:
            continue
        if PATRON_EXCLUIR.search(titulo):
            continue  # aviso de resultado/adjudicación: no es una oportunidad abierta
        if titulo in vistos:
            continue
        vistos.add(titulo)

        fecha_publicacion = None
        nodo_fecha = enlace.find_previous(string=PATRON_FECHA_LISTADO)
        if nodo_fecha:
            coincidencia = PATRON_FECHA_LISTADO.search(str(nodo_fecha))
            if coincidencia:
                fecha_publicacion = _parsear_fecha_listado(coincidencia.group(0))

        avisos.append({
            "titulo": titulo,
            "fecha_publicacion": fecha_publicacion,
            "url_oficial": BASE_URL + href,
        })

    return avisos


# ------------------------------------------------------------------
# Ficha del aviso: descripción + URL del documento adjunto
# ------------------------------------------------------------------
def extraer_descripcion_detalle(soup: BeautifulSoup):
    contenedor = soup.find("main") or soup.find(id="content") or soup
    for parrafo in contenedor.find_all("p"):
        texto = parrafo.get_text(strip=True)
        if len(texto) > 80:  # evita párrafos cortos / boilerplate de menú
            return texto
    return None


def extraer_url_documento(soup: BeautifulSoup):
    contenedor = soup.find("main") or soup.find(id="content") or soup
    for enlace in contenedor.find_all("a", href=True):
        href = enlace["href"]
        if "viewer.html?file=" in href:
            coincidencia = re.search(r"file=([^&]+)", href)
            if coincidencia:
                return unquote(coincidencia.group(1))
        if href.lower().endswith((".pdf", ".docx", ".doc")):
            return href if href.startswith("http") else BASE_URL + href
    return None


def obtener_detalle_aviso(url: str) -> dict:
    try:
        respuesta = requests.get(url, timeout=TIMEOUT_PETICION, headers=CABECERAS)
        respuesta.raise_for_status()
    except Exception as error:
        print(f"      ⚠️ Error descargando la ficha: {error}", flush=True)
        return {"descripcion": None, "url_documento": None}

    soup = BeautifulSoup(respuesta.text, "html.parser")
    return {
        "descripcion": extraer_descripcion_detalle(soup),
        "url_documento": extraer_url_documento(soup),
    }


# ------------------------------------------------------------------
# Fecha de cierre: mejor esfuerzo a partir del PDF adjunto
# ------------------------------------------------------------------
def extraer_texto_pdf(url_pdf: str) -> str:
    import pdfplumber

    try:
        respuesta = requests.get(url_pdf, timeout=TIMEOUT_PETICION, headers=CABECERAS)
        respuesta.raise_for_status()
        with pdfplumber.open(io.BytesIO(respuesta.content)) as pdf:
            paginas = pdf.pages[:3]  # el plazo casi siempre se menciona en las primeras páginas
            return "\n".join((p.extract_text() or "") for p in paginas)
    except Exception as error:
        print(f"      ⚠️ No se pudo leer el PDF adjunto: {error}", flush=True)
        return ""


def extraer_fecha_cierre_de_texto(texto: str):
    """
    Heurística de mejor esfuerzo (ver advertencia en el docstring del
    módulo): busca una frase disparadora y, si la encuentra, intenta leer
    una fecha en los ~60 caracteres siguientes. Se descarta cualquier
    fecha que no caiga entre hoy y (hoy + 3 años): mejor no guardar nada
    que guardar una fecha sin sentido por una mala lectura del PDF.
    """
    if not texto:
        return None

    hoy = date.today()
    limite_superior = hoy.replace(year=hoy.year + 3)

    for coincidencia in PATRON_DISPARADOR_FECHA_CIERRE.finditer(texto):
        fragmento = _traducir_meses(texto[coincidencia.end():coincidencia.end() + 60])
        try:
            fecha = dateutil_parser.parse(fragmento, fuzzy=True, dayfirst=True)
        except (ValueError, OverflowError, TypeError):
            continue
        if hoy <= fecha.date() <= limite_superior:
            return fecha.date()

    return None


def obtener_fecha_cierre(url_documento: str):
    if not url_documento or not url_documento.lower().endswith(".pdf"):
        return None  # de momento solo se procesan PDF (el formato más habitual con diferencia)
    texto = extraer_texto_pdf(url_documento)
    return extraer_fecha_cierre_de_texto(texto)


# ------------------------------------------------------------------
# Normalización al esquema de `licitaciones_internacionales`
# ------------------------------------------------------------------
def construir_registro(aviso: dict) -> dict:
    coincidencia = PATRON_TIPO_PAIS.match(aviso["titulo"])
    if coincidencia:
        tipo_aviso, pais, _resto = coincidencia.groups()
        pais = pais.strip()
    else:
        tipo_aviso, pais = None, None

    slug = aviso["url_oficial"].rstrip("/").split("/")[-1]
    fecha_publicacion = aviso.get("fecha_publicacion")
    fecha_limite = aviso.get("fecha_limite")

    return {
        "codigo_unico": f"AFDB-{slug}",
        "fuente_origen": FUENTE,
        "tipo_aviso": tipo_aviso,
        "titulo": aviso["titulo"],
        "descripcion": aviso.get("descripcion"),
        "pais": pais,
        "organismo": None,   # no disponible de forma fiable con este listado
        "categoria": None,   # no disponible de forma fiable con este listado
        "url_oficial": aviso["url_oficial"],
        "url_documento": aviso.get("url_documento"),
        "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
    }


# ------------------------------------------------------------------
# Decidir qué subir
# ------------------------------------------------------------------
def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
        existente = registros_existentes.get(datos["codigo_unico"])
        texto_completo = (
            f"Título: {datos['titulo']}\n"
            f"{datos.get('descripcion') or ''}\n"
            f"País: {datos.get('pais') or 'No especificado'}"
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


# ------------------------------------------------------------------
# Ejecución principal
# ------------------------------------------------------------------
def ejecutar_sincronizacion():
    hoy = date.today()
    desde = hoy - timedelta(days=DIAS_ATRAS)

    print("=" * 100, flush=True)
    print("SINCRONIZACIÓN DE LICITACIONES INTERNACIONALES — AfDB", flush=True)
    print("=" * 100, flush=True)
    print(f"Ventana: {desde} .. {hoy}", flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)

    candidatos = []
    pagina = 0
    detener = False

    while pagina < MAX_PAGINAS_SEGURIDAD and not detener:
        try:
            html = obtener_pagina(pagina)
        except Exception as error:
            print(f"    ⚠️ Error descargando la página {pagina}: {error}", flush=True)
            break

        avisos = extraer_avisos_de_pagina(html)
        if not avisos:
            print("    No se han reconocido avisos en esta página (¿cambió el diseño de la web?). Fin.", flush=True)
            break

        for aviso in avisos:
            fecha = aviso.get("fecha_publicacion")
            if fecha and fecha < desde:
                print(f"    Llegamos a {fecha}, anterior a {desde}. Fin del escaneo.", flush=True)
                detener = True
                continue
            candidatos.append(aviso)

        pagina += 1
        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    print(f"\nAvisos candidatos en la ventana: {len(candidatos)}", flush=True)

    if not candidatos:
        return

    if len(candidatos) > MAX_DETALLES_POR_EJECUCION:
        print(
            f"⚠️ Hay más candidatos ({len(candidatos)}) que el tope por ejecución "
            f"({MAX_DETALLES_POR_EJECUCION}); se procesan los más recientes y el resto "
            f"se recogerá en la siguiente sincronización.",
            flush=True,
        )
        candidatos = candidatos[:MAX_DETALLES_POR_EJECUCION]

    print("\nDescargando ficha + documento adjunto de cada candidato (para la fecha de cierre)...", flush=True)
    normalizados = []
    for indice, aviso in enumerate(candidatos, start=1):
        print(f"  [{indice}/{len(candidatos)}] {aviso['titulo'][:90]}", flush=True)

        detalle = obtener_detalle_aviso(aviso["url_oficial"])
        aviso["descripcion"] = detalle["descripcion"]
        aviso["url_documento"] = detalle["url_documento"]
        aviso["fecha_limite"] = obtener_fecha_cierre(detalle["url_documento"])

        normalizados.append(construir_registro(aviso))
        time.sleep(PAUSA_ENTRE_DETALLES_SEGUNDOS)

    con_fecha_limite = sum(1 for n in normalizados if n["fecha_limite"])
    print(
        f"\nFecha de cierre detectada en {con_fecha_limite}/{len(normalizados)} avisos "
        f"(el resto queda sin fecha_limite -- ver advertencia en el docstring del módulo).",
        flush=True,
    )

    normalizados = list({n["codigo_unico"]: n for n in normalizados}.values())

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
        supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
    )
    print(f"\nSincronización AfDB completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

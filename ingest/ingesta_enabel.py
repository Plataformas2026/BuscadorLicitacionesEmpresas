# -*- coding: utf-8 -*-
"""
ingesta_enabel.py
-----------------
Sincroniza las licitaciones ABIERTAS de Enabel (Agencia Belga de Cooperación
Internacional) contra la tabla `licitaciones_internacionales` de Supabase,
mediante scraping directo del HTML (requests + BeautifulSoup, sin Playwright).

    Listado:  https://www.enabel.be/public-procurement/

PARTICULARIDADES DEL PORTAL (a diferencia de AFD / service.bund.de):
--------------------------------------------------------------------------
- NO existe ficha de detalle por licitación. Todo el contenido (referencia,
  título, país, fecha de cierre, estado, legislación, descripción y
  adjuntos) está ya en las tarjetas `div.card--tenders` del propio listado,
  de modo que no se descarga ninguna ficha adicional.
- El listado NO publica fecha de publicación. Para que el orden por
  `fecha_publicacion DESC` de la app no deje estas filas al principio (en
  Postgres, NULL va primero en orden descendente), se guarda como
  `fecha_publicacion` la fecha en que el script detecta la licitación por
  primera vez; en ejecuciones posteriores se CONSERVA la ya guardada.
- Como no hay URL propia por licitación, `url_oficial` apunta al listado con
  la referencia como fragmento (`#<referencia>`), y `url_documento` es el
  primer adjunto (normalmente el pliego en PDF).
- El portal tiene ~1.800 licitaciones (la mayoría cerradas). El script usa
  el filtro del propio formulario (`is_status=0` = Open) para recorrer solo
  las abiertas, y además valida cada tarjeta (estado "Open" y fecha de cierre
  no vencida) por si el filtro no se aplicara en alguna página.

Estrategia (igual que AFD): se recorre el listado de abiertas, se comparan
con lo ya existente en Supabase y solo se generan embeddings / suben las
NUEVAS o las que han CAMBIADO (p. ej. un addendum que amplía la fecha de
cierre, `es_actualizada=True`).

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local o programada: python ingesta_enabel.py
"""
import hashlib
import re
import time
from datetime import date
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://www.enabel.be"
LISTADO_URL = BASE_URL + "/public-procurement/"

FUENTE = "Enabel"
ORGANISMO = "Enabel - Belgian Agency for International Cooperation"
TIPO_AVISO = "Public procurement"

# Mismos nombres de campo que el formulario del listado (GET). is_status: 0 = Open, 1 = Close.
PARAMS_LISTADO = {
    "in_category[]": "all",
    "in_country": "all",
    "is_status": "0",
}

MAX_PAGINAS_SEGURIDAD = 40
MAX_PAGINAS_SEGUIDAS_SIN_ABIERTAS = 2  # corte de seguridad si el filtro de estado no se aplicara
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.5
TIMEOUT_CONEXION = 10
TIMEOUT_LECTURA = 30
MAX_REINTENTOS_PETICION = 3
LOTE_ENVIO_SUPABASE = 15
CAPTURA_DEPURACION = "debug_enabel_listado.html"

CABECERAS_PETICION = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

MESES_INGLES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Etiquetas ("<strong>Etiqueta :</strong>") que reconoce el parser de tarjetas.
ETIQUETAS_CONOCIDAS = {
    "country": "pais",
    "closing date": "cierre",
    "status": "estado",
    "applicable legislation": "legislacion",
    "description": "descripcion",
    "attachments": "adjuntos",
}

PATRON_ETIQUETA = re.compile(r"^\s*(.+?)\s*:\s*$")
PATRON_FECHA_CIERRE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})")
# Referencia (con algún dígito, sin espacios) + guion/raya con espacios + título.
PATRON_REFERENCIA_TITULO = re.compile(r"^\s*(?P<ref>(?=\S*\d)[^\s\u2013\u2014]{3,60})\s+[\u2013\u2014-]\s+(?P<titulo>.+)$", re.DOTALL)

# Parámetros de seguimiento (Google Linker) que el navegador añade a los enlaces.
PARAMS_SEGUIMIENTO_PREFIJOS = ("_gl", "_ga", "utm_")


# ============================================================
# HTTP
# ============================================================

def _crear_sesion_http() -> requests.Session:
    sesion = requests.Session()
    estrategia_reintento = Retry(
        total=2,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adaptador = HTTPAdapter(max_retries=estrategia_reintento)
    sesion.mount("https://", adaptador)
    sesion.mount("http://", adaptador)
    sesion.headers.update(CABECERAS_PETICION)
    return sesion


SESION_HTTP = _crear_sesion_http()


def _peticion_con_reintentos(url: str, params: dict = None, max_intentos: int = MAX_REINTENTOS_PETICION):
    """Devuelve la respuesta, o None si falla. Un 404 (página fuera de rango) no se reintenta."""
    for intento in range(1, max_intentos + 1):
        try:
            respuesta = SESION_HTTP.get(url, params=params, timeout=(TIMEOUT_CONEXION, TIMEOUT_LECTURA))
            if respuesta.status_code == 404:
                return None
            respuesta.raise_for_status()
            return respuesta
        except requests.exceptions.RequestException as error:
            if intento < max_intentos:
                espera = intento * 3
                print(
                    f"    Aviso: fallo de red (intento {intento}/{max_intentos}) en "
                    f"{url[:90]}: {error} -- reintentando en {espera}s...",
                    flush=True,
                )
                time.sleep(espera)
            else:
                print(f"    Error definitivo tras {max_intentos} intentos en {url[:90]}: {error}", flush=True)
    return None


# ============================================================
# UTILIDADES DE TEXTO / FECHAS
# ============================================================

def _limpiar_texto(texto: str):
    if not texto:
        return None
    texto = texto.replace("\u00ad", "").replace("\xa0", " ")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def _generar_slug(texto: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (texto or "").strip()).strip("-").upper()
    return slug[:120]


def parsear_fecha_cierre(texto: str):
    """'23 October 2026 13:00' -> date(2026, 10, 23). Devuelve None si no se reconoce."""
    if not texto:
        return None
    coincidencia = PATRON_FECHA_CIERRE.search(texto)
    if not coincidencia:
        return None
    dia, nombre_mes, anio = coincidencia.groups()
    mes = MESES_INGLES.get(nombre_mes.lower())
    if not mes:
        return None
    try:
        return date(int(anio), mes, int(dia))
    except ValueError:
        return None


def separar_referencia_y_titulo(texto: str):
    """'BDI25003-10004 – Public work contract...' -> ('BDI25003-10004', 'Public work contract...')."""
    texto = _limpiar_texto(texto)
    if not texto:
        return None, None
    coincidencia = PATRON_REFERENCIA_TITULO.match(texto)
    if coincidencia:
        return coincidencia.group("ref"), _limpiar_texto(coincidencia.group("titulo"))
    return None, texto


def _limpiar_url_adjunto(href: str):
    href = (href or "").strip()
    if not href:
        return None
    url = urljoin(BASE_URL + "/", href)
    partes = urlparse(url)
    consulta = [
        (k, v) for k, v in parse_qsl(partes.query, keep_blank_values=True)
        if not k.startswith(PARAMS_SEGUIMIENTO_PREFIJOS)
    ]
    return urlunparse(partes._replace(query=urlencode(consulta)))


# ============================================================
# PARSER DE TARJETAS
# ============================================================

def _iterar_nodos_tarjeta(contenedor: Tag):
    """Recorre en orden los nodos de `.news__botton`, descendiendo en `.hidden__card`."""
    for nodo in contenedor.children:
        if isinstance(nodo, Tag) and "hidden__card" in (nodo.get("class") or []):
            yield from _iterar_nodos_tarjeta(nodo)
        else:
            yield nodo


def parsear_tarjeta(tarjeta: Tag):
    """
    Convierte una `div.card--tenders` en un dict con los campos crudos.
    Devuelve None si la tarjeta no tiene título.
    """
    contenedor = tarjeta.select_one("div.news__botton") or tarjeta

    span_titulo = contenedor.select_one("p.h5 span") or contenedor.select_one("p.h5")
    referencia, titulo = separar_referencia_y_titulo(span_titulo.get_text(" ", strip=True) if span_titulo else "")
    if not titulo:
        return None

    campos = {clave: [] for clave in ETIQUETAS_CONOCIDAS.values()}
    adjuntos = []
    etiqueta_actual = None

    for nodo in _iterar_nodos_tarjeta(contenedor):
        if isinstance(nodo, NavigableString):
            texto = _limpiar_texto(str(nodo))
            if texto and etiqueta_actual:
                campos[etiqueta_actual].append(texto)
            continue
        if not isinstance(nodo, Tag):
            continue
        if "h5" in (nodo.get("class") or []):
            continue  # el título ya se ha procesado

        strong = nodo.find("strong") if nodo.name == "p" else None
        etiqueta_detectada = None
        if strong is not None:
            coincidencia = PATRON_ETIQUETA.match(strong.get_text(" ", strip=True))
            if coincidencia:
                etiqueta_detectada = ETIQUETAS_CONOCIDAS.get(coincidencia.group(1).lower())

        if etiqueta_detectada:
            etiqueta_actual = etiqueta_detectada
            resto = nodo.get_text(" ", strip=True).replace(strong.get_text(" ", strip=True), "", 1)
            resto = _limpiar_texto(resto)
            if resto:
                campos[etiqueta_actual].append(resto)
            continue

        # Contenido que continúa la etiqueta anterior (descripción, adjuntos...)
        if etiqueta_actual == "adjuntos":
            for enlace in nodo.find_all("a", href=True) if nodo.name != "a" else [nodo]:
                url = _limpiar_url_adjunto(enlace.get("href"))
                if url and url not in adjuntos:
                    adjuntos.append(url)
        elif etiqueta_actual:
            texto = _limpiar_texto(nodo.get_text(" ", strip=True))
            if texto:
                campos[etiqueta_actual].append(texto)

    def _valor(clave: str):
        return _limpiar_texto(" ".join(campos[clave]))

    return {
        "referencia": referencia,
        "titulo": titulo,
        "pais": _valor("pais"),
        "estado": _valor("estado"),
        "fecha_limite": parsear_fecha_cierre(_valor("cierre")),
        "legislacion": _valor("legislacion"),
        "descripcion": _valor("descripcion"),
        "adjuntos": adjuntos,
    }


def extraer_tarjetas(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    resultado = []
    for tarjeta in soup.select("div.card--tenders"):
        datos = parsear_tarjeta(tarjeta)
        if datos:
            resultado.append(datos)
    return resultado


def _es_abierta(datos: dict, hoy: date) -> bool:
    estado = (datos.get("estado") or "").strip().lower()
    if estado and not estado.startswith("open"):
        return False
    fecha_limite = datos.get("fecha_limite")
    if fecha_limite and fecha_limite < hoy:
        return False
    return True


# ============================================================
# CAMPOS COMPARABLES
# ============================================================

CAMPOS_COMPARABLES = (
    "titulo",
    "descripcion",
    "pais",
    "url_documento",
    "fecha_limite",
)


# ============================================================
# DESCARGA DEL LISTADO
# ============================================================

def extraer_licitaciones_abiertas(hoy: date) -> list:
    encontradas = {}
    paginas_seguidas_sin_abiertas = 0

    for numero_pagina in range(1, MAX_PAGINAS_SEGURIDAD + 1):
        url = LISTADO_URL if numero_pagina == 1 else f"{LISTADO_URL}page/{numero_pagina}/"
        print(f"--> Descargando listado de Enabel (página {numero_pagina})...", flush=True)

        respuesta = _peticion_con_reintentos(url, params=PARAMS_LISTADO)
        if respuesta is None:
            print(f"    Sin respuesta para la página {numero_pagina} (fin del listado o error de red). Se detiene.", flush=True)
            break

        if numero_pagina == 1:
            try:
                with open(CAPTURA_DEPURACION, "w", encoding="utf-8") as f:
                    f.write(respuesta.text)
            except Exception:
                pass

        tarjetas = extraer_tarjetas(respuesta.text)
        print(f"    Tarjetas en la página: {len(tarjetas)}", flush=True)
        if not tarjetas:
            print("    No se encontraron más tarjetas. Fin del listado.", flush=True)
            break

        abiertas_en_pagina = 0
        nuevas_en_pagina = 0
        for datos in tarjetas:
            if not _es_abierta(datos, hoy):
                continue
            abiertas_en_pagina += 1

            if datos["referencia"]:
                codigo = f"ENABEL-{_generar_slug(datos['referencia'])}"
            else:
                huella = hashlib.md5(f"{datos['titulo']}|{datos.get('pais') or ''}".encode("utf-8")).hexdigest()[:12]
                codigo = f"ENABEL-T-{huella}"
            datos["codigo_unico"] = codigo[:150]

            if codigo in encontradas:
                continue
            encontradas[codigo] = datos
            nuevas_en_pagina += 1

        print(f"    Abiertas vigentes: {abiertas_en_pagina} (nuevas en el recorrido: {nuevas_en_pagina})", flush=True)

        if abiertas_en_pagina == 0:
            paginas_seguidas_sin_abiertas += 1
            if paginas_seguidas_sin_abiertas >= MAX_PAGINAS_SEGUIDAS_SIN_ABIERTAS:
                print("    Varias páginas seguidas sin licitaciones abiertas. Fin del escaneo.", flush=True)
                break
        else:
            paginas_seguidas_sin_abiertas = 0

        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    return list(encontradas.values())


# ============================================================
# CONSTRUIR REGISTRO
# ============================================================

def construir_registro(datos: dict, hoy: date) -> dict:
    pais = datos.get("pais")
    referencia = datos.get("referencia")
    fecha_limite = datos.get("fecha_limite")
    adjuntos = datos.get("adjuntos") or []

    ancla = _generar_slug(referencia) if referencia else datos["codigo_unico"]

    return {
        "codigo_unico": datos["codigo_unico"],
        "fuente_origen": FUENTE,
        "tipo_aviso": TIPO_AVISO,
        "titulo": datos["titulo"],
        "descripcion": datos.get("descripcion"),
        "pais": pais,
        "paises": [pais] if pais else [],
        "organismo": ORGANISMO,
        "categoria": None,
        "url_oficial": f"{LISTADO_URL}#{ancla}",
        "url_documento": adjuntos[0] if adjuntos else None,
        # El portal no publica fecha de publicación: se usa la de primera detección
        # (se conserva la ya guardada en ejecuciones posteriores; ver preparar_lote_para_subir).
        "fecha_publicacion": hoy.isoformat(),
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
        "_referencia": referencia,
    }


# ============================================================
# DECIDIR QUE SUBIR
# ============================================================

def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []

    for datos in normalizados:
        referencia = datos.pop("_referencia", None)
        existente = registros_existentes.get(datos["codigo_unico"])

        texto_completo = (
            f"Titulo: {datos['titulo']}\n"
            f"{datos.get('descripcion') or ''}\n"
            f"Referencia: {referencia or 'No especificada'}\n"
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
            str(existente.get(campo)) != str(datos.get(campo))
            for campo in CAMPOS_COMPARABLES
        )

        if not ha_cambiado:
            continue

        # Se conserva la fecha de primera detección ya guardada.
        if existente.get("fecha_publicacion"):
            datos["fecha_publicacion"] = existente["fecha_publicacion"]

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

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - ENABEL", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)
    print(f"Solo licitaciones abiertas con cierre >= {hoy}", flush=True)

    crudas = extraer_licitaciones_abiertas(hoy)
    print(f"\nLicitaciones abiertas detectadas: {len(crudas)}", flush=True)

    if not crudas:
        print(
            "No se ha detectado ninguna licitación abierta. "
            f"Revisa el log de arriba y, si existe, {CAPTURA_DEPURACION}.",
            flush=True,
        )
        return

    normalizados = [construir_registro(d, hoy) for d in crudas]
    normalizados = [n for n in normalizados if n.get("titulo") and n.get("url_oficial")]

    supabase = obtener_cliente_supabase()

    print("\nComparando con lo ya existente en Supabase...", flush=True)
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico", "fecha_publicacion") + CAMPOS_COMPARABLES,
        claves=[n["codigo_unico"] for n in normalizados],
    )
    print(f"Ya existentes en Supabase: {len(registros_existentes)}/{len(normalizados)}", flush=True)

    lote_final = preparar_lote_para_subir(normalizados, registros_existentes)

    if not lote_final:
        print("No hay licitaciones nuevas ni cambios que sincronizar.", flush=True)
        return

    nuevas = sum(1 for d in lote_final if d["es_novedad"])
    print(f"A subir: {len(lote_final)} ({nuevas} nuevas, {len(lote_final) - nuevas} actualizadas)", flush=True)

    subidas = subir_en_lotes(
        supabase,
        "licitaciones_internacionales",
        "codigo_unico",
        lote_final,
        tamano_lote=LOTE_ENVIO_SUPABASE,
    )

    print(f"\nSincronización Enabel completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

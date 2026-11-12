# -*- coding: utf-8 -*-
"""
ingesta_luxdev.py
-----------------
Sincroniza las LICITACIONES (Call for tenders) de LuxDev (Agencia luxemburguesa
de cooperación al desarrollo) contra la tabla `licitaciones_internacionales`
de Supabase, mediante scraping directo del HTML (requests + BeautifulSoup,
sin Playwright).

    Listado:  https://luxdev.lu/en/tenders/call-tenders         (?page=N, base 0)
    Ficha:    https://luxdev.lu/en/tenders/call-tenders/<slug>

QUÉ SON ESTOS REGISTROS:
--------------------------------------------------------------------------
Cada tarjeta del listado (`li.views-row`) es un anuncio de licitación con:
estado (Open / Forecast / Closed), región, título (TRUNCADO con "..." en el
listado), referencia (p. ej. A-LAO037-2026-0386), tipo de contrato
(Works / Goods / Services) y fecha límite ("Deadline: dd.mm.aaaa").
- `codigo_unico` = "LUXDEV-<referencia>" (si no hubiera referencia, el slug).
- `tipo_aviso` = "Call for tenders - <estado>" (Open / Forecast).
- `categoria` = tipo de contrato (Works / Goods / Services).
- `fecha_limite` = fecha "Deadline" real del anuncio (ISO). Si no la hay, NULL
  (la app trata NULL como siempre vigente, ver app/search.py).
- Las licitaciones "Closed" se omiten (ver ESTADOS_EXCLUIDOS).
- `fecha_publicacion` = fecha de primera detección: ni el listado ni la ficha
  publican fecha de publicación (con NULL, el orden DESC de la app los pondría
  los primeros). Se conserva en ejecuciones posteriores.

Estrategia (igual que antes): se recorre el listado completo y, para cada
licitación, se descarga su ficha (peticiones pequeñas, con pausa) de donde salen
el título completo, la región, el estado, la fecha límite, la referencia, el
tipo de contrato, el procedimiento, el documento (aviso de licitación en PDF)
y el proyecto relacionado. Se compara con lo ya existente y solo se calcula el
embedding y se sube lo NUEVO o lo que ha CAMBIADO (estado, título, tipo,
región, descripción, documento o fecha límite). Si la ficha de una licitación ya
existente no se puede descargar en una ejecución, se conservan sus datos
guardados (título, descripción y documento) y no se marca como cambiada.

La ficha no contiene texto libre descriptivo: `descripcion` se compone solo con
los datos estructurados reales de la ficha (referencia, tipo de contrato,
procedimiento, estado, fecha límite, nombre del aviso y proyecto relacionado).
`url_documento` = PDF del aviso de licitación.

Los registros "LUXDEV-<slug>" de proyectos cargados por la versión anterior de
este script no se tocan (códigos distintos).

VALIDACIÓN: el parser del listado y el de la ficha se han probado contra el HTML
real de https://luxdev.lu/en/tenders/call-tenders y de la ficha
/en/tenders/call-tenders/renovation-works-faculty-law-and-political-science-national-university-lao

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local o programada: python ingesta_luxdev.py
"""
import re
import time
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://luxdev.lu"
LISTADO_URL = BASE_URL + "/en/tenders/call-tenders"

FUENTE = "LuxDev"
ORGANISMO = "LuxDev - Luxembourg Development Cooperation Agency"

# Estados del listado (en minúsculas). Closed se omite.
ESTADOS_EXCLUIDOS = {"closed"}

MAX_PAGINAS_SEGURIDAD = 40
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.5
PAUSA_ENTRE_FICHAS_SEGUNDOS = 0.4
TIMEOUT_CONEXION = 10
TIMEOUT_LECTURA = 30
MAX_REINTENTOS_PETICION = 3
LOTE_ENVIO_SUPABASE = 15
CAPTURA_DEPURACION = "debug_luxdev_listado.html"

CABECERAS_PETICION = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# Campos que, si cambian en una licitación ya existente, la marcan como actualizada.
CAMPOS_COMPARABLES = (
    "titulo",
    "tipo_aviso",
    "pais",
    "categoria",
    "descripcion",
    "url_documento",
    "fecha_limite",
)


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
    """Devuelve la respuesta, o None si falla. Un 404 no se reintenta."""
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
# UTILIDADES DE TEXTO
# ============================================================

def _limpiar_texto(texto: str):
    if not texto:
        return None
    texto = texto.replace("\u00ad", "").replace("\xa0", " ")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def _slug_de_url(url: str) -> str:
    ruta = urlparse(url).path.rstrip("/")
    slug = ruta.split("/")[-1] if ruta else ""
    slug = re.sub(r"[^A-Za-z0-9\-_]+", "-", slug).strip("-").lower()
    return (slug or "sin-slug")[:120]


# ============================================================
# PARSER DEL LISTADO
# ============================================================

def _parsear_fecha(texto: str):
    """'dd.mm.aaaa' (formato de LuxDev) -> 'aaaa-mm-dd'. None si no hay fecha válida."""
    if not texto:
        return None
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", texto)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def extraer_proyectos_de_pagina(html: str) -> list:
    """
    Cada licitación es un `li.views-row` con un único <a> que contiene:
      - <header>: <span>Estado</span><span punto/><span>Región</span>
      - <h3>:     título (truncado con "..." si es largo)
      - <footer>: <span>Referencia</span> . <span>Tipo de contrato</span> .
                  <span>Deadline: dd.mm.aaaa</span>
    (Se mantiene el nombre de la función por compatibilidad; devuelve licitaciones.)
    """
    soup = BeautifulSoup(html, "html.parser")
    licitaciones = []

    for li in soup.select("li.views-row"):
        enlace = li.find("a", href=True)
        titulo_tag = enlace.find("h3") if enlace else None
        if not enlace or not titulo_tag:
            continue

        titulo = _limpiar_texto(titulo_tag.get_text(" ", strip=True))
        if not titulo:
            continue

        estado = region = referencia = tipo_contrato = fecha_limite = None

        cabecera = enlace.find("header")
        if cabecera is not None:
            textos = [_limpiar_texto(s.get_text(" ", strip=True)) for s in cabecera.find_all("span")]
            textos = [t for t in textos if t]
            if textos:
                estado = textos[0]
            if len(textos) > 1:
                region = textos[1]

        pie = enlace.find("footer")
        if pie is not None:
            textos = [_limpiar_texto(s.get_text(" ", strip=True)) for s in pie.find_all("span")]
            textos = [t for t in textos if t]
            for t in textos:
                if t.lower().startswith("deadline"):
                    fecha_limite = _parsear_fecha(t)
                elif referencia is None and re.match(r"^[A-Z]-[A-Z0-9]+(-[A-Z0-9]+)+$", t):
                    referencia = t
                elif tipo_contrato is None:
                    tipo_contrato = t

        url = urljoin(BASE_URL + "/", enlace["href"]).split("#")[0]
        clave = _slug_de_url(url) if not referencia else re.sub(r"[^A-Za-z0-9\-_]+", "-", referencia)

        licitaciones.append({
            "titulo": titulo,
            "estado": estado,
            "region": region,
            "referencia": referencia,
            "tipo_contrato": tipo_contrato,
            "fecha_limite": fecha_limite,
            "url_oficial": url,
            "codigo_unico": f"LUXDEV-{clave}"[:150],
        })

    return licitaciones


def extraer_proyectos_listado() -> list:
    encontrados = {}

    for numero_pagina in range(0, MAX_PAGINAS_SEGURIDAD):
        print(f"--> Descargando listado de LuxDev (página {numero_pagina})...", flush=True)
        respuesta = _peticion_con_reintentos(LISTADO_URL, params={"page": numero_pagina})
        if respuesta is None:
            print(f"    Sin respuesta para la página {numero_pagina}. Se detiene.", flush=True)
            break

        if numero_pagina == 0:
            try:
                with open(CAPTURA_DEPURACION, "w", encoding="utf-8") as f:
                    f.write(respuesta.text)
            except Exception:
                pass

        proyectos = extraer_proyectos_de_pagina(respuesta.text)
        print(f"    Licitaciones en la página: {len(proyectos)}", flush=True)
        if not proyectos:
            print("    No se encontraron más licitaciones. Fin del listado.", flush=True)
            break

        nuevos = 0
        for p in proyectos:
            if p["codigo_unico"] not in encontrados:
                encontrados[p["codigo_unico"]] = p
                nuevos += 1

        if nuevos == 0:
            print("    La página no aporta licitaciones nuevas (paginación repetida). Fin del listado.", flush=True)
            break

        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    todos = list(encontrados.values())
    vigentes = [
        p for p in todos
        if (p.get("estado") or "").strip().lower() not in ESTADOS_EXCLUIDOS
    ]
    print(f"\nLicitaciones en el listado: {len(todos)} | no cerradas: {len(vigentes)}", flush=True)
    return vigentes


# ============================================================
# FICHA DE DETALLE
# ============================================================

def _valor_etiqueta(contenedor, etiqueta: str):
    """Texto del bloque cuyo <h3> es `etiqueta` (sin el propio <h3>), o None.
    Cada dato de la ficha es un <div> con <h3>etiqueta</h3> + valor."""
    if contenedor is None:
        return None
    for h3 in contenedor.find_all("h3"):
        if (_limpiar_texto(h3.get_text(" ", strip=True)) or "").lower() == etiqueta.lower():
            bloque = h3.parent
            if bloque is None:
                return None
            partes = []
            for hijo in bloque.children:
                if hijo is h3:
                    continue
                texto = hijo.get_text(" ", strip=True) if hasattr(hijo, "get_text") else str(hijo)
                texto = _limpiar_texto(texto)
                if texto:
                    partes.append(texto)
            return " ".join(partes) or None
    return None


def parsear_ficha(html: str) -> dict:
    """Extrae de la ficha: titulo, estado, region(es), fecha_limite, referencia,
    tipo_contrato, procedimiento, documento y proyecto relacionado, y compone
    `descripcion` solo con esos datos reales."""
    soup = BeautifulSoup(html, "html.parser")
    articulo = soup.select_one("article.node--type-tender") or soup.find("article") or soup
    preview = articulo.select_one("section#preview")

    h1 = articulo.find("h1")
    titulo = _limpiar_texto(h1.get_text(" ", strip=True)) if h1 else None

    estado = _valor_etiqueta(preview, "Status")
    referencia = _valor_etiqueta(preview, "Reference")
    tipo_contrato = _valor_etiqueta(preview, "Type of contract")
    procedimiento = _valor_etiqueta(preview, "Procurement procedure")
    texto_limite = _valor_etiqueta(preview, "Deadline")
    fecha_limite = _parsear_fecha(texto_limite)

    # Regiones de intervención (puede haber varias)
    regiones = []
    if preview is not None:
        for h3 in preview.find_all("h3"):
            if (_limpiar_texto(h3.get_text(" ", strip=True)) or "").lower().startswith("region"):
                span = h3.find_next("span", class_=re.compile(r"font-primary"))
                nombre = _limpiar_texto(span.get_text(" ", strip=True)) if span else None
                if nombre and nombre not in regiones:
                    regiones.append(nombre)

    # Documento (aviso de licitación) y su nombre
    url_documento = None
    nombre_documento = None
    for a in (preview.find_all_next("a", href=True) if preview is not None else []):
        if ".pdf" in a["href"].lower():
            url_documento = urljoin(BASE_URL + "/", a["href"])
            candidatos = [
                _limpiar_texto(s.get_text(" ", strip=True))
                for s in a.find_all("span", class_=re.compile(r"font-bold"))
            ]
            candidatos = [c for c in candidatos if c and c.lower() != "document"]
            nombre_documento = candidatos[-1] if candidatos else None
            break

    # Proyecto relacionado (primer elemento de la sección "Related project";
    # no se confunde con "Explore more", que son otras licitaciones)
    proyecto_relacionado = None
    for h2 in articulo.find_all("h2"):
        if (_limpiar_texto(h2.get_text(" ", strip=True)) or "").lower() == "related project":
            h3 = h2.find_next("h3")
            if h3:
                proyecto_relacionado = _limpiar_texto(h3.get_text(" ", strip=True))
            break

    lineas = []
    if referencia:
        lineas.append(f"Reference: {referencia}")
    if tipo_contrato:
        lineas.append(f"Type of contract: {tipo_contrato}")
    if procedimiento:
        lineas.append(f"Procurement procedure: {procedimiento}")
    if estado:
        lineas.append(f"Status: {estado}")
    if fecha_limite:
        lineas.append(f"Deadline: {fecha_limite}")
    if nombre_documento:
        lineas.append(f"Notice: {nombre_documento}")
    if proyecto_relacionado:
        lineas.append(f"Related project: {proyecto_relacionado}")

    return {
        "titulo": titulo,
        "estado": estado,
        "referencia": referencia,
        "tipo_contrato": tipo_contrato,
        "fecha_limite": fecha_limite,
        "descripcion": "\n".join(lineas) if lineas else None,
        "url_documento": url_documento,
        "regiones": regiones,
    }


def obtener_ficha(url: str):
    """Descarga y parsea la ficha. None si no se puede descargar."""
    respuesta = _peticion_con_reintentos(url)
    if respuesta is None:
        return None
    try:
        return parsear_ficha(respuesta.text)
    except Exception as error:
        print(f"    Aviso: error al parsear la ficha {url[:90]}: {error}", flush=True)
        return None


# ============================================================
# CONSTRUIR REGISTRO
# ============================================================

def construir_registro(p: dict, hoy: date) -> dict:
    region = p.get("region")
    estado = p.get("estado")
    regiones = p.get("regiones") or []
    paises = regiones if regiones else ([region] if region else [])
    return {
        "codigo_unico": p["codigo_unico"],
        "fuente_origen": FUENTE,
        "tipo_aviso": f"Call for tenders - {estado}" if estado else "Call for tenders",
        "titulo": p["titulo"],
        "descripcion": p.get("descripcion"),
        "pais": region,
        "paises": paises,
        "organismo": ORGANISMO,
        "categoria": p.get("tipo_contrato"),
        "url_oficial": p["url_oficial"],
        "url_documento": p.get("url_documento"),
        "fecha_publicacion": hoy.isoformat(),  # primera detección (ver docstring)
        "fecha_limite": p.get("fecha_limite"),
    }


def _texto_completo(datos: dict) -> str:
    return (
        f"Titulo: {datos['titulo']}\n"
        f"{datos.get('descripcion') or ''}\n"
        f"Pais: {datos.get('pais') or 'No especificado'}\n"
        f"Categoria: {datos.get('categoria') or 'No especificada'}\n"
        f"Tipo: {datos.get('tipo_aviso') or ''}"
    )


def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []

    for datos in normalizados:
        existente = registros_existentes.get(datos["codigo_unico"])

        # Si la ficha de una licitación existente no se pudo descargar, se conservan
        # los datos guardados para no marcarla como cambiada (el título del listado
        # viene truncado con "...", así que tampoco se toma de ahí).
        if existente is not None:
            if datos["titulo"].endswith("...") and existente.get("titulo"):
                datos["titulo"] = existente["titulo"]
            if datos.get("descripcion") is None:
                datos["descripcion"] = existente.get("descripcion")
            if datos.get("url_documento") is None:
                datos["url_documento"] = existente.get("url_documento")

        if existente is None:
            datos["texto_completo"] = _texto_completo(datos)
            datos["embedding"] = generar_embedding(datos["texto_completo"])
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

        datos["texto_completo"] = _texto_completo(datos)
        datos["embedding"] = generar_embedding(datos["texto_completo"])
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
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - LUXDEV (CALL FOR TENDERS)", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)

    proyectos = extraer_proyectos_listado()
    if not proyectos:
        print(
            "No se ha detectado ninguna licitación. "
            f"Revisa el log de arriba y, si existe, {CAPTURA_DEPURACION}.",
            flush=True,
        )
        return

    supabase = obtener_cliente_supabase()

    print("\nConsultando lo ya existente en Supabase...", flush=True)
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico", "fecha_publicacion") + CAMPOS_COMPARABLES,
        claves=[p["codigo_unico"] for p in proyectos],
    )
    print(f"Ya existentes en Supabase: {len(registros_existentes)}/{len(proyectos)}", flush=True)

    total_subidos = 0
    total_a_subir = 0

    bloques = [proyectos[i:i + LOTE_ENVIO_SUPABASE] for i in range(0, len(proyectos), LOTE_ENVIO_SUPABASE)]
    for numero_bloque, bloque in enumerate(bloques, 1):
        for p in bloque:
            ficha = obtener_ficha(p["url_oficial"])
            if ficha:
                # Solo se sobrescribe con valores presentes en la ficha.
                p.update({k: v for k, v in ficha.items() if v})
            else:
                print(f"    Aviso: sin ficha para {p['titulo'][:80]}", flush=True)
            time.sleep(PAUSA_ENTRE_FICHAS_SEGUNDOS)

        normalizados = [construir_registro(p, hoy) for p in bloque]
        lote_final = preparar_lote_para_subir(normalizados, registros_existentes)
        if not lote_final:
            continue

        total_a_subir += len(lote_final)
        print(f"--- [BLOQUE {numero_bloque}/{len(bloques)}] Subiendo {len(lote_final)} registros...", flush=True)
        total_subidos += subir_en_lotes(
            supabase,
            "licitaciones_internacionales",
            "codigo_unico",
            lote_final,
            tamano_lote=LOTE_ENVIO_SUPABASE,
        )

    if total_a_subir == 0:
        print("No hay licitaciones nuevas ni cambios que sincronizar.", flush=True)
        return

    print(f"\nSincronización LuxDev completada: {total_subidos}/{total_a_subir} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()
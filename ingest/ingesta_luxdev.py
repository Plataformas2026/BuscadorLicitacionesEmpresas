# -*- coding: utf-8 -*-
"""
ingesta_luxdev.py
-----------------
Sincroniza los PROYECTOS de cooperación de LuxDev (Agencia luxemburguesa de
cooperación al desarrollo) contra la tabla `licitaciones_internacionales`
de Supabase, mediante scraping directo del HTML (requests + BeautifulSoup,
sin Playwright).

    Listado:  https://luxdev.lu/en/projects            (?page=N, base 0)
    Ficha:    https://luxdev.lu/en/projects/<slug>

IMPORTANTE -- QUÉ SON ESTOS REGISTROS:
--------------------------------------------------------------------------
/en/projects NO es un portal de licitaciones: lista proyectos y programas de
cooperación que LuxDev ejecuta o formula. No tienen fecha de cierre ni
referencia de contratación. Por eso:
- `tipo_aviso` = "Project - <estado>" (In execution / In formulation).
- `fecha_limite` = NULL. La app trata los registros sin fecha de cierre como
  siempre vigentes (ver filtro de cierre en app/search.py), así que NO
  caducan solos: los proyectos que pasan a "Closed" dejan de leerse en el
  listado, pero su fila anterior queda en Supabase.
- `categoria` = sector del proyecto (Health, Governance...).
- Los proyectos "Closed" se omiten (ver ESTADOS_EXCLUIDOS).
- `fecha_publicacion` = fecha de primera detección (el listado no publica
  ninguna; con NULL, el orden DESC de la app los pondría los primeros). Se
  conserva en ejecuciones posteriores.

Estrategia (similar a AFD/Enabel): se recorre el listado completo y, para cada
proyecto, se descarga su ficha de detalle (~130 peticiones pequeñas, con
pausa) de donde salen la descripción completa y los datos estructurados
(donantes, periodo, presupuesto, ODS). Se compara con lo ya existente y solo se
calcula el embedding y se sube lo NUEVO o lo que ha CAMBIADO (estado, título,
sector, región, descripción o documento). Si la ficha de un proyecto ya
existente no se puede descargar en una ejecución, se conservan sus datos
guardados y no se marca como cambiado.

La ficha aporta, y se guarda en `descripcion`: contexto, objetivos,
beneficiarios, zona geográfica y actores (texto libre de la ficha), más
donantes, periodo de implementación, duración, presupuesto total y por
contribuyente, y ODS. `url_documento` = primer PDF de la sección
"Documentation" (se prefiere la versión en inglés).

VALIDACIÓN: tanto el parser del listado como el de la ficha se han probado
contra HTML real de luxdev.lu (listado de /en/projects y ficha
/en/projects/support-civil-society-benin).

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
LISTADO_URL = BASE_URL + "/en/projects"

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

# Campos que, si cambian en un proyecto ya existente, lo marcan como actualizado.
CAMPOS_COMPARABLES = (
    "titulo",
    "tipo_aviso",
    "pais",
    "categoria",
    "descripcion",
    "url_documento",
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

def extraer_proyectos_de_pagina(html: str) -> list:
    """
    Cada proyecto es un `li.views-row` con un único <a> que contiene:
      - <p> 1: <span>Estado</span><span punto/><span>Región</span>
      - <h3>:  título
      - <p> 2: sector
    """
    soup = BeautifulSoup(html, "html.parser")
    proyectos = []

    for li in soup.select("li.views-row"):
        enlace = li.find("a", href=True)
        titulo_tag = enlace.find("h3") if enlace else None
        if not enlace or not titulo_tag:
            continue

        titulo = _limpiar_texto(titulo_tag.get_text(" ", strip=True))
        if not titulo:
            continue

        parrafos = enlace.find_all("p")
        estado = region = sector = None

        if parrafos:
            textos_span = [
                _limpiar_texto(s.get_text(" ", strip=True))
                for s in parrafos[0].find_all("span")
            ]
            textos_span = [t for t in textos_span if t]
            if textos_span:
                estado = textos_span[0]
            if len(textos_span) > 1:
                region = textos_span[1]
        if len(parrafos) > 1:
            sector = _limpiar_texto(parrafos[-1].get_text(" ", strip=True))

        url = urljoin(BASE_URL + "/", enlace["href"]).split("#")[0]

        proyectos.append({
            "titulo": titulo,
            "estado": estado,
            "region": region,
            "sector": sector,
            "url_oficial": url,
            "codigo_unico": f"LUXDEV-{_slug_de_url(url)}"[:150],
        })

    return proyectos


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
        print(f"    Proyectos en la página: {len(proyectos)}", flush=True)
        if not proyectos:
            print("    No se encontraron más proyectos. Fin del listado.", flush=True)
            break

        nuevos = 0
        for p in proyectos:
            if p["codigo_unico"] not in encontrados:
                encontrados[p["codigo_unico"]] = p
                nuevos += 1

        if nuevos == 0:
            print("    La página no aporta proyectos nuevos (paginación repetida). Fin del listado.", flush=True)
            break

        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    todos = list(encontrados.values())
    vigentes = [
        p for p in todos
        if (p.get("estado") or "").strip().lower() not in ESTADOS_EXCLUIDOS
    ]
    print(f"\nProyectos en el listado: {len(todos)} | no cerrados: {len(vigentes)}", flush=True)
    return vigentes


# ============================================================
# FICHA DE DETALLE
# ============================================================

def _valor_tras_titulo(contenedor, titulo: str, etiqueta_valor: str):
    """Busca el <h3> cuyo texto es `titulo` y devuelve la primera etiqueta
    `etiqueta_valor` que le sigue en el documento (o None)."""
    if contenedor is None:
        return None
    for h3 in contenedor.find_all("h3"):
        if (_limpiar_texto(h3.get_text(" ", strip=True)) or "").lower() == titulo.lower():
            return h3.find_next(etiqueta_valor)
    return None


def _texto_prose(contenedor) -> list:
    """Texto libre de la ficha: pares 'Encabezado: párrafo'."""
    if contenedor is None:
        return []
    lineas = []
    pendiente = None
    for el in contenedor.find_all(["h3", "h4", "h5", "p", "li"]):
        if el.name == "p" and el.find_parent("li") is not None:
            continue
        texto = _limpiar_texto(el.get_text(" ", strip=True))
        if not texto:
            continue
        if el.name in ("h3", "h4", "h5"):
            pendiente = texto
        elif pendiente:
            lineas.append(f"{pendiente}: {texto}")
            pendiente = None
        else:
            lineas.append(texto)
    return lineas


def parsear_ficha(html: str) -> dict:
    """Extrae de la ficha: descripcion, url_documento y regiones."""
    soup = BeautifulSoup(html, "html.parser")
    lineas = []

    # Texto libre (Context, Objectives, Beneficiaries...)
    preview = soup.select_one("section#preview")
    prose = soup.select_one("section#preview .prose") or preview
    lineas.extend(_texto_prose(prose))

    # Donantes, periodo y duración
    tag = _valor_tras_titulo(preview, "Donor(s)", "ul")
    if tag:
        donantes = [_limpiar_texto(li.get_text(" ", strip=True)) for li in tag.find_all("li")]
        donantes = [d for d in donantes if d]
        if donantes:
            lineas.append("Donor(s): " + "; ".join(donantes))

    tag = _valor_tras_titulo(preview, "Implementation period", "p")
    periodo = _limpiar_texto(tag.get_text(" ", strip=True)) if tag else None
    tag = _valor_tras_titulo(preview, "Total duration", "p")
    duracion = _limpiar_texto(tag.get_text(" ", strip=True)) if tag else None
    if periodo:
        lineas.append(f"Implementation period: {periodo}" + (f" ({duracion})" if duracion else ""))
    elif duracion:
        lineas.append(f"Total duration: {duracion}")

    # Presupuesto
    presupuesto = soup.select_one("section#budget")
    tag = _valor_tras_titulo(presupuesto, "Total budget", "p")
    total = _limpiar_texto(tag.get_text(" ", strip=True)) if tag else None
    if total:
        lineas.append(f"Total budget: {total}")
    if presupuesto is not None:
        contribuciones = []
        for h3 in presupuesto.find_all("h3"):
            if "contributions" in (h3.get_text(" ", strip=True) or "").lower():
                ul = h3.find_next("ul")
                for li in (ul.find_all("li") if ul else []):
                    ps = [_limpiar_texto(x.get_text(" ", strip=True)) for x in li.find_all("p")]
                    ps = [x for x in ps if x]
                    if len(ps) >= 2:
                        contribuciones.append(f"{ps[1]}: {ps[0]}")
                break
        if contribuciones:
            lineas.append("Contributions managed by LuxDev: " + "; ".join(contribuciones))

    # ODS
    odd = soup.select_one("section#odd")
    if odd is not None:
        ods = [_limpiar_texto(h.get_text(" ", strip=True)) for h in odd.find_all("h4")]
        ods = [o for o in ods if o]
        if ods:
            lineas.append("Sustainable Development Goals: " + "; ".join(ods))

    # Documento: primer PDF de "Documentation" (se prefiere inglés)
    url_documento = None
    doc = soup.select_one("section#documentation")
    if doc is not None:
        candidatos = []
        for a in doc.find_all("a", href=True):
            if ".pdf" not in a["href"].lower():
                continue
            idioma = _limpiar_texto(a.find("span").get_text(" ", strip=True)) if a.find("span") else None
            candidatos.append(((idioma or "").lower(), urljoin(BASE_URL + "/", a["href"])))
        if candidatos:
            url_documento = next((u for i, u in candidatos if i == "en"), candidatos[0][1])

    # Regiones de intervención (puede haber varias en proyectos multi-país)
    regiones = []
    if preview is not None:
        for a in preview.find_all("a"):
            h3 = a.find("h3")
            if h3 and (_limpiar_texto(h3.get_text(" ", strip=True)) or "").lower() == "regions of intervention":
                span = a.find("span", class_=re.compile(r"font-primary"))
                nombre = _limpiar_texto(span.get_text(" ", strip=True)) if span else None
                if nombre and nombre not in regiones:
                    regiones.append(nombre)

    return {
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
        "tipo_aviso": f"Project - {estado}" if estado else "Project",
        "titulo": p["titulo"],
        "descripcion": p.get("descripcion"),
        "pais": region,
        "paises": paises,
        "organismo": ORGANISMO,
        "categoria": p.get("sector"),
        "url_oficial": p["url_oficial"],
        "url_documento": p.get("url_documento"),
        "fecha_publicacion": hoy.isoformat(),  # primera detección (ver docstring)
        "fecha_limite": None,
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

        # Si la ficha de un proyecto existente no se pudo descargar, se conservan
        # los datos guardados para no marcarlo como cambiado.
        if existente is not None:
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
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - LUXDEV (PROYECTOS)", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)

    proyectos = extraer_proyectos_listado()
    if not proyectos:
        print(
            "No se ha detectado ningún proyecto. "
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
                p.update(ficha)
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
        print("No hay proyectos nuevos ni cambios que sincronizar.", flush=True)
        return

    print(f"\nSincronización LuxDev completada: {total_subidos}/{total_a_subir} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

# -*- coding: utf-8 -*-
"""
ingesta_aecid.py
----------------
Sincroniza los procedimientos ACTIVOS de la Sede electrónica de la AECID
(Agencia Española de Cooperación Internacional para el Desarrollo) contra la
tabla `licitaciones_internacionales` de Supabase, mediante requests +
BeautifulSoup (sin Playwright).

    Listado:  https://www.aecid.gob.es/activos
    Ficha:    https://www.aecid.gob.es/detalle-procedimiento-activo?...&_es_aecid_sede_procedimientos_ProcedimientoWebPortlet_id=workspace://SpacesStore/<uuid>

PARTICULARIDADES DEL PORTAL
--------------------------------------------------------------------------
- NO es un portal de licitaciones: es el listado de convocatorias y
  procedimientos de la sede electrónica (becas, subvenciones, premios,
  empleo, ...). Es una página Liferay renderizada en servidor: el listado
  completo viene en un único HTML, sin paginación ni JavaScript.
- Jerarquía del listado:   FAMILIA  >  TIPO DE PROCEDIMIENTO  >  PROCEDIMIENTO
  >  TRÁMITES (convocatoria, listas provisionales, adjudicación, ...).
  Cada PROCEDIMIENTO es una "licitación" para Supabase, y su ID estable es el
  UUID de la URL de la ficha (`codigo_unico` = AECID-<uuid>).
- El listado NO publica fecha límite ni descripción. Lo único con fecha son
  los trámites (p. ej. CONVOCATORIA 01/07/2026): se usa la fecha de la
  CONVOCATORIA como fecha de publicación. `fecha_limite` queda en NULL
  (la app lo muestra como "No especificada" y no lo filtra).
- Tres iconos de estado en cada procedimiento: plazo de presentación abierto
  (lock-open), en tramitación (gears) e información actualizada (doc-alert,
  dura 10 días, por eso NO se guarda: provocaría reprocesados falsos).

MAPEO
--------------------------------------------------------------------------
- `categoria`    = Familia (Tipo de procedimiento), p. ej.
  "Becas y ayudas en materia de educación, formación e investigación
  (Becas MAEC-AECID para residencias artísticas)".
- `descripcion`  = "Palabras clave: <tipo de procedimiento>, <estado>, <tipos
  de trámite>, <programas>." + cronología de trámites del listado + (si se
  consigue) el texto completo de la ficha de detalle.
- `pais` = Spain (la entidad convocante); `organismo` = AECID;
  `tipo_aviso` = Convocatoria / Procedimiento.

ESTRATEGIA (igual que BID for the Americas / AFD / Enabel)
--------------------------------------------------------------------------
Se descarga el listado, se filtran los procedimientos (familias puramente
administrativas excluidas; sin actividad en el último año descartados salvo
que tengan plazo abierto o estén en tramitación) y se compara cada uno con
lo que ya hay en Supabase. SOLO se abre la ficha de detalle de los NUEVOS o
con cambios visibles en el listado. Solo se generan embeddings / suben los
nuevos o los que han cambiado (`es_actualizada=True`).

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local o programada: python ingesta_aecid.py
"""
import hashlib
import re
import time
import unicodedata
from datetime import date
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    subir_en_lotes,
)

BASE_URL = "https://www.aecid.gob.es"
LISTADO_URL = BASE_URL + "/activos"

FUENTE = "AECID"
PAIS = "Spain"
ORGANISMO = "Agencia Española de Cooperación Internacional para el Desarrollo (AECID)"
TIPO_AVISO_CONVOCATORIA = "Convocatoria"
TIPO_AVISO_POR_DEFECTO = "Procedimiento"
TABLA = "licitaciones_internacionales"

# ------------------------------------------------------------------
# AJUSTES DE FILTRADO (se pueden tocar sin cambiar la lógica)
# ------------------------------------------------------------------

# No se descarta ningún procedimiento por antigüedad.
MAX_ANTIGUEDAD_DIAS = None

# Familias del listado que son trámites administrativos y no oportunidades
# (registro de ONGD, acceso a información pública, altas de representante,
# certificados). Vacía la tupla para ingestarlo todo.
FAMILIAS_EXCLUIDAS = (
    "Acceso a información pública AECID",
    "Registro de organizaciones no gubernamentales para el desarrollo (ONGD)",
    "Alta representante legal/Autorizado a trámite",
    "Certificación de ausencia de beca de las convocatorias MAEC-AECID",
)

OBTENER_DETALLE = True           # abrir la ficha de los procedimientos nuevos/cambiados
MAX_FICHAS_POR_EJECUCION = 80
PAUSA_ENTRE_FICHAS_SEGUNDOS = 0.7
MAX_CHARS_DETALLE = 6000
LOTE_ENVIO_SUPABASE = 15

TIMEOUT_CONEXION = 10
TIMEOUT_LECTURA = 40
MAX_REINTENTOS_PETICION = 3

CAPTURA_DEPURACION_LISTADO = "debug_aecid_listado.html"
CAPTURA_DEPURACION_DETALLE = "debug_aecid_detalle.html"

CABECERAS_PETICION = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.5",
    "Connection": "keep-alive",
}

PARAM_ID_PROCEDIMIENTO = "_es_aecid_sede_procedimientos_ProcedimientoWebPortlet_id"
PATRON_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
PATRON_FECHA = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")

ENCABEZADO_DETALLE = "Información de la ficha:"
LINEAS_RUIDO = {"acceder", "volver", "imprimir", "compartir", "mostrar todo", "ocultar todo"}
ETIQUETAS_INLINE = ("span", "a", "strong", "b", "em", "i", "u", "small", "sup", "sub", "font", "mark", "abbr", "label")

# Comparables para decidir si una licitación ya existente ha cambiado.
CAMPOS_COMPARABLES = (
    "titulo",
    "descripcion",
    "categoria",
    "organismo",
    "pais",
    "fecha_limite",
)


# ============================================================
# RED
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


def _peticion_con_reintentos(url: str, max_intentos: int = MAX_REINTENTOS_PETICION):
    """Devuelve la respuesta, o None si falla. Un 404 no se reintenta."""
    for intento in range(1, max_intentos + 1):
        try:
            respuesta = SESION_HTTP.get(url, timeout=(TIMEOUT_CONEXION, TIMEOUT_LECTURA))
            if respuesta.status_code == 404:
                return None
            respuesta.raise_for_status()
            respuesta.encoding = "utf-8"
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
# UTILIDADES DE TEXTO / FECHAS / URL
# ============================================================

def _limpiar_texto(texto: str):
    if not texto:
        return None
    texto = texto.replace("\u00ad", "").replace("\xa0", " ")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def _normalizar(texto: str) -> str:
    """Clave de comparación: minúsculas, sin tildes y solo alfanumérico."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def _capitalizar(texto: str) -> str:
    """'LISTAS PROVISIONALES DE ADMITIDOS' -> 'Listas provisionales de admitidos'."""
    texto = _limpiar_texto(texto) or ""
    return texto[:1].upper() + texto[1:].lower() if texto.isupper() else texto


def _unir_sin_repetir(valores: list, separador: str = ", "):
    vistos = set()
    resultado = []
    for valor in valores:
        valor = _limpiar_texto(valor)
        if not valor or _normalizar(valor) in vistos:
            continue
        vistos.add(_normalizar(valor))
        resultado.append(valor)
    return separador.join(resultado) if resultado else None


def parsear_fecha(texto: str):
    """'01/07/2026' -> date(2026, 7, 1). None si no se reconoce."""
    if not texto:
        return None
    coincidencia = PATRON_FECHA.search(texto)
    if not coincidencia:
        return None
    dia, mes, anio = (int(g) for g in coincidencia.groups())
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


def _formatear_fecha(fecha) -> str:
    return fecha.strftime("%d/%m/%Y") if fecha else "sin fecha"


def id_desde_url(href: str):
    """UUID del procedimiento (parámetro workspace://SpacesStore/<uuid>), o None."""
    consulta = parse_qs(urlparse(href).query)
    valor = (consulta.get(PARAM_ID_PROCEDIMIENTO) or [""])[0]
    coincidencia = PATRON_UUID.search(valor) or PATRON_UUID.search(href)
    return coincidencia.group(0).lower() if coincidencia else None


def normalizar_url_detalle(href: str) -> str:
    """URL absoluta, sin el ':443' ni el fragmento (#workspace://... del trámite)."""
    absoluta = urljoin(BASE_URL + "/", href)
    p = urlparse(absoluta)
    return urlunparse((p.scheme or "https", p.hostname or "www.aecid.gob.es", p.path, p.params, p.query, ""))


def codigo_unico_de(proc: dict) -> str:
    if proc.get("id"):
        return f"AECID-{proc['id']}"
    return f"AECID-H{hashlib.md5(_normalizar(proc['titulo']).encode('utf-8')).hexdigest()[:16]}"


# ============================================================
# PARSER DEL LISTADO (HTML renderizado en servidor)
# ============================================================

def _texto_de(elemento):
    return _limpiar_texto(elemento.get_text(" ")) if elemento is not None else None


def _titulo_procedimiento(contenedor, enlace):
    """Título del procedimiento: <span> de la columna central de la cabecera."""
    titulo = _texto_de(contenedor.select_one("div.col-md-10 > span, div.col-md-11 > span"))
    if titulo:
        return titulo
    oculto = _texto_de(enlace.select_one("span.sr-only")) or ""
    return _limpiar_texto(re.sub(r"^Acceder a\s+", "", oculto))


def parsear_listado(html: str) -> list:
    """
    Devuelve una lista de dicts, uno por procedimiento, en el orden del portal:
      id, titulo, url, familia, tipo, abierto, en_tramitacion,
      actos [{tipo, fecha}], programas [str]
    """
    soup = BeautifulSoup(html, "html.parser")
    resultado = []
    vistos = set()

    for enlace in soup.select('span.acceder-detalle a[href*="detalle-procedimiento-activo"]'):
        # Cada trámite y cada programa también lleva un "acceder" al mismo procedimiento,
        # pero con fragmento (#workspace://...): solo interesa el enlace del procedimiento.
        if "#" in enlace.get("href", ""):
            continue
        contenedor = enlace.find_parent("div", class_="container-button-sub-evoAccordion")
        if contenedor is None:
            continue
        titulo = _titulo_procedimiento(contenedor, enlace)
        if not titulo:
            continue

        url = normalizar_url_detalle(enlace.get("href", ""))
        id_proc = id_desde_url(url)
        clave = id_proc or _normalizar(titulo)
        if clave in vistos:
            continue
        vistos.add(clave)

        # Tipo de procedimiento (acordeón exterior) y familia (título anterior a la lista)
        acordeon_tipo = contenedor.find_parent("div", attrs={"data-allow-toggle": True})
        tipo = _texto_de(acordeon_tipo.select_one("span.titulo-proce")) if acordeon_tipo else None
        familia = None
        if acordeon_tipo is not None:
            lista = acordeon_tipo.find_parent("ul")
            fila = lista.find_previous_sibling("div", class_="row") if lista is not None else None
            familia = _texto_de(fila.select_one(".titulo-familia")) if fila is not None else None

        # Trámites y programas viven en el acordeón del propio procedimiento
        raiz = contenedor.find_parent("div", attrs={"data-allow-multiple": True})
        actos = []
        programas = []
        if raiz is not None:
            for acto in raiz.select("div.acto-administrativo"):
                enlace_acto = acto.select_one("span.titulo-tramite a")
                tipo_acto = _texto_de(enlace_acto.find("span")) if enlace_acto is not None else None
                if not tipo_acto:
                    continue
                actos.append({"tipo": tipo_acto, "fecha": parsear_fecha(_texto_de(acto.select_one("span.fecha-tramite")) or "")})
            for programa in raiz.select("div.programa span.titulo-tramite a p"):
                nombre = _texto_de(programa)
                if nombre:
                    programas.append(nombre)

        resultado.append({
            "id": id_proc,
            "titulo": titulo,
            "url": url,
            "familia": familia,
            "tipo": tipo,
            "abierto": contenedor.select_one("p.lock-open") is not None,
            "en_tramitacion": contenedor.select_one("p.gears") is not None,
            "actos": actos,
            "programas": programas,
        })
    return resultado


# ============================================================
# DATOS DERIVADOS DEL LISTADO
# ============================================================

def fecha_ultimo_tramite(proc: dict):
    fechas = [a["fecha"] for a in proc["actos"] if a["fecha"]]
    return max(fechas) if fechas else None


def fecha_convocatoria(proc: dict):
    """Fecha del trámite CONVOCATORIA; si no hay, la del trámite más antiguo; si no, None."""
    fechas_convocatoria = [a["fecha"] for a in proc["actos"] if a["fecha"] and _normalizar(a["tipo"]) == "convocatoria"]
    if fechas_convocatoria:
        return min(fechas_convocatoria)
    fechas = [a["fecha"] for a in proc["actos"] if a["fecha"]]
    return min(fechas) if fechas else None


def familia_excluida(proc: dict) -> bool:
    excluidas = {_normalizar(f) for f in FAMILIAS_EXCLUIDAS}
    return _normalizar(proc.get("familia") or "") in excluidas


def es_vigente(proc: dict, hoy: date) -> bool:
    if familia_excluida(proc):
        return False
    if proc["abierto"] or proc["en_tramitacion"]:
        return True
    if MAX_ANTIGUEDAD_DIAS is None:
        return True
    ultimo = fecha_ultimo_tramite(proc)
    if ultimo is None:  # sin trámites con fecha (p. ej. previsiones): se conservan
        return True
    return (hoy - ultimo).days <= MAX_ANTIGUEDAD_DIAS


def palabras_clave_de(proc: dict) -> list:
    claves = []
    if proc.get("tipo") and _normalizar(proc["tipo"]) != _normalizar(proc["titulo"]):
        claves.append(proc["tipo"])
    if proc["abierto"]:
        claves.append("Plazo de presentación abierto")
    if proc["en_tramitacion"]:
        claves.append("En tramitación")
    claves.extend(_capitalizar(a["tipo"]) for a in proc["actos"])
    claves.extend(_capitalizar(p) for p in proc["programas"])
    vistos = set()
    unicas = []
    for clave in claves:
        if _normalizar(clave) not in vistos:
            vistos.add(_normalizar(clave))
            unicas.append(clave)
    return unicas


def componer_descripcion(palabras_clave: list, texto: str):
    """Palabras clave al INICIO de la descripción, seguidas del texto completo del apartado."""
    partes = []
    if palabras_clave:
        partes.append("Palabras clave: " + ", ".join(palabras_clave) + ".")
    if texto:
        partes.append(texto)
    return "\n\n".join(partes) if partes else None


def cronologia_de(proc: dict):
    lineas = [f"- {_formatear_fecha(a['fecha'])}: {_capitalizar(a['tipo'])}" for a in proc["actos"]]
    if proc["programas"]:
        lineas.append("Programas: " + ", ".join(_capitalizar(p) for p in proc["programas"]) + ".")
    if not lineas:
        return None
    return "Trámites publicados en la sede electrónica:\n" + "\n".join(lineas)


def descripcion_listado(proc: dict):
    """Parte de la descripción que sale solo del listado (palabras clave + cronología)."""
    return componer_descripcion(palabras_clave_de(proc), cronologia_de(proc))


def categoria_de(proc: dict):
    familia = proc.get("familia")
    tipo = proc.get("tipo")
    if familia and tipo and _normalizar(tipo) != _normalizar(familia):
        return f"{familia} ({tipo})"
    return familia or tipo


# ============================================================
# FICHA DE DETALLE (mejor esfuerzo: el HTML de la ficha es genérico)
# ============================================================

def _buscar_url_documento(contenedor):
    """
    Primer enlace a /documents/ que sea la convocatoria. Solo se mira el propio enlace
    (texto, title, aria-label y nombre del fichero): el contexto del contenedor haría que
    cualquier documento pareciese la convocatoria. None si no hay.
    """
    for enlace in contenedor.select('a[href*="/documents/"]'):
        propio = " ".join(filter(None, [
            enlace.get_text(" "), enlace.get("title"), enlace.get("aria-label"),
            unquote(urlparse(enlace["href"]).path.rsplit("/", 1)[-1]),
        ]))
        if "convocatoria" in _normalizar(propio):
            return urljoin(BASE_URL + "/", enlace["href"])
    return None


def parsear_detalle(html: str) -> dict:
    """Devuelve {texto, url_documento}. `texto` es None si la ficha no aporta contenido."""
    soup = BeautifulSoup(html, "html.parser")
    for elemento in soup.select("script, style, noscript, svg, nav, header, footer"):
        elemento.decompose()

    contenedor = (
        soup.select_one('section[id^="portlet_es_aecid_sede_procedimientos_ProcedimientoWebPortlet"]')
        or soup.select_one("#main-content")
        or soup.find("main")
        or soup.body
    )
    if contenedor is None:
        return {"texto": None, "url_documento": None}

    url_documento = _buscar_url_documento(contenedor)

    # Las etiquetas en línea se desenvuelven para que solo los bloques separen líneas.
    # Los enlaces llevan un espacio detrás para que dos contiguos no se peguen.
    for enlace in contenedor.find_all("a"):
        enlace.insert_after(" ")
    for etiqueta in contenedor.find_all(ETIQUETAS_INLINE):
        etiqueta.unwrap()
    contenedor.smooth()

    lineas = []
    for linea in contenedor.get_text("\n").split("\n"):
        linea = _limpiar_texto(linea)
        if not linea or len(linea) < 2 or _normalizar(linea) in LINEAS_RUIDO:
            continue
        if lineas and lineas[-1] == linea:
            continue
        lineas.append(linea)

    texto = ""
    for linea in lineas:
        if len(texto) + len(linea) + 1 > MAX_CHARS_DETALLE:
            break
        texto += ("\n" if texto else "") + linea
    if len(texto) < 40:
        texto = None
    return {"texto": texto, "url_documento": url_documento}


def obtener_detalle(url: str, guardar_captura: bool = False) -> dict:
    respuesta = _peticion_con_reintentos(url)
    if respuesta is None:
        return {"texto": None, "url_documento": None}
    if guardar_captura:
        try:
            with open(CAPTURA_DEPURACION_DETALLE, "w", encoding="utf-8") as f:
                f.write(respuesta.text)
        except Exception:
            pass
    return parsear_detalle(respuesta.text)


# ============================================================
# LECTURA DE LO YA EXISTENTE EN SUPABASE (por fuente)
# ============================================================

def obtener_existentes_de_la_fuente(supabase) -> dict:
    """{codigo_unico: fila} de todo lo ya guardado para esta fuente (paginado)."""
    columnas = ("id", "codigo_unico", "fecha_publicacion", "url_oficial") + CAMPOS_COMPARABLES
    filas = {}
    desde = 0
    paso = 1000
    while True:
        respuesta = (
            supabase.table(TABLA)
            .select(", ".join(columnas))
            .eq("fuente_origen", FUENTE)
            .range(desde, desde + paso - 1)
            .execute()
        )
        for fila in respuesta.data:
            filas[fila["codigo_unico"]] = fila
        if len(respuesta.data) < paso:
            break
        desde += paso
    return filas


def necesita_ficha(proc: dict, existente) -> bool:
    """Se procesa si es nuevo, ha cambiado en el listado o aún no tiene el texto de la ficha."""
    if existente is None:
        return True
    descripcion_actual = existente.get("descripcion") or ""
    parte_listado = descripcion_listado(proc) or ""
    if not (descripcion_actual == parte_listado or descripcion_actual.startswith(parte_listado + "\n\n" + ENCABEZADO_DETALLE)):
        return True
    if str(existente.get("titulo")) != proc["titulo"]:
        return True
    if str(existente.get("categoria")) != str(categoria_de(proc)):
        return True
    if OBTENER_DETALLE and ENCABEZADO_DETALLE not in descripcion_actual:
        return True
    return False


# ============================================================
# CONSTRUIR REGISTRO
# ============================================================

def construir_registro(proc: dict, detalle: dict, existente, hoy: date) -> dict:
    descripcion = descripcion_listado(proc)
    texto_detalle = (detalle or {}).get("texto")
    if texto_detalle:
        descripcion = (descripcion + "\n\n" if descripcion else "") + ENCABEZADO_DETALLE + "\n" + texto_detalle

    # Fecha real = trámite CONVOCATORIA. Sin trámites: fecha de primera detección (se conserva).
    fecha_publicacion = fecha_convocatoria(proc)
    if fecha_publicacion:
        fecha_publicacion = fecha_publicacion.isoformat()
    elif existente and existente.get("fecha_publicacion"):
        fecha_publicacion = existente["fecha_publicacion"]
    else:
        fecha_publicacion = hoy.isoformat()

    es_convocatoria = any(_normalizar(a["tipo"]) == "convocatoria" for a in proc["actos"]) \
        or "convocatoria" in _normalizar(proc["titulo"])

    return {
        "codigo_unico": codigo_unico_de(proc),
        "fuente_origen": FUENTE,
        "tipo_aviso": TIPO_AVISO_CONVOCATORIA if es_convocatoria else TIPO_AVISO_POR_DEFECTO,
        "titulo": proc["titulo"],
        "descripcion": descripcion,
        "pais": PAIS,
        "paises": [PAIS],
        "organismo": ORGANISMO,
        "categoria": categoria_de(proc),
        "url_oficial": proc["url"],
        "url_documento": (detalle or {}).get("url_documento"),
        "fecha_publicacion": fecha_publicacion,
        "fecha_limite": None,  # el listado no la publica
    }


# ============================================================
# DECIDIR QUE SUBIR
# ============================================================

def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []

    for datos in normalizados:
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

def construir_registros(procedimientos: list, existentes: dict, hoy: date) -> list:
    """Abre la ficha solo de los procedimientos nuevos o con cambios y devuelve los registros."""
    registros = []
    fichas_abiertas = 0
    sin_cambios = 0

    for indice, proc in enumerate(procedimientos, start=1):
        existente = existentes.get(codigo_unico_de(proc))
        if not necesita_ficha(proc, existente):
            sin_cambios += 1
            continue

        detalle = None
        if OBTENER_DETALLE and fichas_abiertas < MAX_FICHAS_POR_EJECUCION:
            print(f"  [{indice}/{len(procedimientos)}] ficha: {proc['titulo'][:90]}", flush=True)
            detalle = obtener_detalle(proc["url"], guardar_captura=(fichas_abiertas == 0))
            fichas_abiertas += 1
            if not detalle["texto"]:
                print("      Aviso: la ficha no devolvió texto aprovechable (se guarda solo lo del listado).", flush=True)
            time.sleep(PAUSA_ENTRE_FICHAS_SEGUNDOS)
        elif OBTENER_DETALLE:
            print(f"  [{indice}/{len(procedimientos)}] sin abrir ficha (límite de {MAX_FICHAS_POR_EJECUCION}): {proc['titulo'][:80]}", flush=True)

        registros.append(construir_registro(proc, detalle, existente, hoy))

    print(f"    Sin cambios (no se reprocesan): {sin_cambios} | fichas abiertas: {fichas_abiertas}", flush=True)
    return registros


def ejecutar_sincronizacion():
    hoy = date.today()

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - AECID", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)

    supabase = obtener_cliente_supabase()

    print("\nLeyendo lo ya existente en Supabase para esta fuente...", flush=True)
    existentes = obtener_existentes_de_la_fuente(supabase)
    print(f"Ya existentes en Supabase: {len(existentes)}", flush=True)

    print("\nDescargando el listado de procedimientos activos...", flush=True)
    respuesta = _peticion_con_reintentos(LISTADO_URL)
    if respuesta is None:
        print("No se pudo descargar el listado; se aborta sin tocar Supabase.", flush=True)
        return
    try:
        with open(CAPTURA_DEPURACION_LISTADO, "w", encoding="utf-8") as f:
            f.write(respuesta.text)
    except Exception:
        pass

    procedimientos = parsear_listado(respuesta.text)
    print(f"Procedimientos reconocidos en el listado: {len(procedimientos)}", flush=True)
    if not procedimientos:
        print(f"No se reconoció ninguno (¿ha cambiado el portal?). Revisa {CAPTURA_DEPURACION_LISTADO}.", flush=True)
        return

    excluidos = [p for p in procedimientos if familia_excluida(p)]
    vigentes = [p for p in procedimientos if es_vigente(p, hoy)]
    print(
        f"Excluidos por familia administrativa: {len(excluidos)} | "
        f"descartados por antigüedad: {len(procedimientos) - len(excluidos) - len(vigentes)} | "
        f"a considerar: {len(vigentes)}",
        flush=True,
    )

    normalizados = construir_registros(vigentes, existentes, hoy)
    if not normalizados:
        print("No hay licitaciones nuevas ni cambios que sincronizar.", flush=True)
        return

    lote_final = preparar_lote_para_subir(normalizados, existentes)
    if not lote_final:
        print("No hay licitaciones nuevas ni cambios que sincronizar.", flush=True)
        return

    nuevas = sum(1 for d in lote_final if d["es_novedad"])
    print(f"\nA subir: {len(lote_final)} ({nuevas} nuevas, {len(lote_final) - nuevas} actualizadas)", flush=True)

    subidas = subir_en_lotes(
        supabase,
        TABLA,
        "codigo_unico",
        lote_final,
        tamano_lote=LOTE_ENVIO_SUPABASE,
    )

    print(f"\nSincronización AECID completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

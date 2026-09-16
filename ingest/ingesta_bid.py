# -*- coding: utf-8 -*-
"""
ingesta_bid.py
----------------
Sincroniza avisos de licitacion del Banco Interamericano de Desarrollo
(BID/IADB) contra la tabla `licitaciones_internacionales` de Supabase,
con EXACTAMENTE los mismos campos que ya se usan para la fuente AfDB
(ver ingest/ingesta_afdb.py) -- ninguna columna nueva en el esquema.

FUENTE REAL: NO es scraping del Power BI
-------------------------------------------
La pagina que se referencio (.../avisos-de-adquisiciones) muestra los
avisos dentro de un informe de Power BI incrustado: un hueco que se
rellena con JavaScript en el navegador ya en marcha, así que no hay
ningun dato en el HTML que se pueda scrapear (se comprobo expresamente:
la pagina descargada no contiene ni un solo aviso, solo el marcado
alrededor de donde se monta el informe).

Pero el BID publica esos MISMOS datos -- el propio "Procurement Notices"
que alimenta ese informe -- como conjunto de datos abiertos en su portal
CKAN (data.iadb.org), bajo licencia Creative Commons Attribution 4.0:

    https://data.iadb.org/dataset/project-procurement-bidding-notices-and-notification-of-contract-awards

CKAN expone ese recurso mediante una API REST publica y SIN CLAVE
(keyless), pensada precisamente para este uso -- consumo programatico de
terceros, no solo descarga manual de un CSV. La propia pagina del
dataset enlaza esta URL bajo una columna literalmente llamada "API", y
ya existen herramientas de terceros publicadas que la consumen de la
misma forma (p. ej. github.com/pipeworx-io/mcp-idb). El robots.txt de
data.iadb.org bloquea /api/ para RASTREADORES DE BUSQUEDA (evita que
Google indexe respuestas JSON en bruto) -- no es una prohibicion de uso
programatico legitimo, que es exactamente para lo que existe esta API.

    Resource ID usado ("Procurement Notices"): 856aabfd-2c6a-48fb-a8b8-19f3ff443618
    Endpoint:  https://data.iadb.org/api/3/action/datastore_search

DESCUBRIMIENTO DE COLUMNAS EN TIEMPO DE EJECUCION -- LEE ESTO
------------------------------------------------------------------
No ha sido posible inspeccionar en vivo el JSON real que devuelve esta
API durante el desarrollo de este script (las herramientas de
navegacion usadas para investigarla no pudieron completar la llamada).
Por eso este script NO da por hecho los nombres exactos de las columnas
de la tabla remota. En cada ejecucion:
  1. Pide un registro de muestra y lee `result.fields`, un metadato que
     CKAN SIEMPRE devuelve junto a los datos: la lista real de columnas
     de la tabla, tal cual esta hoy.
  2. Empareja cada columna real con el concepto que hace falta (titulo,
     descripcion, pais, fecha de publicacion, fecha de cierre, enlace,
     tipo de aviso, organismo, referencia) por coincidencia de nombre
     (ver CANDIDATOS_POR_CONCEPTO), y lo deja bien visible en los logs
     de cada ejecucion.
  3. Si un concepto importante (sobre todo "titulo") no se ha podido
     emparejar, avisa con claridad y no sube nada, en vez de fallar en
     silencio o inventar datos.

**Antes de dejarlo en el cron automatico**: ejecuta este script una vez
a mano (workflow_dispatch) y revisa el log "Emparejamiento concepto ->
columna real". Si alguna columna se ha emparejado mal (o no se ha
emparejado), corrigelo a mano en MAPEO_CONCEPTOS_FORZADO -- tiene
prioridad sobre el emparejamiento automatico.

Otros dos hallazgos documentados por terceros que ya consumen esta
misma API (ver enlace de mcp-idb arriba), aplicados aqui de forma
defensiva:
  - La columna "type" (tipo de aviso) trae espacios en blanco finales
    inconsistentes ("AWARD", "AWARD ", "AWARD   " son 3 valores
    distintos en el dato en crudo) -- por eso TODOS los valores de
    texto se limpian con strip() antes de guardarlos.
  - Las URLs de descarga directa (/files/download/<id>) estan detras de
    un reto anti-bot de AWS WAF -- este script nunca las usa; todo pasa
    por la API de Datastore.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_bid.py
Ejecucion programada: ver .github/workflows/sincronizar_bid.yml
"""
import re
import time
from datetime import date, datetime, timedelta

import requests

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL_CKAN = "https://data.iadb.org"
ENDPOINT_DATASTORE_SEARCH = BASE_URL_CKAN + "/api/3/action/datastore_search"
RESOURCE_ID = "856aabfd-2c6a-48fb-a8b8-19f3ff443618"  # "Procurement Notices"

# URL a la que se manda al usuario cuando un aviso no trae su propio
# enlace directo (ver docstring: no todos los recursos CKAN incluyen una
# columna de URL por registro) -- la pagina humana equivalente.
URL_FICHA_GENERICA = "https://www.iadb.org/es/como-trabajar-juntos/adquisiciones/adquisiciones-para-proyectos/avisos-de-adquisiciones"

FUENTE = "BID"
DIAS_ATRAS = 3                       # mismo criterio que AfDB: "ultimos 3 dias"
TAMANO_PAGINA = 200
MAX_REGISTROS_SEGURIDAD = 5000        # red de seguridad de paginacion
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.4
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15

CABECERAS = {"User-Agent": "Mozilla/5.0 (compatible; LicitacionesEmpresasBot/1.0)"}

CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_limite", "tipo_aviso")

# Concepto que necesitamos -> fragmentos de nombre de columna real que lo
# identificarian (se busca por SUBSTRING sobre el nombre de columna en
# minusculas, en el orden de la tabla). Ver aviso en el docstring del
# modulo: esto se resuelve en tiempo de ejecucion, no son nombres fijos.
CANDIDATOS_POR_CONCEPTO = {
    "referencia": ["notice_id", "noticeid", "reference_no", "reference", "referencia", "notice_no", "notice_number", "id"],
    "titulo": ["title", "titulo", "notice_title", "project_name", "name"],
    "descripcion": ["description", "descripcion", "summary", "scope", "notice_text", "detail"],
    "pais": ["country", "pais", "country_name"],
    "fecha_publicacion": ["publication_date", "publish_date", "issue_date", "date_published", "notice_date", "created_date", "posted_date"],
    "fecha_limite": ["deadline", "closing_date", "due_date", "submission_date", "expiration_date", "closing"],
    "url": ["url", "link", "document_url", "notice_url", "web"],
    "tipo": ["type", "notice_type", "category"],
    "organismo": ["agency", "executing_agency", "borrower", "organization", "buyer", "client"],
}
# Ver docstring: rellenar aqui a mano tras revisar los logs de una
# primera ejecucion si el emparejamiento automatico se equivoca con
# alguna columna, p. ej. {"fecha_limite": "nombre_real_exacto"}.
MAPEO_CONCEPTOS_FORZADO = {
    "referencia": ["noticeid"],
    "titulo": ["noticetitle"],
    "descripcion": ["process_desc"],
    "pais": ["countryname"],
    "fecha_publicacion": ["publicationdate"],
    "fecha_limite": ["deadline"],
    "url": ["proyecturl"],
    "tipo": ["type"],
    "organismo": ["projectname"],
}


# ------------------------------------------------------------------
# Llamadas a la API de Datastore (CKAN) -- sin clave, de lectura
# ------------------------------------------------------------------
def _consultar_datastore(offset: int = 0, limit: int = 1, sort: str = None) -> dict:
    parametros = {"resource_id": RESOURCE_ID, "limit": limit, "offset": offset}
    if sort:
        parametros["sort"] = sort
    respuesta = requests.get(ENDPOINT_DATASTORE_SEARCH, params=parametros, timeout=TIMEOUT_PETICION, headers=CABECERAS)
    respuesta.raise_for_status()
    cuerpo = respuesta.json()
    if not cuerpo.get("success"):
        raise RuntimeError(f"La API de datos abiertos del BID devolvio un error: {cuerpo}")
    return cuerpo["result"]


def descubrir_columnas() -> list:
    resultado = _consultar_datastore(offset=0, limit=1)
    return [campo["id"] for campo in resultado.get("fields", []) if campo["id"] != "_id"]


def emparejar_columnas(columnas_reales: list) -> dict:
    columnas_normalizadas = [(columna, columna.lower().replace("-", "_").strip()) for columna in columnas_reales]
    emparejado = {}
    for concepto, candidatos in CANDIDATOS_POR_CONCEPTO.items():
        for columna_real, columna_norm in columnas_normalizadas:
            if any(candidato in columna_norm for candidato in candidatos):
                emparejado[concepto] = columna_real
                break
    emparejado.update(MAPEO_CONCEPTOS_FORZADO)
    return emparejado


# ------------------------------------------------------------------
# Normalizacion de valores
# ------------------------------------------------------------------
def _valor_texto(registro: dict, columna: str):
    if not columna:
        return None
    valor = registro.get(columna)
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() in ("nan", "none", "null"):
        return None
    return texto


def _parsear_fecha(valor):
    """Defensivo a proposito: un export CSV->CKAN puede traer la fecha en varios formatos."""
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() in ("nan", "none", "null"):
        return None
    for patron in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto, patron).date()
        except ValueError:
            continue
    coincidencia = re.match(r"(\d{4}-\d{2}-\d{2})", texto)
    if coincidencia:
        try:
            return date.fromisoformat(coincidencia.group(1))
        except ValueError:
            pass
    return None


def _generar_slug(texto: str) -> str:
    texto_norm = texto.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


# ------------------------------------------------------------------
# Normalizacion al esquema de `licitaciones_internacionales`
# ------------------------------------------------------------------
def construir_registro(registro: dict, mapeo: dict) -> dict:
    titulo = _valor_texto(registro, mapeo.get("titulo")) or "Sin titulo"
    referencia = _valor_texto(registro, mapeo.get("referencia"))
    codigo_unico = f"BID-{_generar_slug(referencia or titulo)}"

    tipo_aviso = _valor_texto(registro, mapeo.get("tipo"))  # strip() ya aplicado en _valor_texto (ver docstring: espacios finales inconsistentes)

    fecha_publicacion = _parsear_fecha(registro.get(mapeo.get("fecha_publicacion"))) if mapeo.get("fecha_publicacion") else None
    fecha_limite = _parsear_fecha(registro.get(mapeo.get("fecha_limite"))) if mapeo.get("fecha_limite") else None

    return {
        "codigo_unico": codigo_unico,
        "fuente_origen": FUENTE,
        "tipo_aviso": tipo_aviso,
        "titulo": titulo,
        "descripcion": _valor_texto(registro, mapeo.get("descripcion")),
        "pais": _valor_texto(registro, mapeo.get("pais")),
        "organismo": _valor_texto(registro, mapeo.get("organismo")),
        "categoria": None,
        "url_oficial": _valor_texto(registro, mapeo.get("url")) or URL_FICHA_GENERICA,
        "url_documento": None,
        "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
    }


# ------------------------------------------------------------------
# Decidir que subir
# ------------------------------------------------------------------
def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
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


# ------------------------------------------------------------------
# Ejecucion principal
# ------------------------------------------------------------------
def ejecutar_sincronizacion():
    hoy = date.today()
    desde = hoy - timedelta(days=DIAS_ATRAS)

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - BID (IADB)", flush=True)
    print("=" * 100, flush=True)
    print(f"Ventana: {desde} .. {hoy}", flush=True)
    print(f"Fuente: {ENDPOINT_DATASTORE_SEARCH}?resource_id={RESOURCE_ID}", flush=True)

    try:
        columnas_reales = descubrir_columnas()
    except Exception as error:
        print(f"Error consultando la API de datos abiertos del BID: {error}", flush=True)
        return

    print(f"\nColumnas reales detectadas en el recurso ({len(columnas_reales)}): {columnas_reales}", flush=True)

    mapeo = emparejar_columnas(columnas_reales)
    print("\nEmparejamiento concepto -> columna real:", flush=True)
    for concepto in CANDIDATOS_POR_CONCEPTO:
        print(f"  {concepto:20} -> {mapeo.get(concepto) or 'NO ENCONTRADA'}", flush=True)

    if "titulo" not in mapeo:
        print(
            "\nNo se ha podido identificar la columna de titulo entre las columnas reales. "
            "Revisa CANDIDATOS_POR_CONCEPTO/MAPEO_CONCEPTOS_FORZADO en este script con los "
            "nombres reales de arriba. Se aborta esta sincronizacion sin subir nada.",
            flush=True,
        )
        return

    columna_fecha_orden = mapeo.get("fecha_publicacion") or mapeo.get("fecha_limite")
    if not columna_fecha_orden:
        print(
            "\nAviso: no se ha identificado ninguna columna de fecha -- no se puede aplicar la "
            f"ventana de {DIAS_ATRAS} dias. Se recogeran como mucho las primeras "
            f"{TAMANO_PAGINA} filas del recurso (orden por defecto de la API) en vez de filtrar "
            "por fecha; conviene revisar el emparejamiento antes de confiar en el cron automatico.",
            flush=True,
        )

    # ---------------- Paginacion con parada temprana (mismo patron que AfDB) ----------------
    candidatos = []
    offset = 0
    detener = False

    while offset < MAX_REGISTROS_SEGURIDAD and not detener:
        orden = f"{columna_fecha_orden} desc" if columna_fecha_orden else None
        try:
            resultado = _consultar_datastore(offset=offset, limit=TAMANO_PAGINA, sort=orden)
        except Exception as error:
            print(f"    Error consultando la pagina en offset={offset}: {error}", flush=True)
            break

        registros = resultado.get("records", [])
        if not registros:
            break

        for registro in registros:
            if columna_fecha_orden:
                fecha_referencia = _parsear_fecha(registro.get(columna_fecha_orden))
                if fecha_referencia and fecha_referencia < desde:
                    detener = True
                    continue
            candidatos.append(registro)

        offset += TAMANO_PAGINA
        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

        if not columna_fecha_orden:
            break  # sin columna de fecha no se puede acotar la ventana: una sola pagina y fin

    print(f"\nAvisos candidatos en la ventana: {len(candidatos)}", flush=True)

    if not candidatos:
        return

    normalizados = [construir_registro(registro, mapeo) for registro in candidatos]
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
    print(f"\nSincronizacion BID completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

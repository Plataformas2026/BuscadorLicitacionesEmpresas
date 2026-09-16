# -*- coding: utf-8 -*-
"""
ingesta_bid.py
----------------
Sincroniza avisos de licitacion del Banco Interamericano de Desarrollo
(BID/IADB) contra la tabla `licitaciones_internacionales` de Supabase,
con EXACTAMENTE los mismos campos que ya se usan para la fuente AfDB
(ver ingest/ingesta_afdb.py) -- ninguna columna nueva en el esquema.

FUENTE REAL: API REST de CKAN (data.iadb.org)
Resource ID ("Procurement Notices"): 856aabfd-2c6a-48fb-a8b8-19f3ff443618
Endpoint:  https://data.iadb.org/api/3/action/datastore_search
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
RESOURCE_ID = "856aabfd-2c6a-48fb-a8b8-19f3ff443618"

URL_FICHA_GENERICA = "https://www.iadb.org/es/como-trabajar-juntos/adquisiciones/adquisiciones-para-proyectos/avisos-de-adquisiciones"

FUENTE = "BID"
DIAS_ATRAS = 3                       # Últimos 3 días
TAMANO_PAGINA = 200
MAX_REGISTROS_SEGURIDAD = 1000        # Suficiente para barrer los más recientes ordenados
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.4
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15

CABECERAS = {"User-Agent": "Mozilla/5.0 (compatible; LicitacionesEmpresasBot/1.0)"}

CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_limite", "tipo_aviso")

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

MAPEO_CONCEPTOS_FORZADO = {
    "referencia": "noticeid",
    "titulo": "noticetitle",
    "descripcion": "process_desc",
    "pais": "countryname",
    "fecha_publicacion": "publicationdate",
    "fecha_limite": "deadline",
    "url": "proyecturl",
    "tipo": "type",
    "organismo": "projectname",
}


# ------------------------------------------------------------------
# Llamadas a la API de Datastore (CKAN) con ordenación nativa
# ------------------------------------------------------------------
def _consultar_datastore(offset: int = 0, limit: int = 1, sort_field: str = None) -> dict:
    parametros = {"resource_id": RESOURCE_ID, "limit": limit, "offset": offset}
    if sort_field:
        parametros["sort"] = f"{sort_field} desc"
        
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
def _extraer_valor_plano(registro: dict, columna: str):
    if not columna:
        return None
    valor = registro.get(columna)
    if valor is None:
        return None
    if isinstance(valor, list):
        if not valor:
            return None
        valor = valor[0]
    texto = str(valor).strip()
    if not texto or texto.lower() in ("nan", "none", "null"):
        return None
    return texto


def _parsear_fecha(valor):
    if valor is None:
        return None
    if isinstance(valor, list):
        if not valor:
            return None
        valor = valor[0]
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


def construir_registro(registro: dict, mapeo: dict) -> dict:
    titulo = _extraer_valor_plano(registro, mapeo.get("titulo")) or "Sin titulo"
    referencia = _extraer_valor_plano(registro, mapeo.get("referencia"))
    codigo_unico = f"BID-{_generar_slug(referencia or titulo)}"

    tipo_aviso = _extraer_valor_plano(registro, mapeo.get("tipo"))

    fecha_publicacion = _parsear_fecha(registro.get(mapeo.get("fecha_publicacion"))) if mapeo.get("fecha_publicacion") else None
    fecha_limite = _parsear_fecha(registro.get(mapeo.get("fecha_limite"))) if mapeo.get("fecha_limite") else None

    return {
        "codigo_unico": codigo_unico,
        "fuente_origen": FUENTE,
        "tipo_aviso": tipo_aviso,
        "titulo": titulo,
        "descripcion": _extraer_valor_plano(registro, mapeo.get("descripcion")),
        "pais": _extraer_valor_plano(registro, mapeo.get("pais")),
        "organismo": _extraer_valor_plano(registro, mapeo.get("organismo")),
        "categoria": None,
        "url_oficial": _extraer_valor_plano(registro, mapeo.get("url")) or URL_FICHA_GENERICA,
        "url_documento": None,
        "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
    }


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
        resultado_inicial = _consultar_datastore(offset=0, limit=1)
        columnas_reales = [campo["id"] for campo in resultado_inicial.get("fields", []) if campo["id"] != "_id"]
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
            "\nNo se ha podido identificar la columna de titulo. Abortando.",
            flush=True,
        )
        return

    columna_fecha_orden = mapeo.get("fecha_publicacion") or mapeo.get("fecha_limite")

    # ---------------- Descarga ordenada desde la API ----------------
    candidatos = []
    offset = 0

    print(f"\nDescargando registros ordenados por '{columna_fecha_orden}' (descendente)...", flush=True)
    while offset < MAX_REGISTROS_SEGURIDAD:
        try:
            resultado = _consultar_datastore(offset=offset, limit=TAMANO_PAGINA, sort_field=columna_fecha_orden)
        except Exception as error:
            print(f"    Error consultando la pagina en offset={offset}: {error}", flush=True)
            break

        registros = resultado.get("records", [])
        if not registros:
            break

        candidatos.extend(registros)
        
        if len(registros) < TAMANO_PAGINA:
            break

        offset += TAMANO_PAGINA
        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    print(f"\nTotal registros descargados: {len(candidatos)}", flush=True)
    # Diagnóstico rápido de fechas
    fechas_muestra = [r.get(columna_fecha_orden) for r in candidatos[:5]]
    print(f"Las 5 fechas más recientes en la API del BID son: {fechas_muestra}")

    if not candidatos:
        return

    # Filtrar estrictamente por la ventana de días requerida
    if columna_fecha_orden:
        candidatos_en_ventana = []
        for registro in candidatos:
            fecha_referencia = _parsear_fecha(registro.get(columna_fecha_orden))
            if fecha_referencia and fecha_referencia >= desde:
                candidatos_en_ventana.append(registro)
        candidatos = candidatos_en_ventana

    print(f"\nAvisos candidatos en la ventana ({desde} a {hoy}): {len(candidatos)}", flush=True)

    if not candidatos:
        print("No hay avisos nuevos dentro de la ventana de fechas.", flush=True)
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

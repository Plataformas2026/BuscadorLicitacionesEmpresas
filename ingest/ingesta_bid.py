# -*- coding: utf-8 -*-
"""
ingesta_bid.py
----------------
Sincroniza avisos y planes de adquisiciones del Banco Interamericano de Desarrollo
(BID/IADB) contra la tabla `licitaciones_internacionales` de Supabase.

AUTO-DETECCION DE RECURSO:
El script visita la pagina oficial del dataset en el portal del BID,
lee el HTML y extrae dinámicamente el `resource_id` actual de la API de Datastore
por si los administradores lo actualizan o recrean en el futuro.

URL del Dataset: https://data.iadb.org/dataset/ati-documents
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
URL_DATASET_WEB = BASE_URL_CKAN + "/dataset/ati-documents"
ENDPOINT_DATASTORE_SEARCH = BASE_URL_CKAN + "/api/3/action/datastore_search"

URL_FICHA_GENERICA = "https://www.iadb.org/es/como-trabajar-juntos/adquisiciones/adquisiciones-para-proyectos/avisos-de-adquisiciones"

FUENTE = "BID"
DIAS_ATRAS = 3                       # Últimos 3 días
TAMANO_PAGINA = 200
MAX_REGISTROS_SEGURIDAD = 1000        
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.4
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15

CABECERAS = {"User-Agent": "Mozilla/5.0 (compatible; LicitacionesEmpresasBot/1.0)"}

CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "tipo_aviso")

CANDIDATOS_POR_CONCEPTO = {
    "referencia": ["project_number", "id"],
    "titulo": ["document_name", "title"],
    "descripcion": ["document_activity", "activity"],
    "pais": ["country", "pais"],
    "fecha_publicacion": ["disclosure_date", "created_date"],
    "fecha_limite": [],
    "url": ["document_url", "url"],
    "tipo": ["document_activity", "policy"],
    "organismo": ["project_number"],
}

MAPEO_CONCEPTOS_FORZADO = {
    "referencia": "project_number",
    "titulo": "document_name",
    "descripcion": "document_activity",
    "pais": "country",
    "fecha_publicacion": "disclosure_date",
    "url": "document_url",
    "tipo": "document_activity",
    "organismo": "project_number",
}


def obtener_resource_id_dinamico() -> str:
    """
    Visita la pagina HTML del dataset y extrae dinamicamente el resource_id
    actualizado desde el enlace de la API de datastore_search.
    """
    print(f"Obteniendo resource_id dinamicamente desde: {URL_DATASET_WEB} ...", flush=True)
    respuesta = requests.get(URL_DATASET_WEB, headers=CABECERAS, timeout=TIMEOUT_PETICION)
    respuesta.raise_for_status()
    html = respuesta.text

    # Buscar patrones como: datastore_search?resource_id=5d3c0ea2-d1f5-4006-94bf-f55e18c5b20d
    coincidencia = re.search(r"datastore_search\?resource_id=([a-f0-9\-]{36})", html)
    if not coincidencia:
        # Fallback alternativo buscando en formato JSON embebido o atributos data
        coincidencia = re.search(r"resource_id['\"]?\s*[:=]\s*['\"]([a-f0-9\-]{36})['\"]", html)
        
    if not coincidencia:
        raise RuntimeError("No se pudo extraer automaticamente el resource_id de la pagina del dataset.")

    resource_id = coincidencia.group(1)
    print(f"-> Resource ID detectado con exito: {resource_id}", flush=True)
    return resource_id


def _consultar_datastore(resource_id: str, offset: int = 0, limit: int = 1, sort_field: str = None) -> dict:
    parametros = {"resource_id": resource_id, "limit": limit, "offset": offset}
    if sort_field:
        parametros["sort"] = f"{sort_field} desc"
        
    respuesta = requests.get(ENDPOINT_DATASTORE_SEARCH, params=parametros, timeout=TIMEOUT_PETICION, headers=CABECERAS)
    respuesta.raise_for_status()
    cuerpo = respuesta.json()
    if not cuerpo.get("success"):
        raise RuntimeError(f"La API de datos abiertos del BID devolvio un error: {cuerpo}")
    return cuerpo["result"]


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
    codigo_unico = f"BID-{_generar_slug(referencia or '')}-{_generar_slug(titulo)}"

    tipo_aviso = _extraer_valor_plano(registro, mapeo.get("tipo"))

    fecha_publicacion = _parsear_fecha(registro.get(mapeo.get("fecha_publicacion"))) if mapeo.get("fecha_publicacion") else None

    return {
        "codigo_unico": codigo_unico[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": tipo_aviso,
        "titulo": titulo,
        "descripcion": _extraer_valor_plano(registro, mapeo.get("descripcion")),
        "pais": _extraer_valor_plano(registro, mapeo.get("pais")),
        "organismo": _extraer_valor_plano(registro, mapeo.get("organismo")),
        "categoria": None,
        "url_oficial": _extraer_valor_plano(registro, mapeo.get("url")) or URL_FICHA_GENERICA,
        "url_documento": _extraer_valor_plano(registro, mapeo.get("url")),
        "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
        "fecha_limite": None,
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


def ejecutar_sincronizacion():
    hoy = date.today()
    desde = hoy - timedelta(days=DIAS_ATRAS)

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - BID (IADB)", flush=True)
    print("=" * 100, flush=True)
    print(f"Ventana: {desde} .. {hoy}", flush=True)

    try:
        # Obtener dinámicamente el resource_id actual en cada ejecución
        resource_id_actual = obtener_resource_id_dinamico()
    except Exception as error:
        print(f"Error obteniendo el resource_id de forma dinamica: {error}", flush=True)
        return

    try:
        resultado_inicial = _consultar_datastore(resource_id_actual, offset=0, limit=1)
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
        print("\nNo se ha podido identificar la columna de titulo. Abortando.", flush=True)
        return

    columna_fecha_orden = mapeo.get("fecha_publicacion")

    candidatos = []
    offset = 0

    print(f"\nDescargando registros ordenados por '{columna_fecha_orden}' (descendente)...", flush=True)
    while offset < MAX_REGISTROS_SEGURIDAD:
        try:
            resultado = _consultar_datastore(resource_id_actual, offset=offset, limit=TAMANO_PAGINA, sort_field=columna_fecha_orden)
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

    if not candidatos:
        return

    # Diagnóstico rápido de fechas
    fechas_muestra = [r.get(columna_fecha_orden) for r in candidatos[:5]]
    print(f"Las 5 fechas más recientes en la API del BID son: {fechas_muestra}", flush=True)

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

# -*- coding: utf-8 -*-
"""
ingesta_ted.py
----------------
Sincroniza avisos de licitación de TED (Tenders Electronic Daily -- el
diario oficial de contratación pública de la UE) relacionados con GIZ
contra la tabla `licitaciones_internacionales` de Supabase.

CAMBIO DE ARQUITECTURA DELIBERADO -- API OFICIAL EN VEZ DE PLAYWRIGHT
--------------------------------------------------------------------------
El usuario avisó de que la tabla de resultados de TED usa clases
dinámicas de Material UI/React, y pidió selectores semánticos robustos
para evitar errores. Investigando esto se encontró algo mejor: TED tiene
una API REST OFICIAL, gratuita y SIN CLAVE para búsqueda de avisos
(confirmado por la documentación oficial en docs.ted.europa.eu/api y
por múltiples proyectos de terceros que ya la usan en producción):

    POST https://api.ted.europa.eu/v3/notices/search

Usar esta API en vez de Playwright es strictly mejor para este caso
concreto: nunca se rompe por un cambio de clases CSS/React, no hace
falta arrancar un navegador, y devuelve JSON estructurado en vez de HTML.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local:      python ingesta_ted.py
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

URL_API_BUSQUEDA = "https://api.ted.europa.eu/v3/notices/search"
URL_API_AVISO = "https://api.ted.europa.eu/v3/notices/"
URL_BASE_AVISO = "https://ted.europa.eu/en/notice/-/detail/"
FUENTE = "TED"
CONSULTA = 'FT~"GIZ" SORT BY publication-date DESC'
CAMPOS_SOLICITADOS = [
    "publication-number", "notice-title", "buyer-name", "buyer-country",
    "publication-date", "deadline", "notice-type",
]
ALCANCE = "ACTIVE"   # avisos actualmente activos -- ver docstring
LIMITE_POR_PAGINA = 50
MAX_PAGINAS = 10
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15
CAMPOS_COMPARABLES = ("titulo", "pais", "fecha_publicacion", "fecha_limite")

CABECERAS_PETICION = {"Content-Type": "application/json", "Accept": "application/json"}

PAISES_ISO3_TED = {
    "AUT": "Austria", "BEL": "Bélgica", "BGR": "Bulgaria", "HRV": "Croacia",
    "CYP": "Chipre", "CZE": "República Checa", "DNK": "Dinamarca", "EST": "Estonia",
    "FIN": "Finlandia", "FRA": "Francia", "DEU": "Alemania", "GRC": "Grecia",
    "HUN": "Hungría", "ISL": "Islandia", "IRL": "Irlanda", "ITA": "Italia",
    "LVA": "Letonia", "LIE": "Liechtenstein", "LTU": "Lituania", "LUX": "Luxemburgo",
    "MLT": "Malta", "NLD": "Países Bajos", "NOR": "Noruega", "POL": "Polonia",
    "PRT": "Portugal", "ROU": "Rumanía", "SVK": "Eslovaquia", "SVN": "Eslovenia",
    "ESP": "España", "SWE": "Suecia", "CHE": "Suiza", "GBR": "Reino Unido",
    "UKR": "Ucrania", "MDA": "Moldavia", "XKX": "Kosovo",
}


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def _valor_multiidioma(valor):
    if valor is None:
        return None
    if isinstance(valor, str):
        return valor.strip() or None
    if isinstance(valor, list):
        return _valor_multiidioma(valor[0]) if valor else None
    if isinstance(valor, dict):
        for idioma in ("eng", "en"):
            if idioma in valor and valor[idioma]:
                return _valor_multiidioma(valor[idioma])
        for lista in valor.values():
            resultado = _valor_multiidioma(lista)
            if resultado:
                return resultado
    return None


def _limpiar_prefijo_titulo(titulo: str) -> str:
    """Elimina prefijos del tipo 'Germany – ', 'France – ', etc. al inicio del título."""
    if not titulo:
        return None
    titulo_limpio = re.sub(r"^[A-Za-z\s]+[–\-]\s*", "", titulo)
    return titulo_limpio.strip()


def parsear_fecha_ted(valor):
    texto = _valor_multiidioma(valor) if isinstance(valor, (dict, list)) else valor
    if not texto:
        return None
    texto = str(texto).strip()[:10]
    try:
        return datetime.strptime(texto, "%Y-%m-%d").date()
    except ValueError:
        return None


def _pagina_de_resultados(token_siguiente: str = None) -> dict:
    cuerpo = {
        "query": CONSULTA,
        "fields": CAMPOS_SOLICITADOS,
        "limit": LIMITE_POR_PAGINA,
        "scope": ALCANCE,
        "paginationMode": "ITERATION",
    }
    if token_siguiente:
        cuerpo["iterationNextToken"] = token_siguiente

    respuesta = requests.post(
        URL_API_BUSQUEDA, json=cuerpo, headers=CABECERAS_PETICION, timeout=TIMEOUT_PETICION
    )
    respuesta.raise_for_status()
    return respuesta.json()


def obtener_descripcion_procedimiento(numero_publicacion: str) -> str:
    """Obtiene la descripción detallada desde el endpoint individual de la API de TED."""
    if not numero_publicacion:
        return None
    
    try:
        url_detalle = f"{URL_API_AVISO}{numero_publicacion}"
        resp = requests.get(url_detalle, headers={"Accept": "application/json"}, timeout=15)
        if resp.status_code == 200:
            datos = resp.json()
            desc = (
                _valor_multiidioma(datos.get("procedure-description")) or
                _valor_multiidioma(datos.get("description")) or
                _valor_multiidioma(datos.get("notice-description"))
            )
            if desc:
                return desc
    except Exception:
        pass
    return None


def extraer_avisos_api() -> list:
    avisos = []
    token_siguiente = None

    for indice_pagina in range(1, MAX_PAGINAS + 1):
        print(f"--> Consultando la API de TED (página {indice_pagina})...", flush=True)
        try:
            cuerpo_respuesta = _pagina_de_resultados(token_siguiente)
        except Exception as error:
            print(f"    Error consultando la API de TED: {error}", flush=True)
            break

        resultados_pagina = cuerpo_respuesta.get("notices") or cuerpo_respuesta.get("results") or []
        print(f"    Avisos recibidos en esta página: {len(resultados_pagina)}", flush=True)
        avisos.extend(resultados_pagina)

        token_siguiente = cuerpo_respuesta.get("iterationNextToken")
        if not token_siguiente or not resultados_pagina:
            break

        time.sleep(0.3)

    return avisos


def construir_registro(aviso: dict) -> dict:
    numero_publicacion = _valor_multiidioma(aviso.get("publication-number")) or ""
    
    # Limpieza de prefijo de país en el título
    titulo_raw = _valor_multiidioma(aviso.get("notice-title"))
    titulo = _limpiar_prefijo_titulo(titulo_raw)
    
    comprador = _valor_multiidioma(aviso.get("buyer-name"))
    codigo_pais = _valor_multiidioma(aviso.get("buyer-country"))
    pais = PAISES_ISO3_TED.get((codigo_pais or "").upper(), codigo_pais) if codigo_pais else None
    tipo_aviso = _valor_multiidioma(aviso.get("notice-type"))

    fecha_publicacion = parsear_fecha_ted(aviso.get("publication-date"))
    fecha_limite = parsear_fecha_ted(aviso.get("deadline"))

    # Obtención de la descripción de procedimiento vía API de detalle
    descripcion = obtener_descripcion_procedimiento(numero_publicacion)
    
    if not descripcion:
        partes_descripcion = []
        if comprador:
            partes_descripcion.append(f"Organismo comprador: {comprador}.")
        if tipo_aviso:
            partes_descripcion.append(f"Tipo de aviso: {tipo_aviso}.")
        descripcion = " ".join(partes_descripcion) or None

    slug_base = numero_publicacion or _generar_slug(titulo or "sin-titulo")
    url_oficial = f"{URL_BASE_AVISO}{numero_publicacion}" if numero_publicacion else None

    return {
        "codigo_unico": f"TED-{_generar_slug(slug_base)}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": tipo_aviso,
        "titulo": titulo,
        "descripcion": descripcion,
        "pais": pais,
        "paises": [pais] if pais else [],
        "organismo": comprador or "GIZ",
        "categoria": None,
        "url_oficial": url_oficial,
        "url_documento": None,
        "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
        "_fecha_pub_obj": fecha_publicacion,  # Campo auxiliar para el filtrado por fechas
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

        # Limpiamos la clave auxiliar temporal antes de enviar
        datos_enviar = {k: v for k, v in datos.items() if k != "_fecha_pub_obj"}

        if existente is None:
            datos_enviar["texto_completo"] = texto_completo
            datos_enviar["embedding"] = generar_embedding(texto_completo)
            datos_enviar["es_novedad"] = True
            datos_enviar["es_actualizada"] = False
            a_subir.append(datos_enviar)
            continue

        ha_cambiado = any(
            str(existente.get(campo)) != str(datos_enviar.get(campo)) for campo in CAMPOS_COMPARABLES
        )
        if not ha_cambiado:
            continue

        datos_enviar["texto_completo"] = texto_completo
        datos_enviar["embedding"] = generar_embedding(texto_completo)
        datos_enviar["es_novedad"] = False
        datos_enviar["es_actualizada"] = True
        a_subir.append(datos_enviar)

    return a_subir


def ejecutar_sincronizacion():
    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - TED (GIZ, via API oficial)", flush=True)
    print("=" * 100, flush=True)
    print(f"Consulta: {CONSULTA}  ·  Alcance: {ALCANCE}", flush=True)

    crudos = extraer_avisos_api()
    print(f"\nTotal avisos recibidos de la API (todas las páginas): {len(crudos)}", flush=True)

    if not crudos:
        print("No se ha recibido ningún aviso.", flush=True)
        return

    normalizados = [construir_registro(a) for a in crudos]

    # --- FILTRO MANUAL EN PYTHON: solo conservar registros de AYER y HOY ---
    hoy = date.today()
    ayer = hoy - timedelta(days=1)
    
    normalizados_filtrados = [
        reg for reg in normalizados 
        if reg.get("_fecha_pub_obj") and (ayer <= reg["_fecha_pub_obj"] <= hoy)
    ]

    print(f"Avisos tras filtrar fecha de publicación (Ayer {ayer} - Hoy {hoy}): {len(normalizados_filtrados)}", flush=True)

    if not normalizados_filtrados:
        print("No hay avisos publicados entre ayer y hoy para procesar.", flush=True)
        return

    supabase = obtener_cliente_supabase()

    print("\nComparando con lo ya existente en Supabase...", flush=True)
    claves_validas = [n["codigo_unico"] for n in normalizados_filtrados if n.get("titulo") and n.get("url_oficial")]
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico") + CAMPOS_COMPARABLES,
        claves=claves_validas,
    )

    lote_final = preparar_lote_para_subir(normalizados_filtrados, registros_existentes)

    if not lote_final:
        print("No hay avisos nuevos ni cambios que sincronizar.", flush=True)
        return

    subidas = subir_en_lotes(
        supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
    )
    print(f"\nSincronizacion TED completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

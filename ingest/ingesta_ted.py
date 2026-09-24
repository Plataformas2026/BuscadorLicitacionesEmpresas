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

Usar esta API en vez de Playwright es estrictamente mejor para este caso
concreto: nunca se rompe por un cambio de clases CSS/React (el propio
problema que motivó el aviso de selectores), no hace falta arrancar un
navegador, y devuelve JSON estructurado en vez de HTML que parsear. Por
eso ESTE script, a diferencia de ingesta_bid.py/ingesta_undp.py/etc, NO
usa Playwright -- es el único caso de las fuentes de este proyecto donde
existe una API pública mejor que la propia tabla web.

AVISO DE FIABILIDAD -- CONTRATO DE LA API NO VERIFICADO EN VIVO
--------------------------------------------------------------------------
No hay salida de red hacia api.ted.europa.eu en este entorno de
desarrollo, así que no se ha podido hacer una llamada real. El formato
de petición/respuesta de abajo (campos, sintaxis de consulta, formato
multi-idioma de título/comprador) se ha reconstruido a partir de la
documentación oficial y de varios proyectos de terceros ya en
producción contra esta misma API -- son fuentes consistentes entre sí,
pero conviene confirmarlo con una ejecución manual (workflow_dispatch)
antes de fiarse del cron automático.

QUERY Y ALCANCE
------------------
Se traduce la búsqueda del usuario (FT=GIZ, search-scope=ACTIVE) a la
sintaxis de consulta experta de la API: `FT~"GIZ"` (texto libre) con
`scope: "ACTIVE"` (solo avisos actualmente activos -- la propia API ya
acota a lo vigente, así que no hace falta una ventana de días aparte,
igual que en ingesta_bcie.py). Paginación en modo ITERATION (token
devuelto por cada llamada), con un tope de seguridad MAX_PAGINAS.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_ted.py
Ejecucion programada: ver .github/workflows/sincronizar_ted.yml
   (NO necesita Playwright/navegador -- solo requests)
"""
import re
import time
from datetime import date, datetime

import requests

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

URL_API_BUSQUEDA = "https://api.ted.europa.eu/v3/notices/search"
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

# Países UE/EEE por su código ISO 3166-1 alfa-3 (el que devuelve
# 'buyer-country' en esta API) -- ver aviso de fiabilidad: acotado a los
# países que realmente pueden aparecer en TED, no una lista global.
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
    """
    Los campos de texto de esta API llegan como {"eng": [...], "fra":
    [...], ...} -- un diccionario de idioma -> lista de valores -- en
    vez de una cadena simple. Se prefiere inglés y, si no está, el
    primer idioma que haya. Devuelve None si no hay nada aprovechable.
    """
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


def parsear_fecha_ted(valor):
    """Las fechas de esta API llegan en ISO 8601 ('2026-09-21' o con hora/zona)."""
    texto = _valor_multiidioma(valor) if isinstance(valor, (dict, list)) else valor
    if not texto:
        return None
    texto = str(texto).strip()[:10]   # solo la parte de fecha, por si trae hora
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


def extraer_avisos_api() -> list:
    """Recorre hasta MAX_PAGINAS páginas de la API (modo ITERATION), devolviendo todos los avisos vistos."""
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
    titulo = _valor_multiidioma(aviso.get("notice-title"))
    comprador = _valor_multiidioma(aviso.get("buyer-name"))
    codigo_pais = _valor_multiidioma(aviso.get("buyer-country"))
    pais = PAISES_ISO3_TED.get((codigo_pais or "").upper(), codigo_pais) if codigo_pais else None
    tipo_aviso = _valor_multiidioma(aviso.get("notice-type"))

    fecha_publicacion = parsear_fecha_ted(aviso.get("publication-date"))
    fecha_limite = parsear_fecha_ted(aviso.get("deadline"))

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
    }


def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
        if not datos.get("titulo") or not datos.get("url_oficial"):
            continue   # sin numero de publicacion no hay forma fiable de identificar el aviso

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
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - TED (GIZ, via API oficial)", flush=True)
    print("=" * 100, flush=True)
    print(f"Consulta: {CONSULTA}  ·  Alcance: {ALCANCE}", flush=True)

    crudos = extraer_avisos_api()
    print(f"\nTotal avisos recibidos de la API (todas las páginas): {len(crudos)}", flush=True)

    if not crudos:
        print(
            "No se ha recibido ningún aviso. Revisa el log de arriba -- si hay un error HTTP, "
            "lo más probable es que el contrato de la API (campos/sintaxis) haya cambiado desde "
            "que se escribió este script (ver aviso de fiabilidad en el docstring).",
            flush=True,
        )
        return

    normalizados = [construir_registro(a) for a in crudos]

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
    print(f"\nSincronizacion TED completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

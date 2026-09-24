# -*- coding: utf-8 -*-
"""
ingesta_ted.py
----------------
Sincroniza avisos de licitación de TED (Tenders Electronic Daily)
relacionados con GIZ publicados entre AYER y HOY contra Supabase.
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

# Campos solicitados
CAMPOS_SOLICITADOS = [
    "publication-number", "notice-title", "buyer-name", "buyer-country",
    "publication-date", "deadline", "notice-type", "procedure-description", "description"
]
ALCANCE = "ACTIVE"
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
    """1. Quita prefijos tipo 'Germany – ', 'France – ', etc. del título."""
    if not titulo:
        return None
    # Elimina país + guión largo (–) o corto (-) al inicio
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


def obtener_consulta_rango_fechas() -> str:
    """Cambio 1: Genera la consulta limitando a publicaciones entre AYER y HOY."""
    hoy = date.today()
    ayer = hoy - timedelta(days=1)
    
    fecha_ayer_str = ayer.strftime("%Y%m%d")
    fecha_hoy_str = hoy.strftime("%Y%m%d")
    
    # Sintaxis experta de TED para rango de fechas
    return f'FT~"GIZ" AND publication-date>={fecha_ayer_str} AND publication-date<={fecha_hoy_str} SORT BY publication-date DESC'


def _pagina_de_resultados(consulta: str, token_siguiente: str = None) -> dict:
    cuerpo = {
        "query": consulta,
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
    """
    Cambio 3: Obtiene la descripción detallada de la sección 2 (Procedure Description)
    directamente de la API de detalle del aviso sin necesidad de Playwright.
    """
    if not numero_publicacion:
        return None
    
    try:
        url_detalle = f"{URL_API_AVISO}{numero_publicacion}"
        resp = requests.get(url_detalle, headers={"Accept": "application/json"}, timeout=15)
        if resp.status_code == 200:
            datos = resp.json()
            # Intenta obtener la descripción del procedimiento (BT-24-Procedure / description)
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
    consulta = obtener_consulta_rango_fechas()

    for indice_pagina in range(1, MAX_PAGINAS + 1):
        print(f"--> Consultando la API de TED (página {indice_pagina})...", flush=True)
        try:
            cuerpo_respuesta = _pagina_de_resultados(consulta, token_siguiente)
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
    
    # 2. Limpieza de prefijo "Germany – " en el título
    titulo_raw = _valor_multiidioma(aviso.get("notice-title"))
    titulo = _limpiar_prefijo_titulo(titulo_raw)
    
    comprador = _valor_multiidioma(aviso.get("buyer-name"))
    codigo_pais = _valor_multiidioma(aviso.get("buyer-country"))
    pais = PAISES_ISO3_TED.get((codigo_pais or "").upper(), codigo_pais) if codigo_pais else None
    tipo_aviso = _valor_multiidioma(aviso.get("notice-type"))

    fecha_publicacion = parsear_fecha_ted(aviso.get("publication-date"))
    fecha_limite = parsear_fecha_ted(aviso.get("deadline"))

    # 3. Obtención de la descripción desde el procedimiento/detalle
    descripcion = obtener_descripcion_procedimiento(numero_publicacion)
    
    # Fallback en caso de que no haya descripción detallada
    if not descripcion:
        partes = []
        if comprador:
            partes.append(f"Organismo comprador: {comprador}.")
        if tipo_aviso:
            partes.append(f"Tipo de aviso: {tipo_aviso}.")
        descripcion = " ".join(partes) or None

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
            continue

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
    consulta_activa = obtener_consulta_rango_fechas()
    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - TED (GIZ, via API oficial)", flush=True)
    print("=" * 100, flush=True)
    print(f"Consulta: {consulta_activa}  ·  Alcance: {ALCANCE}", flush=True)

    crudos = extraer_avisos_api()
    print(f"\nTotal avisos recibidos de la API (todas las páginas): {len(crudos)}", flush=True)

    if not crudos:
        print("No se ha recibido ningún aviso para las fechas solicitadas (Ayer - Hoy).", flush=True)
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

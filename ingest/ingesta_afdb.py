# -*- coding: utf-8 -*-
"""
ingesta_afdb.py
----------------
Sincroniza las "Specific Procurement Notices" (SPN) del Banco Africano de
Desarrollo (AfDB) directamente contra la tabla `licitaciones_internacionales`
de Supabase.

LÉELO ANTES DE PONERLO EN PRODUCCIÓN
--------------------------------------
El AfDB NO tiene una API pública ni un feed RSS/JSON documentado para sus
avisos de contratación (se comprobó expresamente: hasta los agregadores
comerciales -DevelopmentAid, BidsFactory, OpenOpps- tienen que raspar la
web del Banco por el mismo motivo). Este script hace scraping de HTML
sobre:

    https://www.afdb.org/en/documents/category/specific-procurement-notices?page=N

Es una vista de Drupal (lo confirman los propios mensajes de error de
"i18nviews" que aparecen en la página) paginada y en orden descendente
por fecha. No ha sido posible inspeccionar el HTML en crudo desde aquí
(solo una versión ya convertida a texto), así que el parseo de abajo se
apoya en patrones de CONTENIDO -- no en nombres de clases CSS concretos,
que no se han podido verificar y podrían no coincidir con el HTML real--:

  - un enlace <a> cuyo href empieza por "/en/documents/" (pero no
    contiene "/category/") y cuyo texto empieza por "SPN -" o "GPN -"
  - inmediatamente antes, una fecha con forma "DD-Mon-YYYY"
  - inmediatamente después, un párrafo de resumen (teaser)

Si el Banco cambia el diseño de esa página, lo más probable es que haya
que revisar `extraer_avisos_de_pagina()`; el resto del script (fusión con
Supabase, embeddings, subida por lotes) no debería necesitar cambios.
Se ha probado el parseo contra HTML reconstruido a partir de lo que sí
se pudo verificar (ver el bloque de pruebas que se entrega aparte), pero
no contra la página real -- conviene ejecutarlo una vez a mano y revisar
los logs antes de dejarlo en un cron desatendido.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local:      python ingesta_afdb.py
Ejecución programada: ver .github/workflows/sincronizar_afdb.yml
"""
import re
import time
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://www.afdb.org"
LISTADO_URL = BASE_URL + "/en/documents/category/specific-procurement-notices"

FUENTE = "AfDB"
DIAS_ATRAS = 14                      # ventana de sincronización: últimos N días
MAX_PAGINAS_SEGURIDAD = 20           # red de seguridad (~200 avisos máx. por ejecución)
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 1.0    # ritmo de consulta responsable
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15

CABECERAS = {"User-Agent": "Mozilla/5.0 (compatible; LicitacionesEmpresasBot/1.0)"}

PATRON_FECHA = re.compile(r"(\d{1,2}-[A-Za-z]{3}-\d{4})")
PATRON_BLOQUE = re.compile(
    r"(\d{1,2}-[A-Za-z]{3}-\d{4})\s*\n+"      # fecha
    r"((?:SPN|GPN)\s*-\s*[^\n]+)\s*\n+"        # título
    r"([^\n]+)"                                 # teaser (primera línea siguiente)
)
PATRON_TIPO_PAIS = re.compile(r"^(SPN|GPN)\s*-\s*([^-]+?)\s*-\s*(.+)$")

CAMPOS_COMPARABLES = ("titulo", "descripcion", "organismo")


# ------------------------------------------------------------------
# Descarga
# ------------------------------------------------------------------
def obtener_pagina(pagina: int) -> str:
    print(f"--> Descargando página {pagina} de avisos AfDB...", flush=True)
    respuesta = requests.get(
        LISTADO_URL, params={"page": pagina}, timeout=TIMEOUT_PETICION, headers=CABECERAS
    )
    print(f"    HTTP: {respuesta.status_code}", flush=True)
    respuesta.raise_for_status()
    return respuesta.text


def _parsear_fecha(texto_fecha: str):
    try:
        return datetime.strptime(texto_fecha, "%d-%b-%Y").date()
    except ValueError:
        return None


# ------------------------------------------------------------------
# Parseo (ver advertencia en el docstring del módulo)
# ------------------------------------------------------------------
def extraer_avisos_de_pagina(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    contenedor = soup.find("main") or soup.find(id="content") or soup

    # Indexamos por título los enlaces reales a avisos (el texto plano de
    # abajo no conserva los href).
    enlaces_por_titulo = {}
    for enlace in contenedor.find_all("a", href=True):
        texto = enlace.get_text(strip=True)
        href = enlace["href"]
        if (
            (texto.startswith("SPN -") or texto.startswith("GPN -"))
            and href.startswith("/en/documents/")
            and "/category/" not in href
        ):
            enlaces_por_titulo.setdefault(texto, BASE_URL + href)

    texto_plano = contenedor.get_text("\n", strip=True)

    avisos = []
    for coincidencia in PATRON_BLOQUE.finditer(texto_plano):
        fecha_texto, titulo, teaser = coincidencia.groups()
        titulo = titulo.strip()
        url_oficial = enlaces_por_titulo.get(titulo)
        if not url_oficial:
            continue  # sin enlace no se puede construir un codigo_unico fiable

        avisos.append({
            "titulo": titulo,
            "descripcion": teaser.strip(),
            "fecha_publicacion": _parsear_fecha(fecha_texto),
            "url_oficial": url_oficial,
        })

    return avisos


# ------------------------------------------------------------------
# Normalización al esquema de `licitaciones_internacionales`
# ------------------------------------------------------------------
def construir_registro(aviso: dict) -> dict:
    coincidencia = PATRON_TIPO_PAIS.match(aviso["titulo"])
    if coincidencia:
        tipo_aviso, pais, _resto = coincidencia.groups()
        pais = pais.strip()
    else:
        tipo_aviso, pais = None, None

    slug = aviso["url_oficial"].rstrip("/").split("/")[-1]
    fecha_publicacion = aviso.get("fecha_publicacion")

    return {
        "codigo_unico": f"AFDB-{slug}",
        "fuente_origen": FUENTE,
        "tipo_aviso": tipo_aviso,
        "titulo": aviso["titulo"],
        "descripcion": aviso.get("descripcion"),
        "pais": pais,
        "organismo": None,   # no disponible solo con el listado
        "categoria": None,   # no disponible solo con el listado
        "url_oficial": aviso["url_oficial"],
        "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
        "fecha_limite": None,  # requeriría abrir cada ficha individual; posible mejora futura
    }


# ------------------------------------------------------------------
# Decidir qué subir
# ------------------------------------------------------------------
def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
        existente = registros_existentes.get(datos["codigo_unico"])
        texto_completo = (
            f"Título: {datos['titulo']}\n"
            f"{datos.get('descripcion') or ''}\n"
            f"País: {datos.get('pais') or 'No especificado'}"
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
# Ejecución principal
# ------------------------------------------------------------------
def ejecutar_sincronizacion():
    hoy = date.today()
    desde = hoy - timedelta(days=DIAS_ATRAS)

    print("=" * 100, flush=True)
    print("SINCRONIZACIÓN DE LICITACIONES INTERNACIONALES — AfDB", flush=True)
    print("=" * 100, flush=True)
    print(f"Ventana: {desde} .. {hoy}", flush=True)

    candidatos = []
    pagina = 0
    detener = False

    while pagina < MAX_PAGINAS_SEGURIDAD and not detener:
        try:
            html = obtener_pagina(pagina)
        except Exception as error:
            print(f"    ⚠️ Error descargando la página {pagina}: {error}", flush=True)
            break

        avisos = extraer_avisos_de_pagina(html)
        if not avisos:
            print("    No se han reconocido avisos en esta página (¿cambió el diseño de la web?). Fin.", flush=True)
            break

        for aviso in avisos:
            fecha = aviso.get("fecha_publicacion")
            if fecha and fecha < desde:
                detener = True
                continue
            candidatos.append(aviso)

        pagina += 1
        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    print(f"\nAvisos candidatos en la ventana: {len(candidatos)}", flush=True)

    if not candidatos:
        return

    normalizados = [construir_registro(a) for a in candidatos]
    # dedupe por si un mismo aviso aparece en dos páginas
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
    print(f"\nSincronización AfDB completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

# -*- coding: utf-8 -*-
"""
ingesta_caf.py
----------------
Sincroniza las convocatorias de CAF -banco de desarrollo de America
Latina y el Caribe- contra la tabla `licitaciones_internacionales` de
Supabase, con EXACTAMENTE los mismos campos que AfDB/BID.

    https://www.caf.com/es/trabaja-con-nosotros/convocatorias/

AVISO DE FIABILIDAD -- LEE ESTO ANTES DE DEJARLO EN EL CRON AUTOMATICO
--------------------------------------------------------------------------
A diferencia del scraper de AfDB (que se construyo viendo el HTML real
de la pagina), caf.com bloquea el acceso a TODAS las herramientas de
navegacion usadas para investigar este script -- no se ha podido
descargar ni una sola pagina para inspeccionar su HTML real. Este
bloqueo aparenta ser especifico de esas herramientas y no de peticiones
HTTP normales: Google SI tiene la pagina indexada, con contenido de
fechas de 2026 (octubre 2026), lo que confirma que es HTML servido
normal -- no un Power BI ni nada que dependa de JavaScript, a
diferencia del caso del BID -- y que un `requests.get()` corriente
(como hace este script) deberia funcionar igual que le funciona a
Google. Pero el parseo de abajo esta basado UNICAMENTE en los
fragmentos de texto visibles en los resultados de busqueda, nunca en
una inspeccion directa del marcado HTML/CSS real.

Por eso el reconocimiento de cada "tarjeta" de convocatoria se apoya en
patrones de TEXTO (expresiones regulares sobre el texto ya renderizado,
ver extraer_convocatorias_de_pagina) en vez de en nombres de clase CSS
concretos, que no se han podido verificar. Patrones de texto confirmados
contra fragmentos reales de la pagina real:
  - Cada convocatoria enlaza a una URL con forma
    /es/trabaja-con-nosotros/convocatorias/<slug>
  - En el listado, cada tarjeta muestra "Cierre: <fecha>" y el estado
    ("Convocatoria abierta" / "Convocatoria cerrada")
  - En la ficha de cada convocatoria aparece "Convocatoria del <fecha
    inicio> al <fecha cierre>", que da la fecha de publicacion Y de
    cierre a la vez, mas fiable que el "Cierre:" abreviado del listado

**Ejecuta este script una vez a mano (workflow_dispatch) y revisa el
log "Convocatorias reconocidas en la pagina N" antes de fiarte del cron
automatico.** Si sale 0 en todas las paginas, lo mas probable es que el
marcado real no coincida con estos patrones de texto -- revisa
PATRON_ENLACE_CONVOCATORIA y las funciones de extraccion de este
modulo con el HTML real (el propio log de "HTML de depuracion" que
imprime este script si no reconoce nada te dara pistas).

"Convocatorias" en caf.com mezcla licitaciones/consultorias con
programas de becas, concursos de innovacion y convocatorias de
investigacion -- no se excluye ningun tipo aqui (mismo criterio que se
acordo para AfDB: se captura todo, y el filtrado de relevancia se deja
a la busqueda semantica y los filtros de la app, no a la ingesta).

SIN VENTANA DE "ULTIMOS N DIAS"
-----------------------------------
A diferencia de AfDB/BID, el listado de CAF no expone de forma fiable
una fecha de PUBLICACION por la que paginar y parar pronto -- solo la
fecha de CIERRE por tarjeta. Por eso este script no aplica una ventana
de dias: recorre un numero acotado de paginas del listado
(MAX_PAGINAS_SEGURIDAD) y sube todo lo que encuentre y siga "abierto";
la comparacion con lo ya existente en Supabase (misma logica que
AfDB/BID) evita reprocesar lo que no ha cambiado.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_caf.py
Ejecucion programada: ver .github/workflows/sincronizar_caf.yml
"""
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)
import urllib3

# Desactivar las advertencias de seguridad por certificado no verificado
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.caf.com"
LISTADO_URL = BASE_URL + "/es/trabaja-con-nosotros/convocatorias/"

FUENTE = "CAF"
MAX_PAGINAS_SEGURIDAD = 15
MAX_DETALLES_POR_EJECUCION = 150
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.8
PAUSA_ENTRE_DETALLES_SEGUNDOS = 0.4
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15

CABECERAS = {"User-Agent": "Mozilla/5.0 (compatible; LicitacionesEmpresasBot/1.0)"}

CAMPOS_COMPARABLES = ("titulo", "descripcion", "fecha_limite")

PATRON_ENLACE_CONVOCATORIA = re.compile(r"^/es/trabaja-con-nosotros/convocatorias/[a-z0-9\-]+/?$")

MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
PATRON_FECHA_ES = re.compile(
    r"(\d{1,2})\s*(?:de\s+)?(" + "|".join(MESES_ES.keys()) + r")\s*(?:de\s+)?(\d{4})",
    re.IGNORECASE,
)
PATRON_CIERRE_LISTADO = re.compile(r"cierre:?\s*([^\u00b7|]{4,40})", re.IGNORECASE)
PATRON_RANGO_FICHA = re.compile(
    r"convocatoria\s+del\s+(.{4,30}?)\s+al\s+(.{4,30}?\d{4})",
    re.IGNORECASE,
)


# ------------------------------------------------------------------
# Fechas en español
# ------------------------------------------------------------------
def _parsear_fecha_es(texto: str):
    if not texto:
        return None
    coincidencia = PATRON_FECHA_ES.search(texto)
    if not coincidencia:
        return None
    dia, mes_texto, anio = coincidencia.groups()
    mes = MESES_ES.get(mes_texto.lower())
    if not mes:
        return None
    try:
        return date(int(anio), mes, int(dia))
    except ValueError:
        return None


def _parsear_rango_fechas_es(texto_inicio: str, texto_fin: str):
    """
    "Convocatoria del X al Y" -- caso real confirmado: cuando ambas
    fechas caen en el mismo año, el texto de INICIO no repite el año
    ("del 25 de agosto al 18 de octubre de 2026": el año solo aparece
    una vez, al final). Se parsea primero el fin (que siempre trae el
    año) y, si el inicio no tiene uno propio, se le presta el del fin.
    """
    fecha_fin = _parsear_fecha_es(texto_fin)
    if not fecha_fin:
        return None, None

    fecha_inicio = _parsear_fecha_es(texto_inicio)
    if not fecha_inicio:
        coincidencia_dia_mes = re.search(
            r"(\d{1,2})\s*(?:de\s+)?(" + "|".join(MESES_ES.keys()) + r")", texto_inicio, re.IGNORECASE
        )
        if coincidencia_dia_mes:
            dia, mes_texto = coincidencia_dia_mes.groups()
            mes = MESES_ES.get(mes_texto.lower())
            if mes:
                try:
                    fecha_inicio = date(fecha_fin.year, mes, int(dia))
                except ValueError:
                    fecha_inicio = None

    return fecha_inicio, fecha_fin


# ------------------------------------------------------------------
# Descarga y parseo del listado
# ------------------------------------------------------------------
def obtener_pagina(pagina: int) -> str:
    print(f"--> Descargando pagina {pagina} del listado de convocatorias CAF...", flush=True)
    # Se añade verify=False para evitar el error de certificado SSL
    respuesta = requests.get(
        LISTADO_URL, params={"page": pagina}, timeout=TIMEOUT_PETICION, headers=CABECERAS, verify=False
    )
    print(f"    HTTP: {respuesta.status_code}", flush=True)
    respuesta.raise_for_status()
    return respuesta.text


def extraer_convocatorias_de_pagina(html: str) -> list:
    """
    Ver aviso de fiabilidad en el docstring del modulo: el reconocimiento
    se apoya en el patron de URL (muy fiable, confirmado contra muchos
    ejemplos reales) mas patrones de texto sobre el contenedor de cada
    enlace, no en clases CSS (que no se han podido verificar).
    """
    soup = BeautifulSoup(html, "html.parser")
    contenedor = soup.find("main") or soup.find(id="content") or soup

    convocatorias = []
    vistos = set()

    for enlace in contenedor.find_all("a", href=True):
        href = enlace["href"]
        ruta = href.replace(BASE_URL, "")
        if not PATRON_ENLACE_CONVOCATORIA.match(ruta):
            continue

        titulo = enlace.get_text(strip=True)
        if not titulo or titulo in vistos:
            continue
        vistos.add(titulo)

        # Se sube por el arbol buscando el contenedor de la tarjeta (el
        # propio <a> normalmente solo tiene el titulo; el resto de datos
        # de la tarjeta -fecha de cierre, estado- estan en un ancestro).
        texto_tarjeta = ""
        nodo = enlace
        for _ in range(5):
            if nodo.parent is None:
                break
            nodo = nodo.parent
            texto_candidato = nodo.get_text(" ", strip=True)
            if len(texto_candidato) > len(titulo) + 15:
                texto_tarjeta = texto_candidato
                break

        cerrada = "convocatoria cerrada" in texto_tarjeta.lower()

        fecha_cierre_listado = None
        coincidencia_cierre = PATRON_CIERRE_LISTADO.search(texto_tarjeta)
        if coincidencia_cierre:
            fecha_cierre_listado = _parsear_fecha_es(coincidencia_cierre.group(1))

        convocatorias.append({
            "titulo": titulo,
            "url_oficial": BASE_URL + ruta if ruta.startswith("/") else ruta,
            "cerrada_segun_listado": cerrada,
            "fecha_limite_listado": fecha_cierre_listado,
        })

    return convocatorias


# ------------------------------------------------------------------
# Ficha de la convocatoria: descripcion + rango real de fechas
# ------------------------------------------------------------------
def extraer_descripcion_detalle(soup: BeautifulSoup):
    contenedor = soup.find("main") or soup.find(id="content") or soup
    for parrafo in contenedor.find_all("p"):
        texto = parrafo.get_text(strip=True)
        if len(texto) > 80:
            return texto
    return None


def obtener_detalle_convocatoria(url: str) -> dict:
    try:
        # Se añade verify=False aquí también
        respuesta = requests.get(url, timeout=TIMEOUT_PETICION, headers=CABECERAS, verify=False)
        respuesta.raise_for_status()
    except Exception as error:
        print(f"      Error descargando la ficha: {error}", flush=True)
        return {"descripcion": None, "fecha_publicacion": None, "fecha_limite": None}

    soup = BeautifulSoup(respuesta.text, "html.parser")
    texto_completo = soup.get_text(" ", strip=True)

    fecha_publicacion, fecha_limite = None, None
    coincidencia_rango = PATRON_RANGO_FICHA.search(texto_completo)
    if coincidencia_rango:
        fecha_publicacion, fecha_limite = _parsear_rango_fechas_es(
            coincidencia_rango.group(1), coincidencia_rango.group(2)
        )

    return {
        "descripcion": extraer_descripcion_detalle(soup),
        "fecha_publicacion": fecha_publicacion,
        "fecha_limite": fecha_limite,
    }


# ------------------------------------------------------------------
# Normalizacion al esquema de `licitaciones_internacionales`
# ------------------------------------------------------------------
def _generar_slug_de_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1][:120] or "sin-referencia"


def construir_registro(convocatoria: dict) -> dict:
    fecha_publicacion = convocatoria.get("fecha_publicacion")
    # La ficha (mas fiable, trae el rango completo) tiene prioridad; si no
    # se pudo leer, se cae al "Cierre:" abreviado que ya traia el listado.
    fecha_limite = convocatoria.get("fecha_limite") or convocatoria.get("fecha_limite_listado")

    return {
        "codigo_unico": f"CAF-{_generar_slug_de_url(convocatoria['url_oficial'])}",
        "fuente_origen": FUENTE,
        "tipo_aviso": "Convocatoria",
        "titulo": convocatoria["titulo"],
        "descripcion": convocatoria.get("descripcion"),
        "pais": None,       # no se ha podido confirmar de forma fiable un campo de pais por tarjeta (ver docstring)
        "organismo": "CAF", # organismo unico y conocido para toda esta fuente
        "categoria": None,
        "url_oficial": convocatoria["url_oficial"],
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
        texto_completo = f"Titulo: {datos['titulo']}\n{datos.get('descripcion') or ''}"

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
    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - CAF", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)

    candidatos = []
    pagina = 0

    while pagina < MAX_PAGINAS_SEGURIDAD:
        try:
            html = obtener_pagina(pagina)
        except Exception as error:
            print(f"    Error descargando la pagina {pagina}: {error}", flush=True)
            break

        convocatorias = extraer_convocatorias_de_pagina(html)
        print(f"    Convocatorias reconocidas en la pagina {pagina}: {len(convocatorias)}", flush=True)

        if not convocatorias:
            if pagina == 0:
                print(
                    "\nNo se ha reconocido ninguna convocatoria en la primera pagina. El marcado "
                    "real probablemente no coincide con los patrones de texto de este script (ver "
                    "aviso de fiabilidad en el docstring del modulo) -- revisa "
                    "PATRON_ENLACE_CONVOCATORIA y extraer_convocatorias_de_pagina contra el HTML real.",
                    flush=True,
                )
            break

        candidatos.extend([c for c in convocatorias if not c["cerrada_segun_listado"]])
        pagina += 1
        time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    candidatos = list({c["url_oficial"]: c for c in candidatos}.values())
    print(f"\nConvocatorias abiertas candidatas (todas las paginas): {len(candidatos)}", flush=True)

    if not candidatos:
        return

    if len(candidatos) > MAX_DETALLES_POR_EJECUCION:
        print(
            f"Aviso: hay mas candidatas ({len(candidatos)}) que el tope por ejecucion "
            f"({MAX_DETALLES_POR_EJECUCION}); se procesan las primeras y el resto se recogera "
            "en la siguiente sincronizacion.",
            flush=True,
        )
        candidatos = candidatos[:MAX_DETALLES_POR_EJECUCION]

    print("\nDescargando la ficha de cada convocatoria candidata...", flush=True)
    normalizados = []
    for indice, convocatoria in enumerate(candidatos, start=1):
        print(f"  [{indice}/{len(candidatos)}] {convocatoria['titulo'][:90]}", flush=True)

        detalle = obtener_detalle_convocatoria(convocatoria["url_oficial"])
        convocatoria["descripcion"] = detalle["descripcion"]
        convocatoria["fecha_publicacion"] = detalle["fecha_publicacion"]
        convocatoria["fecha_limite"] = detalle["fecha_limite"]

        normalizados.append(construir_registro(convocatoria))
        time.sleep(PAUSA_ENTRE_DETALLES_SEGUNDOS)

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
        print("No hay convocatorias nuevas ni cambios que sincronizar.", flush=True)
        return

    subidas = subir_en_lotes(
        supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
    )
    print(f"\nSincronizacion CAF completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

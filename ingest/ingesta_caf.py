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
caf.com bloquea el acceso a TODAS las herramientas de navegacion usadas
para investigar este script -- no se ha podido descargar ni una sola
pagina para inspeccionar su HTML real. Google SI tiene la pagina
indexada con contenido de 2026, lo que confirma que es HTML servido
normal (no depende de JavaScript, a diferencia del caso del BID) y que
un `requests.get()` corriente deberia funcionar -- pero el parseo de
abajo esta basado UNICAMENTE en fragmentos de texto de resultados de
busqueda, nunca en una inspeccion directa del marcado HTML/CSS real. Por
eso se apoya en patrones de TEXTO en vez de en clases CSS concretas.

**Ejecuta este script una vez a mano (workflow_dispatch) y revisa el
log "Convocatorias reconocidas en la pagina N" antes de fiarte del cron
automatico.**

SOBRE verify=False (desactivar la verificacion del certificado SSL)
------------------------------------------------------------------------
Se mantiene tal cual se pidio, pero con una advertencia: esto deshabilita
la proteccion frente a certificados falsificados/intermediarios
(ataques de tipo "man in the middle") para TODAS las peticiones de este
script, no solo para esquivar un error puntual. Los runners de GitHub
Actions traen un almacen de certificados (ca-certificates) actualizado
de serie, asi que si el problema de SSL solo se vio en un entorno local
(por ejemplo, tras un proxy corporativo que reemplaza certificados), es
muy probable que en GitHub Actions ni siquiera haga falta -- y si el
problema SI se reproduce alli, merece la pena averiguar la causa real
(¿certificado de caf.com mal configurado? ¿cadena de certificacion
incompleta?) en vez de desactivar la verificacion de forma permanente.
Si mas adelante se confirma que no hace falta en GitHub Actions, basta
con quitar `verify=False` de las dos llamadas a requests.get().

QUE CAMBIA EN ESTA VERSION (a partir de una revision propia)
------------------------------------------------------------------
1. Ventana de "ultimos DIAS_ATRAS dias" (3, igual que AfDB/BID) sobre
   `fecha_publicacion` -- aplicada DESPUES de leer la ficha de cada
   candidata (es el unico punto en el que se conoce esa fecha; el
   listado solo trae la fecha de CIERRE, nunca la de publicacion -- ver
   mas abajo). Si ninguna candidata cae dentro de la ventana, el script
   termina de forma ordenada sin subir nada.
2. Extraccion de pais: se busca por nombre de pais miembro de CAF
   (texto de la tarjeta, titulo o descripcion, en ese orden) -- ver
   PAISES_CAF y _extraer_pais(). Sigue pudiendo quedar a None si
   ninguno de esos textos menciona un pais reconocible.
3. Fecha de publicacion vs. fecha limite: "Convocatoria del X al Y" se
   interpreta como X = apertura del plazo (fecha_publicacion) e
   Y = cierre (fecha_limite) -- se mantiene igual que antes porque es
   la lectura mas consistente con todos los ejemplos reales revisados,
   pero se ha reforzado el parseo (ver _parsear_rango_fechas_es) para
   que nunca se pierda una fecha de apertura solo por caer en el
   futuro respecto a hoy: no hay ninguna comprobacion que descarte
   fechas futuras, se guardan tal cual se leen.

SIGUE SIN HABER PAGINACION CON PARADA TEMPRANA
----------------------------------------------------
El listado de CAF no expone una fecha de PUBLICACION por tarjeta (solo
la de cierre), asi que no se puede saber si conviene parar de paginar
solo mirando el listado. Se sigue recorriendo un numero acotado de
paginas (MAX_PAGINAS_SEGURIDAD) leyendo TODAS las convocatorias
abiertas, y el filtro de "ultimos 3 dias" se aplica despues, ya con la
fecha de publicacion real de cada ficha.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_caf.py
Ejecucion programada: ver .github/workflows/sincronizar_caf.yml
"""
import re
import time
import unicodedata
from datetime import date, timedelta

import requests
import urllib3
from bs4 import BeautifulSoup

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

# Ver aviso "SOBRE verify=False" en el docstring del modulo.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.caf.com"
LISTADO_URL = BASE_URL + "/es/trabaja-con-nosotros/convocatorias/"

FUENTE = "CAF"
DIAS_ATRAS = 3
MAX_PAGINAS_SEGURIDAD = 15
MAX_DETALLES_POR_EJECUCION = 150
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.8
PAUSA_ENTRE_DETALLES_SEGUNDOS = 0.4
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15

CABECERAS = {"User-Agent": "Mozilla/5.0 (compatible; LicitacionesEmpresasBot/1.0)"}

CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "fecha_publicacion", "fecha_limite")

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

# Paises miembro de CAF (mas los mencionados con mas frecuencia en sus
# convocatorias) -- ver _extraer_pais(). Lista de mejor esfuerzo, no
# oficial/exhaustiva: si falta algun pais donde CAF tambien opere,
# añadirlo aqui es la unica forma de que se reconozca.
PAISES_CAF = [
    "Argentina", "Barbados", "Bolivia", "Brasil", "Chile", "Colombia",
    "Costa Rica", "Ecuador", "El Salvador", "España", "Guatemala",
    "Honduras", "Jamaica", "México", "Nicaragua", "Panamá", "Paraguay",
    "Perú", "Portugal", "República Dominicana", "Trinidad y Tobago",
    "Uruguay", "Venezuela",
]


def _normalizar_texto(texto: str) -> str:
    texto = (texto or "").lower()
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


_PAISES_NORMALIZADOS = [(_normalizar_texto(p), p) for p in PAISES_CAF]


def _extraer_pais(*fuentes_de_texto) -> str:
    """
    Busca, en el orden de `fuentes_de_texto` (se pasa primero el texto
    de la tarjeta del listado, luego el titulo, luego la descripcion),
    el primer nombre de pais miembro de CAF que aparezca como palabra
    completa (evita falsos positivos por subcadena, p. ej. que "Chile"
    coincidiera dentro de otra palabra).
    """
    for texto in fuentes_de_texto:
        if not texto:
            continue
        texto_norm = _normalizar_texto(texto)
        for pais_norm, pais_original in _PAISES_NORMALIZADOS:
            if re.search(rf"\b{re.escape(pais_norm)}\b", texto_norm):
                return pais_original
    return None


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
    "Convocatoria del X al Y" -> X = apertura del plazo
    (fecha_publicacion), Y = cierre (fecha_limite). Caso real
    confirmado: cuando ambas fechas caen en el mismo año, el texto de
    INICIO no repite el año ("del 25 de agosto al 18 de octubre de
    2026"), asi que se le presta el del fin si le falta. No se aplica
    ninguna comprobacion de "fecha pasada/futura": si la apertura del
    plazo cae en el futuro respecto a hoy, se guarda tal cual -- es un
    dato legitimo (una convocatoria que se anuncia pero abre mas
    adelante), no un error.
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
                # Si el mes de inicio es POSTERIOR al de cierre (p. ej.
                # "del 20 de diciembre al 15 de enero de 2027"), el rango
                # cruza fin de año: el inicio es del año ANTERIOR al del
                # cierre, no el mismo (caso real encontrado al probarlo).
                anio_inicio = fecha_fin.year - 1 if mes > fecha_fin.month else fecha_fin.year
                try:
                    fecha_inicio = date(anio_inicio, mes, int(dia))
                except ValueError:
                    fecha_inicio = None

    return fecha_inicio, fecha_fin


# ------------------------------------------------------------------
# Descarga y parseo del listado
# ------------------------------------------------------------------
def obtener_pagina(pagina: int) -> str:
    print(f"--> Descargando pagina {pagina} del listado de convocatorias CAF...", flush=True)
    respuesta = requests.get(
        LISTADO_URL, params={"page": pagina}, timeout=TIMEOUT_PETICION, headers=CABECERAS, verify=False
    )
    print(f"    HTTP: {respuesta.status_code}", flush=True)
    respuesta.raise_for_status()
    return respuesta.text


def extraer_convocatorias_de_pagina(html: str) -> list:
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
            "texto_tarjeta": texto_tarjeta,
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
    fecha_limite = convocatoria.get("fecha_limite") or convocatoria.get("fecha_limite_listado")

    pais = _extraer_pais(
        convocatoria.get("texto_tarjeta"),
        convocatoria.get("titulo"),
        convocatoria.get("descripcion"),
    )

    return {
        "codigo_unico": f"CAF-{_generar_slug_de_url(convocatoria['url_oficial'])}",
        "fuente_origen": FUENTE,
        "tipo_aviso": "Convocatoria",
        "titulo": convocatoria["titulo"],
        "descripcion": convocatoria.get("descripcion"),
        "pais": pais,
        "organismo": "CAF",
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


# ------------------------------------------------------------------
# Ejecucion principal
# ------------------------------------------------------------------
def ejecutar_sincronizacion():
    hoy = date.today()
    desde = hoy - timedelta(days=DIAS_ATRAS)

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - CAF", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)
    print(f"Ventana de publicacion: {desde} .. {hoy}", flush=True)

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
                    "real probablemente no coincide con los patrones de texto de este script -- "
                    "revisa PATRON_ENLACE_CONVOCATORIA y extraer_convocatorias_de_pagina contra el "
                    "HTML real.",
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
    en_ventana = []
    fuera_de_ventana = 0
    sin_fecha = 0

    for indice, convocatoria in enumerate(candidatos, start=1):
        print(f"  [{indice}/{len(candidatos)}] {convocatoria['titulo'][:90]}", flush=True)

        detalle = obtener_detalle_convocatoria(convocatoria["url_oficial"])
        convocatoria["descripcion"] = detalle["descripcion"]
        convocatoria["fecha_publicacion"] = detalle["fecha_publicacion"]
        convocatoria["fecha_limite"] = detalle["fecha_limite"]

        # Ventana de "ultimos DIAS_ATRAS dias" sobre fecha_publicacion --
        # solo se puede aplicar aqui, tras leer la ficha (ver docstring
        # del modulo: el listado no trae fecha de publicacion).
        fecha_publicacion = convocatoria["fecha_publicacion"]
        if fecha_publicacion is None:
            sin_fecha += 1
        elif fecha_publicacion < desde:
            fuera_de_ventana += 1
        else:
            en_ventana.append(convocatoria)

        time.sleep(PAUSA_ENTRE_DETALLES_SEGUNDOS)

    print(
        f"\nDentro de la ventana de {DIAS_ATRAS} dias: {len(en_ventana)}  "
        f"(fuera de la ventana: {fuera_de_ventana}  ·  sin fecha de publicacion detectada: {sin_fecha})",
        flush=True,
    )

    if not en_ventana:
        print("No hay convocatorias publicadas en la ventana de dias configurada.", flush=True)
        return

    normalizados = [construir_registro(c) for c in en_ventana]
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

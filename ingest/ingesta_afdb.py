```python
# -*- coding: utf-8 -*-
"""
ingesta_afdb.py
----------------
Sincroniza avisos de contratacion del Banco Africano de Desarrollo (AfDB)
directamente contra la tabla `licitaciones_internacionales` de Supabase.

Fuente (confirmada contra la pagina real, no es una API publica):

    https://www.afdb.org/en/projects-and-operations/procurement?page=N

Se reconoce cualquier prefijo de 2 a 6 letras mayusculas seguido de
" - pais - ..." (AMI, AAO, IFB, EOI, PPM, SPN, GPN...), y tambien los
avisos de adjudicacion en frances ("Attribution de contrat/marches -
pais - ...") e ingles ("Contract Award(s) - ..."): antes se descartaban
por no ser oportunidades abiertas, pero un analisis de enlaces reales
que el sistema no capturaba mostro que 3 de 5 eran precisamente avisos
de este tipo que el usuario SI quiere ver -- se han dejado de excluir.

CAMBIOS EN ESTA VERSION
-----------------------
1. Se ha eliminado por completo la extraccion de fecha de cierre desde
   el PDF adjunto: no se encontraba de forma fiable en la practica. El
   campo `fecha_limite` se mantiene en el esquema (por si se rellena por
   otra via en el futuro) pero este script ya no intenta rellenarlo.

2. Se ha eliminado el tope `MAX_DETALLES_POR_EJECUCION` (antes 80): se
   procesan TODOS los avisos candidatos dentro de la ventana de 3 dias,
   sin limite.

3. Ya no se excluyen avisos de adjudicacion/resultado.

4. NUEVO: se filtran los enlaces para aceptar solo titulos que tengan
   formato reconocido de aviso de contratacion/adjudicacion. Esto evita
   documentos generales como "Board Documents".

5. NUEVO: se añade una segunda barrera en `construir_registro()` para
   evitar crear registros cuyo tipo de aviso no haya podido reconocerse.

Sigue sin haber API publica del AfDB para esto (se comprobo
expresamente): es scraping de HTML, con lo que ello implica de
fragilidad ante cambios de diseno de la web.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_afdb.py
Ejecucion programada: ver .github/workflows/sincronizar_afdb.yml
"""

import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)


# ============================================================
# CONFIGURACION
# ============================================================

BASE_URL = "https://www.afdb.org"
LISTADO_URL = BASE_URL + "/en/projects-and-operations/procurement"

FUENTE = "AfDB"

DIAS_ATRAS = 3

MAX_PAGINAS_SEGURIDAD = 60

PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.8
PAUSA_ENTRE_DETALLES_SEGUNDOS = 0.4

TIMEOUT_PETICION = 30

LOTE_ENVIO_SUPABASE = 15

CABECERAS = {
    "User-Agent": "Mozilla/5.0 (compatible; LicitacionesEmpresasBot/1.0)"
}


# ============================================================
# PATRONES
# ============================================================

PATRON_FECHA_LISTADO = re.compile(
    r"\d{1,2}-[A-Za-z]{3}-\d{4}"
)


# Prefijo corto de 2 a 6 letras mayusculas:
# AMI, AAO, IFB, EOI, PPM, SPN, GPN, etc.
PATRON_TIPO_PAIS = re.compile(
    r"^([A-ZÀ-ÖØ-Þ]{2,6})\s*-\s*([^-]+?)\s*-\s*(.+)$"
)


# Avisos de adjudicacion en frances:
# Attribution de contrat - pais - ...
# Attribution de contrats - pais - ...
# Attribution de marché - pais - ...
# Attribution de marchés - pais - ...
PATRON_ADJUDICACION_FR = re.compile(
    r"^(Attribution de contrats?|Attribution de march[eé]s)"
    r"\s*-\s*([^-]+?)\s*-\s*(.+)$",
    re.IGNORECASE,
)


# Avisos de adjudicacion en ingles:
# Contract Award - ...
# Contract Awards - ...
PATRON_CONTRACT_AWARD = re.compile(
    r"^(Contract Awards?)\s*-\s*(.+)$",
    re.IGNORECASE,
)


# ============================================================
# CAMPOS COMPARABLES
# ============================================================

CAMPOS_COMPARABLES = (
    "titulo",
    "descripcion",
    "pais",
    "url_documento",
)


# ============================================================
# VALIDACION DE AVISOS
# ============================================================

def es_aviso_contratacion(titulo: str) -> bool:
    """
    Devuelve True solo si el titulo corresponde a un aviso
    reconocido de contratacion o adjudicacion.

    Ejemplos que SI pasan:

        AMI - RDC - ...
        AAO - Senegal - ...
        IFB - Ghana - ...
        EOI - Nigeria - ...
        PPM - ...
        SPN - ...
        GPN - ...

        Attribution de contrat - ...
        Attribution de marchés - ...

        Contract Award - ...
        Contract Awards - ...

    Ejemplos que NO pasan:

        Board Documents
        Annual Report
        News
        Publications
        etc.
    """

    if PATRON_TIPO_PAIS.match(titulo):
        return True

    if PATRON_ADJUDICACION_FR.match(titulo):
        return True

    if PATRON_CONTRACT_AWARD.match(titulo):
        return True

    return False


# ============================================================
# DESCARGA DEL LISTADO
# ============================================================

def obtener_pagina(pagina: int) -> str:

    print(
        f"--> Descargando pagina {pagina} del listado de AfDB...",
        flush=True
    )

    respuesta = requests.get(
        LISTADO_URL,
        params={"page": pagina},
        timeout=TIMEOUT_PETICION,
        headers=CABECERAS,
    )

    print(
        f"    HTTP: {respuesta.status_code}",
        flush=True
    )

    respuesta.raise_for_status()

    return respuesta.text


# ============================================================
# FECHAS
# ============================================================

def _parsear_fecha_listado(texto_fecha: str):

    try:
        return datetime.strptime(
            texto_fecha,
            "%d-%b-%Y"
        ).date()

    except ValueError:
        return None


# ============================================================
# EXTRAER AVISOS DE UNA PAGINA
# ============================================================

def extraer_avisos_de_pagina(html: str):
    """
    Extrae avisos reales de contratacion/adjudicacion.

    Para cada enlace real a un aviso (href bajo /en/documents/,
    que no sea un enlace de categoria), busca hacia atras en el DOM
    el nodo de texto mas cercano con forma de fecha "DD-Mon-YYYY".

    IMPORTANTE:
    Solo se aceptan titulos que tengan un formato reconocido
    de aviso de contratacion/adjudicacion.

    Esto evita que documentos generales como "Board Documents"
    entren como candidatos.
    """

    soup = BeautifulSoup(html, "html.parser")

    contenedor = (
        soup.find("main")
        or soup.find(id="content")
        or soup
    )

    avisos = []

    # Se deduplica por URL, no por titulo.
    vistos = set()

    for enlace in contenedor.find_all("a", href=True):

        titulo = enlace.get_text(strip=True)

        href = enlace["href"]

        # ----------------------------------------------------
        # FILTRO BASICO DEL ENLACE
        # ----------------------------------------------------

        if (
            not titulo
            or not href.startswith("/en/documents/")
            or "/category/" in href
        ):
            continue


        # ----------------------------------------------------
        # CAMBIO 1
        # ----------------------------------------------------
        # Solo aceptar avisos de contratacion/adjudicacion
        # reconocidos.
        #
        # Esto elimina cosas como:
        #   Board Documents
        #   Annual Report
        #   Publications
        #   etc.
        # ----------------------------------------------------

        if not es_aviso_contratacion(titulo):
            continue


        # ----------------------------------------------------
        # DEDUPLICACION
        # ----------------------------------------------------

        if href in vistos:
            continue

        vistos.add(href)


        # ----------------------------------------------------
        # FECHA DE PUBLICACION
        # ----------------------------------------------------

        fecha_publicacion = None

        nodo_fecha = enlace.find_previous(
            string=PATRON_FECHA_LISTADO
        )

        if nodo_fecha:

            coincidencia = PATRON_FECHA_LISTADO.search(
                str(nodo_fecha)
            )

            if coincidencia:

                fecha_publicacion = _parsear_fecha_listado(
                    coincidencia.group(0)
                )


        # ----------------------------------------------------
        # GUARDAR AVISO
        # ----------------------------------------------------

        avisos.append({
            "titulo": titulo,
            "fecha_publicacion": fecha_publicacion,
            "url_oficial": BASE_URL + href,
        })


    return avisos


# ============================================================
# FICHA DEL AVISO
# ============================================================

def extraer_descripcion_detalle(soup: BeautifulSoup):

    contenedor = (
        soup.find("main")
        or soup.find(id="content")
        or soup
    )

    for parrafo in contenedor.find_all("p"):

        texto = parrafo.get_text(strip=True)

        # Evita parrafos cortos / boilerplate de menu
        if len(texto) > 80:
            return texto

    return None


# ============================================================
# URL DEL DOCUMENTO ADJUNTO
# ============================================================

def extraer_url_documento(soup: BeautifulSoup):

    contenedor = (
        soup.find("main")
        or soup.find(id="content")
        or soup
    )

    for enlace in contenedor.find_all("a", href=True):

        href = enlace["href"]


        # Viewer del AfDB
        if "viewer.html?file=" in href:

            coincidencia = re.search(
                r"file=([^&]+)",
                href
            )

            if coincidencia:

                return unquote(
                    coincidencia.group(1)
                )


        # Documento directo
        if href.lower().endswith(
            (".pdf", ".docx", ".doc")
        ):

            return (
                href
                if href.startswith("http")
                else BASE_URL + href
            )


    return None


# ============================================================
# OBTENER DETALLE
# ============================================================

def obtener_detalle_aviso(url: str) -> dict:

    try:

        respuesta = requests.get(
            url,
            timeout=TIMEOUT_PETICION,
            headers=CABECERAS,
        )

        respuesta.raise_for_status()

    except Exception as error:

        print(
            f"      Error descargando la ficha: {error}",
            flush=True
        )

        return {
            "descripcion": None,
            "url_documento": None,
        }


    soup = BeautifulSoup(
        respuesta.text,
        "html.parser"
    )

    return {
        "descripcion": extraer_descripcion_detalle(soup),
        "url_documento": extraer_url_documento(soup),
    }


# ============================================================
# NORMALIZACION
# ============================================================

def _extraer_tipo_y_pais(titulo: str):

    coincidencia = PATRON_TIPO_PAIS.match(titulo)

    if coincidencia:

        tipo_aviso, pais, _resto = coincidencia.groups()

        return (
            tipo_aviso,
            pais.strip()
        )


    coincidencia_fr = PATRON_ADJUDICACION_FR.match(
        titulo
    )

    if coincidencia_fr:

        _prefijo, pais, _resto = coincidencia_fr.groups()

        return (
            "Attribution de contrat",
            pais.strip()
        )


    coincidencia_ca = PATRON_CONTRACT_AWARD.match(
        titulo
    )

    if coincidencia_ca:

        return (
            "Contract Award",
            None
        )


    return None, None


# ============================================================
# CONSTRUIR REGISTRO
# ============================================================

def construir_registro(aviso: dict) -> dict:

    tipo_aviso, pais = _extraer_tipo_y_pais(
        aviso["titulo"]
    )


    # --------------------------------------------------------
    # CAMBIO 2
    # --------------------------------------------------------
    # Segunda barrera de seguridad.
    #
    # Si por cualquier motivo llega hasta aqui un titulo
    # que no corresponde a un aviso reconocido, no se crea
    # ningun registro.
    # --------------------------------------------------------

    if tipo_aviso is None:

        print(
            f"      Aviso descartado por tipo no reconocido: "
            f"{aviso['titulo']}",
            flush=True
        )

        return None


    slug = (
        aviso["url_oficial"]
        .rstrip("/")
        .split("/")[-1]
    )

    fecha_publicacion = aviso.get(
        "fecha_publicacion"
    )


    return {

        "codigo_unico": f"AFDB-{slug}",

        "fuente_origen": FUENTE,

        "tipo_aviso": tipo_aviso,

        "titulo": aviso["titulo"],

        "descripcion": aviso.get(
            "descripcion"
        ),

        "pais": pais,

        "organismo": None,

        "categoria": None,

        "url_oficial": aviso[
            "url_oficial"
        ],

        "url_documento": aviso.get(
            "url_documento"
        ),

        "fecha_publicacion": (
            fecha_publicacion.isoformat()
            if fecha_publicacion
            else None
        ),

        "fecha_limite": None,
    }


# ============================================================
# DECIDIR QUE SUBIR
# ============================================================

def preparar_lote_para_subir(
    normalizados: list,
    registros_existentes: dict
) -> list:

    a_subir = []


    for datos in normalizados:

        # Seguridad por si algun registro fuese None
        if datos is None:
            continue


        existente = registros_existentes.get(
            datos["codigo_unico"]
        )


        texto_completo = (
            f"Titulo: {datos['titulo']}\n"
            f"{datos.get('descripcion') or ''}\n"
            f"Pais: {datos.get('pais') or 'No especificado'}"
        )


        # ----------------------------------------------------
        # NUEVO REGISTRO
        # ----------------------------------------------------

        if existente is None:

            datos["texto_completo"] = (
                texto_completo
            )

            datos["embedding"] = generar_embedding(
                texto_completo
            )

            datos["es_novedad"] = True

            datos["es_actualizada"] = False

            a_subir.append(datos)

            continue


        # ----------------------------------------------------
        # COMPROBAR CAMBIOS
        # ----------------------------------------------------

        ha_cambiado = any(
            str(existente.get(campo))
            != str(datos.get(campo))
            for campo in CAMPOS_COMPARABLES
        )


        if not ha_cambiado:
            continue


        datos["texto_completo"] = (
            texto_completo
        )

        datos["embedding"] = generar_embedding(
            texto_completo
        )

        datos["es_novedad"] = False

        datos["es_actualizada"] = True

        a_subir.append(datos)


    return a_subir


# ============================================================
# EJECUCION PRINCIPAL
# ============================================================

def ejecutar_sincronizacion():

    hoy = date.today()

    desde = hoy - timedelta(
        days=DIAS_ATRAS
    )


    print(
        "=" * 100,
        flush=True
    )

    print(
        "SINCRONIZACION DE LICITACIONES "
        "INTERNACIONALES - AfDB",
        flush=True
    )

    print(
        "=" * 100,
        flush=True
    )

    print(
        f"Ventana: {desde} .. {hoy}",
        flush=True
    )

    print(
        f"Fuente: {LISTADO_URL}",
        flush=True
    )


    # ========================================================
    # BUSQUEDA DE CANDIDATOS
    # ========================================================

    candidatos = []

    pagina = 0

    detener = False


    while (
        pagina < MAX_PAGINAS_SEGURIDAD
        and not detener
    ):

        try:

            html = obtener_pagina(
                pagina
            )

        except Exception as error:

            print(
                f"    Error descargando la pagina "
                f"{pagina}: {error}",
                flush=True
            )

            break


        avisos = extraer_avisos_de_pagina(
            html
        )


        if not avisos:

            print(
                "    No se han reconocido avisos "
                "en esta pagina "
                "(¿cambio el diseno de la web?). Fin.",
                flush=True
            )

            break


        for aviso in avisos:

            fecha = aviso.get(
                "fecha_publicacion"
            )


            if fecha and fecha < desde:

                print(
                    f"    Llegamos a {fecha}, "
                    f"anterior a {desde}. "
                    f"Fin del escaneo.",
                    flush=True
                )

                detener = True

                continue


            candidatos.append(
                aviso
            )


        pagina += 1

        time.sleep(
            PAUSA_ENTRE_PAGINAS_SEGUNDOS
        )


    print(
        f"\nAvisos candidatos en la ventana: "
        f"{len(candidatos)}",
        flush=True
    )


    if not candidatos:

        return


    # ========================================================
    # DESCARGAR DETALLE
    # ========================================================

    print(
        "\nDescargando ficha de cada candidato "
        "(descripcion + documento adjunto)...",
        flush=True
    )


    normalizados = []


    for indice, aviso in enumerate(
        candidatos,
        start=1
    ):

        print(
            f"  [{indice}/{len(candidatos)}] "
            f"{aviso['titulo'][:90]}",
            flush=True
        )


        detalle = obtener_detalle_aviso(
            aviso["url_oficial"]
        )


        aviso["descripcion"] = (
            detalle["descripcion"]
        )

        aviso["url_documento"] = (
            detalle["url_documento"]
        )


        registro = construir_registro(
            aviso
        )


        # Solo añadir registros validos
        if registro is not None:

            normalizados.append(
                registro
            )


        time.sleep(
            PAUSA_ENTRE_DETALLES_SEGUNDOS
        )


    # ========================================================
    # DEDUPLICAR
    # ========================================================

    normalizados = list(
        {
            n["codigo_unico"]: n
            for n in normalizados
        }.values()
    )


    # ========================================================
    # SUPABASE
    # ========================================================

    supabase = obtener_cliente_supabase()


    print(
        "\nComparando con lo ya existente en Supabase...",
        flush=True
    )


    registros_existentes = (
        obtener_registros_existentes(
            supabase,
            tabla="licitaciones_internacionales",
            columna_clave="codigo_unico",
            columnas=(
                "id",
                "codigo_unico"
            ) + CAMPOS_COMPARABLES,
            claves=[
                n["codigo_unico"]
                for n in normalizados
            ],
        )
    )


    # ========================================================
    # PREPARAR SUBIDA
    # ========================================================

    lote_final = preparar_lote_para_subir(
        normalizados,
        registros_existentes
    )


    if not lote_final:

        print(
            "No hay avisos nuevos ni cambios "
            "que sincronizar.",
            flush=True
        )

        return


    # ========================================================
    # SUBIR A SUPABASE
    # ========================================================

    subidas = subir_en_lotes(
        supabase,
        "licitaciones_internacionales",
        "codigo_unico",
        lote_final,
        tamano_lote=LOTE_ENVIO_SUPABASE,
    )


    print(
        f"\nSincronizacion AfDB completada: "
        f"{subidas}/{len(lote_final)} registros subidos.",
        flush=True
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    ejecutar_sincronizacion()
```

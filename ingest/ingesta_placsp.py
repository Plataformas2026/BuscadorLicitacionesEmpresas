# -*- coding: utf-8 -*-
"""
ingesta_placsp.py
-----------------

Sincroniza las licitaciones abiertas de la Plataforma de Contratación
del Sector Público (PLACSP) contra la misma tabla de Supabase utilizada
por Enabel:

    licitaciones_internacionales

La estructura de datos sigue el mismo modelo que ingesta_enabel.py.

Fuentes PLACSP:
    1. Licitaciones Generales PLACSP
    2. Licitaciones Agregadas PLACSP

Estrategia:

    - Descargar ambos feeds Atom.
    - Extraer las licitaciones vigentes.
    - Deduplicar por expediente.
    - Normalizar al mismo esquema de Enabel.
    - Buscar únicamente esos codigo_unico en:
          licitaciones_internacionales
    - Si no existe:
          generar embedding
          es_novedad = True
          es_actualizada = False
    - Si existe y no ha cambiado:
          no hacer nada
    - Si existe y ha cambiado:
          regenerar embedding
          es_novedad = False
          es_actualizada = True
    - Insertar/actualizar mediante la misma función común utilizada
      por Enabel.

IMPORTANTE:
    Este script NO utiliza ni modifica la tabla antigua `licitaciones`.
    Tampoco modifica registros de Enabel.
"""

from datetime import date, datetime
import os
import re
import time

import lxml.etree as ET
import requests

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)


# ============================================================
# 1. CONFIGURACIÓN
# ============================================================

FUENTE = "PLACSP"
TIPO_AVISO = "Public procurement"
ORGANISMO_POR_DEFECTO = "Plataforma de Contratación del Sector Público"
PAIS = "España"

TABLA_SUPABASE = "licitaciones_internacionales"

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

FEEDS_ATOM = [
    {
        "nombre": "Licitaciones Generales PLACSP",
        "url": (
            "https://contrataciondelsectorpublico.gob.es/"
            "sindicacion/sindicacion_643/"
            "licitacionesPerfilesContratanteCompleto3.atom"
        ),
    },
    {
        "nombre": "Licitaciones Agregadas PLACSP",
        "url": (
            "https://contrataciondelsectorpublico.gob.es/"
            "sindicacion/sindicacion_1044/"
            "PlataformasAgregadasSinMenores.atom"
        ),
    },
]

MAX_PAGINAS = 15
LOTE_ENVIO_SUPABASE = 15

PAUSA_ENTRE_PAGINAS = 0.5
MAX_REINTENTOS_HTTP = 3


# ============================================================
# NAMESPACES XML
# ============================================================

NS = {
    "atom": "http://www.w3.org/2005/Atom",

    "cac": (
        "urn:dgpe:names:draft:codice:"
        "schema:xsd:CommonAggregateComponents-2"
    ),

    "cbc": (
        "urn:dgpe:names:draft:codice:"
        "schema:xsd:CommonBasicComponents-2"
    ),

    "cac-place-ext": (
        "urn:dgpe:names:draft:codice-place-ext:"
        "schema:xsd:CommonAggregateComponents-2"
    ),

    "cbc-place-ext": (
        "urn:dgpe:names:draft:codice-place-ext:"
        "schema:xsd:CommonBasicComponents-2"
    ),
}


# ============================================================
# MAPEO NUTS
# ============================================================

MAPEO_NUTS = {
    "ES11": "Galicia",
    "ES12": "Principado de Asturias",
    "ES13": "Cantabria",
    "ES21": "País Vasco",
    "ES22": "Comunidad Foral de Navarra",
    "ES23": "La Rioja",
    "ES24": "Aragón",
    "ES30": "Comunidad de Madrid",
    "ES41": "Castilla y León",
    "ES42": "Castilla-La Mancha",
    "ES43": "Extremadura",
    "ES51": "Cataluña",
    "ES52": "Comunidad Valenciana",
    "ES53": "Illes Balears",
    "ES61": "Andalucía",
    "ES62": "Región de Murcia",
    "ES63": "Ciudad Autónoma de Ceuta",
    "ES64": "Ciudad Autónoma de Melilla",
    "ES70": "Canarias",

    "ES111": "A Coruña",
    "ES112": "Lugo",
    "ES113": "Ourense",
    "ES114": "Pontevedra",
    "ES120": "Asturias",
    "ES130": "Cantabria",
    "ES211": "Álava",
    "ES212": "Guipúzcoa",
    "ES213": "Vizcaya",
    "ES220": "Navarra",
    "ES230": "La Rioja",
    "ES241": "Huesca",
    "ES242": "Teruel",
    "ES243": "Zaragoza",
    "ES300": "Madrid",
    "ES411": "Ávila",
    "ES412": "Burgos",
    "ES413": "León",
    "ES414": "Palencia",
    "ES415": "Salamanca",
    "ES416": "Segovia",
    "ES417": "Soria",
    "ES418": "Valladolid",
    "ES419": "Zamora",
    "ES421": "Albacete",
    "ES422": "Ciudad Real",
    "ES423": "Cuenca",
    "ES424": "Guadalajara",
    "ES425": "Toledo",
    "ES431": "Badajoz",
    "ES432": "Cáceres",
    "ES511": "Barcelona",
    "ES512": "Girona",
    "ES513": "Lleida",
    "ES514": "Tarragona",
    "ES521": "Alicante",
    "ES522": "Castellón",
    "ES523": "Valencia",
    "ES531": "Eivissa i Formentera",
    "ES532": "Mallorca",
    "ES533": "Menorca",
    "ES611": "Almería",
    "ES612": "Cádiz",
    "ES613": "Córdoba",
    "ES614": "Granada",
    "ES615": "Huelva",
    "ES616": "Jaén",
    "ES617": "Málaga",
    "ES618": "Sevilla",
    "ES620": "Murcia",
    "ES630": "Ceuta",
    "ES640": "Melilla",
    "ES703": "El Hierro",
    "ES704": "Fuerteventura",
    "ES705": "Gran Canaria",
    "ES706": "La Gomera",
    "ES707": "La Palma",
    "ES708": "Lanzarote",
    "ES709": "Tenerife",
}


# ============================================================
# CAMPOS COMPARABLES
# ============================================================
#
# Son exactamente campos que existen en la estructura de Enabel.
#
# `descripcion` se construye incluyendo los datos específicos de
# PLACSP (objeto, tipo, CPV, lugar e importe), de modo que un cambio
# en cualquiera de ellos provoque una actualización.
# ============================================================

CAMPOS_COMPARABLES = (
    "titulo",
    "descripcion",
    "pais",
    "url_documento",
    "fecha_limite",
)


# ============================================================
# SESIÓN HTTP
# ============================================================

def crear_sesion_robusta():
    session = requests.Session()

    retries = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(max_retries=retries)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "application/atom+xml,application/xml,"
                "text/xml;q=0.9,*/*;q=0.8"
            ),
        }
    )

    return session


# ============================================================
# UTILIDADES
# ============================================================

def normalizar_organo(texto):
    if not texto:
        return ""

    texto = texto.lower().strip()

    texto = re.sub(r"[áàäâ]", "a", texto)
    texto = re.sub(r"[éèëê]", "e", texto)
    texto = re.sub(r"[íìïî]", "i", texto)
    texto = re.sub(r"[óòöô]", "o", texto)
    texto = re.sub(r"[úùüû]", "u", texto)

    texto = re.sub(r"[^a-z0-9\s]", "", texto)

    return re.sub(r"\s+", " ", texto)


def limpiar_texto(texto):
    if texto is None:
        return ""

    texto = str(texto).replace("\xa0", " ")
    texto = texto.replace("\u00ad", "")

    return re.sub(r"\s+", " ", texto).strip()


def _texto(el, xpath, ns=NS):
    nodo = el.find(xpath, ns)

    if nodo is not None and nodo.text:
        return nodo.text.strip()

    return None


def generar_codigo_unico(expediente, enlace):
    """
    Genera el identificador estable que se utilizará en la tabla común.

    Prioridad:
        ContractFolderID / expediente

    Fallback:
        URL de la licitación.
    """

    base = limpiar_texto(expediente) or limpiar_texto(enlace)

    base = base.upper()

    base = re.sub(
        r"[^A-Z0-9._-]+",
        "-",
        base
    )

    base = base.strip("-")

    if not base:
        base = "SIN-EXPEDIENTE"

    return f"PLACSP-{base}"[:150]


# ============================================================
# TIPO DE CONTRATO
# ============================================================

def traducir_tipo_contrato(codigo_raw):
    if not codigo_raw:
        return "No especificado"

    limpio = str(codigo_raw).strip().lower()

    mapping_codigos = {
        "1": "Suministros",
        "2": "Servicios",
        "3": "Obras",
        "4": "Concesión de obras",
        "5": "Gestión de servicios públicos",
        "6": "Concesión de servicios",
        "7": (
            "Colaboración entre el sector público "
            "y el sector privado"
        ),
        "8": "Administrativo especial",
        "21": "Privado",
        "patrimonial": "Patrimonial",
        "otros": "Otros",
    }

    if limpio in mapping_codigos:
        return mapping_codigos[limpio]

    mapping_texto = {
        "obres": "Obras",
        "obras": "Obras",
        "serveis": "Servicios",
        "servicios": "Servicios",
        "subministraments": "Suministros",
        "suministros": "Suministros",
        "concesión de obras públicas": (
            "Concesión de obras públicas"
        ),
        "concesión de obras": "Concesión de obras",
        "gestión de servicios públicos": (
            "Gestión de servicios públicos"
        ),
        "concesión de servicios": (
            "Concesión de servicios"
        ),
    }

    return mapping_texto.get(
        limpio,
        codigo_raw.strip()
    )


# ============================================================
# PETICIONES HTTP
# ============================================================

def descargar_feed(session, url):
    for intento in range(1, MAX_REINTENTOS_HTTP + 1):

        try:
            response = session.get(
                url,
                timeout=30
            )

            if response.status_code == 404:
                return None

            response.raise_for_status()

            return response

        except requests.exceptions.RequestException as error:

            print(
                f"   ⚠️ Error descargando feed "
                f"(intento {intento}/{MAX_REINTENTOS_HTTP}): "
                f"{error}"
            )

            if intento < MAX_REINTENTOS_HTTP:
                time.sleep(2 * intento)

    return None


# ============================================================
# EXTRACCIÓN DE UNA LICITACIÓN
# ============================================================

def extraer_datos_entry(entry, hoy):
    # --------------------------------------------------------
    # ENLACE
    # --------------------------------------------------------

    enlace_el = entry.find(
        "atom:link",
        NS
    )

    enlace = (
        enlace_el.get("href")
        if enlace_el is not None
        else ""
    )

    if enlace and "contrataciondelestado.es" in enlace:
        enlace = enlace.replace(
            "contrataciondelestado.es",
            "contrataciondelsectorpublico.gob.es"
        )

    enlace = enlace.strip()

    if not enlace:
        return None

    # --------------------------------------------------------
    # ESTADO
    # --------------------------------------------------------

    codigo_estado = "PUB"

    try:

        estado_el = entry.find(
            ".//cbc-place-ext:"
            "ContractFolderStatusCode",
            NS
        )

        if estado_el is None:

            estado_el = entry.find(
                ".//cbc:ContractFolderStatusCode",
                NS
            )

        if (
            estado_el is not None
            and estado_el.text
        ):
            codigo_estado = (
                estado_el.text
                .strip()
                .upper()
            )

    except Exception:
        pass

    estados_cerrados = {
        "EV",
        "ADJ",
        "RES",
        "ANUL",
        "FOR",
        "AS",
        "RE",
        "CAN",
    }

    if codigo_estado in estados_cerrados:
        return None

    # --------------------------------------------------------
    # FECHA DE PUBLICACIÓN
    # --------------------------------------------------------

    txt_updated = _texto(
        entry,
        "atom:updated"
    )

    txt_published = _texto(
        entry,
        "atom:published"
    )

    txt_fecha = (
        txt_updated
        or txt_published
    )

    if not txt_fecha:
        return None

    fecha_publicacion = txt_fecha.split("T")[0]

    # --------------------------------------------------------
    # FECHA LÍMITE
    # --------------------------------------------------------

    end_date_el = entry.find(
        ".//cac:TenderingProcess/"
        "cac:TenderSubmissionDeadlinePeriod/"
        "cbc:EndDate",
        NS
    )

    fecha_limite = None

    if (
        end_date_el is not None
        and end_date_el.text
    ):
        fecha_limite = (
            end_date_el.text
            .strip()[:10]
        )

        try:
            fecha_limite_date = datetime.strptime(
                fecha_limite,
                "%Y-%m-%d"
            ).date()

            if fecha_limite_date < hoy:
                return None

        except Exception:
            pass

    # --------------------------------------------------------
    # TÍTULO
    # --------------------------------------------------------

    titulo = (
        _texto(
            entry,
            "atom:title"
        )
        or "Sin título"
    )

    titulo = limpiar_texto(titulo)

    # --------------------------------------------------------
    # EXPEDIENTE
    # --------------------------------------------------------

    expediente = (
        _texto(
            entry,
            ".//cbc:ContractFolderID",
            NS
        )
        or enlace
    )

    expediente = limpiar_texto(expediente)

    codigo_unico = generar_codigo_unico(
        expediente,
        enlace
    )

    # --------------------------------------------------------
    # CPV
    # --------------------------------------------------------

    cpv_codigo = "No especificado"

    try:

        cpv_elements = (
            entry.findall(
                ".//cac-place-ext:"
                "ContractFolderStatus/"
                "cac:ProcurementProject/"
                "cac:RequiredCommodityClassification/"
                "cbc:ItemClassificationCode",
                NS
            )
            or entry.findall(
                ".//cbc:ItemClassificationCode",
                NS
            )
        )

        if cpv_elements:

            valores_cpv = [
                el.text.strip()
                for el in cpv_elements
                if el.text
            ]

            if valores_cpv:
                cpv_codigo = ", ".join(
                    valores_cpv
                )

    except Exception:
        pass

    # --------------------------------------------------------
    # LUGAR DE EJECUCIÓN
    # --------------------------------------------------------

    lugar_ejecucion = "No especificado"

    try:

        lugar_el = entry.find(
            ".//cac:ProcurementProject/"
            "cac:RealizedLocation/"
            "cbc:CountrySubentity",
            NS
        )

        if (
            lugar_el is not None
            and lugar_el.text
        ):

            lugar_ejecucion = (
                lugar_el.text.strip()
            )

        else:

            lugar_el = entry.find(
                ".//cac:ProcurementProject/"
                "cac:RealizedLocation/"
                "cbc:CountrySubentityCode",
                NS
            )

            if (
                lugar_el is not None
                and lugar_el.text
            ):

                codigo_lugar = (
                    lugar_el.text.strip()
                )

                lugar_ejecucion = (
                    MAPEO_NUTS.get(
                        codigo_lugar,
                        codigo_lugar
                    )
                )

    except Exception:
        pass

    # --------------------------------------------------------
    # TIPO DE CONTRATO
    # --------------------------------------------------------

    type_code_el = entry.find(
        ".//cac-place-ext:"
        "ContractFolderStatus/"
        "cac:ProcurementProject/"
        "cbc:TypeCode",
        NS
    )

    if type_code_el is None:

        type_code_el = entry.find(
            ".//cbc:TypeCode",
            NS
        )

    tipo_contrato_raw = (
        type_code_el.text.strip()
        if (
            type_code_el is not None
            and type_code_el.text
        )
        else "No especificado"
    )

    tipo_contrato = traducir_tipo_contrato(
        tipo_contrato_raw
    )

    # --------------------------------------------------------
    # IMPORTE
    # --------------------------------------------------------

    importe = 0.0

    try:

        presupuesto_el = entry.find(
            ".//cac:BudgetAmount/"
            "cbc:EstimatedOverallContractAmount",
            NS
        )

        if presupuesto_el is None:

            presupuesto_el = entry.find(
                ".//cac:BudgetAmount/"
                "cbc:TaxExclusiveAmount",
                NS
            )

        if presupuesto_el is None:

            presupuesto_el = entry.find(
                ".//cac:BudgetAmount/"
                "cbc:TotalAmount",
                NS
            )

        if (
            presupuesto_el is not None
            and presupuesto_el.text
        ):

            importe = float(
                presupuesto_el.text
                .strip()
                .replace(",", ".")
            )

    except Exception:
        importe = 0.0

    # --------------------------------------------------------
    # ÓRGANO
    # --------------------------------------------------------

    organo = ORGANISMO_POR_DEFECTO

    rutas_organo = [
        ".//cac-place-ext:"
        "LocatedContractingParty//"
        "cac:PartyName//cbc:Name",

        ".//cac:ContractingParty//"
        "cac:PartyName//cbc:Name",

        ".//cac:TenderingParty//"
        "cac:PartyName//cbc:Name",

        ".//cac:ContractingParty//"
        "cac:Party//"
        "cac:PartyName//cbc:Name",

        ".//cbc:PartyName//cbc:Name",
    ]

    for ruta in rutas_organo:

        organo_el = entry.find(
            ruta,
            NS
        )

        if (
            organo_el is not None
            and organo_el.text
            and organo_el.text.strip()
        ):

            organo = organo_el.text.strip()
            break

    # --------------------------------------------------------
    # DESCRIPCIÓN / OBJETO
    # --------------------------------------------------------

    descripcion_base = (
        _texto(
            entry,
            ".//cac-place-ext:"
            "ContractFolderStatus/"
            "cac:ProcurementProject/"
            "cbc:Name",
            NS
        )
        or _texto(
            entry,
            ".//cac:ProcurementProject/"
            "cbc:Description",
            NS
        )
        or ""
    )

    descripcion_base = limpiar_texto(
        descripcion_base
    )

    # --------------------------------------------------------
    # DESCRIPCIÓN NORMALIZADA
    # --------------------------------------------------------
    #
    # La tabla compartida no tiene columnas específicas
    # para CPV, importe, lugar, etc.
    #
    # Por ello se incluyen dentro de descripcion.
    # Esto permite además que CAMPOS_COMPARABLES detecte
    # modificaciones en esos datos.
    # --------------------------------------------------------

    descripcion = (
        f"{descripcion_base}\n\n"
        f"Tipo de contrato: {tipo_contrato}\n"
        f"CPV: {cpv_codigo}\n"
        f"Lugar de ejecución: {lugar_ejecucion}\n"
        f"Importe: {importe} EUR\n"
        f"Expediente: {expediente}"
    ).strip()

    # --------------------------------------------------------
    # CATEGORÍA
    # --------------------------------------------------------

    categoria = tipo_contrato

    # --------------------------------------------------------
    # TEXTO COMPLETO PARA EMBEDDING
    # --------------------------------------------------------

    texto_completo = (
        f"passage: "
        f"Título: {titulo}. "
        f"Objeto: {descripcion_base}. "
        f"Organismo: {organo}. "
        f"Tipo de contrato: {tipo_contrato}. "
        f"CPV: {cpv_codigo}. "
        f"Lugar de ejecución: {lugar_ejecucion}. "
        f"Importe: {importe} EUR. "
        f"Expediente: {expediente}."
    )

    # --------------------------------------------------------
    # REGISTRO CRUDO
    # --------------------------------------------------------

    return {
        "codigo_unico": codigo_unico,
        "fuente_origen": FUENTE,
        "tipo_aviso": TIPO_AVISO,
        "titulo": titulo,
        "descripcion": descripcion,
        "pais": PAIS,
        "paises": [PAIS],
        "organismo": organo,
        "categoria": categoria,
        "url_oficial": enlace,
        "url_documento": None,
        "fecha_publicacion": fecha_publicacion,
        "fecha_limite": fecha_limite,
        "texto_completo": texto_completo,
        "_expediente": expediente,
        "_updated": txt_updated or txt_fecha,
    }


# ============================================================
# EXTRAER TODAS LAS LICITACIONES DE UN FEED
# ============================================================

def extraer_feed(
    session,
    nombre_feed,
    url_inicial,
    hoy
):
    url_actual = url_inicial
    pagina = 0

    licitaciones_por_expediente = {}

    print()
    print("=" * 100)
    print(f"PROCESANDO: {nombre_feed}")
    print("=" * 100)

    while (
        url_actual
        and pagina < MAX_PAGINAS
    ):

        pagina += 1

        print(
            f"--> Página {pagina}/{MAX_PAGINAS}: "
            f"{url_actual}"
        )

        response = descargar_feed(
            session,
            url_actual
        )

        if response is None:
            print(
                "   ⚠️ No se pudo descargar la página. "
                "Se detiene este feed."
            )
            break

        try:

            parser = ET.XMLParser(
                recover=True
            )

            root = ET.fromstring(
                response.content,
                parser=parser
            )

        except Exception as error:

            print(
                f"   ⚠️ Error parseando XML: {error}"
            )

            break

        entries = (
            root.findall(
                "atom:entry",
                NS
            )
            or root.findall(
                ".//{http://www.w3.org/2005/Atom}entry"
            )
        )

        if not entries:

            print(
                "   No hay más entries. "
                "Fin del feed."
            )

            break

        procesadas = 0
        vigentes = 0

        for entry in entries:

            datos = extraer_datos_entry(
                entry,
                hoy
            )

            if datos is None:
                continue

            procesadas += 1

            codigo = datos["codigo_unico"]

            # ------------------------------------------------
            # DEDUPLICACIÓN ENTRE FEEDS / PÁGINAS
            # ------------------------------------------------

            if codigo not in licitaciones_por_expediente:

                licitaciones_por_expediente[
                    codigo
                ] = datos

                vigentes += 1

            else:

                anterior = (
                    licitaciones_por_expediente[
                        codigo
                    ]
                )

                fecha_anterior = (
                    anterior.get(
                        "_updated"
                    )
                    or ""
                )

                fecha_nueva = (
                    datos.get(
                        "_updated"
                    )
                    or ""
                )

                if fecha_nueva > fecha_anterior:

                    licitaciones_por_expediente[
                        codigo
                    ] = datos

        print(
            f"   Entries vigentes procesadas: "
            f"{procesadas}"
        )

        print(
            f"   Nuevas únicas en este recorrido: "
            f"{vigentes}"
        )

        # ----------------------------------------------------
        # SIGUIENTE PÁGINA
        # ----------------------------------------------------

        next_link_el = root.find(
            "atom:link[@rel='next']",
            NS
        )

        if next_link_el is None:

            next_link_el = root.find(
                ".//{http://www.w3.org/2005/Atom}"
                "link[@rel='next']"
            )

        url_actual = (
            next_link_el.get("href")
            if next_link_el is not None
            else None
        )

        if (
            url_actual
            and "contrataciondelestado.es"
            in url_actual
        ):
            url_actual = url_actual.replace(
                "contrataciondelestado.es",
                "contrataciondelsectorpublico.gob.es"
            )

        time.sleep(
            PAUSA_ENTRE_PAGINAS
        )

    return list(
        licitaciones_por_expediente.values()
    )


# ============================================================
# EXTRAER PLACSP COMPLETO
# ============================================================

def extraer_licitaciones_placsp(hoy):

    session = crear_sesion_robusta()

    todas = {}

    for feed in FEEDS_ATOM:

        datos_feed = extraer_feed(
            session,
            feed["nombre"],
            feed["url"],
            hoy
        )

        for datos in datos_feed:

            codigo = datos["codigo_unico"]

            if codigo not in todas:

                todas[codigo] = datos

            else:

                anterior = todas[codigo]

                fecha_anterior = (
                    anterior.get("_updated")
                    or ""
                )

                fecha_nueva = (
                    datos.get("_updated")
                    or ""
                )

                if fecha_nueva > fecha_anterior:
                    todas[codigo] = datos

    resultado = list(
        todas.values()
    )

    # El campo interno no se debe enviar a Supabase.
    for datos in resultado:
        datos.pop("_updated", None)
        datos.pop("_expediente", None)

    print()
    print(
        f"TOTAL LICITACIONES PLACSP ÚNICAS: "
        f"{len(resultado)}"
    )

    return resultado


# ============================================================
# LIMPIEZA DE LICITACIONES PLACSP CADUCADAS
# ============================================================

def limpiar_licitaciones_placsp_caducadas(
    supabase,
    hoy
):
    """
    Limpia únicamente registros PLACSP.

    IMPORTANTE:
        Nunca toca registros de Enabel.

    Se identifica PLACSP mediante:
        codigo_unico LIKE 'PLACSP-%'
    """

    print()
    print(
        "Comprobando licitaciones PLACSP caducadas..."
    )

    try:

        respuesta = (
            supabase
            .table(TABLA_SUPABASE)
            .select(
                "id,codigo_unico,fecha_limite"
            )
            .like(
                "codigo_unico",
                "PLACSP-%"
            )
            .execute()
        )

        registros = (
            respuesta.data or []
        )

    except Exception as error:

        print(
            f"⚠️ Error leyendo PLACSP para "
            f"limpieza: {error}"
        )

        return

    ids_a_eliminar = []

    for registro in registros:

        fecha_limite = (
            registro.get(
                "fecha_limite"
            )
        )

        if not fecha_limite:
            continue

        try:

            fecha = datetime.strptime(
                str(fecha_limite)[:10],
                "%Y-%m-%d"
            ).date()

        except Exception:
            continue

        if fecha < hoy:

            ids_a_eliminar.append(
                registro["id"]
            )

    if not ids_a_eliminar:

        print(
            "   No hay licitaciones PLACSP "
            "caducadas para eliminar."
        )

        return

    print(
        f"   Licitaciones PLACSP caducadas: "
        f"{len(ids_a_eliminar)}"
    )

    tamano_lote = 50

    eliminadas = 0

    for i in range(
        0,
        len(ids_a_eliminar),
        tamano_lote
    ):

        lote = ids_a_eliminar[
            i:i + tamano_lote
        ]

        try:

            (
                supabase
                .table(TABLA_SUPABASE)
                .delete()
                .in_("id", lote)
                .execute()
            )

            eliminadas += len(lote)

        except Exception as error:

            print(
                f"   ⚠️ Error eliminando lote "
                f"de caducadas: {error}"
            )

    print(
        f"   🗑️ PLACSP caducadas eliminadas: "
        f"{eliminadas}"
    )


# ============================================================
# PREPARAR REGISTROS
# ============================================================

def preparar_lote_para_subir(
    normalizados,
    registros_existentes
):
    """
    Aplica la misma filosofía de sincronización
    que Enabel.

    NUEVO:
        embedding
        es_novedad = True
        es_actualizada = False

    EXISTENTE SIN CAMBIOS:
        no se sube

    EXISTENTE CON CAMBIOS:
        embedding nuevo
        es_novedad = False
        es_actualizada = True

    La fecha_publicacion original se conserva.
    """

    a_subir = []

    for datos_originales in normalizados:

        datos = dict(
            datos_originales
        )

        codigo = datos[
            "codigo_unico"
        ]

        existente = (
            registros_existentes
            .get(codigo)
        )

        # ----------------------------------------------------
        # REGISTRO NUEVO
        # ----------------------------------------------------

        if existente is None:

            texto_completo = (
                datos["texto_completo"]
            )

            datos["embedding"] = (
                generar_embedding(
                    texto_completo
                )
            )

            datos["es_novedad"] = True
            datos["es_actualizada"] = False

            a_subir.append(
                datos
            )

            continue

        # ----------------------------------------------------
        # REGISTRO EXISTENTE
        # ----------------------------------------------------

        ha_cambiado = any(
            str(
                existente.get(
                    campo
                )
            )
            !=
            str(
                datos.get(
                    campo
                )
            )
            for campo in CAMPOS_COMPARABLES
        )

        if not ha_cambiado:
            continue

        # ----------------------------------------------------
        # CONSERVAR FECHA ORIGINAL
        # ----------------------------------------------------

        if existente.get(
            "fecha_publicacion"
        ):

            datos[
                "fecha_publicacion"
            ] = existente[
                "fecha_publicacion"
            ]

        # ----------------------------------------------------
        # REGENERAR EMBEDDING
        # ----------------------------------------------------

        texto_completo = (
            datos["texto_completo"]
        )

        datos["embedding"] = (
            generar_embedding(
                texto_completo
            )
        )

        datos["es_novedad"] = False
        datos["es_actualizada"] = True

        a_subir.append(
            datos
        )

    return a_subir


# ============================================================
# SINCRONIZACIÓN
# ============================================================

def ejecutar_sincronizacion():

    hoy = date.today()

    print()
    print("=" * 100)
    print(
        "SINCRONIZACIÓN PLACSP → "
        "licitaciones_internacionales"
    )
    print("=" * 100)

    print(
        f"Fecha de ejecución: {hoy}"
    )

    print(
        f"Tabla destino: {TABLA_SUPABASE}"
    )

    # ========================================================
    # SUPABASE
    # ========================================================

    supabase = obtener_cliente_supabase()

    # ========================================================
    # LIMPIAR CADUCADAS
    # ========================================================

    limpiar_licitaciones_placsp_caducadas(
        supabase,
        hoy
    )

    # ========================================================
    # EXTRAER PLACSP
    # ========================================================

    normalizados = (
        extraer_licitaciones_placsp(
            hoy
        )
    )

    if not normalizados:

        print()
        print(
            "No se han encontrado licitaciones "
            "PLACSP vigentes."
        )

        return

    # ========================================================
    # VALIDAR DATOS
    # ========================================================

    normalizados = [
        datos
        for datos in normalizados
        if (
            datos.get("codigo_unico")
            and datos.get("titulo")
            and datos.get("url_oficial")
        )
    ]

    print()
    print(
        f"Licitaciones PLACSP válidas: "
        f"{len(normalizados)}"
    )

    # ========================================================
    # BUSCAR EXISTENTES
    # ========================================================

    claves = [
        datos["codigo_unico"]
        for datos in normalizados
    ]

    print()
    print(
        "Comparando con "
        "licitaciones_internacionales..."
    )

    registros_existentes = (
        obtener_registros_existentes(
            supabase,
            tabla=TABLA_SUPABASE,
            columna_clave="codigo_unico",
            columnas=(
                "id",
                "codigo_unico",
                "fecha_publicacion",
            ) + CAMPOS_COMPARABLES,
            claves=claves,
        )
    )

    print(
        f"Registros PLACSP ya existentes: "
        f"{len(registros_existentes)}/"
        f"{len(normalizados)}"
    )

    # ========================================================
    # PREPARAR NUEVOS / ACTUALIZACIONES
    # ========================================================

    lote_final = (
        preparar_lote_para_subir(
            normalizados,
            registros_existentes
        )
    )

    if not lote_final:

        print()
        print(
            "No hay licitaciones nuevas ni "
            "cambios que sincronizar."
        )

        print(
            "Sincronización PLACSP finalizada."
        )

        return

    # ========================================================
    # CONTADORES
    # ========================================================

    nuevas = sum(
        1
        for datos in lote_final
        if datos.get(
            "es_novedad"
        ) is True
    )

    actualizadas = sum(
        1
        for datos in lote_final
        if datos.get(
            "es_actualizada"
        ) is True
    )

    print()
    print(
        f"A subir: {len(lote_final)}"
    )

    print(
        f"   🆕 Nuevas: {nuevas}"
    )

    print(
        f"   🔄 Actualizadas: {actualizadas}"
    )

    # ========================================================
    # SUBIR
    # ========================================================

    subidas = subir_en_lotes(
        supabase,
        TABLA_SUPABASE,
        "codigo_unico",
        lote_final,
        tamano_lote=LOTE_ENVIO_SUPABASE,
    )

    # ========================================================
    # RESUMEN
    # ========================================================

    print()
    print("=" * 100)
    print(
        "SINCRONIZACIÓN PLACSP FINALIZADA"
    )
    print("=" * 100)

    print(
        f"Detectadas: {len(normalizados)}"
    )

    print(
        f"Nuevas: {nuevas}"
    )

    print(
        f"Actualizadas: {actualizadas}"
    )

    print(
        f"Subidas correctamente: "
        f"{subidas}/{len(lote_final)}"
    )

    print("=" * 100)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    ejecutar_sincronizacion()

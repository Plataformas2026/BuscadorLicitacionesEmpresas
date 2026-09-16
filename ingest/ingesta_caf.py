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
normal y que un `requests.get()` corriente deberia funcionar -- pero el
parseo de abajo esta basado UNICAMENTE en fragmentos de texto de
resultados de busqueda, nunca en una inspeccion directa del marcado
HTML/CSS real.

**Ejecuta este script una vez a mano (workflow_dispatch) y revisa el
log "Convocatorias reconocidas en la pagina N" antes de fiarte del cron
automatico.**

SOBRE verify=False: se mantiene (ver turnos anteriores) -- deshabilita
la verificacion del certificado SSL para todas las peticiones de este
script. Si el problema que motivo añadirlo solo se dio en un entorno
local con proxy corporativo, probablemente no haga falta en GitHub
Actions (trae certificados al dia); si se reproduce alli tambien,
merece la pena investigar la causa real en vez de dejarlo desactivado
de forma permanente.

CAMBIO DE ESTRATEGIA: TABLA AUXILIAR EN VEZ DE VENTANA DE DIAS
--------------------------------------------------------------------
El portal de CAF no expone una fecha de publicacion fiable por tarjeta
(solo la de cierre), y el volumen de convocatorias abiertas es
manejable -- asi que, en vez de filtrar por "ultimos N dias" (lo que
podia descartar convocatorias legitimas solo porque su fecha de
publicacion no se pudo leer bien), este script ahora:

  1. Rastrea TODAS las convocatorias actualmente abiertas del listado
     (sin ventana de fechas).
  2. Las registra/actualiza en la tabla auxiliar `caf_convocatorias_activas`
     (ver sql/schema.sql) -- un upsert barato con lo que ya se sabe por
     el listado (titulo, URL, fecha de cierre), SIN leer la ficha ni
     generar ningun embedding todavia.
  3. Solo las que sean REALMENTE NUEVAS en esa tabla (columna `visto` a
     False) pasan a la fase cara: leer su ficha, calcular su embedding
     y sincronizarlas con `licitaciones_internacionales`. Las que ya
     estaban registradas como vistas se saltan por completo -- no se
     vuelven a cargar ni a re-procesar en cada ejecucion.
  4. Al arrancar cada ejecucion, se eliminan de la tabla auxiliar las
     convocatorias cuya fecha de cierre ya ha pasado.

Esta tabla auxiliar es de uso EXCLUSIVO de este script -- nunca se
borra ni se modifica nada en `licitaciones_internacionales` a partir de
su ciclo de vida; esa tabla y su logica de embeddings/matching quedan
totalmente intactas, tal como se pidio. Si una convocatoria deja de
estar en la tabla auxiliar (por caducar) pero ya se habia sincronizado
antes con `licitaciones_internacionales`, su registro en la tabla
principal permanece igual que siempre.

Limitacion aceptada de este diseño: una convocatoria marcada como
"vista" ya no se vuelve a leer su ficha en ejecuciones posteriores, asi
que si CAF ampliara su fecha de cierre despues de la primera
sincronizacion, este script no lo detectaria (la fecha de cierre que
lleva la tabla auxiliar SI se refresca en cada ejecucion desde el
listado -- ver mas arriba -- pero la ficha completa, con su
descripcion, no se vuelve a leer). Es el mismo compromiso que se pidio
explicitamente: evitar releer y regenerar embeddings de lo que ya se
conoce.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_caf.py
Ejecucion programada: ver .github/workflows/sincronizar_caf.yml
"""
import re
import time
import unicodedata
from datetime import date, datetime, timezone

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
TABLA_AUXILIAR = "caf_convocatorias_activas"
MAX_PAGINAS_SEGURIDAD = 15
MAX_DETALLES_POR_EJECUCION = 150   # tope sobre las NUEVAS (no sobre el total de abiertas rastreadas)
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


def _generar_slug_de_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1][:120] or "sin-referencia"


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

        url_oficial = BASE_URL + ruta if ruta.startswith("/") else ruta

        convocatorias.append({
            "codigo_unico": f"CAF-{_generar_slug_de_url(url_oficial)}",
            "titulo": titulo,
            "url_oficial": url_oficial,
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
# Tabla auxiliar: ciclo de vida de las convocatorias activas
# ------------------------------------------------------------------
def limpiar_convocatorias_caducadas(supabase, hoy: date) -> int:
    """Elimina de la tabla auxiliar (NUNCA de licitaciones_internacionales) lo que ya cerro."""
    respuesta_previa = (
        supabase.table(TABLA_AUXILIAR)
        .select("codigo_unico")
        .lt("fecha_limite", hoy.isoformat())
        .execute()
    )
    caducadas = respuesta_previa.data or []
    if not caducadas:
        return 0

    supabase.table(TABLA_AUXILIAR).delete().lt("fecha_limite", hoy.isoformat()).execute()
    print(
        f"Eliminadas {len(caducadas)} convocatorias caducadas de la tabla auxiliar "
        f"(fecha_limite anterior a {hoy.isoformat()}).",
        flush=True,
    )
    return len(caducadas)


def refrescar_tabla_auxiliar(supabase, candidatos: list) -> dict:
    """
    Upsert barato (sin leer fichas) de TODAS las convocatorias abiertas
    detectadas en el listado -- mantiene fecha_limite y
    ultima_comprobacion al dia incluso para las ya vistas, para que la
    limpieza de caducadas sea fiable sin tener que releer su ficha.
    `visto` se conserva tal cual estuviera; solo es False para las que
    no existian todavia en la tabla. Devuelve, por codigo_unico, el
    estado ANTES de este refresco (para poder distinguir las nuevas).
    """
    if not candidatos:
        return {}

    codigos = [c["codigo_unico"] for c in candidatos]
    respuesta_existentes = (
        supabase.table(TABLA_AUXILIAR).select("codigo_unico, visto").in_("codigo_unico", codigos).execute()
    )
    visto_por_codigo = {f["codigo_unico"]: bool(f["visto"]) for f in (respuesta_existentes.data or [])}

    ahora = datetime.now(timezone.utc).isoformat()
    filas = []
    for c in candidatos:
        codigo = c["codigo_unico"]
        fecha_limite = c.get("fecha_limite_listado")
        filas.append({
            "codigo_unico": codigo,
            "titulo": c["titulo"],
            "url_oficial": c["url_oficial"],
            "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
            "visto": visto_por_codigo.get(codigo, False),
            "ultima_comprobacion": ahora,
        })

    supabase.table(TABLA_AUXILIAR).upsert(filas, on_conflict="codigo_unico").execute()

    return visto_por_codigo


def marcar_como_vistas(supabase, codigos: list):
    if not codigos:
        return
    supabase.table(TABLA_AUXILIAR).update({"visto": True}).in_("codigo_unico", codigos).execute()


# ------------------------------------------------------------------
# Normalizacion al esquema de `licitaciones_internacionales`
# ------------------------------------------------------------------
def construir_registro(convocatoria: dict) -> dict:
    fecha_publicacion = convocatoria.get("fecha_publicacion")
    fecha_limite = convocatoria.get("fecha_limite") or convocatoria.get("fecha_limite_listado")

    pais = _extraer_pais(
        convocatoria.get("texto_tarjeta"),
        convocatoria.get("titulo"),
        convocatoria.get("descripcion"),
    )

    return {
        "codigo_unico": convocatoria["codigo_unico"],
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
# Decidir que subir a licitaciones_internacionales
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

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - CAF", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)

    supabase = obtener_cliente_supabase()

    limpiar_convocatorias_caducadas(supabase, hoy)

    # --- Rastreo completo de convocatorias abiertas (sin ventana de dias) ---
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

    candidatos = list({c["codigo_unico"]: c for c in candidatos}.values())
    print(f"\nConvocatorias abiertas rastreadas (todas las paginas): {len(candidatos)}", flush=True)

    if not candidatos:
        return

    # --- Refresco barato de la tabla auxiliar + deteccion de novedades ---
    visto_antes = refrescar_tabla_auxiliar(supabase, candidatos)
    nuevas = [c for c in candidatos if not visto_antes.get(c["codigo_unico"], False)]

    print(
        f"Ya registradas como vistas (se saltan, no se releen ni se regeneran embeddings): "
        f"{len(candidatos) - len(nuevas)}",
        flush=True,
    )
    print(f"Nuevas (no vistas todavia): {len(nuevas)}", flush=True)

    if not nuevas:
        print("No hay convocatorias nuevas -- todas las abiertas ya estaban registradas como vistas.", flush=True)
        return

    if len(nuevas) > MAX_DETALLES_POR_EJECUCION:
        print(
            f"Aviso: hay mas novedades ({len(nuevas)}) que el tope por ejecucion "
            f"({MAX_DETALLES_POR_EJECUCION}); se procesan las primeras y el resto se recogera "
            "en la siguiente sincronizacion (siguen sin 'visto' en la tabla auxiliar).",
            flush=True,
        )
        nuevas = nuevas[:MAX_DETALLES_POR_EJECUCION]

    print("\nDescargando la ficha de cada convocatoria nueva...", flush=True)
    normalizados = []
    for indice, convocatoria in enumerate(nuevas, start=1):
        print(f"  [{indice}/{len(nuevas)}] {convocatoria['titulo'][:90]}", flush=True)

        detalle = obtener_detalle_convocatoria(convocatoria["url_oficial"])
        convocatoria["descripcion"] = detalle["descripcion"]
        convocatoria["fecha_publicacion"] = detalle["fecha_publicacion"]
        convocatoria["fecha_limite"] = detalle["fecha_limite"]

        normalizados.append(construir_registro(convocatoria))
        time.sleep(PAUSA_ENTRE_DETALLES_SEGUNDOS)

    normalizados = list({n["codigo_unico"]: n for n in normalizados}.values())

    print("\nComparando con lo ya existente en Supabase (tabla principal)...", flush=True)
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico") + CAMPOS_COMPARABLES,
        claves=[n["codigo_unico"] for n in normalizados],
    )

    lote_final = preparar_lote_para_subir(normalizados, registros_existentes)

    if lote_final:
        subidas = subir_en_lotes(
            supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
        )
        print(f"\nSincronizacion CAF completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)
    else:
        print("\nNo hay cambios que subir a licitaciones_internacionales.", flush=True)

    # Se marcan como vistas TODAS las procesadas en este lote (tanto si se
    # subieron de verdad como si ya estaban igual en la tabla principal):
    # en ambos casos ya se han comprobado y no deben tratarse como
    # candidatas nuevas en la siguiente ejecucion.
    marcar_como_vistas(supabase, [c["codigo_unico"] for c in nuevas])


if __name__ == "__main__":
    ejecutar_sincronizacion()

# -*- coding: utf-8 -*-
"""
ingesta_giz_satellite.py
--------------------------
Sincroniza los avisos de licitación publicados en el portal de adquisiciones 
de la GIZ (Vergabemarktplatz GIZ, plataforma cosinex/DTVP) contra la tabla 
`licitaciones_internacionales` de Supabase.

URL: https://ausschreibungen.giz.de/Satellite/company/welcome.do?method=showTable&fromSearch=1

CAMPOS DE DETALLE (categoria y descripcion)
-------------------------------------------
El listado no trae ni categoria ni una descripcion real, asi que se completan
navegando a las dos pestañas publicas de la ficha de cada aviso:

  1. Overview  (.../project/<ID>/en/overview)
     -> `categoria`: apartado "Subject matter of the contract". Cada entrada
        tiene el formato "<b>CODIGO-CPV</b> Texto de la categoria"; se elimina
        el codigo y se conserva solo el texto (varias entradas se unen con "; ").

  2. Procedure information  (.../project/<ID>/en/processdata/eforms)
     -> `descripcion`: bloque "Procurement Scope" > "Scope of the procedure".
        Se combinan "Short description", la descripcion de "Procurement (type
        and scope ...)" (solo si aporta algo distinto de la corta) y la
        duracion del contrato ("Duration: 3 months.").
        SEGUNDA OPCION (fallback): algunos avisos (p. ej. los de tipo UVgO /
        "Ex post notice") no usan la plantilla eForms anterior sino la plantilla
        clasica, sin "Scope of the procedure". En ese caso se lee el bloque
        "Object of the contract" > "Scope of the procurement": el texto de
        "Type and scope of performance" mas el plazo de "Execution periods"
        ("Period of service provision: ...").
        Si la ficha no aporta texto descriptivo por ninguna de las dos vias se
        mantiene el valor provisional "Referencia GIZ: <n>." que ya generaba la
        version anterior.

TEXTO PARA EL EMBEDDING
-----------------------
`texto_completo` (base del embedding) incluye ahora la linea "Categoria: ..."
cuando el aviso tiene categoria. Solo afecta a los registros que se suban o
actualicen a partir de ahora: los ya guardados conservan su embedding anterior
hasta que cambien o se vuelvan a procesar.

Para no visitar fichas innecesarias, la navegacion al detalle se hace DESPUES de
consultar Supabase y solo para los avisos que la necesitan: nuevos, con cambios
en el listado, o ya guardados pero todavia sin categoria/descripcion real
(esto ultimo rellena automaticamente los avisos subidos por la version anterior
que sigan dentro de la ventana de hoy/ayer).

AVISO DE FIABILIDAD
-------------------
La estructura HTML se ha validado contra el HTML real de DOS avisos: uno con
plantilla eForms (ID CXTRYYRDYDBPQKKV, suministro de equipos, Irak) y uno con
plantilla clasica (ID CXTRYY6DY66CDC0N, trainings MAP, Ex post notice). Si otro
tipo de aviso usa otros encabezados, el campo correspondiente queda vacio, se
avisa en el log (con las secciones detectadas) y se conserva el valor
provisional; la sincronizacion nunca se detiene por ello.
"""

import re
from datetime import date, timedelta

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import sync_playwright

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

BASE_URL = "https://ausschreibungen.giz.de"
LISTADO_URL = BASE_URL + "/Satellite/company/welcome.do?method=showTable&fromSearch=1"
FUENTE = "GIZ-Satellite"
TIEMPO_ESPERA_CARGA_MS = 45000
TIMEOUT_PETICION = 30
LOTE_ENVIO_SUPABASE = 15

# Campos que vienen del listado y campos que vienen de la ficha de detalle.
# Cualquier diferencia en cualquiera de ellos marca el aviso como actualizado.
CAMPOS_LISTADO = ("titulo", "fecha_limite")
CAMPOS_DETALLE = ("categoria", "descripcion")
CAMPOS_COMPARABLES = CAMPOS_LISTADO + CAMPOS_DETALLE

CAPTURA_DEPURACION = "debug_giz_satellite_tabla.png"

CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PATRON_REFERENCIA_TITULO = re.compile(r"^\s*(\d{4,10})\s*[-–]\s*(.+)$")

# --- Fichas de detalle ------------------------------------------------------
PLANTILLA_URL_DETALLE = BASE_URL + "/Satellite/public/company/project/{id}/en/{pestana}"
PESTANA_OVERVIEW = "overview"
PESTANA_EFORMS = "processdata/eforms"

TIEMPO_ESPERA_DETALLE_MS = TIMEOUT_PETICION * 1000
MAX_REINTENTOS_DETALLE = 3
PAUSA_ENTRE_REINTENTOS_S = 3
PAUSA_ENTRE_FICHAS_MS = 500
MAX_LONGITUD_DESCRIPCION = 5000

# Valor provisional que ya generaba la version anterior cuando el titulo traia
# un numero de referencia. Sirve de respaldo y para reconocer registros antiguos.
PREFIJO_DESCRIPCION_PROVISIONAL = "Referencia GIZ:"

# Encabezados exactos (en minusculas) tal y como aparecen en la version inglesa.
ETIQUETAS_CATEGORIA = ("subject matter of the contract",)
ETIQUETAS_ALCANCE = ("scope of the procedure",)
# Plantilla clasica (fallback): "Object of the contract" > "Scope of the procurement"
ETIQUETAS_ALCANCE_ALTERNATIVO = ("scope of the procurement",)

# El identificador de la licitacion (p. ej. CXTRYYRDYDBPQKKV) aparece como
# segmento de ruta tanto en /notice/<ID> como en /project/<ID>/...
PATRON_ID_LICITACION = re.compile(
    r"(?:^|/)(?:notice|project)/([A-Za-z0-9]{8,})(?=[/?#;]|$)"
)

# Codigo numerico inicial (CPV "31682210-5", numeros de referencia...). Exige
# que termine en espacio/fin para no comerse texto como "3D printing".
PATRON_CODIGO_INICIAL = re.compile(r"^\s*\d[\d\-.]*(?=\s|$)\s*[-–:]?\s*")

_TIPOS_RECURSO_PRESCINDIBLES = {"image", "media", "font", "stylesheet"}

_JS_EXTRAER_FILAS = """
() => {
    const resultados = [];
    const filas = Array.from(document.querySelectorAll('table tbody tr'));

    for (const fila of filas) {
        const celdas = fila.querySelectorAll('td');
        if (celdas.length < 3) continue;

        const fechaPub = (celdas[0]?.innerText || '').trim();
        const fechaLimite = (celdas[1]?.innerText || '').trim();
        
        const enlaceEl = celdas[2]?.querySelector('a') || fila.querySelector('a');
        const titulo = (celdas[2]?.innerText || enlaceEl?.innerText || '').trim();
        const href = enlaceEl ? enlaceEl.getAttribute('href') : '';
        const tipo = celdas[3] ? (celdas[3].innerText || '').trim() : '';

        if (titulo) {
            resultados.push({
                titulo: titulo,
                href: href,
                fecha_pub_raw: fechaPub,
                fecha_limite_raw: fechaLimite,
                tipo_procedimiento: tipo
            });
        }
    }
    return resultados;
}
"""


def _generar_slug(texto: str) -> str:
    texto_norm = (texto or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


def parsear_fecha_alemana(texto: str):
    if not texto:
        return None
    coincidencia = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", texto)
    if not coincidencia:
        return None
    dia, mes, anio = coincidencia.groups()
    try:
        return date(int(anio), int(mes), int(dia))
    except ValueError:
        return None


def extraer_avisos_playwright() -> list:
    todos_los_avisos = []
    
    # Rango de fechas permitido: Hoy y Ayer
    hoy = date.today()
    ayer = hoy - timedelta(days=1)

    try:
        with sync_playwright() as p:
            navegador = None
            try:
                navegador = p.chromium.launch(headless=True)
                pagina = navegador.new_page(user_agent=CABECERAS_USER_AGENT)

                print(f"--> Cargando el portal de licitaciones de la GIZ: {LISTADO_URL}...", flush=True)
                pagina.goto(LISTADO_URL, timeout=TIEMPO_ESPERA_CARGA_MS, wait_until="domcontentloaded")

                try:
                    pagina.wait_for_selector('table tbody tr', timeout=TIEMPO_ESPERA_CARGA_MS)
                except Exception as error:
                    print(f"    No apareció la tabla de avisos a tiempo: {error}", flush=True)
                    pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)
                    print(f"    Captura de depuración guardada en {CAPTURA_DEPURACION}.", flush=True)
                    return []

                pagina_actual = 1
                alcanzado_limite_fecha = False

                while True:
                    avisos_pagina = pagina.evaluate(_JS_EXTRAER_FILAS)
                    print(f"    Procesando página {pagina_actual} ({len(avisos_pagina)} avisos)...", flush=True)

                    for aviso in avisos_pagina:
                        fecha_pub = parsear_fecha_alemana(aviso.get("fecha_pub_raw"))
                        
                        if fecha_pub:
                            # Si es de hoy o ayer, lo conservamos
                            if fecha_pub >= ayer:
                                todos_los_avisos.append(aviso)
                            # Como la tabla viene ordenada por fecha descendente,
                            # si encontramos una fecha anterior a ayer, podemos parar.
                            elif fecha_pub < ayer:
                                alcanzado_limite_fecha = True
                        else:
                            # Si por algún motivo no se puede parsear la fecha, conservamos por seguridad
                            todos_los_avisos.append(aviso)

                    if alcanzado_limite_fecha:
                        print(f"--> Alcanzados avisos con fecha anterior a ayer ({ayer.strftime('%d.%m.%Y')}). Deteniendo paginación.", flush=True)
                        break

                    # Buscar botón para pasar a la siguiente página
                    boton_siguiente = pagina.query_selector('a.next-page, a[title*="Nächste"], a[title*="weiter"]')
                    if not boton_siguiente or not boton_siguiente.is_visible():
                        break

                    pagina_actual += 1
                    print(f"--> Avanzando a la página {pagina_actual}...", flush=True)
                    boton_siguiente.click()
                    pagina.wait_for_timeout(2000)
                    pagina.wait_for_selector('table tbody tr', timeout=TIEMPO_ESPERA_CARGA_MS)

            except Exception as error:
                print(f"Error durante la navegación con Playwright: {error}", flush=True)
                try:
                    if "pagina" in locals():
                        pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)
                        print(f"Captura de depuración guardada en {CAPTURA_DEPURACION}.", flush=True)
                except Exception:
                    pass
            finally:
                if navegador is not None:
                    try:
                        navegador.close()
                    except Exception:
                        pass
    except Exception as error:
        print(f"Error inesperado no capturado dentro de Playwright: {error}", flush=True)

    return todos_los_avisos


# =============================================================================
# FICHAS DE DETALLE: parseo (funciones puras, sin red) 
# =============================================================================

def _texto_limpio(nodo):
    """Texto plano de un nodo HTML con espacios normalizados, o None si esta vacio."""
    if nodo is None:
        return None
    texto = nodo.get_text(" ", strip=True).replace("\u00ad", "")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def _quitar_codigo_inicial(texto):
    """Elimina codigos numericos iniciales (CPV, referencias). Devuelve None si no queda texto."""
    if not texto:
        return None
    while True:
        limpio = PATRON_CODIGO_INICIAL.sub("", texto, count=1)
        if limpio == texto:
            break
        texto = limpio
    texto = texto.strip()
    return texto or None


def extraer_categoria_html(html: str):
    """
    Pestaña Overview -> apartado "Subject matter of the contract".

    Estructura real:
        <div class="sub-headline-container"><h4 class="sub-headline"><span>Subject matter of the contract </span>...
        <div class="control-group"><p><b>31682210-5</b> Instrumentation and control equipment</p></div>
        <div class="control-group"><p><b>38000000-5</b> Laboratory, optical ...</p></div>
    """
    soup = BeautifulSoup(html or "", "html.parser")

    for h4 in soup.select("h4.sub-headline"):
        titulo = (_texto_limpio(h4.find("span")) or "").lower()
        if titulo not in ETIQUETAS_CATEGORIA:
            continue

        cabecera = h4.find_parent("div", class_="sub-headline-container") or h4
        categorias = []

        for hermano in cabecera.find_next_siblings():
            clases = hermano.get("class") or []
            if "sub-headline-container" in clases:
                break  # empieza otro apartado
            if "control-group" not in clases:
                continue

            for parrafo in (hermano.find_all("p") or [hermano]):
                # Se trabaja sobre una copia para quitar el <b> del codigo sin alterar el arbol
                copia = BeautifulSoup(str(parrafo), "html.parser")
                for negrita in copia.find_all("b"):
                    negrita.decompose()
                texto = _quitar_codigo_inicial(_texto_limpio(copia))
                if texto and texto not in categorias:
                    categorias.append(texto)

        if categorias:
            return "; ".join(categorias)

    return None


def _unir_frases(partes: list) -> str:
    """Une fragmentos asegurando que cada uno termina en signo de puntuacion."""
    frases = []
    for parte in partes:
        parte = (parte or "").strip()
        if not parte:
            continue
        if parte[-1] not in ".!?":
            parte += "."
        frases.append(parte)
    return " ".join(frases)


def _normalizar_para_comparar(texto: str) -> str:
    return re.sub(r"\s+", " ", (texto or "").strip().lower()).rstrip(".")


def _frases_duracion(pares: list) -> list:
    """
    Convierte los pares (etiqueta, valor) del bloque de duracion en frases.
    El campo "Duration" solo indica el TIPO de duracion ("Duration in months"),
    asi que se ignora; el dato real esta en "Duration in months" -> "3".
    """
    frases = []
    for etiqueta, valor in pares:
        etiqueta = etiqueta or ""
        if etiqueta.strip().lower() == "duration":
            continue
        coincidencia = re.match(r"duration in (\w+)$", etiqueta.strip(), re.IGNORECASE)
        if coincidencia:
            frases.append(f"Duration: {valor} {coincidencia.group(1).lower()}")
        elif etiqueta:
            frases.append(f"{etiqueta}: {valor}")
        else:
            frases.append(valor)
    return frases


def _buscar_fieldset(soup, etiquetas_leyenda: tuple):
    """<fieldset> cuya <legend> coincide (sin distinguir mayusculas) con alguna de `etiquetas_leyenda`."""
    for leyenda in soup.find_all("legend"):
        if (_texto_limpio(leyenda) or "").lower() in etiquetas_leyenda:
            return leyenda.find_parent("fieldset")
    return None


def _recortar_descripcion(descripcion: str) -> str:
    if len(descripcion) > MAX_LONGITUD_DESCRIPCION:
        descripcion = descripcion[: MAX_LONGITUD_DESCRIPCION - 1].rstrip() + "…"
    return descripcion


def _descripcion_scope_of_the_procedure(soup):
    """
    Pestaña Procedure information (plantilla eForms) ->
    "Procurement Scope" > "Scope of the procedure".

    Dentro de ese <fieldset> hay bloques encabezados por <h4 class="sub-headline">
    (Short description / Procurement (type and scope ...) / Scope of the contract /
    Duration of the contract ...). Se recorre en orden de documento y se asigna
    cada valor de solo lectura al ultimo encabezado visto.
    """
    fieldset = _buscar_fieldset(soup, ETIQUETAS_ALCANCE)
    if fieldset is None:
        return None

    secciones = []  # [[titulo_en_minusculas, [(etiqueta, valor), ...]], ...]
    for nodo in fieldset.descendants:
        if not isinstance(nodo, Tag):
            continue
        clases = nodo.get("class") or []
        if nodo.name == "h4" and "sub-headline" in clases:
            secciones.append([(_texto_limpio(nodo.find("span")) or "").lower(), []])
        elif nodo.name == "div" and "control-group" in clases and secciones:
            valor = _texto_limpio(nodo.select_one("span.read-only"))
            if valor:
                etiqueta = _texto_limpio(nodo.select_one("label.description span"))
                secciones[-1][1].append((etiqueta, valor))

    corta = alcance = None
    duracion = []
    for titulo, pares in secciones:
        if not pares:
            continue
        if "short description" in titulo:
            corta = corta or " ".join(valor for _, valor in pares)
        elif titulo.startswith("procurement"):
            alcance = alcance or " ".join(valor for _, valor in pares)
        elif "duration" in titulo:
            duracion = duracion or _frases_duracion(pares)

    textos = []
    if corta and alcance:
        c, a = _normalizar_para_comparar(corta), _normalizar_para_comparar(alcance)
        if c in a:
            textos.append(alcance)      # la larga ya contiene a la corta
        elif a in c:
            textos.append(corta)
        else:
            textos.extend([corta, alcance])
    else:
        textos.extend(t for t in (corta, alcance) if t)

    if not textos:
        return None  # sin texto descriptivo, la duracion sola no sirve como descripcion

    return _recortar_descripcion(_unir_frases(textos + duracion))


def _descripcion_scope_of_the_procurement(soup):
    """
    FALLBACK. Plantilla clasica ->  "Object of the contract" > "Scope of the procurement".

    Estructura real (distinta de la eForms): los valores son <span class="read-only">
    dentro de bloques encabezados por <h4 class="sub-headline">, y el texto de
    cada campo puede llevar su <label> justo antes (p. ej. "Period of service
    provision"). El bloque "Execution periods" NO usa <div class="control-group">,
    por eso se recorre el fieldset en orden de documento fijandose en label / span.

    Se usa:
      - "Type and scope of performance"  -> texto descriptivo del alcance
      - "Execution periods"              -> plazo (equivale a la duracion)
    El resto de bloques del fieldset (lugar de ejecucion, etc.) se ignoran.
    """
    fieldset = _buscar_fieldset(soup, ETIQUETAS_ALCANCE_ALTERNATIVO)
    if fieldset is None:
        return None

    secciones = []  # [[titulo_en_minusculas, [(etiqueta, valor), ...]], ...]
    etiqueta = None
    for nodo in fieldset.descendants:
        if not isinstance(nodo, Tag):
            continue
        clases = nodo.get("class") or []
        if nodo.name == "h4" and "sub-headline" in clases:
            secciones.append([(_texto_limpio(nodo.find("span")) or "").lower(), []])
            etiqueta = None
        elif nodo.name == "label" and secciones:
            etiqueta = _texto_limpio(nodo) or etiqueta
        elif nodo.name == "span" and "read-only" in clases and secciones:
            valor = _texto_limpio(nodo)
            if valor:
                secciones[-1][1].append((etiqueta, valor))
            etiqueta = None

    texto = None
    plazos = []
    for titulo, pares in secciones:
        if not pares:
            continue
        if titulo.startswith("type and scope"):
            texto = texto or " ".join(valor for _, valor in pares)
        elif "execution period" in titulo:
            plazos = plazos or [f"{et}: {valor}" if et else valor for et, valor in pares]

    if not texto:
        return None  # los plazos solos no sirven como descripcion

    return _recortar_descripcion(_unir_frases([texto] + plazos))


def extraer_descripcion_html(html: str):
    """
    Pestaña Procedure information. Primero "Scope of the procedure" (plantilla
    eForms); si no aporta texto, "Scope of the procurement" (plantilla clasica).
    """
    soup = BeautifulSoup(html or "", "html.parser")
    return _descripcion_scope_of_the_procedure(soup) or _descripcion_scope_of_the_procurement(soup)


def _leyendas_html(html: str, maximo: int = 8) -> list:
    """Titulos de seccion (<legend>) de una ficha; solo para diagnosticar en el log."""
    soup = BeautifulSoup(html or "", "html.parser")
    vistas = []
    for leyenda in soup.find_all("legend"):
        texto = _texto_limpio(leyenda)
        if texto and texto not in vistas:
            vistas.append(texto)
    return vistas[:maximo]


# =============================================================================
# FICHAS DE DETALLE: navegacion con Playwright
# =============================================================================

def _id_desde_url(url: str):
    coincidencia = PATRON_ID_LICITACION.search(url or "")
    return coincidencia.group(1) if coincidencia else None


def _bloquear_recursos_prescindibles(ruta):
    """Evita descargar imagenes, fuentes y CSS: solo interesa el texto de la ficha."""
    try:
        if ruta.request.resource_type in _TIPOS_RECURSO_PRESCINDIBLES:
            ruta.abort()
        else:
            ruta.continue_()
    except Exception:
        pass


def _resolver_urls_detalle(pagina, url_oficial: str):
    """
    Devuelve (url_overview, url_eforms) del aviso, o (None, None).
    Si la URL del listado ya lleva el ID (/notice/<ID> o /project/<ID>/...) no
    se navega; si no (p. ej. un enlace de reenvio), se sigue la redireccion y se
    lee el ID de la URL final.
    """
    identificador = _id_desde_url(url_oficial)

    if not identificador and url_oficial and url_oficial != LISTADO_URL:
        try:
            pagina.goto(url_oficial, timeout=TIEMPO_ESPERA_DETALLE_MS, wait_until="domcontentloaded")
            identificador = _id_desde_url(pagina.url)
        except Exception as error:
            print(f"      No se pudo seguir el enlace del aviso: {error}", flush=True)

    if not identificador:
        return None, None

    return (
        PLANTILLA_URL_DETALLE.format(id=identificador, pestana=PESTANA_OVERVIEW),
        PLANTILLA_URL_DETALLE.format(id=identificador, pestana=PESTANA_EFORMS),
    )


def _abrir_ficha_con_reintentos(pagina, url: str):
    """Navega a `url` y devuelve su HTML; reintenta con espera creciente. None si falla."""
    for intento in range(1, MAX_REINTENTOS_DETALLE + 1):
        try:
            pagina.goto(url, timeout=TIEMPO_ESPERA_DETALLE_MS, wait_until="domcontentloaded")
            pagina.wait_for_selector("#content", timeout=TIEMPO_ESPERA_DETALLE_MS, state="attached")
            return pagina.content()
        except Exception as error:
            if intento < MAX_REINTENTOS_DETALLE:
                espera = intento * PAUSA_ENTRE_REINTENTOS_S
                print(
                    f"      Intento {intento}/{MAX_REINTENTOS_DETALLE} fallido ({error}). "
                    f"Reintentando en {espera}s...",
                    flush=True,
                )
                pagina.wait_for_timeout(espera * 1000)
            else:
                print(f"      Ficha no disponible tras {MAX_REINTENTOS_DETALLE} intentos: {url}", flush=True)
    return None


def extraer_detalle_aviso(pagina, url_oficial: str) -> dict:
    """Visita las pestañas Overview y Procedure information de un aviso."""
    detalle = {"categoria": None, "descripcion": None}

    url_overview, url_eforms = _resolver_urls_detalle(pagina, url_oficial)
    if not url_overview:
        print(f"      No se pudo determinar el identificador de la licitacion en: {url_oficial}", flush=True)
        return detalle

    html_overview = _abrir_ficha_con_reintentos(pagina, url_overview)
    if html_overview:
        detalle["categoria"] = extraer_categoria_html(html_overview)
        if not detalle["categoria"]:
            print("      Aviso: no se encontró 'Subject matter of the contract' en la pestaña Overview.", flush=True)

    html_eforms = _abrir_ficha_con_reintentos(pagina, url_eforms)
    if html_eforms:
        detalle["descripcion"] = extraer_descripcion_html(html_eforms)
        if not detalle["descripcion"]:
            print(
                "      Aviso: no se encontró texto ni en 'Scope of the procedure' ni en 'Scope of the procurement' "
                f"(Procedure information). Secciones detectadas: {_leyendas_html(html_eforms)}",
                flush=True,
            )

    return detalle


def extraer_detalles_playwright(registros: list) -> dict:
    """
    Abre UN navegador y recorre las fichas de los registros indicados.
    Devuelve {codigo_unico: {"categoria": ..., "descripcion": ...}}.
    Un fallo en un aviso nunca interrumpe a los demas.
    """
    detalles = {}
    if not registros:
        return detalles

    try:
        with sync_playwright() as p:
            navegador = None
            try:
                navegador = p.chromium.launch(headless=True)
                pagina = navegador.new_page(user_agent=CABECERAS_USER_AGENT)
                pagina.route("**/*", _bloquear_recursos_prescindibles)

                total = len(registros)
                for indice, registro in enumerate(registros, start=1):
                    codigo = registro["codigo_unico"]
                    print(f"    [{indice}/{total}] Ficha de detalle de {codigo}...", flush=True)
                    try:
                        detalles[codigo] = extraer_detalle_aviso(pagina, registro["url_oficial"])
                    except Exception as error:
                        print(f"      Error inesperado con {codigo}: {error}", flush=True)

                    if indice < total:
                        pagina.wait_for_timeout(PAUSA_ENTRE_FICHAS_MS)

            except Exception as error:
                print(f"Error durante la navegación a las fichas de detalle: {error}", flush=True)
            finally:
                if navegador is not None:
                    try:
                        navegador.close()
                    except Exception:
                        pass
    except Exception as error:
        print(f"Error inesperado no capturado en las fichas de detalle: {error}", flush=True)

    return detalles


# =============================================================================
# CONSTRUCCION DE REGISTROS Y SUBIDA
# =============================================================================

def construir_registro(aviso: dict) -> dict:
    titulo_crudo = (aviso.get("titulo") or "").strip()
    href = aviso.get("href") or ""
    fecha_pub_raw = aviso.get("fecha_pub_raw") or ""
    fecha_limite_raw = aviso.get("fecha_limite_raw") or ""
    tipo_procedimiento = aviso.get("tipo_procedimiento") or None

    coincidencia_ref = PATRON_REFERENCIA_TITULO.match(titulo_crudo)
    if coincidencia_ref:
        referencia, titulo = coincidencia_ref.groups()
    else:
        referencia, titulo = None, titulo_crudo

    fecha_publicacion = parsear_fecha_alemana(fecha_pub_raw)
    fecha_limite = parsear_fecha_alemana(fecha_limite_raw)

    url_oficial = f"{BASE_URL}{href}" if href.startswith("/") else (href or LISTADO_URL)
    slug_base = referencia or _generar_slug(titulo or href)

    return {
        "codigo_unico": f"GIZSAT-{_generar_slug(slug_base)}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": tipo_procedimiento,
        "titulo": titulo or titulo_crudo or None,
        # Valor provisional; se sustituye por la descripcion real de la ficha si se obtiene
        "descripcion": f"{PREFIJO_DESCRIPCION_PROVISIONAL} {referencia}." if referencia else None,
        "pais": "Alemania",
        "paises": ["Alemania"],
        "organismo": "GIZ",
        "categoria": None,
        "url_oficial": url_oficial,
        "url_documento": None,
        "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
    }


def _es_descripcion_provisional(texto) -> bool:
    return not texto or str(texto).strip().startswith(PREFIJO_DESCRIPCION_PROVISIONAL)


def _necesita_detalle(registro: dict, existente) -> bool:
    """
    True si merece la pena visitar la ficha del aviso:
      - es nuevo;
      - ya existe pero todavia no tiene categoria o descripcion real
        (registros subidos por la version anterior, o ficha que fallo);
      - cambió algo en el listado (titulo / fecha limite).
    """
    if existente is None:
        return True
    if _es_descripcion_provisional(existente.get("descripcion")) or not existente.get("categoria"):
        return True
    return any(str(existente.get(campo)) != str(registro.get(campo)) for campo in CAMPOS_LISTADO)


def fusionar_detalle(registro: dict, detalle, existente) -> None:
    """
    Vuelca categoria/descripcion en el registro (modifica `registro`).
    Prioridad: dato recien extraido > dato ya guardado en Supabase > provisional.
    Asi un fallo puntual de red nunca borra datos buenos ya almacenados, y los
    avisos cuya ficha no se visitó quedan idénticos a lo que hay en la base.
    """
    detalle = detalle or {}
    existente = existente or {}

    categoria = detalle.get("categoria") or existente.get("categoria") or None

    descripcion = detalle.get("descripcion")
    if not descripcion and not _es_descripcion_provisional(existente.get("descripcion")):
        descripcion = existente.get("descripcion")

    registro["categoria"] = categoria
    if descripcion:
        registro["descripcion"] = descripcion
    # si no hay nada mejor, se conserva el provisional puesto por construir_registro


def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
        if not datos.get("titulo") or not datos.get("url_oficial"):
            continue

        existente = registros_existentes.get(datos["codigo_unico"])
        linea_categoria = f"Categoria: {datos['categoria']}\n" if datos.get("categoria") else ""
        texto_completo = (
            f"Titulo: {datos['titulo']}\n{datos.get('descripcion') or ''}\n"
            f"{linea_categoria}"
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
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - GIZ SATELLITE (portal propio)", flush=True)
    print("=" * 100, flush=True)

    crudos = extraer_avisos_playwright()
    print(f"\nTotal avisos rastreados de hoy y ayer: {len(crudos)}", flush=True)

    if not crudos:
        print(
            "No se ha extraído ningún aviso reciente. Revisa el log de arriba y la captura de depuración "
            f"({CAPTURA_DEPURACION}).",
            flush=True,
        )
        return

    normalizados = [construir_registro(a) for a in crudos]

    sin_fecha_limite = sum(1 for n in normalizados if not n.get("fecha_limite"))
    if sin_fecha_limite:
        print(
            f"Avisos sin fecha límite reconocida o concluidos (p. ej. 'AV'): {sin_fecha_limite}/{len(normalizados)}",
            flush=True,
        )

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

    # Fichas de detalle: solo para los avisos que las necesitan
    pendientes = [
        n for n in normalizados
        if n.get("titulo") and n.get("url_oficial")
        and _necesita_detalle(n, registros_existentes.get(n["codigo_unico"]))
    ]
    print(f"\nAvisos que requieren ficha de detalle: {len(pendientes)}/{len(normalizados)}", flush=True)
    detalles = extraer_detalles_playwright(pendientes)

    if pendientes:
        con_categoria = sum(1 for d in detalles.values() if d.get("categoria"))
        con_descripcion = sum(1 for d in detalles.values() if d.get("descripcion"))
        print(
            f"Fichas leídas: {len(detalles)}/{len(pendientes)} | con categoría: {con_categoria} "
            f"| con descripción: {con_descripcion}",
            flush=True,
        )

    for registro in normalizados:
        codigo = registro["codigo_unico"]
        fusionar_detalle(registro, detalles.get(codigo), registros_existentes.get(codigo))

    lote_final = preparar_lote_para_subir(normalizados, registros_existentes)

    if not lote_final:
        print("No hay avisos nuevos ni cambios que sincronizar.", flush=True)
        return

    subidas = subir_en_lotes(
        supabase, "licitaciones_internacionales", "codigo_unico", lote_final, tamano_lote=LOTE_ENVIO_SUPABASE
    )
    print(f"\nSincronizacion GIZ-Satellite completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

# -*- coding: utf-8 -*-
"""
ingesta_ugpe.py
----------------
Sincroniza los concursos de la UGPE (Unidade de Gestão de Projetos Especiais
de Cabo Verde) contra la tabla `licitaciones_internacionales` de Supabase,
adoptando exactamente la misma estrategia robusta basada en tabla auxiliar
que el script de CAF (`caf_convocatorias_activas` adaptado a `ugpe_concursos_activos`).

Portal objetivo: https://ugpe.gov.cv/en/concursos

ESTRATEGIA DE LA TABLA AUXILIAR (IGUAL QUE CAF):
--------------------------------------------------------------------------
1. Rastrea TODAS las convocatorias actualmente abiertas del listado mediante
   Playwright (sin ventana de fechas fija).
2. Las registra/actualiza en la tabla auxiliar `ugpe_concursos_activos`
   (ver sql/schema.sql) mediante un upsert económico (título, URL, fecha límite)
   SIN leer la ficha ni generar ningún embedding todavía.
3. Solo las que sean REALMENTE NUEVAS en esa tabla (columna `visto` a False)
   pasan a la fase costosa: descargar su ficha detallada, calcular su
   embedding y sincronizarlas con `licitaciones_internacionales`. Las que ya
   estaban registradas como vistas se saltan por completo.
4. Al arrancar cada ejecución, se eliminan de la tabla auxiliar los concursos
   cuya fecha límite ya ha pasado.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local o programada: python ingesta_ugpe.py
"""

import re
import time
from datetime import date, datetime, timezone
import asyncio
import nest_asyncio

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

# Aplicar patch para permitir bucles anidados si es necesario
try:
    nest_asyncio.apply()
except Exception:
    pass

BASE_URL = "https://ugpe.gov.cv"
LISTADO_URL = BASE_URL + "/en/concursos"

FUENTE = "UGPE"
TABLA_AUXILIAR = "ugpe_concursos_activos"
PAIS_UGPE = "Cabo Verde"

MAX_PAGINAS_SEGURIDAD = 15
MAX_DETALLES_POR_EJECUCION = 150  # Tope sobre las NUEVAS por ejecución
TIEMPO_ESPERA_CARGA_MS = 45000
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 0.8
PAUSA_ENTRE_DETALLES_SEGUNDOS = 0.4
TIMEOUT_PETICION = 30
CAPTURA_DEPURACION = "debug_ugpe_listado.png"
LOTE_ENVIO_SUPABASE = 15

CABECERAS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

PATRON_ENLACE_CONCURSO = re.compile(r"/(?:en/)?concurso/([a-z0-9\-]+)/?$", re.IGNORECASE)

PALABRAS_ESTADO_CERRADO = ["encerrado", "closed", "cancelado", "cancelled", "anulado"]

MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
PATRON_FECHA_LARGA_PT = re.compile(
    r"(\d{1,2})\s+de\s+(" + "|".join(MESES_PT.keys()) + r")\s+de\s+(\d{4})", re.IGNORECASE
)
PATRON_FECHA_LARGA_PT_SIN_DE = re.compile(
    r"(\d{1,2})\s+(" + "|".join(MESES_PT.keys()) + r")\s*,?\s+(\d{4})", re.IGNORECASE
)

ETIQUETA_ID = "Projeto ID"
ETIQUETA_CATEGORIA = "Categoria"
ETIQUETA_ABRANGENCIA = "Abrangência"
ETIQUETA_ESTADO_ANUNCIO = "Estado Anúncio"
ETIQUETA_FECHA_PUBLICACION = "Data Publicação"
ETIQUETA_DEADLINE = "Deadline"
ETIQUETA_FECHA_ENCERRAMENTO = "Data Encerramento"
ETIQUETA_ESTADO_CONCURSO = "Estado Concurso"
ETIQUETAS_FICHA = [
    ETIQUETA_ID, ETIQUETA_CATEGORIA, ETIQUETA_ABRANGENCIA, ETIQUETA_ESTADO_ANUNCIO,
    ETIQUETA_FECHA_PUBLICACION, ETIQUETA_DEADLINE, ETIQUETA_FECHA_ENCERRAMENTO, ETIQUETA_ESTADO_CONCURSO,
]

CAMPOS_COMPARABLES = ("titulo", "descripcion", "pais", "paises", "fecha_publicacion", "fecha_limite")


# ------------------------------------------------------------------
# Funciones auxiliares de fecha y estado
# ------------------------------------------------------------------
def _parsear_fecha_flexible(texto: str):
    if not texto:
        return None
    texto = texto.strip()
    if not texto:
        return None

    for patron in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, patron).date()
        except ValueError:
            continue

    texto_norm = texto.lower().replace("ç", "c").replace("ã", "a")
    for patron_regex in (PATRON_FECHA_LARGA_PT, PATRON_FECHA_LARGA_PT_SIN_DE):
        coincidencia = patron_regex.search(texto_norm)
        if coincidencia:
            dia, mes_texto, anio = coincidencia.groups()
            mes = MESES_PT.get(mes_texto.replace("ç", "c").replace("ã", "a"))
            if mes:
                try:
                    return date(int(anio), mes, int(dia))
                except ValueError:
                    pass

    coincidencia_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", texto)
    if coincidencia_iso:
        try:
            return date(int(coincidencia_iso.group(1)), int(coincidencia_iso.group(2)), int(coincidencia_iso.group(3)))
        except ValueError:
            pass

    return None


def _estado_es_cerrado(texto_estado: str) -> bool:
    if not texto_estado:
        return False
    texto_norm = texto_estado.strip().lower()
    return any(palabra in texto_norm for palabra in PALABRAS_ESTADO_CERRADO)


def _generar_slug(texto: str) -> str:
    texto_norm = texto.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", texto_norm).strip("-")
    return (slug or "sin-referencia")[:120]


# ------------------------------------------------------------------
# Listado (Playwright Async)
# ------------------------------------------------------------------
_JS_EXTRAER_TARJETAS = """
() => {
    const resultados = [];
    const vistos = new Set();
    const enlaces = Array.from(document.querySelectorAll('a[href*="/concurso/"]'));
    for (const enlace of enlaces) {
        const href = enlace.getAttribute('href') || '';
        const titulo = (enlace.innerText || '').trim();
        if (!titulo || vistos.has(href)) continue;
        vistos.add(href);

        let nodo = enlace;
        let textoContenedor = '';
        for (let i = 0; i < 6 && nodo.parentElement; i++) {
            nodo = nodo.parentElement;
            const texto = (nodo.innerText || '').trim();
            if (texto.length > titulo.length + 15) {
                textoContenedor = texto;
                break;
            }
        }
        resultados.push({ titulo, href, textoContenedor });
    }
    return resultados;
}
"""

async def extraer_tarjetas_de_pagina(page) -> list:
    filas = await page.evaluate(_JS_EXTRAER_TARJETAS)
    tarjetas = []
    for fila in filas:
        href = fila.get("href", "")
        coincidencia = PATRON_ENLACE_CONCURSO.search(href)
        if not coincidencia:
            continue
        slug = coincidencia.group(1)
        texto_tarjeta = fila.get("textoContenedor", "")
        tarjetas.append({
            "titulo": fila.get("titulo", "").strip(),
            "slug": slug,
            "url_oficial": f"{BASE_URL}/concurso/{slug}",
            "cerrada_segun_listado": _estado_es_cerrado(texto_tarjeta),
            "texto_tarjeta": texto_tarjeta,
        })
    return tarjetas


async def rastrear_listado_completo(page) -> list:
    tarjetas = []
    pagina = 1

    while pagina <= MAX_PAGINAS_SEGURIDAD:
        url_pagina = f"{LISTADO_URL}?page={pagina}"
        print(f"--> Cargando {url_pagina} ...", flush=True)
        try:
            await page.goto(url_pagina, timeout=TIEMPO_ESPERA_CARGA_MS, wait_until="domcontentloaded")
            await page.wait_for_selector('a[href*="/concurso/"]', timeout=10000)
        except Exception as error:
            print(f"    No apareció ningún concurso reconocible a tiempo en la página {pagina}: {error}", flush=True)
            if pagina == 1:
                await page.screenshot(path=CAPTURA_DEPURACION, full_page=True)
                print(f"    Captura de depuración guardada en {CAPTURA_DEPURACION}.", flush=True)
            break

        tarjetas_pagina = await extraer_tarjetas_de_pagina(page)
        print(f"    Concursos reconocidos en la página {pagina}: {len(tarjetas_pagina)}", flush=True)

        if not tarjetas_pagina:
            break

        tarjetas.extend(tarjetas_pagina)
        pagina += 1
        await asyncio.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    return list({t["url_oficial"]: t for t in tarjetas}.values())


async def rastrear_listado_playwright() -> list:
    async with async_playwright() as p:
        navegador = None
        try:
            navegador = await p.chromium.launch(headless=True)
            contexto = await navegador.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            pagina = await contexto.new_page()
            return await rastrear_listado_completo(pagina)
        except Exception as error:
            print(f"Error durante la navegación del listado con Playwright: {error}", flush=True)
            return []
        finally:
            if navegador is not None:
                try:
                    await navegador.close()
                except Exception:
                    pass


# ------------------------------------------------------------------
# Ficha del concurso (requests)
# ------------------------------------------------------------------
def _extraer_campos_ficha(lineas: list) -> dict:
    campos = {}
    for indice, linea in enumerate(lineas):
        linea_limpia = linea.strip()
        if linea_limpia in ETIQUETAS_FICHA and linea_limpia not in campos:
            for siguiente in lineas[indice + 1:indice + 4]:
                valor = siguiente.strip()
                if valor:
                    campos[linea_limpia] = valor
                    break
    return campos


def _extraer_tipos_documentos(lineas: list) -> list:
    tipos = []
    vistos = set()
    for linea in lineas:
        coincidencia = re.match(r"tipo\s*:\s*(.+)", linea.strip(), re.IGNORECASE)
        if coincidencia:
            tipo_limpio = coincidencia.group(1).strip()
            if tipo_limpio and tipo_limpio not in vistos:
                vistos.add(tipo_limpio)
                tipos.append(tipo_limpio)
    return tipos


def obtener_detalle_concurso(url: str) -> dict:
    try:
        respuesta = requests.get(url, timeout=TIMEOUT_PETICION, headers=CABECERAS)
        respuesta.raise_for_status()
    except Exception as error:
        print(f"      Error descargando la ficha: {error}", flush=True)
        return None

    soup = BeautifulSoup(respuesta.text, "html.parser")
    
    h2_titulo = soup.find("h2", class_=re.compile(r"font-semibold.*text-myblue", re.IGNORECASE))
    titulo_real = h2_titulo.get_text(strip=True) if h2_titulo else None

    contenedor = soup.find("main") or soup.find(id="content") or soup
    lineas = contenedor.get_text("\n", strip=True).split("\n")

    campos = _extraer_campos_ficha(lineas)
    tipos_documentos = _extraer_tipos_documentos(lineas)

    fecha_publicacion = _parsear_fecha_flexible(campos.get(ETIQUETA_FECHA_PUBLICACION))
    fecha_limite = _parsear_fecha_flexible(campos.get(ETIQUETA_DEADLINE))

    partes_descripcion = []
    if campos.get(ETIQUETA_CATEGORIA):
        partes_descripcion.append(f"Categoria: {campos[ETIQUETA_CATEGORIA]}.")
    if campos.get(ETIQUETA_ABRANGENCIA):
        partes_descripcion.append(f"Abrangência: {campos[ETIQUETA_ABRANGENCIA]}.")
    if tipos_documentos:
        partes_descripcion.append("Documentos disponíveis: " + ", ".join(tipos_documentos) + ".")
    descripcion = " ".join(partes_descripcion) or None

    return {
        "titulo_real": titulo_real,
        "referencia": campos.get(ETIQUETA_ID),
        "categoria": campos.get(ETIQUETA_CATEGORIA),
        "descripcion": descripcion,
        "fecha_publicacion": fecha_publicacion,
        "fecha_limite": fecha_limite,
        "estado_concurso": campos.get(ETIQUETA_ESTADO_CONCURSO) or campos.get(ETIQUETA_ESTADO_ANUNCIO),
    }


# ------------------------------------------------------------------
# Tabla auxiliar: ciclo de vida de los concursos activos
# ------------------------------------------------------------------
def limpiar_concursos_caducados(supabase, hoy: date) -> int:
    """Elimina de la tabla auxiliar (NUNCA de licitaciones_internacionales) lo que ya cerró."""
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
        f"Eliminados {len(caducadas)} concursos caducados de la tabla auxiliar "
        f"(fecha_limite anterior a {hoy.isoformat()}).",
        flush=True,
    )
    return len(caducadas)


def refrescar_tabla_auxiliar(supabase, candidatos: list) -> dict:
    """
    Upsert rápido de todos los concursos activos encontrados en el listado.
    Devuelve el estado 'visto' anterior por codigo_unico para detectar novedades.
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
        fecha_limite = c.get("fecha_limite_aux")
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
# Normalización al esquema de `licitaciones_internacionales`
# ------------------------------------------------------------------
def construir_registro(tarjeta: dict, detalle: dict) -> dict:
    referencia = (detalle or {}).get("referencia") or None
    slug_base = referencia or tarjeta["slug"]
    titulo_final = (detalle or {}).get("titulo_real") or tarjeta["titulo"]
    
    fecha_pub = (detalle or {}).get("fecha_publicacion")
    fecha_lim = (detalle or {}).get("fecha_limite")

    return {
        "codigo_unico": f"UGPE-{_generar_slug(slug_base)}",
        "fuente_origen": FUENTE,
        "tipo_aviso": "Concurso",
        "titulo": titulo_final,
        "descripcion": (detalle or {}).get("descripcion"),
        "pais": PAIS_UGPE,
        "paises": [PAIS_UGPE],
        "organismo": "UGPE",
        "categoria": (detalle or {}).get("categoria"),
        "url_oficial": tarjeta["url_oficial"],
        "url_documento": None,
        "fecha_publicacion": fecha_pub.isoformat() if fecha_pub else None,
        "fecha_limite": fecha_lim.isoformat() if fecha_lim else None,
    }


# ------------------------------------------------------------------
# Preparar lote para subir
# ------------------------------------------------------------------
def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
        existente = registros_existentes.get(datos["codigo_unico"])
        texto_completo = (
            f"Titulo: {datos['titulo']}\n{datos.get('descripcion') or ''}\n"
            f"Pais: {datos.get('pais') or 'Cabo Verde'}"
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
# Ejecución Principal
# ------------------------------------------------------------------
async def ejecutar_sincronizacion():
    hoy = date.today()

    print("=" * 100, flush=True)
    print("SINCRONIZACIÓN DE LICITACIONES INTERNACIONALES - UGPE (Cabo Verde)", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)

    supabase = obtener_cliente_supabase()

    limpiar_concursos_caducados(supabase, hoy)

    # --- Rastreo completo de concursos activos mediante Playwright ---
    try:
        tarjetas = await rastrear_listado_playwright()
    except Exception as error:
        print(f"Error inesperado durante el rastreo del listado: {error}", flush=True)
        return

    tarjetas = [t for t in tarjetas if not t["cerrada_segun_listado"]]
    print(f"\nConcursos activos rastreados: {len(tarjetas)}", flush=True)

    if not tarjetas:
        print("No se ha rastreado ningún concurso activo.", flush=True)
        return

    # Asignar código único previo y preparar para tabla auxiliar
    candidatos = []
    for t in tarjetas:
        slug_base = t["slug"]
        codigo_unico = f"UGPE-{_generar_slug(slug_base)}"
        candidatos.append({
            "codigo_unico": codigo_unico,
            "titulo": t["titulo"],
            "url_oficial": t["url_oficial"],
            "slug": t["slug"],
            "texto_tarjeta": t["texto_tarjeta"],
            "fecha_limite_aux": None  # Se completará cuando se descargue la ficha o se mantendrá de antes
        })

    # --- Refresco de la tabla auxiliar y detección de novedades ---
    visto_antes = refrescar_tabla_auxiliar(supabase, candidatos)
    nuevas = [c for c in candidatos if not visto_antes.get(c["codigo_unico"], False)]

    print(
        f"Ya registrados como vistos (se saltan, no se releen ni generan embeddings): "
        f"{len(candidatos) - len(nuevas)}",
        flush=True,
    )
    print(f"Nuevos (no vistos todavía): {len(nuevas)}", flush=True)

    if not nuevas:
        print("No hay concursos nuevos -- todos los activos ya estaban registrados como vistos.", flush=True)
        return

    if len(nuevas) > MAX_DETALLES_POR_EJECUCION:
        print(
            f"Aviso: hay más novedades ({len(nuevas)}) que el tope por ejecución "
            f"({MAX_DETALLES_POR_EJECUCION}); se procesan las primeras y el resto se recogerá en la siguiente.",
            flush=True,
        )
        nuevas = nuevas[:MAX_DETALLES_POR_EJECUCION]

    print("\nDescargando la ficha de cada concurso nuevo...", flush=True)
    normalizados = []
    codigos_procesados = []

    for indice, convocatoria in enumerate(nuevas, start=1):
        print(f"  [{indice}/{len(nuevas)}] {convocatoria['titulo'][:90]}", flush=True)

        detalle = obtener_detalle_concurso(convocatoria["url_oficial"])
        
        # Si se extrajo fecha límite en la ficha, actualizar el auxiliar
        if detalle and detalle.get("fecha_limite"):
            convocatoria["fecha_limite_aux"] = detalle.get("fecha_limite")

        registro = construir_registro(convocatoria, detalle)
        normalizados.append(registro)
        codigos_procesados.append(convocatoria["codigo_unico"])
        time.sleep(PAUSA_ENTRE_DETALLES_SEGUNDOS)

    # Actualizar la fecha límite en la tabla auxiliar con los datos reales de la ficha descargada
    if nuevas:
        refrescar_tabla_auxiliar(supabase, nuevas)

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
        print(f"\nSincronización UGPE completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)
    else:
        print("\nNo hay cambios que subir a licitaciones_internacionales.", flush=True)

    # Marcar como vistas todas las procesadas en este ciclo
    marcar_como_vistas(supabase, codigos_procesados)


if __name__ == "__main__":
    asyncio.run(ejecutar_sincronizacion())

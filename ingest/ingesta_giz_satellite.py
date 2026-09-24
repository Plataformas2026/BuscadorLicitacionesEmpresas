# -*- coding: utf-8 -*-
"""
ingesta_giz_satellite.py
--------------------------
Sincroniza los avisos de licitación publicados en el propio portal de
adquisiciones de la GIZ (Vergabemarktplatz GIZ, plataforma cosinex/DTVP)
contra la tabla `licitaciones_internacionales` de Supabase.

    https://ausschreibungen.giz.de/Satellite/company/welcome.do

A diferencia de TED y Service-Bund (ver sus propios docstrings), para
esta fuente NO se encontró ninguna API oficial ni feed RSS -- solo el
listado HTML del propio portal, así que se usa Playwright (navegador
real, headless), igual que ingesta_bid.py/ingesta_undp.py/etc.

Es el portal PROPIO de la GIZ (no una búsqueda de "GIZ" dentro de un
portal más general, a diferencia de TED/Service-Bund), así que aquí NO
hace falta ningún filtro de palabra clave -- todo lo publicado en este
portal es, por definición, de la GIZ.

AVISO DE FIABILIDAD -- ESTA ES LA FUENTE MENOS VERIFICADA DEL BLOQUE
--------------------------------------------------------------------------
No hay salida de red hacia ausschreibungen.giz.de en este entorno de
desarrollo. Lo que sí se ha podido confirmar por búsqueda (páginas
indexadas, guía oficial en PDF de la GIZ):
  - La plataforma es cosinex/DTVP (Deutsches Vergabeportal), un software
    de e-procurement usado por muchas administraciones alemanas.
  - La URL del listado es .../Satellite/company/welcome.do?method=show
    Table&fromSearch=1 (confirmada indexada con esos parámetros).
  - Cada aviso individual vive en .../Satellite/notice/<ID> (ID
    alfanumérico tipo "CXTRYY6YTVGFLGE9"), y su título sigue siempre el
    patrón "<código de referencia numérico> - <título>" (confirmado en
    varios avisos reales indexados, p. ej. "10013531 - Support to
    SAHPRA's...").
  - "Angebotsfrist" es el término legal ESTÁNDAR alemán para la fecha
    límite de una licitación (confirmado en la documentación general
    de contratación pública alemana, no específico de este portal, pero
    es el término que también usa Service-Bund) -- se asume que este
    portal lo usa igual, sin poder confirmarlo en la página real.
Lo que NO se ha podido confirmar: la estructura exacta de la tabla de
resultados (si usa JavaScript para pintar las filas, qué columnas
expone, o si expresa la fecha límite con esa palabra literal). Por eso
la extracción de fecha límite es deliberadamente defensiva (por patrón
de texto sobre toda la fila, no por una columna fija) y puede no
encontrar nada. **Revisa el log "Avisos sin fecha límite reconocida" y
la captura de depuración tras la primera ejecución manual
(workflow_dispatch) antes de fiarte del cron automático -- de las tres
fuentes de este bloque, esta es la que más probablemente necesite un
ajuste tras verla contra la página real.**

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecucion local:      python ingesta_giz_satellite.py
Ejecucion programada: ver .github/workflows/sincronizar_giz_satellite.yml
   (necesita el paso extra "playwright install --with-deps chromium")
"""
import re
from datetime import date

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
CAMPOS_COMPARABLES = ("titulo", "fecha_limite")
CAPTURA_DEPURACION = "debug_giz_satellite_tabla.png"

CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# "10013531 - Support to SAHPRA's..." / "81322888 - Consultancy to..."
# -- patron de titulo confirmado contra avisos reales indexados.
PATRON_REFERENCIA_TITULO = re.compile(r"^\s*(\d{4,10})\s*[-–]\s*(.+)$")
PATRON_ANGEBOTSFRIST = re.compile(r"Angebotsfrist:?\s*(\d{1,2}\.\d{1,2}\.\d{4})")
PATRON_VEROEFFENTLICHT = re.compile(r"Ver(?:[oö]|oe)ffentlich\w*:?\s*(\d{1,2}\.\d{1,2}\.\d{4})", re.IGNORECASE)

# Cada aviso individual vive en /Satellite/notice/<ID> -- ver aviso de
# fiabilidad en el docstring.
_JS_EXTRAER_FILAS = """
() => {
    const resultados = [];
    const vistos = new Set();
    const enlaces = Array.from(document.querySelectorAll('a[href*="/Satellite/notice/"]'));

    for (const enlace of enlaces) {
        const href = enlace.getAttribute('href') || '';
        if (vistos.has(href)) continue;
        vistos.add(href);

        const titulo = (enlace.innerText || '').trim();

        // Texto de la fila/contenedor completo (para poder buscar por
        // patron "Angebotsfrist"/"Veroeffentlicht" fuera del propio
        // enlace, ver aviso de fiabilidad).
        let nodo = enlace;
        let textoContenedor = '';
        for (let i = 0; i < 6 && nodo.parentElement; i++) {
            nodo = nodo.parentElement;
            const texto = (nodo.innerText || '').trim();
            if (texto.length > titulo.length + 10) {
                textoContenedor = texto;
                break;
            }
        }

        resultados.push({ titulo, href, texto_contenedor: textoContenedor });
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
    avisos = []

    try:
        with sync_playwright() as p:
            navegador = None
            try:
                navegador = p.chromium.launch(headless=True)
                pagina = navegador.new_page(user_agent=CABECERAS_USER_AGENT)

                print(f"--> Cargando el portal de licitaciones de la GIZ: {LISTADO_URL}...", flush=True)
                pagina.goto(LISTADO_URL, timeout=TIEMPO_ESPERA_CARGA_MS, wait_until="domcontentloaded")

                try:
                    pagina.wait_for_selector('a[href*="/Satellite/notice/"]', timeout=TIEMPO_ESPERA_CARGA_MS)
                except Exception as error:
                    print(f"    No aparecio ningun aviso reconocible a tiempo: {error}", flush=True)
                    pagina.screenshot(path=CAPTURA_DEPURACION, full_page=True)
                    print(f"    Captura de depuracion guardada en {CAPTURA_DEPURACION}.", flush=True)
                    return []

                avisos = pagina.evaluate(_JS_EXTRAER_FILAS)
                print(f"    Avisos reconocidos: {len(avisos)}", flush=True)

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

    return avisos


def construir_registro(aviso: dict) -> dict:
    titulo_crudo = (aviso.get("titulo") or "").strip()
    href = aviso.get("href") or ""
    texto_contenedor = aviso.get("texto_contenedor") or ""

    # "10013531 - Support to SAHPRA's..." -> referencia + titulo limpio
    coincidencia_ref = PATRON_REFERENCIA_TITULO.match(titulo_crudo)
    if coincidencia_ref:
        referencia, titulo = coincidencia_ref.groups()
    else:
        referencia, titulo = None, titulo_crudo

    fecha_limite = None
    coincidencia_frist = PATRON_ANGEBOTSFRIST.search(texto_contenedor)
    if coincidencia_frist:
        fecha_limite = parsear_fecha_alemana(coincidencia_frist.group(1))

    fecha_publicacion = None
    coincidencia_veroeff = PATRON_VEROEFFENTLICHT.search(texto_contenedor)
    if coincidencia_veroeff:
        fecha_publicacion = parsear_fecha_alemana(coincidencia_veroeff.group(1))

    url_oficial = f"{BASE_URL}{href}" if href.startswith("/") else (href or None)
    slug_base = referencia or _generar_slug(titulo or href)

    return {
        "codigo_unico": f"GIZSAT-{_generar_slug(slug_base)}"[:150],
        "fuente_origen": FUENTE,
        "tipo_aviso": None,
        "titulo": titulo or titulo_crudo or None,
        "descripcion": f"Referencia GIZ: {referencia}." if referencia else None,
        "pais": "Alemania",
        "paises": ["Alemania"],
        "organismo": "GIZ",
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
    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - GIZ SATELLITE (portal propio)", flush=True)
    print("=" * 100, flush=True)

    crudos = extraer_avisos_playwright()
    print(f"\nTotal avisos rastreados: {len(crudos)}", flush=True)

    if not crudos:
        print(
            "No se ha extraído ningún aviso. Revisa el log de arriba y la captura de depuración "
            f"({CAPTURA_DEPURACION}) -- ver aviso de fiabilidad en el docstring: es la fuente menos "
            "verificada de este bloque.",
            flush=True,
        )
        return

    normalizados = [construir_registro(a) for a in crudos]

    sin_fecha_limite = sum(1 for n in normalizados if not n.get("fecha_limite"))
    if sin_fecha_limite:
        print(
            f"Avisos sin fecha límite reconocida: {sin_fecha_limite}/{len(normalizados)} -- "
            "ver aviso de fiabilidad en el docstring (es posible que este portal no use el "
            "literal 'Angebotsfrist' o que la fecha viva en otro sitio de la página).",
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

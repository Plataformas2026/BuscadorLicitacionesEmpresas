# -*- coding: utf-8 -*-
"""
ingesta_ec_intpa.py
-------------------
Sincroniza las licitaciones (calls for tenders) de la Comisión Europea --
DG INTPA (International Partnerships) -- publicadas en el EU Funding &
Tenders Portal contra la tabla `licitaciones_internacionales` de Supabase.

Portal:  https://ec.europa.eu/info/funding-tenders/opportunities/portal/
         screen/opportunities/calls-for-tenders
Filtro:  cftPartyLegalEntityId=47352462  (autoridad contratante = INTPA)

POR QUÉ PLAYWRIGHT
--------------------------------------------------------------------------
El portal es una SPA Angular: el HTML que devuelve el servidor es un
armazón vacío y los resultados se pintan con JavaScript. Con requests +
BeautifulSoup (como en AFD/Enabel/LuxDev) no se vería ninguna licitación,
así que se renderiza cada página con Playwright (igual que UGPE, UNDP...) y
el DOM ya pintado se analiza con BeautifulSoup. El parser se ha probado
contra el DOM real aportado (566 resultados, 50 por página).

QUÉ SE EXTRAE
--------------------------------------------------------------------------
  Del LISTADO: título, referencia, fechas de publicación y límite, estado
  (Open For Submission / Forthcoming / Closed), procedimiento, tipo de
  contrato y enlace a la ficha.

  De la FICHA de cada licitación abierta (se abre con Playwright): descripción
  completa del objeto, valor estimado, CPV, naturaleza y duración máxima del
  contrato, método de adjudicación, acuerdo marco, lugares de ejecución
  (-> `pais`/`paises`), programa, hitos con hora y zona horaria (plazo de
  solicitudes de participación / ofertas), autoridad contratante, referencia
  TED y lista de documentos (-> `url_documento`). Todo se guarda en
  `descripcion`.

DECISIONES DE DISEÑO
--------------------------------------------------------------------------
- Solo se guardan avisos con estado "Open For Submission" y fecha límite no
  vencida (ESTADOS_INCLUIDOS). Los "Forthcoming" (avisos previos PIN, sin
  plazo) se omiten: cuando se publica la licitación real, el PIN pasa a
  "Closed" y su fila quedaría huérfana en Supabase (sin fecha_limite, la app
  no la caducaría). Si los quieres, añade "forthcoming" a ESTADOS_INCLUIDOS.
- `codigo_unico` = EC-INTPA-<uuid del aviso>-<CN|PIN>, tomado de la URL de
  la ficha (estable aunque cambie el título).
- `url_oficial` = URL de la ficha SIN los parámetros de navegación (?order=,
  &pageNumber=, ...). `url_documento` = primer documento publicado (p. ej.
  "Additional information to Contract Notice"); si no hay, el ZIP con todos.
- `categoria` = tipo de contrato; `tipo_aviso` = "Contract notice" (o
  "Prior information notice") + procedimiento.
- `paises` = lugares de ejecución de la ficha (sin el código numérico). `pais`
  (campo de texto que usan el filtro y el matching de la app) = ese nombre si
  es uno solo, los nombres separados por coma si son 2-3, y "Multi-country" si
  son más (la lista completa queda en `paises` y en la descripción).
- Se descarga la ficha de TODAS las abiertas en cada ejecución (~40) para
  detectar cambios (plazos prorrogados, nuevos documentos). Si la ficha de una
  ya guardada falla, se conservan sus datos guardados y no se marca cambiada.
- Orden: se pide el listado ordenado por fecha límite DESCENDENTE
  (sortBy=deadlineDate) para que las abiertas salgan primero y se pueda
  parar pronto. Si así no se obtiene ninguna abierta (orden no soportado o
  portal distinto), se repite ordenado por fecha de publicación recorriendo
  todas las páginas.

NO VALIDADO contra el portal real: el orden por deadlineDate, la apertura
directa de la ficha por URL y la carga en GitHub Actions (el portal podría
limitar IPs de centros de datos). Los parsers del listado y de la ficha sí
están validados con el DOM real.

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local o programada: python ingesta_ec_intpa.py
"""
import asyncio
import math
import re
import time
from datetime import date, datetime
from urllib.parse import urlencode, urljoin, urlparse

import nest_asyncio
from bs4 import BeautifulSoup
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    obtener_registros_existentes,
    subir_en_lotes,
)

try:
    nest_asyncio.apply()
except Exception:
    pass

BASE_URL = "https://ec.europa.eu"
LISTADO_URL = (
    BASE_URL
    + "/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-tenders"
)
ID_AUTORIDAD_INTPA = "47352462"

FUENTE = "EC-INTPA"
ORGANISMO = "European Commission - DG INTPA (International Partnerships)"

ESTADOS_INCLUIDOS = {"open for submission"}

TAMANO_PAGINA = 50
MAX_PAGINAS_SEGURIDAD = 20
MAX_PAGINAS_SEGUIDAS_SIN_ABIERTAS = 2  # solo con orden por fecha límite
MAX_DETALLES_POR_EJECUCION = 120
PAUSA_ENTRE_DETALLES_SEGUNDOS = 1.0
ESPERA_DOCUMENTOS_MS = 8000
TIEMPO_ESPERA_CARGA_MS = 45000
PAUSA_RENDER_MS = 1500
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 1.0
LOTE_ENVIO_SUPABASE = 15
CAPTURA_DEPURACION = "debug_ec_intpa_listado.html"
CAPTURA_DEPURACION_FICHA = "debug_ec_intpa_ficha.html"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

MESES_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
PATRON_FECHA = r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})"

CAMPOS_COMPARABLES = (
    "titulo",
    "descripcion",
    "tipo_aviso",
    "categoria",
    "pais",
    "paises",
    "url_documento",
    "fecha_publicacion",
    "fecha_limite",
)

# Etiquetas de la ficha que no se repiten en la descripción (ya van en otra parte)
ETIQUETAS_OMITIR = {"description", "procedure identifier", "ted publication date"}


# ============================================================
# UTILIDADES
# ============================================================

def _limpiar_texto(texto):
    if not texto:
        return None
    texto = texto.replace("\u00ad", "").replace("\xa0", " ")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def _parsear_fecha(texto):
    """'03 November 2026' -> date(2026, 11, 3). None si no se reconoce."""
    if not texto:
        return None
    m = re.search(PATRON_FECHA, texto)
    if not m:
        return None
    dia, mes_txt, anio = m.groups()
    mes = MESES_EN.get(mes_txt.lower())
    if not mes:
        return None
    try:
        return date(int(anio), mes, int(dia))
    except ValueError:
        return None


def _slug(texto: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", texto or "").strip("-")


def _url_listado(numero_pagina: int, sort_by: str) -> str:
    parametros = {
        "order": "DESC",
        "pageNumber": numero_pagina,
        "pageSize": TAMANO_PAGINA,
        "sortBy": sort_by,
        "isExactMatch": "true",
        "cftPartyLegalEntityId": ID_AUTORIDAD_INTPA,
    }
    return f"{LISTADO_URL}?{urlencode(parametros)}"


def _url_ficha_limpia(href: str):
    """Quita ?order=...&pageNumber=... (parámetros de navegación del listado)."""
    if not href:
        return None
    absoluta = urljoin(BASE_URL + "/", href)
    partes = urlparse(absoluta)
    return f"{partes.scheme}://{partes.netloc}{partes.path}"


# ============================================================
# PARSER DEL DOM RENDERIZADO
# ============================================================

def extraer_total_resultados(html: str):
    m = re.search(r"([\d.,]+)\s*item\(s\)\s*found", BeautifulSoup(html, "html.parser").get_text(" "))
    if not m:
        return None
    try:
        return int(re.sub(r"[.,]", "", m.group(1)))
    except ValueError:
        return None


def extraer_licitaciones_de_pagina(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    licitaciones = []

    for tarjeta in soup.find_all("sedia-result-card-calls-for-tenders"):
        enlace = tarjeta.select_one("eui-card-header-title a")
        titulo = _limpiar_texto(enlace.get_text(" ", strip=True)) if enlace else None
        if not titulo:
            continue

        href = enlace.get("href")
        url_oficial = _url_ficha_limpia(href)

        # Identificador estable: <uuid>-<CN|PIN> al final de la ruta de la ficha
        segmento = urlparse(url_oficial).path.rstrip("/").split("/")[-1] if url_oficial else ""

        bloques = tarjeta.select("eui-card-header-subtitle sedia-result-card-type")
        referencia = None
        fecha_publicacion = fecha_limite = None
        if bloques:
            span = bloques[0].find("span")
            referencia = _limpiar_texto(span.get_text(" ", strip=True)) if span else None
        if len(bloques) > 1:
            texto_fechas = _limpiar_texto(bloques[1].get_text(" ", strip=True)) or ""
            m_pub = re.search(r"Publication date:\s*" + PATRON_FECHA, texto_fechas)
            m_lim = re.search(r"Deadline:\s*" + PATRON_FECHA, texto_fechas)
            if m_pub:
                fecha_publicacion = _parsear_fecha(" ".join(m_pub.groups()))
            if m_lim:
                fecha_limite = _parsear_fecha(" ".join(m_lim.groups()))

        etiqueta_estado = tarjeta.select_one("sedia-project-status span.eui-label")
        estado = _limpiar_texto(etiqueta_estado.get_text(" ", strip=True)) if etiqueta_estado else None

        contenido = tarjeta.select_one("eui-card-content")
        texto_contenido = _limpiar_texto(contenido.get_text(" ", strip=True)) if contenido else ""
        procedimiento = tipo_contrato = None
        m = re.search(r"Procedure type:\s*(.*?)\s*(?:\||Contract type:|$)", texto_contenido or "")
        if m:
            procedimiento = _limpiar_texto(m.group(1))
        m = re.search(r"Contract type:\s*(.*)$", texto_contenido or "")
        if m:
            tipo_contrato = _limpiar_texto(m.group(1))

        if segmento:
            codigo = f"EC-INTPA-{segmento}"
        elif referencia:
            codigo = f"EC-INTPA-{_slug(referencia)}"
        else:
            continue

        licitaciones.append({
            "codigo_unico": codigo[:150],
            "titulo": titulo,
            "referencia": referencia,
            "estado": estado,
            "es_pin": segmento.upper().endswith("-PIN") or (referencia or "").upper().endswith("-PIN"),
            "procedimiento": procedimiento,
            "tipo_contrato": tipo_contrato,
            "fecha_publicacion": fecha_publicacion,
            "fecha_limite": fecha_limite,
            "url_oficial": url_oficial,
        })

    return licitaciones


# ============================================================
# PARSER DE LA FICHA
# ============================================================

def _texto_multilinea(el):
    """Texto conservando los saltos de párrafo (la descripción los lleva)."""
    if el is None:
        return None
    texto = el.get_text().replace("\u00ad", "").replace("\xa0", " ")
    texto = re.sub(r"[ \t]+", " ", texto)
    lineas = [l.strip() for l in texto.split("\n")]
    texto = "\n".join(lineas)
    texto = re.sub(r"\n{3,}", "\n\n", texto).strip()
    return texto or None


def _pares_etiqueta_valor(contenedor) -> list:
    """Pares (etiqueta, valor) de la tarjeta de detalles. Dos maquetaciones:
    a) <div class="eui-u-f-bold">Etiqueta</div><div>Valor</div>[<div>Valor</div>...]
    b) <div euiinputgroup><div class="row"><eui-label><strong>Etiqueta</strong>...
       <div class="row"><eui-label>Valor</eui-label>
    """
    pares, vistos = [], set()

    def _anadir(etiqueta, valor):
        if not etiqueta or not valor:
            return
        clave = etiqueta.lower()
        if clave in vistos:
            return
        vistos.add(clave)
        pares.append((etiqueta, valor))

    for lab in contenedor.select("div.eui-u-f-bold"):
        etiqueta = _limpiar_texto(lab.get_text(" ", strip=True))
        valores = [_texto_multilinea(sib) for sib in lab.find_next_siblings("div")]
        valores = [v for v in valores if v]
        if etiqueta and valores:
            _anadir(etiqueta, "; ".join(valores) if len(valores) > 1 else valores[0])

    for grupo in contenedor.find_all(attrs={"euiinputgroup": True}):
        filas = grupo.find_all("div", class_="row", recursive=False)
        if len(filas) >= 2:
            _anadir(
                _limpiar_texto(filas[0].get_text(" ", strip=True)),
                _limpiar_texto(filas[1].get_text(" ", strip=True)),
            )
    return pares


def _nombre_lugar(texto: str) -> str:
    """'20000931 - St Kitts and Nevis' -> 'St Kitts and Nevis'."""
    return re.sub(r"^\s*\d+\s*-\s*", "", texto or "").strip()


def parsear_ficha(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    resultado = {
        "texto_descripcion": None,
        "pares": [],
        "lugares": [],
        "ted_texto": None,
        "ted_url": None,
        "documentos": [],
        "url_documento": None,
        "fecha_limite_ficha": None,
    }

    cab = soup.select_one("eui-card-header.scroll-gi")
    tarjeta = cab.find_parent("eui-card") if cab else None
    if tarjeta is not None:
        pares = _pares_etiqueta_valor(tarjeta)
        for etiqueta, valor in pares:
            clave = etiqueta.lower()
            if clave == "description":
                resultado["texto_descripcion"] = valor
            elif clave.startswith("place(s) of delivery"):
                resultado["lugares"] = [
                    _nombre_lugar(x) for x in valor.split(";") if _nombre_lugar(x)
                ]
            elif clave.startswith("deadline for receipt") and not resultado["fecha_limite_ficha"]:
                resultado["fecha_limite_ficha"] = _parsear_fecha_numerica(valor)
        resultado["pares"] = pares

    cab_ted = soup.select_one("eui-card-header.scroll-ref")
    tarjeta_ted = cab_ted.find_parent("eui-card") if cab_ted else None
    if tarjeta_ted is not None:
        enlace = tarjeta_ted.find("a", href=True)
        if enlace:
            resultado["ted_texto"] = _limpiar_texto(enlace.get_text(" ", strip=True))
            resultado["ted_url"] = enlace["href"]

    docs = soup.select_one("documents-list")
    if docs is not None:
        for fila in docs.select("tbody tr"):
            titulo_a = fila.select_one("eui-card-header-title a[href]")
            if not titulo_a:
                continue
            titulo = _limpiar_texto(titulo_a.get_text(" ", strip=True))
            href = urljoin(BASE_URL + "/", titulo_a["href"])
            sub = fila.select_one("eui-card-header-subtitle")
            detalle_doc = _limpiar_texto(sub.get_text(" ", strip=True)) if sub else None
            idioma = fila.select_one("eui-badge")
            idioma = _limpiar_texto(idioma.get_text(" ", strip=True)) if idioma else None
            linea = titulo or href
            extras = [x for x in (detalle_doc, idioma) if x]
            if extras:
                linea += " (" + "; ".join(extras) + ")"
            resultado["documentos"].append(linea)
            if not resultado["url_documento"]:
                resultado["url_documento"] = href
        if not resultado["url_documento"]:
            zip_a = docs.select_one("a[href$='.zip']")
            if zip_a:
                resultado["url_documento"] = urljoin(BASE_URL + "/", zip_a["href"])

    return resultado


def _parsear_fecha_numerica(texto):
    """'03/11/2026 16:00 America/Barbados' -> date(2026, 11, 3)."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", texto or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _esta_abierta(lic: dict, hoy: date) -> bool:
    if (lic.get("estado") or "").strip().lower() not in ESTADOS_INCLUIDOS:
        return False
    limite = lic.get("fecha_limite")
    return limite is None or limite >= hoy


# ============================================================
# RENDERIZADO CON PLAYWRIGHT
# ============================================================

async def _renderizar(page, url: str) -> str:
    await page.goto(url, wait_until="domcontentloaded", timeout=TIEMPO_ESPERA_CARGA_MS)
    try:
        await page.wait_for_selector("sedia-result-card-calls-for-tenders", timeout=TIEMPO_ESPERA_CARGA_MS)
    except PlaywrightTimeoutError:
        print("    Aviso: no aparecieron tarjetas en el tiempo de espera.", flush=True)
    await page.wait_for_timeout(PAUSA_RENDER_MS)
    return await page.content()


async def _recorrer(page, sort_by: str, parar_sin_abiertas: bool, hoy: date) -> dict:
    encontradas = {}
    paginas_seguidas_sin_abiertas = 0
    total_paginas = None

    for numero_pagina in range(1, MAX_PAGINAS_SEGURIDAD + 1):
        print(f"--> EC-INTPA: página {numero_pagina} (orden {sort_by})...", flush=True)
        html = await _renderizar(page, _url_listado(numero_pagina, sort_by))

        if numero_pagina == 1:
            try:
                with open(CAPTURA_DEPURACION, "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass
            total = extraer_total_resultados(html)
            if total:
                total_paginas = math.ceil(total / TAMANO_PAGINA)
                print(f"    Resultados totales en el portal: {total} ({total_paginas} páginas)", flush=True)

        licitaciones = extraer_licitaciones_de_pagina(html)
        abiertas = [l for l in licitaciones if _esta_abierta(l, hoy)]
        print(f"    Avisos en la página: {len(licitaciones)} | abiertos: {len(abiertas)}", flush=True)

        if not licitaciones:
            print("    Sin avisos: fin del listado.", flush=True)
            break

        for l in abiertas:
            encontradas.setdefault(l["codigo_unico"], l)

        paginas_seguidas_sin_abiertas = 0 if abiertas else paginas_seguidas_sin_abiertas + 1
        if parar_sin_abiertas and paginas_seguidas_sin_abiertas >= MAX_PAGINAS_SEGUIDAS_SIN_ABIERTAS:
            print("    Varias páginas seguidas sin abiertas: se detiene (orden por fecha límite).", flush=True)
            break
        if total_paginas and numero_pagina >= total_paginas:
            break

        await asyncio.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)

    return encontradas


async def _renderizar_ficha(page, url: str) -> str:
    await page.goto(url, wait_until="domcontentloaded", timeout=TIEMPO_ESPERA_CARGA_MS)
    await page.wait_for_selector("eui-card-header.scroll-gi", timeout=TIEMPO_ESPERA_CARGA_MS)
    try:
        await page.wait_for_selector("documents-list eui-card-header-title", timeout=ESPERA_DOCUMENTOS_MS)
    except PlaywrightTimeoutError:
        pass  # puede no haber documentos
    await page.wait_for_timeout(PAUSA_RENDER_MS)
    return await page.content()


async def _anadir_fichas(page, licitaciones: list):
    guardada_depuracion = False
    for i, lic in enumerate(licitaciones[:MAX_DETALLES_POR_EJECUCION], 1):
        print(f"--> Ficha {i}/{min(len(licitaciones), MAX_DETALLES_POR_EJECUCION)}: {lic['titulo'][:70]}", flush=True)
        try:
            html = await _renderizar_ficha(page, lic["url_oficial"])
            if not guardada_depuracion:
                try:
                    with open(CAPTURA_DEPURACION_FICHA, "w", encoding="utf-8") as f:
                        f.write(html)
                    guardada_depuracion = True
                except Exception:
                    pass
            lic["ficha"] = parsear_ficha(html)
        except Exception as error:
            print(f"    Aviso: no se pudo leer la ficha ({error}).", flush=True)
            lic["ficha"] = None
        await asyncio.sleep(PAUSA_ENTRE_DETALLES_SEGUNDOS)


async def _extraer_abiertas_async() -> list:
    hoy = date.today()
    async with async_playwright() as p:
        navegador = await p.chromium.launch(headless=True)
        contexto = await navegador.new_context(
            user_agent=USER_AGENT, locale="en-GB", viewport={"width": 1366, "height": 900}
        )
        page = await contexto.new_page()
        try:
            encontradas = await _recorrer(page, "deadlineDate", True, hoy)
            if not encontradas:
                print("Sin abiertas con orden por fecha límite; se reintenta por fecha de publicación...", flush=True)
                encontradas = await _recorrer(page, "startDate", False, hoy)
            if encontradas:
                print(f"\nLeyendo las fichas de {len(encontradas)} licitaciones abiertas...", flush=True)
                await _anadir_fichas(page, list(encontradas.values()))
        finally:
            await contexto.close()
            await navegador.close()
    print(f"\nLicitaciones abiertas detectadas: {len(encontradas)}", flush=True)
    return list(encontradas.values())


# ============================================================
# CONSTRUIR REGISTRO / DIFF
# ============================================================

def _pais_desde_lugares(lugares: list):
    if not lugares:
        return None
    if len(lugares) <= 3:
        return ", ".join(lugares)
    return "Multi-country"


def construir_registro(l: dict) -> dict:
    tipo_base = "Prior information notice" if l.get("es_pin") else "Contract notice"
    procedimiento = l.get("procedimiento")
    ficha = l.get("ficha")

    partes = []
    if ficha and ficha.get("texto_descripcion"):
        partes.append(ficha["texto_descripcion"])

    lineas = []
    if l.get("referencia"):
        lineas.append(f"Reference: {l['referencia']}")
    if procedimiento:
        lineas.append(f"Procedure type: {procedimiento}")
    if l.get("tipo_contrato"):
        lineas.append(f"Contract type: {l['tipo_contrato']}")
    if l.get("fecha_publicacion"):
        lineas.append(f"Publication date: {l['fecha_publicacion'].isoformat()}")
    if l.get("fecha_limite"):
        lineas.append(f"Deadline: {l['fecha_limite'].isoformat()}")
    if ficha:
        ya_incluidas = {"procedure type", "contract type"}
        for etiqueta, valor in ficha.get("pares", []):
            clave = etiqueta.lower()
            if clave in ETIQUETAS_OMITIR or clave in ya_incluidas:
                continue
            lineas.append(f"{etiqueta}: {valor}")
        if ficha.get("ted_texto"):
            lineas.append(
                f"TED notice: {ficha['ted_texto']}" + (f" ({ficha['ted_url']})" if ficha.get("ted_url") else "")
            )
        if ficha.get("documentos"):
            lineas.append("Documents: " + " | ".join(ficha["documentos"]))
    else:
        lineas.append("Contracting authority: European Commission - DG INTPA")
    partes.append("\n".join(lineas))

    lugares = (ficha or {}).get("lugares") or []
    fecha_limite = l.get("fecha_limite") or (ficha or {}).get("fecha_limite_ficha")

    return {
        "codigo_unico": l["codigo_unico"],
        "fuente_origen": FUENTE,
        "tipo_aviso": f"{tipo_base} - {procedimiento}" if procedimiento else tipo_base,
        "titulo": l["titulo"],
        "descripcion": "\n\n".join(p for p in partes if p),
        "pais": _pais_desde_lugares(lugares),
        "paises": lugares,
        "organismo": ORGANISMO,
        "categoria": l.get("tipo_contrato"),
        "url_oficial": l["url_oficial"],
        "url_documento": (ficha or {}).get("url_documento"),
        "fecha_publicacion": l["fecha_publicacion"].isoformat() if l.get("fecha_publicacion") else None,
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
        "_ficha_ok": bool(ficha),
    }


def _texto_completo(d: dict) -> str:
    return (
        f"Titulo: {d['titulo']}\n"
        f"{d.get('descripcion') or ''}\n"
        f"Pais: {d.get('pais') or 'No especificado'}\n"
        f"Categoria: {d.get('categoria') or 'No especificada'}\n"
        f"Tipo: {d.get('tipo_aviso') or ''}"
    )


def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []
    for datos in normalizados:
        existente = registros_existentes.get(datos["codigo_unico"])

        # Si la ficha no se pudo leer en esta ejecución, se conservan los datos
        # ya guardados de esa licitación (para no marcarla como cambiada).
        ficha_ok = datos.pop("_ficha_ok", True)
        if existente is not None and not ficha_ok:
            for campo in ("descripcion", "url_documento", "pais", "paises"):
                if existente.get(campo) is not None:
                    datos[campo] = existente[campo]

        if existente is None:
            es_novedad, es_actualizada = True, False
        else:
            ha_cambiado = any(
                str(existente.get(c)) != str(datos.get(c)) for c in CAMPOS_COMPARABLES
            )
            if not ha_cambiado:
                continue
            es_novedad, es_actualizada = False, True

        datos["texto_completo"] = _texto_completo(datos)
        datos["embedding"] = generar_embedding(datos["texto_completo"])
        datos["es_novedad"] = es_novedad
        datos["es_actualizada"] = es_actualizada
        a_subir.append(datos)
    return a_subir


# ============================================================
# EJECUCION PRINCIPAL
# ============================================================

def ejecutar_sincronizacion():
    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - EC (DG INTPA)", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL} (cftPartyLegalEntityId={ID_AUTORIDAD_INTPA})", flush=True)

    licitaciones = asyncio.run(_extraer_abiertas_async())
    if not licitaciones:
        print(
            "No se ha detectado ninguna licitación abierta. "
            f"Revisa el log y, si existe, {CAPTURA_DEPURACION}.",
            flush=True,
        )
        return

    normalizados = [construir_registro(l) for l in licitaciones]

    supabase = obtener_cliente_supabase()
    registros_existentes = obtener_registros_existentes(
        supabase,
        tabla="licitaciones_internacionales",
        columna_clave="codigo_unico",
        columnas=("id", "codigo_unico") + CAMPOS_COMPARABLES,
        claves=[d["codigo_unico"] for d in normalizados],
    )
    print(f"Ya existentes en Supabase: {len(registros_existentes)}/{len(normalizados)}", flush=True)

    lote_final = preparar_lote_para_subir(normalizados, registros_existentes)
    if not lote_final:
        print("No hay licitaciones nuevas ni cambios que sincronizar.", flush=True)
        return

    print(f"Subiendo {len(lote_final)} registros...", flush=True)
    subidos = subir_en_lotes(
        supabase,
        "licitaciones_internacionales",
        "codigo_unico",
        lote_final,
        tamano_lote=LOTE_ENVIO_SUPABASE,
    )
    print(f"\nSincronización EC-INTPA completada: {subidos}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

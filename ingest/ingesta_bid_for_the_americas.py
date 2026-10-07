# -*- coding: utf-8 -*-
"""
ingesta_bid_for_the_americas.py
-------------------------------
Sincroniza las licitaciones VIGENTES de "BID for the Americas APP"
(ConnectAmericas) contra la tabla `licitaciones_internacionales` de
Supabase, mediante Playwright (navegador real) + BeautifulSoup.

    Listado:  https://bidfortheamericas.connectamericas.com/es/licitaciones
    Ficha:    https://bidfortheamericas.connectamericas.com/es/licitaciones/<pais>/<slug>-<id>

PARTICULARIDADES DEL PORTAL
--------------------------------------------------------------------------
- Es una app Next.js que pinta TODO en el navegador: el HTML que devuelve
  el servidor solo trae textos de traducción y SEO (`__NEXT_DATA__`), sin
  ningún aviso. Por eso se usa Playwright y no `requests`.
- LISTADO con scroll infinito (`#scrollableDivTenders`, de 10 en 10, con un
  contador "N resultados"). Cada tarjeta trae: país, título, categorías
  (chips en mayúsculas), monto (opcional) y fecha de cierre.
- Las tarjetas NO tienen enlace <a> ni ID: "Ver detalle" es un botón. La URL
  de la ficha (`.../<pais>/<slug>-3475`) solo se conoce haciendo clic, y el
  número final es el ID estable del aviso (`codigo_unico` = BIDAPP-<id>).
- FICHA: título (h1), categorías y subcategorías, Ubicación, Cierre de
  postulación, Unidad ejecutora, Descripción, Tipo de licitación y
  "Palabras clave de este pliego". El contacto y el enlace al aviso original
  están tras login, así que `url_documento` es NULL.
- Ni listado ni ficha publican fecha de publicación: se guarda la fecha de
  primera detección y se CONSERVA la ya guardada en ejecuciones posteriores
  (mismo criterio que Enabel).

MAPEO
--------------------------------------------------------------------------
- `descripcion` = "Palabras clave: a, b, c." + texto COMPLETO del apartado
  Descripción de la ficha.
- `categoria`   = categorías principales + (subcategorías), p. ej.
  "TECNOLOGÍA; SALUD (Equipos; Implementación de sistemas)".
- `pais`        = Ubicación de la ficha; `organismo` = Unidad ejecutora;
  `tipo_aviso`   = Tipo de licitación.

ESTRATEGIA (igual que AFD / Enabel / GIZ Satellite)
--------------------------------------------------------------------------
Se recorre el listado completo, se compara cada tarjeta con lo que ya hay en
Supabase para esta fuente (país + título normalizado + fecha de cierre) y SOLO
se abre la ficha (clic + vuelta al listado) de las tarjetas NUEVAS o con
cambios visibles. Solo se generan embeddings / suben las nuevas o las que han
cambiado (`es_actualizada=True`).

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local o programada: python ingesta_bid_for_the_americas.py
(necesita `python -m playwright install --with-deps chromium`).
"""
import hashlib
import re
import unicodedata
from datetime import date
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    subir_en_lotes,
)

BASE_URL = "https://bidfortheamericas.connectamericas.com"
LISTADO_URL = BASE_URL + "/es/licitaciones"

FUENTE = "BID for the Americas APP"
ORGANISMO_POR_DEFECTO = "BID for the Americas (ConnectAmericas)"
TIPO_AVISO_POR_DEFECTO = "Licitación"
TABLA = "licitaciones_internacionales"

TIEMPO_ESPERA_CARGA_MS = 45000
TIEMPO_ESPERA_FICHA_MS = 25000
PAUSA_SCROLL_MS = 1200
MAX_SCROLLS_SIN_CRECER = 3
MAX_SCROLLS = 80
MAX_FICHAS_POR_EJECUCION = 150
PAUSA_ENTRE_FICHAS_MS = 400
LOTE_ENVIO_SUPABASE = 15

CAPTURA_LISTADO = "debug_bid_for_the_americas_listado.html"
CAPTURA_FICHA = "debug_bid_for_the_americas_ficha.html"
CAPTURA_API = "debug_bid_for_the_americas_api.txt"

CABECERAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

MESES_ES = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
}
PATRON_FECHA_ES = re.compile(r"(\d{1,2})\s*/\s*([A-Za-zñÑ]{3,4})\.?\s*/\s*(\d{4})")
PATRON_ID_URL = re.compile(r"-(\d+)/?$")
PATRON_URL_FICHA = re.compile(r"/licitaciones/[^/?#]+/[^/?#]+")
PATRON_TOTAL = re.compile(r"(\d[\d.,]*)\s+resultados?", re.IGNORECASE)

# Comparables para decidir si una licitación ya existente ha cambiado.
CAMPOS_COMPARABLES = (
    "titulo",
    "descripcion",
    "categoria",
    "organismo",
    "pais",
    "fecha_limite",
)

# Hosts que no aportan nada al scraping (acelera la carga y evita ruido).
HOSTS_PRESCINDIBLES = (
    "googletagmanager.com", "google-analytics.com", "clarity.ms",
    "facebook.net", "facebook.com", "accounts.google.com",
)


# ============================================================
# UTILIDADES DE TEXTO / FECHAS
# ============================================================

def _limpiar_texto(texto: str):
    if not texto:
        return None
    texto = texto.replace("\u00ad", "").replace("\xa0", " ")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def _limpiar_parrafos(texto: str):
    """Como _limpiar_texto pero CONSERVANDO los saltos de línea del texto original."""
    if not texto:
        return None
    texto = texto.replace("\u00ad", "").replace("\xa0", " ").replace("\r", "")
    lineas = [re.sub(r"[ \t]+", " ", linea).strip() for linea in texto.split("\n")]
    texto = "\n".join(lineas)
    texto = re.sub(r"\n{3,}", "\n\n", texto).strip()
    return texto or None


def _normalizar(texto: str) -> str:
    """Clave de comparación: minúsculas, sin tildes y solo alfanumérico."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def _clave_listado(pais: str, titulo: str) -> str:
    return f"{_normalizar(pais)}|{_normalizar(titulo)}"


def _unir_sin_repetir(valores: list, separador: str = "; "):
    vistos = set()
    resultado = []
    for valor in valores:
        valor = _limpiar_texto(valor)
        if not valor or valor.lower() in vistos:
            continue
        vistos.add(valor.lower())
        resultado.append(valor)
    return separador.join(resultado) if resultado else None


def parsear_fecha_es(texto: str):
    """'08/oct/2026' -> date(2026, 10, 8). None si no se reconoce."""
    if not texto:
        return None
    coincidencia = PATRON_FECHA_ES.search(texto)
    if not coincidencia:
        return None
    dia, nombre_mes, anio = coincidencia.groups()
    mes = MESES_ES.get(nombre_mes.lower()[:3])
    if not mes:
        return None
    try:
        return date(int(anio), mes, int(dia))
    except ValueError:
        return None


def _es_chip_categoria(texto: str, excluidos: set) -> bool:
    """Los chips de categoría son texto en MAYÚSCULAS sin cifras ('TECNOLOGÍA', 'AGUA Y SANEAMIENTO')."""
    if not texto or texto in excluidos:
        return False
    if texto != texto.upper() or re.search(r"\d", texto):
        return False
    if texto.upper().startswith("USD") or len(texto) < 3:
        return False
    return bool(re.search(r"[A-ZÁÉÍÓÚÑÜ]", texto))


def codigo_unico_desde_url(url: str) -> str:
    ruta = urlparse(url).path.rstrip("/")
    coincidencia = PATRON_ID_URL.search(ruta)
    if coincidencia:
        return f"BIDAPP-{coincidencia.group(1)}"
    return f"BIDAPP-H{hashlib.md5(ruta.encode('utf-8')).hexdigest()[:16]}"


def componer_descripcion(palabras_clave: list, texto: str):
    """Palabras clave al INICIO de la descripción, seguidas del texto completo del apartado."""
    partes = []
    if palabras_clave:
        partes.append("Palabras clave: " + ", ".join(palabras_clave) + ".")
    if texto:
        partes.append(texto)
    return "\n\n".join(partes) if partes else None


# ============================================================
# PARSER DEL LISTADO (HTML renderizado)
# ============================================================

def _botones_ver_detalle(contenedor):
    return [
        d for d in contenedor.find_all("div", attrs={"dir": "auto"})
        if _limpiar_texto(d.get_text(" ")) == "Ver detalle"
    ]


def _contenedor_tarjeta(boton):
    """Sube desde 'Ver detalle' hasta el bloque que contiene SOLO esa licitación."""
    nodo = boton
    while nodo.parent is not None and nodo.parent.name not in ("body", "html", "[document]"):
        padre = nodo.parent
        if len(_botones_ver_detalle(padre)) > 1:
            break
        nodo = padre
    return nodo


def _valor_tras_etiqueta(contenedor, etiqueta: str):
    etiqueta = etiqueta.lower()
    for d in contenedor.find_all("div", attrs={"dir": "auto"}):
        if (_limpiar_texto(d.get_text(" ")) or "").lower().rstrip(":").strip() == etiqueta.rstrip(":"):
            siguiente = d.find_next("div", attrs={"dir": "auto"})
            return _limpiar_texto(siguiente.get_text(" ")) if siguiente else None
    return None


def parsear_listado(html: str) -> list:
    """Devuelve una lista de dicts {titulo, pais, categorias, monto, fecha_limite} (en orden)."""
    soup = BeautifulSoup(html, "html.parser")
    resultado = []
    for boton in _botones_ver_detalle(soup):
        tarjeta = _contenedor_tarjeta(boton)
        spans = [_limpiar_texto(s.get_text(" ")) for s in tarjeta.select("span > span")]
        spans = [s for s in spans if s]
        if len(spans) < 2:
            continue
        pais, titulo = spans[0], spans[1]

        excluidos = {pais, titulo}
        categorias = []
        for d in tarjeta.find_all("div", attrs={"dir": "auto"}):
            texto = _limpiar_texto(d.get_text(" "))
            if _es_chip_categoria(texto, excluidos) and texto not in categorias:
                categorias.append(texto)

        resultado.append({
            "titulo": titulo,
            "pais": pais,
            "categorias": categorias,
            "monto": _valor_tras_etiqueta(tarjeta, "Monto:"),
            "fecha_limite": parsear_fecha_es(_valor_tras_etiqueta(tarjeta, "Fecha de cierre:") or ""),
        })
    return resultado


# ============================================================
# PARSER DE LA FICHA (HTML renderizado)
# ============================================================

def parsear_ficha(html: str) -> dict:
    resultado = {
        "titulo": None, "categorias": [], "subcategorias": [], "pais": None,
        "fecha_limite": None, "organismo": None, "descripcion": None,
        "tipo_aviso": None, "palabras_clave": [],
    }
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if h1 is None:
        return resultado
    resultado["titulo"] = _limpiar_texto(h1.get_text(" "))

    elementos = h1.find_all_next("div", attrs={"dir": "auto"})
    textos = []
    for el in elementos:
        crudo = el.get_text("")
        textos.append((_limpiar_texto(crudo) or "", crudo))

    def _etq(t):
        return t.lower().rstrip(":").strip()

    def _indice(etiqueta, empieza_por=False):
        for i, (limpio, _) in enumerate(textos):
            if (_etq(limpio).startswith(etiqueta) if empieza_por else _etq(limpio) == etiqueta):
                return i
        return None

    def _valor(etiqueta, conservar_saltos=False):
        i = _indice(etiqueta)
        if i is None or i + 1 >= len(textos):
            return None
        limpio, crudo = textos[i + 1]
        return _limpiar_parrafos(crudo) if conservar_saltos else limpio or None

    i_sub = _indice("subcategorías", empieza_por=True)
    i_ubi = _indice("ubicación")
    fin_principales = i_sub if i_sub is not None else (i_ubi if i_ubi is not None else 0)
    for limpio, _ in textos[:fin_principales]:
        if _es_chip_categoria(limpio, set()) and limpio not in resultado["categorias"]:
            resultado["categorias"].append(limpio)
    if i_sub is not None and i_ubi is not None and i_ubi > i_sub:
        for limpio, _ in textos[i_sub + 1:i_ubi]:
            if limpio and limpio not in resultado["subcategorias"]:
                resultado["subcategorias"].append(limpio)

    resultado["pais"] = _valor("ubicación")
    resultado["fecha_limite"] = parsear_fecha_es(_valor("cierre de postulación") or "")
    resultado["organismo"] = _valor("unidad ejecutora")
    resultado["descripcion"] = _valor("descripción", conservar_saltos=True)
    resultado["tipo_aviso"] = _valor("tipo de licitación")

    claves = _valor("palabras clave de este pliego")
    if claves:
        claves = re.sub(r"\.\s*$", "", claves)
        resultado["palabras_clave"] = [c.strip() for c in re.split(r"[,;]", claves) if c.strip()]

    return resultado


# ============================================================
# NAVEGACIÓN CON PLAYWRIGHT
# ============================================================

_JS_CONTAR_TARJETAS = """
() => [...document.querySelectorAll('div[dir="auto"]')]
      .filter(e => (e.textContent || '').trim() === 'Ver detalle').length
"""

_JS_HAY_TARJETAS = """
() => [...document.querySelectorAll('div[dir="auto"]')]
      .some(e => (e.textContent || '').trim() === 'Ver detalle')
"""

_JS_TEXTO_TOTAL = "() => document.body.innerText"

_JS_SCROLL = """
() => {
  const el = document.getElementById('scrollableDivTenders');
  if (el) { el.scrollTop = el.scrollHeight; } else { window.scrollTo(0, document.body.scrollHeight); }
}
"""

# Marca con data-gt-target el botón 'Ver detalle' de la tarjeta cuyo título coincide.
_JS_MARCAR_TARJETA = """
(titulo) => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const botones = [...document.querySelectorAll('div[dir="auto"]')]
      .filter(e => norm(e.textContent) === 'Ver detalle');
  document.querySelectorAll('[data-gt-target]').forEach(e => e.removeAttribute('data-gt-target'));
  for (const b of botones) {
    let nodo = b;
    while (nodo.parentElement && nodo.parentElement !== document.body) {
      const p = nodo.parentElement;
      const n = [...p.querySelectorAll('div[dir="auto"]')]
          .filter(e => norm(e.textContent) === 'Ver detalle').length;
      if (n > 1) break;
      nodo = p;
    }
    const spans = [...nodo.querySelectorAll('span > span')].map(s => norm(s.textContent)).filter(Boolean);
    if (spans.length >= 2 && spans[1] === titulo) {
      b.setAttribute('data-gt-target', '1');
      b.scrollIntoView({block: 'center'});
      return true;
    }
  }
  return false;
}
"""

_JS_FICHA_LISTA = """
() => {
  const h1 = document.querySelector('h1');
  if (!h1) return false;
  const t = (h1.textContent || '').trim();
  return t !== '' && t !== 'Licitaciones' && /Descripci[oó]n/.test(document.body.innerText);
}
"""


def _bloquear_recursos_prescindibles(ruta):
    solicitud = ruta.request
    if solicitud.resource_type in ("image", "font", "media") or any(h in solicitud.url for h in HOSTS_PRESCINDIBLES):
        ruta.abort()
    else:
        ruta.continue_()


def _contar_tarjetas(pagina) -> int:
    return pagina.evaluate(_JS_CONTAR_TARJETAS)


def _total_declarado(pagina):
    try:
        coincidencia = PATRON_TOTAL.search(pagina.evaluate(_JS_TEXTO_TOTAL) or "")
        if coincidencia:
            return int(re.sub(r"[^\d]", "", coincidencia.group(1)))
    except Exception:
        pass
    return None


def _cargar_listado(pagina):
    pagina.goto(LISTADO_URL, timeout=TIEMPO_ESPERA_CARGA_MS, wait_until="domcontentloaded")
    pagina.wait_for_function(_JS_HAY_TARJETAS, timeout=TIEMPO_ESPERA_CARGA_MS)


def _scroll_hasta_el_final(pagina) -> int:
    """Hace scroll infinito hasta cargar todos los avisos (o hasta que deje de crecer)."""
    total = _total_declarado(pagina)
    previas = -1
    sin_crecer = 0
    for _ in range(MAX_SCROLLS):
        actuales = _contar_tarjetas(pagina)
        if total and actuales >= total:
            break
        if actuales == previas:
            sin_crecer += 1
            if sin_crecer >= MAX_SCROLLS_SIN_CRECER:
                break
        else:
            sin_crecer = 0
        previas = actuales
        pagina.evaluate(_JS_SCROLL)
        pagina.wait_for_timeout(PAUSA_SCROLL_MS)
    return _contar_tarjetas(pagina)


def _asegurar_tarjeta_marcada(pagina, titulo: str) -> bool:
    """Hace scroll hasta que la tarjeta con ese título está en el DOM y la marca para el clic."""
    previas = -1
    sin_crecer = 0
    for _ in range(MAX_SCROLLS):
        if pagina.evaluate(_JS_MARCAR_TARJETA, titulo):
            return True
        actuales = _contar_tarjetas(pagina)
        if actuales == previas:
            sin_crecer += 1
            if sin_crecer >= MAX_SCROLLS_SIN_CRECER:
                return False
        else:
            sin_crecer = 0
        previas = actuales
        pagina.evaluate(_JS_SCROLL)
        pagina.wait_for_timeout(PAUSA_SCROLL_MS)
    return False


def _volver_al_listado(pagina):
    try:
        pagina.go_back(timeout=TIEMPO_ESPERA_FICHA_MS, wait_until="domcontentloaded")
        pagina.wait_for_function(_JS_HAY_TARJETAS, timeout=TIEMPO_ESPERA_FICHA_MS)
    except Exception:
        _cargar_listado(pagina)


def extraer_ficha_por_clic(pagina, titulo: str, guardar_captura: bool = False):
    """
    Localiza la tarjeta por su título, hace clic en 'Ver detalle', lee la ficha
    y vuelve al listado. Devuelve (url_ficha, datos_ficha) o (None, None) si falla.
    """
    if not _asegurar_tarjeta_marcada(pagina, titulo):
        print("      No se encontró la tarjeta en el listado (¿ha cambiado el aviso?).", flush=True)
        return None, None

    try:
        pagina.locator('[data-gt-target="1"]').first.click(timeout=10000)
        pagina.wait_for_url(PATRON_URL_FICHA, timeout=TIEMPO_ESPERA_FICHA_MS)
        try:
            pagina.wait_for_function(_JS_FICHA_LISTA, timeout=TIEMPO_ESPERA_FICHA_MS)
        except Exception:
            print("      Aviso: la ficha tardó en completarse; se parsea lo que haya.", flush=True)
        url = pagina.url.split("#")[0].split("?")[0]
        html = pagina.content()
        if guardar_captura:
            try:
                with open(CAPTURA_FICHA, "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass
        datos = parsear_ficha(html)
    except Exception as error:
        print(f"      Error abriendo la ficha: {error}", flush=True)
        try:
            _cargar_listado(pagina)
        except Exception:
            pass
        return None, None

    _volver_al_listado(pagina)
    return url, datos


# ============================================================
# LECTURA DE LO YA EXISTENTE EN SUPABASE (por fuente)
# ============================================================

def obtener_existentes_de_la_fuente(supabase) -> dict:
    """{codigo_unico: fila} de todo lo ya guardado para esta fuente (paginado)."""
    columnas = ("id", "codigo_unico", "fecha_publicacion", "url_oficial") + CAMPOS_COMPARABLES
    filas = {}
    desde = 0
    paso = 1000
    while True:
        respuesta = (
            supabase.table(TABLA)
            .select(", ".join(columnas))
            .eq("fuente_origen", FUENTE)
            .range(desde, desde + paso - 1)
            .execute()
        )
        for fila in respuesta.data:
            filas[fila["codigo_unico"]] = fila
        if len(respuesta.data) < paso:
            break
        desde += paso
    return filas


def _indexar_por_clave_listado(existentes: dict) -> dict:
    indice = {}
    for fila in existentes.values():
        if fila.get("titulo"):
            indice[_clave_listado(fila.get("pais") or "", fila["titulo"])] = fila
    return indice


def necesita_ficha(tarjeta: dict, existente) -> bool:
    """La ficha solo se abre si la licitación es nueva, ha cambiado en el listado o está incompleta."""
    if existente is None:
        return True
    limite = tarjeta["fecha_limite"].isoformat() if tarjeta.get("fecha_limite") else None
    if str(existente.get("fecha_limite")) != str(limite):
        return True
    if not existente.get("categoria") or not existente.get("descripcion"):
        return True
    return False


# ============================================================
# CONSTRUIR REGISTRO
# ============================================================

def construir_registro(tarjeta: dict, url: str, ficha: dict, hoy: date) -> dict:
    texto_descripcion = ficha.get("descripcion")
    descripcion = componer_descripcion(ficha.get("palabras_clave") or [], texto_descripcion)

    principales = ficha.get("categorias") or tarjeta.get("categorias") or []
    subcategorias = ficha.get("subcategorias") or []
    categoria = _unir_sin_repetir(principales)
    if categoria and subcategorias:
        categoria = f"{categoria} ({_unir_sin_repetir(subcategorias)})"

    pais = ficha.get("pais") or tarjeta.get("pais")
    fecha_limite = ficha.get("fecha_limite") or tarjeta.get("fecha_limite")

    return {
        "codigo_unico": codigo_unico_desde_url(url),
        "fuente_origen": FUENTE,
        "tipo_aviso": ficha.get("tipo_aviso") or TIPO_AVISO_POR_DEFECTO,
        "titulo": tarjeta["titulo"] or ficha.get("titulo"),
        "descripcion": descripcion,
        "pais": pais,
        "paises": [pais] if pais else [],
        "organismo": ficha.get("organismo") or ORGANISMO_POR_DEFECTO,
        "categoria": categoria,
        "url_oficial": url,
        "url_documento": None,  # el aviso original y el contacto están tras login
        # El portal no publica fecha de publicación: se usa la de primera detección
        # (se conserva la ya guardada en ejecuciones posteriores).
        "fecha_publicacion": hoy.isoformat(),
        "fecha_limite": fecha_limite.isoformat() if fecha_limite else None,
    }


# ============================================================
# DECIDIR QUE SUBIR
# ============================================================

def preparar_lote_para_subir(normalizados: list, registros_existentes: dict) -> list:
    a_subir = []

    for datos in normalizados:
        existente = registros_existentes.get(datos["codigo_unico"])

        texto_completo = (
            f"Titulo: {datos['titulo']}\n"
            f"{datos.get('descripcion') or ''}\n"
            f"Pais: {datos.get('pais') or 'No especificado'}\n"
            f"Categoria: {datos.get('categoria') or 'No especificada'}"
        )

        if existente is None:
            datos["texto_completo"] = texto_completo
            datos["embedding"] = generar_embedding(texto_completo)
            datos["es_novedad"] = True
            datos["es_actualizada"] = False
            a_subir.append(datos)
            continue

        ha_cambiado = any(
            str(existente.get(campo)) != str(datos.get(campo))
            for campo in CAMPOS_COMPARABLES
        )
        if not ha_cambiado:
            continue

        # Se conserva la fecha de primera detección ya guardada.
        if existente.get("fecha_publicacion"):
            datos["fecha_publicacion"] = existente["fecha_publicacion"]

        datos["texto_completo"] = texto_completo
        datos["embedding"] = generar_embedding(texto_completo)
        datos["es_novedad"] = False
        datos["es_actualizada"] = True
        a_subir.append(datos)

    return a_subir


# ============================================================
# EJECUCION PRINCIPAL
# ============================================================

def recorrer_portal(existentes: dict, hoy: date) -> list:
    """
    Una sola sesión de navegador: carga el listado completo y abre solo las fichas
    de las tarjetas nuevas o con cambios. Devuelve los registros normalizados.
    """
    indice_existentes = _indexar_por_clave_listado(existentes)
    normalizados = {}
    endpoints_json = []

    with sync_playwright() as p:
        navegador = None
        try:
            navegador = p.chromium.launch(headless=True)
            contexto = navegador.new_context(user_agent=CABECERAS_USER_AGENT, locale="es-ES")
            pagina = contexto.new_page()
            pagina.route("**/*", _bloquear_recursos_prescindibles)

            # Registro de las llamadas JSON de la app (para depurar o pasar a la API en el futuro).
            def _registrar_respuesta(respuesta):
                try:
                    tipo = respuesta.headers.get("content-type", "")
                    if "json" in tipo and respuesta.request.resource_type in ("xhr", "fetch"):
                        endpoints_json.append(f"{respuesta.status} {respuesta.request.method} {respuesta.url}")
                except Exception:
                    pass

            pagina.on("response", _registrar_respuesta)

            print(f"--> Cargando el listado: {LISTADO_URL}", flush=True)
            try:
                _cargar_listado(pagina)
            except Exception as error:
                print(f"    No aparecieron tarjetas a tiempo: {error}", flush=True)

            total_cargadas = _scroll_hasta_el_final(pagina)
            print(f"    Tarjetas cargadas tras el scroll: {total_cargadas} (declaradas: {_total_declarado(pagina)})", flush=True)

            html_listado = pagina.content()
            try:
                with open(CAPTURA_LISTADO, "w", encoding="utf-8") as f:
                    f.write(html_listado)
            except Exception:
                pass

            tarjetas = parsear_listado(html_listado)
            print(f"    Tarjetas reconocidas por el parser: {len(tarjetas)}", flush=True)
            if not tarjetas:
                print(f"    No se reconoció ninguna tarjeta. Revisa {CAPTURA_LISTADO}.", flush=True)
                return []

            vigentes = [t for t in tarjetas if not (t["fecha_limite"] and t["fecha_limite"] < hoy)]
            pendientes = [
                t for t in vigentes
                if necesita_ficha(t, indice_existentes.get(_clave_listado(t["pais"], t["titulo"])))
            ]
            print(
                f"    Vigentes: {len(vigentes)} | ya conocidas y sin cambios: {len(vigentes) - len(pendientes)} "
                f"| fichas a abrir: {len(pendientes)}",
                flush=True,
            )
            if len(pendientes) > MAX_FICHAS_POR_EJECUCION:
                print(f"    Se limita a {MAX_FICHAS_POR_EJECUCION} fichas; el resto queda para la próxima ejecución.", flush=True)
                pendientes = pendientes[:MAX_FICHAS_POR_EJECUCION]

            for indice, tarjeta in enumerate(pendientes, start=1):
                print(f"  [{indice}/{len(pendientes)}] {tarjeta['pais']} | {tarjeta['titulo'][:90]}", flush=True)
                url, ficha = extraer_ficha_por_clic(pagina, tarjeta["titulo"], guardar_captura=(indice == 1))
                if not url or ficha is None:
                    continue
                registro = construir_registro(tarjeta, url, ficha, hoy)
                normalizados[registro["codigo_unico"]] = registro
                pagina.wait_for_timeout(PAUSA_ENTRE_FICHAS_MS)

            sin_categoria = sum(1 for r in normalizados.values() if not r.get("categoria"))
            sin_descripcion = sum(1 for r in normalizados.values() if not r.get("descripcion"))
            print(
                f"    Fichas leídas: {len(normalizados)} | sin categoría: {sin_categoria} | sin descripción: {sin_descripcion}",
                flush=True,
            )
            if normalizados and (sin_categoria == len(normalizados) or sin_descripcion == len(normalizados)):
                print(f"    ATENCIÓN: ninguna ficha devolvió categoría o descripción; revisa {CAPTURA_FICHA}.", flush=True)

        except Exception as error:
            print(f"Error durante la navegación con Playwright: {error}", flush=True)
        finally:
            try:
                with open(CAPTURA_API, "w", encoding="utf-8") as f:
                    f.write("\n".join(dict.fromkeys(endpoints_json)) or "(sin llamadas JSON registradas)")
            except Exception:
                pass
            if navegador is not None:
                try:
                    navegador.close()
                except Exception:
                    pass

    return list(normalizados.values())


def ejecutar_sincronizacion():
    hoy = date.today()

    print("=" * 100, flush=True)
    print("SINCRONIZACION DE LICITACIONES INTERNACIONALES - BID FOR THE AMERICAS APP", flush=True)
    print("=" * 100, flush=True)
    print(f"Fuente: {LISTADO_URL}", flush=True)
    print(f"Solo licitaciones vigentes con cierre >= {hoy}", flush=True)

    supabase = obtener_cliente_supabase()

    print("\nLeyendo lo ya existente en Supabase para esta fuente...", flush=True)
    existentes = obtener_existentes_de_la_fuente(supabase)
    print(f"Ya existentes en Supabase: {len(existentes)}", flush=True)

    normalizados = recorrer_portal(existentes, hoy)
    normalizados = [n for n in normalizados if n.get("titulo") and n.get("url_oficial")]

    if not normalizados:
        print("No hay licitaciones nuevas ni cambios que sincronizar.", flush=True)
        return

    lote_final = preparar_lote_para_subir(normalizados, existentes)

    if not lote_final:
        print("No hay licitaciones nuevas ni cambios que sincronizar.", flush=True)
        return

    nuevas = sum(1 for d in lote_final if d["es_novedad"])
    print(f"\nA subir: {len(lote_final)} ({nuevas} nuevas, {len(lote_final) - nuevas} actualizadas)", flush=True)

    subidas = subir_en_lotes(
        supabase,
        TABLA,
        "codigo_unico",
        lote_final,
        tamano_lote=LOTE_ENVIO_SUPABASE,
    )

    print(f"\nSincronización BID for the Americas APP completada: {subidas}/{len(lote_final)} registros subidos.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

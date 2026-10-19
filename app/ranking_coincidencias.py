"""
ranking_coincidencias.py
------------------------
Orden de las empresas en la pestaña «Coincidencia Inteligente».

POR QUÉ EXISTE
--------------
La RPC de Supabase (`buscar_empresas_para_licitacion` /
`buscar_empresas_por_embedding`) devuelve las empresas ordenadas por una ÚNICA
cifra abstracta, la similitud semántica global (`similarity`), que puntúa alto
incluso a empresas sin ninguna palabra en común con la licitación. Este módulo
la sustituye por una nota de 0 a 100 construida SOLO con las palabras clave de
la empresa que de verdad coinciden con la licitación, y reordena la lista con
ella. No cambia ni añade nada a los datos de las empresas ni de la licitación
(lo que reciben la IA y las explicaciones es exactamente lo mismo) y no toca
`similarity`. Es Python puro: los cosenos entre palabras clave y términos de la
licitación los calcula matching.py con el modelo de la app y se pasan ya hechos.

PUNTUACIÓN (0 a 100 puntos)
---------------------------
  60 puntos  COINCIDENCIAS EXACTAS: palabras clave de la empresa que aparecen en
             el texto de la licitación (definición más abajo). Cada una vale 1
             (1,5 si está en el título; 0,25 si es genérica). Con 3 puntos de
             valor ya se alcanzan los 60:  puntos = 60 × min(1, suma / 3).
             Sin coincidencias exactas esta parte suma 0.
  40 puntos  COINCIDENCIAS SIMILARES: palabra clave de la empresa (en el idioma de
             la licitación) cuyo significado es claramente el de un término de
             la licitación («Data ≈ Información»), sin ser la misma palabra.
             Cada pareja vale 1 y con 3 se alcanzan los 40:
             puntos = 40 × min(1, nº / 3). Sin parejas claras suma 0.
  Regla de corte: sin coincidencias exactas ni similares la nota es 0. La
  similitud global de la RPC (`similarity`) YA NO puntúa; solo desempata.
Orden: mayor nota > más coincidencias exactas relevantes > su peso (rareza) >
`similarity` de la RPC > orden original (ordenación estable).

PAREJAS SIMILARES
-----------------
matching.py compara, con el mismo modelo de embeddings de la app, cada palabra
clave con los términos (palabras y pares de palabras) de la licitación y pasa
aquí, por palabra clave, su mejor término y su similitud (`afines`). Como los
cosenos de este modelo van muy juntos, solo se acepta una pareja si supera
`UMBRAL_SEMANTICO` Y destaca (media + `Z_SEMANTICO` desviaciones) frente al resto de
parejas de la búsqueda (`umbral_semantico`). Si la licitación ya contiene la
palabra tal cual, esa palabra cuenta como exacta, no como similar.

Módulo SIN dependencias de Streamlit ni de Supabase: funciones puras. Usa
`snowballstemmer` (raíces en es/en/fr/pt); si no estuviera instalado, recurre
a un recorte simple de plurales para que la pestaña no se rompa.
"""
import math
import re
import unicodedata

try:
    import snowballstemmer
except ImportError:  # sin la librería se sigue funcionando, con raíces más toscas
    snowballstemmer = None

IDIOMAS = ("es", "en", "fr", "pt")
CAMPOS_PALABRAS_CLAVE = {
    "es": "palabras_clave",
    "en": "palabras_clave_en",
    "fr": "palabras_clave_fr",
    "pt": "palabras_clave_pt",
}
_NOMBRE_SNOWBALL = {"es": "spanish", "en": "english", "fr": "french", "pt": "portuguese"}

HOLGURA_FRASE = 2            # palabras intercaladas permitidas dentro de una frase clave
BONUS_TITULO = 1.5
PESO_GENERICO = 0.25
UMBRAL_RELEVANTE = 0.5       # peso mínimo para contar en el componente 1 (los genéricos quedan por debajo)

# Palabras tan habituales en licitaciones que coincidir en ellas no demuestra afinidad (se comparan por raíz).
TERMINOS_GENERICOS = (
    "servicio", "servicios", "proyecto", "proyectos", "gestion", "desarrollo", "consultoria", "sistema",
    "sistemas", "apoyo", "asistencia", "tecnica", "tecnico", "estudio", "estudios", "contrato", "programa",
    "service", "services", "project", "projects", "management", "development", "consulting", "consultancy",
    "system", "systems", "support", "assistance", "technical", "study", "studies", "contract", "programme",
    "gestao", "servico", "servicos", "projeto", "projetos", "projet", "projets", "gestion", "etude", "etudes",
)

# Artículos, preposiciones y conjunciones de los 4 idiomas (no forman parte de una palabra clave).
_VACIAS = frozenset("""
a al algo ante bajo con contra de del desde durante e el ella en entre es esta este esto la las le les lo los mas
me mi mis no nos o para pero por que se si sin sobre su sus te tu un una unas uno unos y ya
about after all also an and any are as at be been but by for from has have in into is it its of on or our out
over per than that the their them then there these they this those to under up via was we were what when where
which while who will with within without you your
au aux avec ce ces dans des du elle en et eux il ils je leur leurs lui ma mais mes moi mon ne nos notre nous on ou
par pas pour qu que qui sa ses son sous sur ta tes ton tu une vers vos votre vous
ao aos as com da das dela dele depois do dos e ela ele em entre essa esse isso isto mais mas na nas nem no nos num
numa os ou pela pelas pelo pelos por qual quando quem se sem seu seus sua suas tambem um uma voce
""".split())


# ============================================================
# TEXTO Y RAÍCES
# ============================================================

def normalizar(texto) -> str:
    """Minúsculas y sin acentos."""
    texto = unicodedata.normalize("NFD", str(texto or "").casefold())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def _palabras(texto) -> list:
    return re.findall(r"[a-z0-9]+", normalizar(texto))


_STEMMERS = {}


def _stemmer(idioma: str):
    if idioma not in _STEMMERS:
        _STEMMERS[idioma] = snowballstemmer.stemmer(_NOMBRE_SNOWBALL[idioma]) if snowballstemmer else None
    return _STEMMERS[idioma]


def _raiz_basica(palabra: str) -> str:
    if len(palabra) > 5 and palabra.endswith("es"):
        return palabra[:-2]
    if len(palabra) > 3 and palabra.endswith("s") and not palabra.endswith("ss"):
        return palabra[:-1]
    return palabra


def raiz(palabra: str, idioma: str) -> str:
    """Raíz de una palabra YA normalizada (sin acentos), en el idioma indicado."""
    stemmer = _stemmer(idioma)
    if stemmer is None:
        return _raiz_basica(palabra)
    return stemmer.stemWord(palabra)


def _secuencia_de_raices(texto, idioma: str) -> list:
    """Raíces de las palabras del texto sin palabras vacías, en orden."""
    return [raiz(p, idioma) for p in _palabras(texto) if p not in _VACIAS]


_RAICES_GENERICAS = {i: {raiz(normalizar(t), i) for t in TERMINOS_GENERICOS} for i in IDIOMAS}


# ============================================================
# LA LICITACIÓN
# ============================================================

class LicitacionAnalizada:
    """El texto de la licitación convertido a raíces, una vez por cada idioma."""

    def __init__(self, texto: str, titulo: str = ""):
        self.secuencia = {i: _secuencia_de_raices(texto, i) for i in IDIOMAS}
        self.titulo = {i: set(_secuencia_de_raices(titulo, i)) for i in IDIOMAS}


def _contiene_frase(secuencia: list, frase: tuple) -> bool:
    """¿Aparecen las raíces de `frase`, en orden, con a lo sumo HOLGURA_FRASE palabras de por medio?"""
    n = len(frase)
    if n == 0:
        return False
    if n == 1:
        return frase[0] in secuencia
    limite = n + HOLGURA_FRASE
    for inicio, raiz_inicial in enumerate(secuencia):
        if raiz_inicial != frase[0]:
            continue
        siguiente = 1
        for posicion in range(inicio + 1, min(inicio + limite, len(secuencia))):
            if secuencia[posicion] == frase[siguiente]:
                siguiente += 1
                if siguiente == n:
                    return True
    return False


# ============================================================
# LAS EMPRESAS
# ============================================================

def _lista(valor) -> list:
    if isinstance(valor, str):
        return [valor]
    return [str(v) for v in (valor or []) if v]


def palabras_clave_de(empresa: dict) -> dict:
    """{idioma: [palabras clave]} de una empresa (los campos multilingües que ya tiene la tabla `empresas`)."""
    return {i: _lista(empresa.get(campo)) for i, campo in CAMPOS_PALABRAS_CLAVE.items()}


def _palabras_de(empresa: dict, palabras_extra: dict = None) -> dict:
    """Palabras clave de la empresa por idioma (de su propio dict o, si no las trae, de `palabras_extra`)."""
    palabras = palabras_clave_de(empresa)
    if not any(palabras.values()):
        palabras = (palabras_extra or {}).get(empresa.get("numero_interno"), palabras)
    return palabras


def _conceptos_de(palabras_por_idioma: dict) -> dict:
    """
    {clave del concepto: (raíces, idioma, texto original)} de TODAS las palabras clave de la empresa. La clave es la
    tupla de raíces: el mismo concepto en varios idiomas escrito igual da la misma clave y cuenta una vez.
    """
    conceptos = {}
    for idioma, lista in palabras_por_idioma.items():
        for palabra in lista:
            frase = tuple(_secuencia_de_raices(palabra, idioma))
            if frase and frase not in conceptos:
                conceptos[frase] = (frase, idioma, palabra.strip())
    return conceptos


def _peso_base(frase: tuple, idioma: str) -> float:
    """Peso de una frase por lo específica que es (sin contar la rareza entre empresas)."""
    extra_palabras = 1 + 0.5 * (len(frase) - 1)
    generica = all(r in _RAICES_GENERICAS[idioma] or len(r) < 3 for r in frase)
    return extra_palabras * (PESO_GENERICO if generica else 1.0), generica


IDIOMA_POR_DEFECTO = "es"
PUNTOS_EXACTAS = 60.0
PUNTOS_SIMILARES = 40.0
COINCIDENCIAS_PARA_MAXIMO = 3.0     # valor (exactas) o nº de parejas (similares) con el que se obtienen todos los puntos
VALOR_GENERICA = 0.25
UMBRAL_SEMANTICO = 0.85             # coseno mínimo para considerar «similares» una palabra clave y un término
Z_SEMANTICO = 1.5                   # y además debe destacar frente al resto de parejas de la búsqueda
MINIMO_PARA_ESTADISTICA = 8
MAXIMO_TERMINOS = 60

# Palabras funcionales que SOLO existen en un idioma (para adivinar el idioma de la licitación).
_MARCAS_IDIOMA = {
    "es": frozenset("el los las que con una del se su sus como entre sobre mas pero este esta".split()),
    "en": frozenset("the and of to for with is are on this that by from be will which".split()),
    "fr": frozenset("le les des du et pour dans une est sont avec sur par au aux qui".split()),
    "pt": frozenset("os uma do dos das nao em pelo pela sao ao seu seus sua suas".split()),
}


def idioma_de(texto: str) -> str:
    """Idioma (es/en/fr/pt) del texto de la licitación, por sus palabras funcionales. Español si no hay indicios."""
    palabras = _palabras(texto)
    cuentas = {i: sum(1 for p in palabras if p in marcas) for i, marcas in _MARCAS_IDIOMA.items()}
    mejor = max(IDIOMAS, key=lambda i: cuentas[i])
    return mejor if cuentas[mejor] > 0 else IDIOMA_POR_DEFECTO


def palabras_a_evaluar(empresa: dict, idioma: str, palabras_extra: dict = None) -> list:
    """Palabras clave de la empresa en el idioma de la licitación (si no tiene en ese idioma, las de los demás)."""
    por_idioma = _palabras_de(empresa, palabras_extra)
    elegidas = [p for p in por_idioma.get(idioma, []) if p.strip()]
    if elegidas:
        return elegidas
    return [p for i in IDIOMAS for p in por_idioma.get(i, []) if p.strip()]


def textos_de_palabras_clave(empresas: list, idioma: str, palabras_extra: dict = None) -> list:
    """Textos distintos de las palabras clave que se comparan semánticamente (ver `palabras_a_evaluar`)."""
    vistos = {}
    for empresa in empresas:
        for palabra in palabras_a_evaluar(empresa, idioma, palabras_extra):
            limpia = " ".join(palabra.split())
            if limpia:
                vistos.setdefault(normalizar(limpia), limpia)
    return list(vistos.values())


def terminos_de_licitacion(texto: str, titulo: str = "", maximo: int = MAXIMO_TERMINOS) -> list:
    """
    Términos de la licitación con los que comparar el significado de las palabras clave: palabras sueltas y pares de
    palabras seguidas, sin artículos ni palabras genéricas. Primero los del título, luego los más repetidos.
    """
    idioma = idioma_de(texto)
    genericas = _RAICES_GENERICAS[idioma]

    def utiles(cadena):
        fichas = re.findall(r"[^\W\d_]+", str(cadena or ""))
        return [(f, normalizar(f)) for f in fichas]

    cuentas, en_titulo, original = {}, set(), {}

    def anotar(cadena, es_titulo):
        fichas = utiles(cadena)
        for i, (f, n) in enumerate(fichas):
            validas = lambda x: x not in _VACIAS and len(x) >= 3 and raiz(x, idioma) not in genericas
            if validas(n) and len(n) >= 4:
                cuentas[n] = cuentas.get(n, 0) + 1
                original.setdefault(n, f)
                if es_titulo:
                    en_titulo.add(n)
            if i + 1 < len(fichas):
                g, m = fichas[i + 1]
                if validas(n) and validas(m):
                    clave = f"{n} {m}"
                    cuentas[clave] = cuentas.get(clave, 0) + 1
                    original.setdefault(clave, f"{f} {g}")
                    if es_titulo:
                        en_titulo.add(clave)

    anotar(titulo, True)
    anotar(texto, False)
    orden = sorted(cuentas, key=lambda k: (k not in en_titulo, -cuentas[k], k))
    return [original[k] for k in orden[:maximo]]


def umbral_semantico(similitudes: list) -> float:
    """Coseno mínimo para aceptar una pareja: el umbral fijo y, con suficientes datos, además destacar sobre el resto."""
    if len(similitudes) < MINIMO_PARA_ESTADISTICA:
        return UMBRAL_SEMANTICO
    media = sum(similitudes) / len(similitudes)
    desviacion = math.sqrt(sum((v - media) ** 2 for v in similitudes) / len(similitudes))
    return max(UMBRAL_SEMANTICO, media + Z_SEMANTICO * desviacion)


def _palabra_en_licitacion(texto: str, frase: tuple, idioma: str) -> str:
    """El fragmento de la licitación (tal como está escrito) que corresponde a una palabra clave."""
    fichas = re.findall(r"[^\W_]+", str(texto or ""))
    raices = [raiz(normalizar(f), idioma) for f in fichas]
    if not frase:
        return ""
    for inicio, r in enumerate(raices):
        if r != frase[0]:
            continue
        siguiente = 1
        fin = inicio
        for posicion in range(inicio + 1, min(inicio + len(frase) + HOLGURA_FRASE, len(raices))):
            if siguiente == len(frase):
                break
            if raices[posicion] == frase[siguiente]:
                siguiente += 1
                fin = posicion
        if siguiente == len(frase):
            return " ".join(fichas[inicio:fin + 1])
    return ""


def ranking_empresas(empresas: list, texto: str, titulo: str = "", palabras_extra: dict = None,
                     afines: dict = None) -> list:
    """
    Para cada empresa (en el mismo orden de entrada): su nota de 0 a 100 y las coincidencias que la justifican.
    Devuelve dicts {puntuacion, puntos_exactas, puntos_similares, exactas, similares, n_exactas, peso_exacto,
    similarity, coincidencias, indice}.

    `palabras_extra`: {numero_interno: {idioma: [palabras]}} para empresas cuyo dict no trae las palabras clave
    (se usan SOLO aquí; nunca se añaden a los datos de la empresa).
    `afines`: {palabra clave normalizada: (término de la licitación, coseno)}. Sin ellas no hay parejas similares.
    """
    afines = afines or {}
    idioma = idioma_de(texto)
    corte = umbral_semantico([sim for _, sim in afines.values()])
    licitacion = LicitacionAnalizada(texto, titulo)
    conceptos_por_empresa = [_conceptos_de(_palabras_de(empresa, palabras_extra)) for empresa in empresas]

    # Cuántas empresas tienen cada concepto (rareza = más específico; solo desempata)
    n_empresas = max(len(empresas), 1)
    frecuencia = {}
    for conceptos in conceptos_por_empresa:
        for clave in conceptos:
            frecuencia[clave] = frecuencia.get(clave, 0) + 1

    resultado = []
    for indice, (empresa, conceptos) in enumerate(zip(empresas, conceptos_por_empresa)):
        coincidencias, exactas = [], []
        raices_exactas = set()
        for clave, (frase, idioma_palabra, original) in conceptos.items():
            if not _contiene_frase(licitacion.secuencia[idioma_palabra], frase):
                continue
            peso, generica = _peso_base(frase, idioma_palabra)
            peso *= 1 + math.log(n_empresas / frecuencia[clave])
            en_titulo = all(r in licitacion.titulo[idioma_palabra] for r in frase)
            if en_titulo:
                peso *= BONUS_TITULO
            coincidencias.append({
                "palabra": original, "idioma": idioma_palabra, "peso": round(peso, 4),
                "generica": generica, "en_titulo": en_titulo,
            })
            valor = (VALOR_GENERICA if generica else 1.0) * (BONUS_TITULO if en_titulo else 1.0)
            exactas.append({
                "palabra": " ".join(original.split()),
                "termino": _palabra_en_licitacion(texto, frase, idioma_palabra) or original,
                "idioma": idioma_palabra, "en_titulo": en_titulo, "generica": generica, "valor": valor,
            })
            raices_exactas.update(frase)
        coincidencias.sort(key=lambda c: -c["peso"])
        exactas.sort(key=lambda e: -e["valor"])
        relevantes = [c for c in coincidencias if c["peso"] >= UMBRAL_RELEVANTE and not c["generica"]]
        ya_exactas = {normalizar(" ".join(c["palabra"].split())) for c in coincidencias}

        # Parejas similares: palabra clave (idioma de la licitación) ≈ término de la licitación, una por término.
        similares, terminos_usados = [], set()
        candidatas = []
        for palabra in palabras_a_evaluar(empresa, idioma, palabras_extra):
            limpia = " ".join(palabra.split())
            clave = normalizar(limpia)
            if clave in ya_exactas or clave not in afines:
                continue
            termino, sim = afines[clave]
            if sim < corte or normalizar(termino) == clave:
                continue
            raices_termino = _secuencia_de_raices(termino, idioma)
            if raices_termino and all(r in raices_exactas for r in raices_termino):
                continue  # ese término de la licitación ya está cubierto por una coincidencia exacta
            if tuple(raices_termino) == tuple(_secuencia_de_raices(limpia, idioma)):
                continue
            candidatas.append((sim, limpia, termino))
        for sim, limpia, termino in sorted(candidatas, key=lambda c: -c[0]):
            if normalizar(termino) in terminos_usados or any(normalizar(limpia) == normalizar(x["palabra"]) for x in similares):
                continue
            terminos_usados.add(normalizar(termino))
            similares.append({"palabra": limpia, "termino": termino, "similitud": round(float(sim), 4)})

        puntos_exactas = PUNTOS_EXACTAS * min(1.0, sum(e["valor"] for e in exactas) / COINCIDENCIAS_PARA_MAXIMO)
        puntos_similares = PUNTOS_SIMILARES * min(1.0, len(similares) / COINCIDENCIAS_PARA_MAXIMO)
        resultado.append({
            "puntuacion": round(puntos_exactas + puntos_similares, 1),
            "puntos_exactas": round(puntos_exactas, 1),
            "puntos_similares": round(puntos_similares, 1),
            "exactas": exactas,
            "similares": similares,
            "idioma": idioma,
            "n_exactas": len(relevantes),
            "peso_exacto": round(sum(c["peso"] for c in coincidencias), 4),
            "similarity": float(empresa.get("similarity") or 0.0),
            "coincidencias": coincidencias,
            "indice": indice,
        })
    return resultado


def ordenar_con_detalle(empresas: list, texto: str, titulo: str = "", palabras_extra: dict = None,
                        afines: dict = None) -> tuple:
    """
    Devuelve (empresas ordenadas, detalle) donde `detalle[i]` es el dict de `ranking_empresas` de la empresa que ocupa
    la posición i. Las empresas son los mismos objetos de entrada, sin modificar.
    """
    if not empresas:
        return [], []
    claves = ranking_empresas(empresas, texto, titulo, palabras_extra, afines)
    orden = sorted(
        range(len(empresas)),
        key=lambda i: (-claves[i]["puntuacion"], -claves[i]["n_exactas"], -claves[i]["peso_exacto"],
                       -claves[i]["similarity"], i),
    )
    return [empresas[i] for i in orden], [claves[i] for i in orden]


def ordenar_por_coincidencia(empresas: list, texto: str, titulo: str = "", palabras_extra: dict = None,
                             afines: dict = None) -> list:
    """Las mismas empresas en el nuevo orden: mayor nota (0-100) primero (ver docstring del módulo)."""
    return ordenar_con_detalle(empresas, texto, titulo, palabras_extra, afines)[0]

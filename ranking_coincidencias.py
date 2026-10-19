"""
ranking_coincidencias.py
------------------------
Orden de las empresas en la pestaña «Coincidencia Inteligente».

POR QUÉ EXISTE
--------------
La RPC de Supabase (`buscar_empresas_para_licitacion` /
`buscar_empresas_por_embedding`) devuelve las empresas ya ordenadas por una
ÚNICA cifra, la similitud semántica (`similarity`, coseno entre embeddings).
Este módulo solo REORDENA esa lista en Python con una jerarquía en la que
las palabras clave de la empresa que aparecen de verdad en la licitación van
por delante de la similitud semántica. No cambia ni añade nada a los datos de
las empresas ni de la licitación (lo que reciben la IA y las explicaciones es
exactamente lo mismo), no toca `similarity` y no hace ninguna consulta ni
llamada al modelo: solo trabaja con textos que la pestaña ya tiene.

JERARQUÍA (ordenación lexicográfica, NO una suma ponderada)
-----------------------------------------------------------
Cada empresa recibe una clave de tres componentes que se comparan de uno en
uno, de izquierda a derecha, de modo que un componente solo se mira si los
anteriores empatan; así, ninguna similitud semántica, por alta que sea,
adelanta a una empresa con más coincidencias exactas:

  1. Nº de coincidencias exactas RELEVANTES (conceptos distintos).
  2. Peso total de esas coincidencias (más específicas / en el título = más).
  3. Similitud semántica de la RPC (`similarity`).
  Desempate final: el orden original de la RPC (ordenación estable).

COINCIDENCIA EXACTA (LÉXICA)
----------------------------
Una palabra clave de la empresa coincide con la licitación cuando todas sus
palabras (sin artículos ni preposiciones) aparecen en el texto de la
licitación, en el mismo orden y casi seguidas (como mucho dos palabras de por
medio), comparando RAÍCES y no cadenas: sin mayúsculas, sin acentos ni signos,
singular/plural y derivados habituales («residuo/residuos», «depuración/
depurar», «water/waters»). Nunca por subcadena: «agua» no coincide con
«paraguas». Se usan las raíces del idioma de cada campo (palabras_clave en
español, _en inglés, _fr francés, _pt portugués), comparadas con las raíces
del texto de la licitación en ese mismo idioma; por eso una empresa con
palabras clave en varios idiomas coincide con una licitación escrita en
cualquiera de ellos. No se inventan traducciones: solo cuentan las que la
empresa ya tiene guardadas. El mismo concepto escrito igual en varios idiomas
cuenta una sola vez.

PESO DE CADA COINCIDENCIA
-------------------------
  peso = especificidad × (1,5 si está en el título) × (frase: 1 + 0,5 por cada
         palabra extra)
  especificidad = 1 + ln(N / n): N = empresas que se están ordenando y
         n = cuántas tienen ese mismo concepto entre sus palabras clave. Un
         término que tiene casi todas («agua» en un sector de aguas)
         informa poco; uno que tiene una sola empresa, mucho.
  Las palabras de `TERMINOS_GENERICOS` (servicio, proyecto, gestión...) y las de
  menos de 3 letras pesan solo un 25 % y NO cuentan como coincidencia relevante
  (componente 1), aunque suman un poco al peso.

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


def ranking_empresas(empresas: list, texto: str, titulo: str = "", palabras_extra: dict = None) -> list:
    """
    Calcula, para cada empresa (en el mismo orden de entrada), su clave de ranking y las coincidencias que la
    justifican. Devuelve una lista de dicts: {n_exactas, peso_exacto, similarity, coincidencias, indice}.

    `palabras_extra`: {numero_interno: {idioma: [palabras]}} para empresas cuyo dict no trae las palabras clave
    (se usan SOLO aquí; nunca se añaden a los datos de la empresa).
    """
    licitacion = LicitacionAnalizada(texto, titulo)
    palabras_extra = palabras_extra or {}

    conceptos_por_empresa = []
    for empresa in empresas:
        palabras = palabras_clave_de(empresa)
        if not any(palabras.values()):
            palabras = palabras_extra.get(empresa.get("numero_interno"), palabras)
        conceptos_por_empresa.append(_conceptos_de(palabras))

    # Cuántas empresas tienen cada concepto (rareza = más específico)
    n_empresas = max(len(empresas), 1)
    frecuencia = {}
    for conceptos in conceptos_por_empresa:
        for clave in conceptos:
            frecuencia[clave] = frecuencia.get(clave, 0) + 1

    resultado = []
    for indice, (empresa, conceptos) in enumerate(zip(empresas, conceptos_por_empresa)):
        coincidencias = []
        for clave, (frase, idioma, original) in conceptos.items():
            if not _contiene_frase(licitacion.secuencia[idioma], frase):
                continue
            peso, generica = _peso_base(frase, idioma)
            peso *= 1 + math.log(n_empresas / frecuencia[clave])
            if all(r in licitacion.titulo[idioma] for r in frase):
                peso *= BONUS_TITULO
            coincidencias.append({"palabra": original, "idioma": idioma, "peso": round(peso, 4), "generica": generica})

        coincidencias.sort(key=lambda c: -c["peso"])
        relevantes = [c for c in coincidencias if c["peso"] >= UMBRAL_RELEVANTE and not c["generica"]]
        resultado.append({
            "n_exactas": len(relevantes),
            "peso_exacto": round(sum(c["peso"] for c in coincidencias), 4),
            "similarity": float(empresa.get("similarity") or 0.0),
            "coincidencias": coincidencias,
            "indice": indice,
        })
    return resultado


def ordenar_por_coincidencia(empresas: list, texto: str, titulo: str = "", palabras_extra: dict = None) -> list:
    """
    Las mismas empresas (los mismos objetos, sin modificar) en el nuevo orden:
    más coincidencias exactas relevantes > mayor peso de esas coincidencias > mayor similitud semántica >
    orden original de la RPC.
    """
    if len(empresas) < 2:
        return list(empresas)
    claves = ranking_empresas(empresas, texto, titulo, palabras_extra)
    orden = sorted(
        range(len(empresas)),
        key=lambda i: (-claves[i]["n_exactas"], -claves[i]["peso_exacto"], -claves[i]["similarity"], i),
    )
    return [empresas[i] for i in orden]

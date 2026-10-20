"""
ranking_relevancia.py
---------------------
NUEVO ranking de «Coincidencia Inteligente» (modo de prueba A/B): «Relevancia real».

Es INDEPENDIENTE del ranking actual (ranking_coincidencias.py): no usa su puntuación, su fórmula ni sus
coincidencias. Solo comparte con él utilidades de texto de bajo nivel (normalizar, raíces por idioma, lista de palabras
genéricas). Es Python puro (numpy para los cosenos): no llama a Supabase, a la IA ni al modelo; los embeddings los
calcula matching.py y llegan ya hechos en `vectores_conceptos` / `vectores_palabras`.

PUNTO DE PARTIDA: EL OBJETO DE LA LICITACIÓN, NO LAS PALABRAS DE LA EMPRESA
---------------------------------------------------------------------------
El ranking actual recorre las palabras clave de cada empresa y comprueba si aparecen. Este hace lo contrario:

 A. `analizar_objeto` descompone la licitación en CONCEPTOS (fragmentos del título, del objeto, del alcance...
    separados por preposiciones, signos y palabras genéricas). «Suministro de equipamiento de laboratorio para
    centro de acuicultura sostenible» → núcleo: «laboratorio», «centro», «acuicultura sostenible»; instrumentales
    (se ignoran): «suministro», «equipamiento». También indica si se contrata un servicio, un suministro o una obra.
 B. Cada concepto pesa según DÓNDE está (título 1,0; objeto 0,6; alcance 0,35; información complementaria 0,2), +0,15
    por cada zona extra donde se desarrolla, y un poco más si es una frase de varias palabras. No cuenta repeticiones.
 C. Para cada empresa se busca la MEJOR evidencia de cada concepto (nunca se suman varias evidencias del mismo
    concepto, así que nada puntúa dos veces):
        1,00        palabra clave idéntica al concepto (mismas raíces, en cualquier idioma escrita igual)
        √J          palabra clave que comparte raíces (J = solapamiento: «acuicultura» con «acuicultura sostenible»)
        0,50-0,85   palabra clave de significado afín (coseno del modelo por encima de un umbral que además debe
                    destacar frente al resto de parejas de la búsqueda); nunca llega a valer como la exacta
        0,60 / 0,55 / 0,50 / 0,40   el concepto aparece, literalmente, en sus proyectos tipo / títulos de su historial /
                    descripción de actividad / sector: información documentada, nada se presupone
 D. Especialidad: la cobertura se multiplica por 0,8-1,0 según qué parte de sus palabras clave se relaciona con el objeto
    (una empresa con 12 palabras clave y 1 coincidencia es menos especialista que una con 2 y 2). Y un concepto que
    cubren casi todas las empresas candidatas pesa menos (hasta -35 %), porque no discrimina.
 E. Contexto: la similitud global del perfil (dato de la RPC, relativo al resto de candidatas) ajusta ±10 %. Sin
    evidencia de ningún concepto la nota es 0 aunque esta similitud sea alta.

    cobertura  = min(1, Σ peso_concepto × valor_evidencia / Σ pesos de los 3 conceptos principales)
    nota (0-100) = 100 × cobertura × especialidad(0,8-1,0) × contexto(0,9-1,1), con máximo 100

F. Requisitos obligatorios (`extraer_requisitos` / `verificar_requisitos`): se evalúan APARTE de la afinidad y no
    cambian la nota. Con los datos del proyecto solo se pueden identificar certificaciones (ISO, UNE, ENAC, EMAS, ENS...)
    que la licitación exige con palabras como «deberá», «exigirá», «obligatorio»; cada una queda como cumplida (figura en
    las certificaciones de la empresa), incumplida (la empresa tiene certificaciones registradas y no figura) o no
    verificable (la empresa no tiene certificaciones registradas: lo desconocido NO se marca como incumplido).
    `REQUISITOS_INCUMPLIDOS_AL_FINAL` (False) permite hacer que las incumplidas pasen al final del orden.
G. `explicar` genera la explicación a partir de los mismos números que han producido la nota.

Orden: mayor nota > más conceptos del núcleo cubiertos > mayor similitud global > orden original de la RPC.
"""
import math
import re

import numpy as np

from ranking_coincidencias import (
    CAMPOS_PALABRAS_CLAVE,
    IDIOMAS,
    _RAICES_GENERICAS,
    _VACIAS,
    _secuencia_de_raices,
    idioma_de,
    normalizar,
    raiz,
)

PESO_ZONA = {"titulo": 1.0, "objeto": 0.6, "alcance": 0.35, "complementaria": 0.2}
ETIQUETA_ZONA = {"titulo": "título", "objeto": "objeto", "alcance": "alcance", "complementaria": "información complementaria"}
BONUS_ZONA_EXTRA = 0.15
MAX_PALABRAS_CONCEPTO = 4
MAX_CONCEPTOS = 24
CONCEPTOS_PRINCIPALES = 3          # los de mayor peso fijan el 100 % de cobertura
VALOR_PERFIL = {"proyectos tipo": 0.60, "historial de licitaciones": 0.55, "descripción de actividad": 0.50, "sector": 0.40}
UMBRAL_SEMANTICO = 0.85            # coseno mínimo concepto↔palabra clave
Z_SEMANTICO = 1.5                  # y además debe destacar frente al resto de parejas de la búsqueda
MINIMO_PARA_ESTADISTICA = 8
EVIDENCIA_MINIMA = 0.5             # valor desde el que una evidencia cuenta como «cubre el concepto»
REQUISITOS_INCUMPLIDOS_AL_FINAL = False

PREFIJOS_CERTIFICACION = ("iso", "une", "ohsas", "enac")
ACRONIMOS_CERTIFICACION = ("emas", "ens", "cmmi", "itil", "ecovadis", "prince2", "pmp")
_INDICIOS_OBLIGATORIO = (
    "debera", "deberan", "exigira", "exigiran", "exigid", "obligatori", "requisito", "imprescindible", "necesari",
    "acredit", "es preciso", "se requiere", "must", "shall", "required", "mandatory", "doit", "obligatoire", "exige",
    "exigido", "exigencia",
)
# Vocabulario administrativo y verbos de redacción de pliegos: no describen QUÉ se contrata (se ignoran como los genéricos).
_ADMINISTRATIVAS = frozenset("""
adjudicatario adjudicataria contratista licitador licitadores licitadora empresa empresas oferta ofertas plazo plazos
requiere requieren requerira incluye incluyen incluir dotar acreditar cumplir realizar ejecutar prestar garantizar
certificacion certificaciones iso une ohsas enac mediante objeto presente cuyo cuya asi dicho dicha debera deberan exigira exigiran sera seran podra tendra
contractor bidder bidders tender company companies offer include includes including require requires required provide
provides ensure ensures shall must will within
prestataire soumissionnaire entreprise offre inclut doit devra
prestador licitante empresa proposta inclui deve devera
""".split())
_CONECTORES = frozenset("de del of da do dos das du des".split())   # unen dos palabras de un mismo concepto
_ARTICULOS = frozenset("la las los el le les the a o as os".split())
_INDICIOS_TIPO = {
    "suministro": ("suministro", "suministros", "supply", "supplies", "fourniture", "fornecimento", "adquisicion"),
    "obra": ("obra", "obras", "construccion", "works", "construction", "travaux", "reforma"),
    "servicio": ("servicio", "servicios", "consultoria", "asistencia", "asesoramiento", "service", "services",
                 "consulting", "consultancy", "assistance", "prestation", "servico", "servicos"),
}


# ============================================================
# A-B. EL OBJETO DE LA LICITACIÓN
# ============================================================

def _texto(valor) -> str:
    return " ".join(str(valor or "").split())


def _zonas(licitacion: dict) -> list:
    """[(zona, texto)] de la licitación: título; objeto (categoría y 2 primeras frases); alcance; complementaria."""
    descripcion = _texto(licitacion.get("descripcion"))
    manual = _texto(licitacion.get("descripcion_manual"))
    principal = descripcion or manual            # una licitación escrita a mano solo tiene la descripción manual
    frases = re.split(r"(?<=[.;:!?])\s+", principal)
    return [
        ("titulo", _texto(licitacion.get("titulo"))),
        ("objeto", " ".join(filter(None, [_texto(licitacion.get("categoria")), " ".join(frases[:2])]))),
        ("alcance", " ".join(frases[2:])),
        ("complementaria", manual if descripcion else ""),
    ]


def _fragmentos(texto: str, idioma: str, ignorar: frozenset = frozenset()) -> tuple:
    """
    Convierte un texto en [(texto a mostrar, raíces)] de conceptos candidatos y [palabras ignoradas por genéricas].
    Un signo de puntuación, un número, una palabra funcional o una palabra genérica/administrativa CORTAN el
    fragmento; «de», «del», «of»... (con su artículo) lo UNEN: «perforación de pozos» es un solo concepto.
    """
    genericas = _RAICES_GENERICAS[idioma]
    conceptos, instrumentales = [], []
    for segmento in re.split(r"[.,;:()\[\]\n¿?¡!/–—\-|\"“”]+", texto):
        actual, pendientes = [], []        # actual: [(texto, raíz)] de palabras con contenido

        def cerrar():
            for i in range(0, len(actual), MAX_PALABRAS_CONCEPTO):
                trozo = actual[i:i + MAX_PALABRAS_CONCEPTO]
                conceptos.append((" ".join(o for o, _ in trozo), tuple(r for o, r in trozo if r)))
            actual.clear()
            pendientes.clear()

        for ficha in re.findall(r"[^\W_]+", segmento):
            n = normalizar(ficha)
            r = raiz(n, idioma)
            if (n in _CONECTORES or (pendientes and n in _ARTICULOS)) and actual:
                pendientes.append((ficha, ""))
            elif n in _VACIAS or len(n) < 3 or n.isdigit():
                cerrar()
            elif n in _ADMINISTRATIVAS or n in ignorar or r in genericas:
                cerrar()
                instrumentales.append(n)
            else:
                actual.extend(pendientes)          # los conectores solo se conservan entre dos palabras con contenido
                pendientes.clear()
                actual.append((ficha, r))
        cerrar()
    return [(" ".join(t for t in texto_c.split()), tuple(r for r in raices)) for texto_c, raices in conceptos], instrumentales


def analizar_objeto(licitacion: dict) -> dict:
    """
    Descompone la licitación en conceptos con su peso contextual.
    Devuelve {idioma, tipo_contrato, conceptos: [{texto, clave, raices, zonas, peso, nucleo}], instrumentales}.
    """
    zonas = _zonas(licitacion)
    texto_total = " ".join(t for _, t in zonas)
    ignorar = frozenset(normalizar(licitacion.get("pais") or "").split())   # dónde es la obra no es QUÉ se contrata
    idioma = idioma_de(texto_total)

    conceptos, instrumentales = [], []
    frecuencia = {}
    for zona, texto in zonas:
        fragmentos, genericas = _fragmentos(texto, idioma, ignorar)
        instrumentales += genericas
        for mostrar, raices in fragmentos:
            conjunto = frozenset(raices)
            existente = next((c for c in conceptos if c["conjunto"] == conjunto), None) or next(
                (c for c in conceptos if c["conjunto"] <= conjunto or conjunto <= c["conjunto"]), None
            )
            if existente:
                existente["zonas"].add(zona)
                frecuencia[existente["clave"]] = frecuencia.get(existente["clave"], 0) + 1
            else:
                concepto = {"texto": mostrar, "clave": normalizar(mostrar), "raices": raices, "conjunto": conjunto,
                            "zonas": {zona}}
                conceptos.append(concepto)
                frecuencia[concepto["clave"]] = 1

    for c in conceptos:
        mejor = max(PESO_ZONA[z] for z in c["zonas"])
        c["peso"] = round((mejor + BONUS_ZONA_EXTRA * (len(c["zonas"]) - 1)) * min(1.3, 1 + 0.15 * (len(c["raices"]) - 1)), 4)
        c["nucleo"] = bool(c["zonas"] & {"titulo", "objeto"})
    conceptos.sort(key=lambda c: (-c["peso"], -frecuencia[c["clave"]], c["clave"]))
    conceptos = conceptos[:MAX_CONCEPTOS]

    normalizado = normalizar(texto_total)
    puntos = {tipo: sum(len(re.findall(rf"\b{p}\b", normalizado)) for p in pistas) for tipo, pistas in _INDICIOS_TIPO.items()}
    tipo = max(puntos, key=puntos.get) if any(puntos.values()) else "no determinado"
    for c in conceptos:
        c.pop("conjunto", None)
    return {
        "idioma": idioma,
        "tipo_contrato": tipo,
        "conceptos": conceptos,
        "instrumentales": sorted(set(instrumentales)),
    }


# ============================================================
# EMPRESAS: PALABRAS CLAVE Y PERFIL DOCUMENTADO
# ============================================================

def palabras_unicas(empresa: dict, palabras_extra: dict = None) -> list:
    """[(palabra, idioma)] de todas las palabras clave de la empresa; la misma palabra escrita igual solo una vez."""
    por_idioma = {i: [str(p) for p in (empresa.get(campo) or []) if p] if not isinstance(empresa.get(campo), str)
                  else [empresa.get(campo)] for i, campo in CAMPOS_PALABRAS_CLAVE.items()}
    if not any(por_idioma.values()):
        por_idioma = (palabras_extra or {}).get(empresa.get("numero_interno"), por_idioma)
    vistas, resultado = set(), []
    for idioma in IDIOMAS:
        for palabra in por_idioma.get(idioma, []) or []:
            limpia = _texto(palabra)
            clave = normalizar(limpia)
            if clave and clave not in vistas:
                vistas.add(clave)
                resultado.append((limpia, idioma))
    return resultado


def _frases_de_perfil(empresa: dict, referencias: list) -> list:
    """[(fuente, texto)] de la información documentada de la empresa (nada inventado)."""
    frases = []
    for proyecto in empresa.get("proyectos_tipo") or []:
        if proyecto:
            frases.append(("proyectos tipo", _texto(proyecto)))
    for ref in (referencias or [])[:30]:
        if ref.get("titulo"):
            frases.append(("historial de licitaciones", _texto(ref["titulo"])))
    if empresa.get("descripcion_actividad"):
        frases.append(("descripción de actividad", _texto(empresa["descripcion_actividad"])))
    sector = ", ".join(filter(None, [_texto(empresa.get("sector")), _texto(empresa.get("subsector"))]))
    if sector:
        frases.append(("sector", sector))
    return frases


def textos_necesarios(objeto: dict, empresas: list, palabras_extra: dict = None) -> tuple:
    """(textos de conceptos, textos de palabras clave) cuyos embeddings hay que calcular (sin repetidos)."""
    conceptos = [c["texto"] for c in objeto["conceptos"]]
    vistos = {}
    for empresa in empresas:
        for palabra, _ in palabras_unicas(empresa, palabras_extra):
            vistos.setdefault(normalizar(palabra), palabra)
    return conceptos, list(vistos.values())


def _cosenos(vectores_conceptos: dict, vectores_palabras: dict, claves_conceptos: list, claves_palabras: list) -> dict:
    """{(clave concepto, clave palabra): coseno} entre los vectores disponibles."""
    cc = [c for c in claves_conceptos if c in vectores_conceptos]
    pp = [p for p in claves_palabras if p in vectores_palabras]
    if not cc or not pp:
        return {}
    a = np.array([vectores_conceptos[c] for c in cc], dtype=float)
    b = np.array([vectores_palabras[p] for p in pp], dtype=float)
    a /= np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-12)
    b /= np.maximum(np.linalg.norm(b, axis=1, keepdims=True), 1e-12)
    m = a @ b.T
    return {(c, p): float(m[i, j]) for i, c in enumerate(cc) for j, p in enumerate(pp)}


def _corte_semantico(cosenos: dict) -> float:
    valores = list(cosenos.values())
    if len(valores) < MINIMO_PARA_ESTADISTICA:
        return UMBRAL_SEMANTICO
    media = sum(valores) / len(valores)
    desviacion = math.sqrt(sum((v - media) ** 2 for v in valores) / len(valores))
    return max(UMBRAL_SEMANTICO, media + Z_SEMANTICO * desviacion)


# ============================================================
# F. REQUISITOS OBLIGATORIOS
# ============================================================

def extraer_requisitos(licitacion: dict) -> list:
    """
    Certificaciones que la licitación EXIGE (mencionadas junto a «deberá», «obligatorio», «requisito»...).
    Devuelve [{etiqueta, prefijo, numero}]. Las simplemente citadas, sin indicio de obligatoriedad, no se consideran.
    """
    texto = " ".join(_texto(licitacion.get(c)) for c in ("titulo", "descripcion", "descripcion_manual"))
    normal = normalizar(texto)
    encontrados, vistos = [], set()
    patron = rf"\b({'|'.join(PREFIJOS_CERTIFICACION)})[\s\-:/]*(\d{{3,6}})\b"
    candidatos = [(m.start(), m.end(), f"{m.group(1)} {m.group(2)}", m.group(1), m.group(2)) for m in re.finditer(patron, normal)]
    for acronimo in ACRONIMOS_CERTIFICACION:
        candidatos += [(m.start(), m.end(), acronimo, acronimo, None) for m in re.finditer(rf"\b{acronimo}\b", normal)]
    for inicio, fin, clave, prefijo, numero in candidatos:
        ventana = normal[max(0, inicio - 160): fin + 160]
        if clave in vistos or not any(i in ventana for i in _INDICIOS_OBLIGATORIO):
            continue
        vistos.add(clave)
        encontrados.append({"etiqueta": clave.upper(), "prefijo": prefijo, "numero": numero})
    return encontrados


def verificar_requisitos(requisitos: list, empresa: dict) -> list:
    """
    Cada requisito pasa a {etiqueta, estado, motivo} con estado «cumplido», «incumplido» o «no verificable».
    Solo se da por incumplido cuando la empresa tiene certificaciones registradas y la exigida no figura entre ellas.
    """
    registradas = normalizar(_texto(empresa.get("certificaciones")))
    resultado = []
    for r in requisitos:
        if not registradas:
            estado, motivo = "no verificable", "la empresa no tiene certificaciones registradas"
        elif r["numero"] is not None:
            figura = r["numero"] in set(re.findall(r"\b\d{3,6}\b", registradas))
            estado, motivo = ("cumplido", "figura en sus certificaciones") if figura else (
                "incumplido", "no figura entre las certificaciones registradas")
        else:
            figura = bool(re.search(rf"\b{r['prefijo']}\b", registradas))
            estado, motivo = ("cumplido", "figura en sus certificaciones") if figura else (
                "incumplido", "no figura entre las certificaciones registradas")
        resultado.append({"etiqueta": r["etiqueta"], "estado": estado, "motivo": motivo})
    return resultado


# ============================================================
# C-E. EVIDENCIAS Y NOTA
# ============================================================

def _roots_de(texto: str, idioma: str) -> frozenset:
    genericas = _RAICES_GENERICAS[idioma]
    return frozenset(r for r in _secuencia_de_raices(texto, idioma) if r not in genericas)


def _valor_lexico(K: frozenset, C: frozenset):
    """(valor, tipo) del solapamiento de raíces entre una palabra clave K y un concepto C, o None."""
    if not (K and C and K & C):
        return None
    if K == C:
        return 1.0, "exacta"
    if K < C:                          # la palabra clave es una parte del concepto («acuicultura» en «acuicultura sostenible»)
        return 0.65 + 0.35 * len(K) / len(C), "parcial"
    if C < K:                          # la palabra clave es más específica que el concepto
        return 0.9, "parcial"
    return 0.8 * math.sqrt(len(K & C) / len(K | C)), "parcial"


def _evidencias(objeto: dict, empresa: dict, palabras_extra, referencias, cosenos: dict, corte: float) -> dict:
    """
    {clave concepto: mejor evidencia} de una empresa. Cada concepto toma su mejor evidencia (nunca se suman varias) y
    cada PALABRA CLAVE cubre como mucho un concepto, el que más pesa: así una misma palabra no puntúa dos veces.
    Las frases del perfil documentado (descripción, historial...) sí pueden respaldar varios conceptos.
    """
    idioma = objeto["idioma"]
    palabras = palabras_unicas(empresa, palabras_extra)
    raices_palabra = {normalizar(p): _roots_de(p, idioma) for p, _ in palabras}
    perfil = [(fuente, texto, _roots_de(texto, idioma)) for fuente, texto in _frases_de_perfil(empresa, referencias)]

    propuestas = []                                   # (valor × peso, valor, concepto, tipo, fuente, extra)
    for concepto in objeto["conceptos"]:
        C = frozenset(concepto["raices"])
        for palabra, idioma_palabra in palabras:
            clave = normalizar(palabra)
            lexico = _valor_lexico(raices_palabra[clave], C)
            if lexico:
                propuestas.append((lexico[0] * concepto["peso"], lexico[0], concepto, lexico[1], palabra,
                                   {"idioma": idioma_palabra}))
            elif clave == concepto["clave"]:
                propuestas.append((concepto["peso"], 1.0, concepto, "exacta", palabra, {"idioma": idioma_palabra}))
            cos = cosenos.get((concepto["clave"], clave))
            if cos is not None and cos >= corte and not (raices_palabra[clave] & C):
                valor = 0.5 + 0.35 * min(1.0, (cos - corte) / max(1 - corte, 1e-9))
                propuestas.append((valor * concepto["peso"], valor, concepto, "similar", palabra,
                                   {"idioma": idioma_palabra, "coseno": round(cos, 4)}))
        for fuente, texto, R in perfil:
            if C and C <= R:
                propuestas.append((VALOR_PERFIL[fuente] * concepto["peso"], VALOR_PERFIL[fuente], concepto, "perfil",
                                   fuente, {"texto": texto}))

    evidencias, palabras_usadas = {}, set()
    for _, valor, concepto, tipo, fuente, extra in sorted(propuestas, key=lambda p: -p[0]):
        if concepto["clave"] in evidencias:
            continue
        if tipo != "perfil":
            if normalizar(fuente) in palabras_usadas:
                continue
            palabras_usadas.add(normalizar(fuente))
        evidencias[concepto["clave"]] = {
            "concepto": concepto["texto"], "clave": concepto["clave"], "zonas": sorted(concepto["zonas"]),
            "valor": round(valor, 4), "tipo": tipo, "fuente": fuente, **extra,
        }

    # Palabras clave relacionadas con algún concepto (para medir la especialidad), hayan puntuado o no
    relacionadas = set()
    for palabra, _ in palabras:
        clave = normalizar(palabra)
        for concepto in objeto["conceptos"]:
            cos = cosenos.get((concepto["clave"], clave))
            if (raices_palabra[clave] & frozenset(concepto["raices"])) or (cos is not None and cos >= corte):
                relacionadas.add(clave)
                break
    return {"por_concepto": evidencias, "relacionadas": relacionadas, "n_palabras": len(palabras)}


def evaluar_candidatas(objeto: dict, empresas: list, licitacion: dict, palabras_extra: dict = None,
                       referencias_por_empresa: dict = None, vectores_conceptos: dict = None,
                       vectores_palabras: dict = None) -> list:
    """
    Nota «Relevancia real» (0-100) de cada empresa, en el mismo orden de entrada, con todo lo que la justifica.
    No modifica las empresas ni la licitación.
    """
    referencias_por_empresa = referencias_por_empresa or {}
    claves_conceptos = [c["clave"] for c in objeto["conceptos"]]
    claves_palabras = sorted({normalizar(p) for e in empresas for p, _ in palabras_unicas(e, palabras_extra)})
    cosenos = _cosenos(vectores_conceptos or {}, vectores_palabras or {}, claves_conceptos, claves_palabras)
    corte = _corte_semantico(cosenos)
    semantica_disponible = bool(cosenos)

    todas = [_evidencias(objeto, e, palabras_extra, referencias_por_empresa.get(e.get("numero_interno"), []),
                         cosenos, corte) for e in empresas]

    # D. un concepto que cubren casi todas las candidatas discrimina menos
    n = max(len(empresas), 1)
    peso_efectivo = {}
    for c in objeto["conceptos"]:
        cubren = sum(1 for ev in todas if ev["por_concepto"].get(c["clave"], {}).get("valor", 0) >= EVIDENCIA_MINIMA)
        peso_efectivo[c["clave"]] = c["peso"] * (0.65 + 0.35 * (1 - cubren / n)) if n > 1 else c["peso"]
    principales = sorted(peso_efectivo.values(), reverse=True)[:CONCEPTOS_PRINCIPALES]
    referencia = sum(principales)

    sims = [float(e.get("similarity") or 0.0) for e in empresas]
    menor, mayor = min(sims, default=0.0), max(sims, default=0.0)
    requisitos = extraer_requisitos(licitacion)
    nucleo = {c["clave"] for c in objeto["conceptos"] if c["nucleo"]}

    resultado = []
    for indice, (empresa, ev) in enumerate(zip(empresas, todas)):
        por_concepto = ev["por_concepto"]
        aportes = sorted(
            ({**e, "peso": round(peso_efectivo[e["clave"]], 4), "aporte": round(peso_efectivo[e["clave"]] * e["valor"], 4)}
             for e in por_concepto.values()), key=lambda e: -e["aporte"])
        suma = sum(e["aporte"] for e in aportes)
        cobertura = min(1.0, suma / referencia) if referencia > 0 else 0.0
        n_palabras = ev["n_palabras"]
        foco = min(1.0, len(ev["relacionadas"]) / min(n_palabras, 3)) if n_palabras and ev["relacionadas"] else 0.0
        especialidad = 0.8 + 0.2 * foco
        contexto = 0.5 if mayor == menor else (sims[indice] - menor) / (mayor - menor)
        factor_contexto = 0.9 + 0.2 * contexto
        nota = min(100.0, 100.0 * cobertura * especialidad * factor_contexto) if suma > 0 else 0.0

        sin_cubrir = [c["texto"] for c in objeto["conceptos"] if c["nucleo"] and c["clave"] not in por_concepto]
        limitaciones = []
        if not n_palabras:
            limitaciones.append("la empresa no tiene palabras clave registradas")
        if not empresa.get("descripcion_actividad"):
            limitaciones.append("no tiene descripción de actividad")
        if not (referencias_por_empresa.get(empresa.get("numero_interno")) or empresa.get("proyectos_tipo")):
            limitaciones.append("no tiene historial ni proyectos tipo registrados")
        if not semantica_disponible:
            limitaciones.append("no se pudo comparar el significado de las palabras (solo coincidencias literales)")
        verificados = verificar_requisitos(requisitos, empresa)

        resultado.append({
            "puntuacion": round(nota, 1),
            "cobertura": round(cobertura, 4),
            "especialidad": round(especialidad, 4),
            "contexto": round(factor_contexto, 4),
            "evidencias": aportes,
            "sin_cubrir": sin_cubrir,
            "n_nucleo_cubiertos": sum(1 for k in por_concepto if k in nucleo),
            "palabras_relacionadas": len(ev["relacionadas"]),
            "n_palabras": n_palabras,
            "requisitos": verificados,
            "limitaciones": limitaciones,
            "similarity": sims[indice],
            "indice": indice,
        })
    return resultado


def ordenar_relevancia(objeto: dict, empresas: list, licitacion: dict, palabras_extra: dict = None,
                       referencias_por_empresa: dict = None, vectores_conceptos: dict = None,
                       vectores_palabras: dict = None) -> tuple:
    """(empresas ordenadas, detalle alineado). Las empresas son los mismos objetos de entrada."""
    if not empresas:
        return [], []
    detalle = evaluar_candidatas(objeto, empresas, licitacion, palabras_extra, referencias_por_empresa,
                                 vectores_conceptos, vectores_palabras)

    def clave(i):
        d = detalle[i]
        incumple = REQUISITOS_INCUMPLIDOS_AL_FINAL and any(r["estado"] == "incumplido" for r in d["requisitos"])
        return (incumple, -d["puntuacion"], -d["n_nucleo_cubiertos"], -d["similarity"], i)

    orden = sorted(range(len(empresas)), key=clave)
    return [empresas[i] for i in orden], [detalle[i] for i in orden]


# ============================================================
# G. EXPLICACIÓN
# ============================================================

def _num(valor: float, decimales: int = 1) -> str:
    return f"{valor:.{decimales}f}".replace(".", ",")


def explicar(detalle: dict) -> list:
    """Líneas (Markdown) que explican la nota con los mismos números con los que se calculó."""
    lineas = []
    ev = detalle["evidencias"]
    if ev:
        piezas = []
        for e in ev[:6]:
            zona = "/".join(ETIQUETA_ZONA[z] for z in e["zonas"])
            if e["tipo"] == "exacta":
                de = f"palabra clave «{e['fuente']}» (exacta)"
            elif e["tipo"] == "parcial":
                de = f"palabra clave «{e['fuente']}» (parcial)"
            elif e["tipo"] == "similar":
                de = f"palabra clave «{e['fuente']}» (afín, {_num(e['coseno'] * 100)} %)"
            else:
                de = f"su {e['fuente']}"
            piezas.append(f"«{e['concepto']}» ({zona}) ← {de}")
        lineas.append("**Cubre del objeto:** " + "; ".join(piezas) + (f"; y {len(ev) - 6} más" if len(ev) > 6 else ""))
    else:
        lineas.append("**Cubre del objeto:** ningún concepto de la licitación aparece en sus palabras clave ni en su perfil documentado.")
    if detalle["sin_cubrir"]:
        lineas.append("**Sin cubrir (núcleo):** " + "; ".join(f"«{c}»" for c in detalle["sin_cubrir"][:6]))
    if detalle["n_palabras"]:
        lineas.append(
            f"**Especialidad:** {detalle['palabras_relacionadas']} de {detalle['n_palabras']} palabras clave "
            "se relacionan con el objeto de la licitación."
        )
    if detalle["requisitos"]:
        iconos = {"cumplido": "✅", "incumplido": "❌", "no verificable": "❔"}
        lineas.append("**Requisitos obligatorios (aparte de la nota):** " + "; ".join(
            f"{iconos[r['estado']]} {r['etiqueta']} {r['estado']} ({r['motivo']})" for r in detalle["requisitos"]))
    if detalle["limitaciones"]:
        lineas.append("**Información no disponible:** " + "; ".join(detalle["limitaciones"]) + ".")
    if detalle["puntuacion"] > 0:
        lineas.append(
            f"**Razón del ranking:** cobertura del objeto {_num(detalle['cobertura'] * 100)} % × especialidad "
            f"{_num(detalle['especialidad'], 2)} × contexto {_num(detalle['contexto'], 2)} = {_num(detalle['puntuacion'])} / 100."
        )
    else:
        lineas.append("**Razón del ranking:** sin evidencia de ningún concepto de la licitación: 0 / 100.")
    return lineas

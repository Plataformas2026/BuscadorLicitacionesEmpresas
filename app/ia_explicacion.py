"""
ia_explicacion.py
------------------
Capa de justificación en lenguaje natural (tipo RAG) sobre la lógica de
coincidencias YA EXISTENTE en matching.py -- no la sustituye ni decide
por sí misma si una empresa encaja o no: solo redacta en prosa los
motivos concretos que `explicar_coincidencia()` ya ha detectado, con
toda la información disponible de la licitación y de la empresa como
contexto. El "retrieve" de este RAG es el propio análisis determinista
que ya teníamos (nunca datos inventados); el "generate" es esta capa.

Proveedor: Groq (https://console.groq.com) -- capa gratuita real, sin
tarjeta de crédito, API compatible con la de OpenAI
(`https://api.groq.com/openai/v1/chat/completions`). Se usa `requests`
directamente (ya es una dependencia del proyecto) en vez de añadir el
SDK oficial `groq`, para no sumar una dependencia nueva solo para esto.

Si no hay `GROQ_API_KEY` configurada, o la llamada falla por cualquier
motivo (red, límite de peticiones, error del proveedor...), estas
funciones devuelven `None` en vez de lanzar una excepción -- la Pestaña
2 sigue funcionando exactamente igual que antes (con su explicación
determinista) y solo deja de ofrecer el botón/párrafo de IA. Ver
render_tab2() en matching.py.
"""
import requests
import streamlit as st

from config import GROQ_API_KEY, GROQ_MODELO

URL_GROQ = "https://api.groq.com/openai/v1/chat/completions"
TIMEOUT_PETICION_SEGUNDOS = 20

INSTRUCCIONES_SISTEMA = (
    "Eres un asistente que ayuda a un equipo de desarrollo de negocio a "
    "decidir si presentarse a una licitación internacional. Se te da "
    "información verificada sobre una licitación y sobre el perfil de una "
    "empresa candidata, incluyendo un análisis de coincidencias ya "
    "realizado por otro sistema (una lista de motivos concretos). Tu "
    "única tarea es redactar, en español y en un solo párrafo de 3 a 5 "
    "frases, una justificación natural y bien argumentada de por qué esa "
    "empresa es (o no es) una buena candidata, basándote EXCLUSIVAMENTE "
    "en los datos que se te proporcionan. No inventes cifras, países, "
    "sectores, certificaciones ni ningún dato que no aparezca "
    "explícitamente abajo. Si la información disponible es escasa o "
    "débil, dilo con honestidad en vez de rellenar con generalidades. No "
    "repitas la lista de motivos tal cual: intégrala en una explicación "
    "fluida, como se la contarías a un compañero de equipo."
)


def _valor_o_nada(etiqueta: str, valor) -> str:
    if valor is None or valor == "" or valor == []:
        return ""
    if isinstance(valor, list):
        valor = ", ".join(str(v) for v in valor if v)
        if not valor:
            return ""
    return f"- {etiqueta}: {valor}\n"


def construir_prompt(licitacion: dict, empresa: dict, motivos_deterministas: list) -> str:
    """
    Reúne, en texto plano, TODA la información relevante ya disponible
    -- de la licitación (más que título y descripción cuando la
    licitación está en nuestra propia base de datos: país, organismo,
    tipo de aviso, fecha límite) y del perfil de la empresa (los campos
    pedidos explícitamente: sector, subsector, ámbito geográfico,
    descripción de actividad, palabras clave, proyectos tipo,
    experiencia países, zona geográfica de interés, tamaño, facturación
    e importe mínimo/máximo de proyectos -- este último vive en
    `notas_libres`, ver ingest/sync_empresas_drive.py) -- más los
    motivos concretos que `explicar_coincidencia()` ya ha encontrado,
    que es la evidencia que el modelo debe usar como base (el "retrieve"
    de este RAG).
    """
    bloque_licitacion = (
        _valor_o_nada("Título", licitacion.get("titulo"))
        + _valor_o_nada("Descripción", licitacion.get("descripcion") or licitacion.get("descripcion_manual"))
        + _valor_o_nada("País", licitacion.get("pais"))
        + _valor_o_nada("Organismo/fuente", licitacion.get("organismo") or licitacion.get("fuente_origen"))
        + _valor_o_nada("Tipo de aviso", licitacion.get("tipo_aviso"))
        + _valor_o_nada("Fecha límite", licitacion.get("fecha_limite"))
    )

    bloque_empresa = (
        _valor_o_nada("Nombre", empresa.get("nombre_empresa"))
        + _valor_o_nada("Sector", empresa.get("sector"))
        + _valor_o_nada("Subsector", empresa.get("subsector"))
        + _valor_o_nada("Ámbito geográfico de operación", empresa.get("ambito_geografico"))
        + _valor_o_nada("Descripción de la actividad", empresa.get("descripcion_actividad"))
        + _valor_o_nada("Palabras clave", empresa.get("palabras_clave"))
        + _valor_o_nada("Principales proyectos tipo", empresa.get("proyectos_tipo"))
        + _valor_o_nada("Experiencia en países", empresa.get("experiencia_paises"))
        + _valor_o_nada("Zona geográfica de interés", empresa.get("zona_geografica_interes"))
        + _valor_o_nada("Tamaño", empresa.get("tamano"))
        + _valor_o_nada("Facturación anual", empresa.get("facturacion_anual"))
        + _valor_o_nada("Importe mínimo/máximo de proyectos preferido", empresa.get("notas_libres"))
    )

    bloque_analisis = "\n".join(f"- {motivo}" for motivo in motivos_deterministas if motivo)

    return (
        f"LICITACIÓN:\n{bloque_licitacion}\n"
        f"EMPRESA CANDIDATA:\n{bloque_empresa}\n"
        f"ANÁLISIS DE COINCIDENCIAS YA REALIZADO (úsalo como base de tu explicación):\n{bloque_analisis}\n"
    )


def groq_configurado() -> bool:
    return bool(GROQ_API_KEY)


def _llamar_groq(prompt_usuario: str):
    """Devuelve el texto generado, o None si algo falla (ver docstring del módulo)."""
    if not groq_configurado():
        return None

    try:
        respuesta = requests.post(
            URL_GROQ,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODELO,
                "messages": [
                    {"role": "system", "content": INSTRUCCIONES_SISTEMA},
                    {"role": "user", "content": prompt_usuario},
                ],
                "temperature": 0.3,
                "max_tokens": 400,
            },
            timeout=TIMEOUT_PETICION_SEGUNDOS,
        )
        respuesta.raise_for_status()
        print("Status code:", respuesta.status_code)
        print("Response text:", respuesta.text)
        cuerpo = respuesta.json()
        return cuerpo["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def _clave_cache(licitacion: dict, empresa: dict) -> str:
    clave_licitacion = licitacion.get("codigo_unico") or licitacion.get("titulo") or ""
    return f"{clave_licitacion}::{empresa.get('numero_interno')}"


def justificacion_en_cache(licitacion: dict, empresa: dict):
    """True/valor si esta pareja licitación+empresa ya se generó en esta
    sesión (para poder mostrarla sin más al volver a renderizar la
    Pestaña 2, sin que el usuario tenga que pulsar el botón otra vez)."""
    cache = st.session_state.get("cache_justificacion_ia", {})
    clave = _clave_cache(licitacion, empresa)
    return clave in cache, cache.get(clave)


def generar_justificacion_ia(licitacion: dict, empresa: dict, motivos_deterministas: list):
    """
    Punto de entrada usado por matching.py. Cachea en sesión por la
    combinación licitación+empresa (nunca vuelve a llamar a la API para
    la misma pareja en la misma sesión del usuario) -- ver
    TIMEOUT_PETICION_SEGUNDOS y el aviso de límites gratuitos en el
    docstring del módulo: no se llama automáticamente para todas las
    coincidencias de golpe, solo bajo demanda desde un botón por
    empresa (render_tab2()).
    """
    if "cache_justificacion_ia" not in st.session_state:
        st.session_state.cache_justificacion_ia = {}

    clave = _clave_cache(licitacion, empresa)

    if clave in st.session_state.cache_justificacion_ia:
        return st.session_state.cache_justificacion_ia[clave]

    prompt = construir_prompt(licitacion, empresa, motivos_deterministas)
    resultado = _llamar_groq(prompt)
    st.session_state.cache_justificacion_ia[clave] = resultado
    return resultado

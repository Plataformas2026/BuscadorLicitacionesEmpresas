"""
matching.py
-----------
Lógica de la Pestaña 2 (Coincidencia Inteligente: Licitación -> Empresas).

Flujo:
  1. `localizar_licitacion()` -- el usuario escribe un título, un enlace o
     un identificador; se intenta primero un match exacto (enlace o
     codigo_unico) y, si no lo hay, una búsqueda semántica que devuelve
     varios candidatos para que el usuario confirme cuál es.
  2. `obtener_coincidencias()` -- con la licitación ya confirmada (existe
     en nuestra tabla), se llama a la función RPC
     `buscar_empresas_para_licitacion`, que resuelve TODO en SQL (nunca
     hace falta mover un vector de 384 posiciones a través de Python).
  3. Si la licitación NO está en nuestra tabla (el usuario la pega/escribe
     directamente), `obtener_coincidencias_texto_libre()` calcula el
     embedding al vuelo y usa `buscar_empresas_por_embedding`.
  4. `explicar_coincidencia()` -- construye la explicación de cada
     coincidencia a partir de datos concretos (similitud, país,
     solapamiento de palabras clave): nada inventado ni una llamada a un
     LLM externo, para mantener la app dentro de la arquitectura gratuita.
"""
import re

import pandas as pd
import streamlit as st
from sentence_transformers import SentenceTransformer
from supabase import Client

from search import buscar_semantica

COLUMNAS_EMPRESA_MATCH = (
    "id_empresa, nombre_empresa, sector, subsector, tipo_empresa, web, "
    "descripcion_actividad, palabras_clave, proyectos_tipo, "
    "experiencia_paises, zona_geografica, tamano, contacto_nombre, "
    "contacto_cargo, contacto_email"
)

PATRON_URL = re.compile(r"^https?://", re.IGNORECASE)


def localizar_licitacion(supabase: Client, encoder: SentenceTransformer, entrada: str) -> list:
    """
    Devuelve una lista de licitaciones candidatas para la entrada del
    usuario. Si hay un match EXACTO por enlace o por codigo_unico,
    devuelve solo ese (confianza total). Si no, hace una búsqueda
    semántica por el texto y devuelve los primeros candidatos para que el
    usuario elija.
    """
    entrada = entrada.strip()
    if not entrada:
        return []

    columnas = (
        "codigo_unico, titulo, pais, fuente_origen, url_oficial, "
        "fecha_publicacion, fecha_limite"
    )

    # 1. Match exacto por enlace
    if PATRON_URL.match(entrada):
        respuesta = (
            supabase.table("licitaciones_internacionales")
            .select(columnas)
            .eq("url_oficial", entrada)
            .limit(1)
            .execute()
        )
        if respuesta.data:
            return respuesta.data

    # 2. Match exacto por codigo_unico (con o sin mayúsculas)
    respuesta = (
        supabase.table("licitaciones_internacionales")
        .select(columnas)
        .ilike("codigo_unico", entrada)
        .limit(1)
        .execute()
    )
    if respuesta.data:
        return respuesta.data

    # 3. Búsqueda semántica por título/texto -> varios candidatos a elegir
    candidatos = buscar_semantica(supabase, encoder, entrada, match_threshold=0.15, match_count=8)
    return candidatos[:8]


def obtener_coincidencias(supabase: Client, codigo_unico: str, match_threshold: float = 0.15, match_count: int = 30) -> list:
    respuesta = supabase.rpc(
        "buscar_empresas_para_licitacion",
        {
            "codigo_unico_licitacion": codigo_unico,
            "match_threshold": match_threshold,
            "match_count": match_count,
        },
    ).execute()
    return respuesta.data or []


def obtener_coincidencias_texto_libre(
    supabase: Client,
    encoder: SentenceTransformer,
    titulo: str,
    descripcion: str = "",
    match_threshold: float = 0.15,
    match_count: int = 30,
) -> list:
    """Para licitaciones que el usuario pega/escribe y que NO están en nuestra tabla."""
    texto_completo = f"Título: {titulo}. {descripcion}".strip()
    vector_query = encoder.encode(f"passage: {texto_completo}").tolist()
    respuesta = supabase.rpc(
        "buscar_empresas_por_embedding",
        {
            "query_embedding": vector_query,
            "match_threshold": match_threshold,
            "match_count": match_count,
        },
    ).execute()
    return respuesta.data or []


def _normalizar(texto: str) -> str:
    return (texto or "").casefold()


def explicar_coincidencia(texto_licitacion: str, pais_licitacion: str, empresa: dict) -> list:
    """
    Devuelve una lista de frases (bullets) explicando por qué esta
    empresa encaja con la licitación, basada en datos concretos:
      - nivel de similitud semántica
      - coincidencia de país / zona geográfica
      - palabras clave de la empresa presentes en el texto de la licitación
      - sector/subsector, como contexto
    """
    motivos = []
    texto_norm = _normalizar(texto_licitacion)

    similarity = empresa.get("similarity")
    if similarity is not None:
        pct = round(similarity * 100, 1)
        if similarity >= 0.35:
            nivel = "muy alta"
        elif similarity >= 0.25:
            nivel = "alta"
        elif similarity >= 0.18:
            nivel = "media"
        else:
            nivel = "orientativa"
        motivos.append(f"Afinidad semántica {nivel} con el objeto de la licitación ({pct}%).")

    paises_empresa = set(_normalizar(p) for p in (empresa.get("experiencia_paises") or []))
    zonas_empresa = set(_normalizar(z) for z in (empresa.get("zona_geografica") or []))
    if pais_licitacion:
        pais_norm = _normalizar(pais_licitacion)
        if pais_norm in paises_empresa:
            motivos.append(f"Experiencia previa acreditada en {pais_licitacion}.")
        elif any(pais_norm in z or z in pais_norm for z in zonas_empresa):
            motivos.append(f"La empresa opera en la zona geográfica de {pais_licitacion}.")

    palabras_coincidentes = [
        palabra for palabra in (empresa.get("palabras_clave") or [])
        if palabra and _normalizar(palabra) in texto_norm
    ]
    if palabras_coincidentes:
        motivos.append("Palabras clave de la empresa presentes en la licitación: " + ", ".join(palabras_coincidentes) + ".")

    if empresa.get("sector"):
        detalle_sector = empresa["sector"]
        if empresa.get("subsector"):
            detalle_sector += f" ({empresa['subsector']})"
        motivos.append(f"Sector de actividad: {detalle_sector}.")

    if not motivos:
        motivos.append("Coincidencia detectada por similitud semántica general del perfil de la empresa.")

    return motivos


def formatear_tabla_coincidencias(coincidencias: list) -> pd.DataFrame:
    if not coincidencias:
        return pd.DataFrame()
    df = pd.DataFrame(coincidencias)
    return pd.DataFrame({
        "Relevancia (%)": (df["similarity"] * 100).round(1),
        "Empresa": df["nombre_empresa"],
        "ID": df["id_empresa"],
        "Sector": df["sector"].fillna("No especificado"),
        "Tipo": df["tipo_empresa"].fillna("No especificado"),
    })


# ------------------------------------------------------------------
# Interfaz de la Pestaña 2
# ------------------------------------------------------------------
def _resetear_seleccion():
    st.session_state.tab2_candidatos = None
    st.session_state.tab2_licitacion_elegida = None
    st.session_state.tab2_coincidencias = None


def render_tab2(supabase: Client, encoder: SentenceTransformer):
    st.subheader("Coincidencia Inteligente: Licitación → Empresas")
    st.caption(
        "Introduce el título, el enlace oficial o el identificador de una licitación. "
        "Si no está en nuestra base de datos, también puedes describirla directamente."
    )

    for clave, valor in (
        ("tab2_candidatos", None),
        ("tab2_licitacion_elegida", None),
        ("tab2_coincidencias", None),
    ):
        if clave not in st.session_state:
            st.session_state[clave] = valor

    entrada = st.text_input(
        "Título, enlace o identificador de la licitación",
        placeholder="ej. AFDB-spn-namibia-... · https://www.afdb.org/... · perforación de pozos en Namibia",
        key="tab2_entrada",
        on_change=_resetear_seleccion,
    )

    if st.button("🔎 Buscar licitación", key="tab2_buscar_licitacion", use_container_width=True):
        with st.spinner("Buscando la licitación..."):
            st.session_state.tab2_candidatos = localizar_licitacion(supabase, encoder, entrada)
            st.session_state.tab2_licitacion_elegida = None
            st.session_state.tab2_coincidencias = None

    candidatos = st.session_state.tab2_candidatos

    # --- Selección de la licitación entre los candidatos encontrados ---
    if candidatos:
        if len(candidatos) == 1:
            st.session_state.tab2_licitacion_elegida = candidatos[0]
            st.success(f"Licitación identificada: **{candidatos[0]['titulo']}**")
        else:
            opciones = {f"{c['titulo']}  ·  {c.get('pais') or 's/país'}": c for c in candidatos}
            etiqueta_elegida = st.selectbox(
                "Se han encontrado varias licitaciones parecidas — elige la correcta:",
                list(opciones.keys()),
                key="tab2_selector_candidato",
            )
            st.session_state.tab2_licitacion_elegida = opciones[etiqueta_elegida]

    elif candidatos is not None:  # se buscó y no hubo NINGÚN resultado
        st.warning("No se ha encontrado ninguna licitación parecida en nuestra base de datos.")

    # --- Fallback: introducir la licitación directamente ---
    with st.expander("✏️ ¿No está en la base de datos? Descríbela directamente"):
        titulo_manual = st.text_input("Título de la licitación", key="tab2_titulo_manual")
        descripcion_manual = st.text_area("Descripción / objeto del contrato", key="tab2_descripcion_manual", height=80)
        if st.button("Usar esta descripción", key="tab2_usar_manual"):
            if titulo_manual.strip():
                st.session_state.tab2_licitacion_elegida = {
                    "titulo": titulo_manual,
                    "pais": None,
                    "codigo_unico": None,
                    "descripcion_manual": descripcion_manual,
                }
                st.session_state.tab2_coincidencias = None
            else:
                st.warning("Escribe al menos un título.")

    licitacion = st.session_state.tab2_licitacion_elegida

    if licitacion:
        st.markdown(f"**Licitación seleccionada:** {licitacion['titulo']}")

        if st.button("Buscar empresas coincidentes", key="tab2_buscar_empresas", use_container_width=True):
            with st.spinner("Cruzando con la base de datos de empresas..."):
                if licitacion.get("codigo_unico"):
                    st.session_state.tab2_coincidencias = obtener_coincidencias(supabase, licitacion["codigo_unico"])
                else:
                    st.session_state.tab2_coincidencias = obtener_coincidencias_texto_libre(
                        supabase, encoder, licitacion["titulo"], licitacion.get("descripcion_manual", "")
                    )

        coincidencias = st.session_state.tab2_coincidencias
        if coincidencias is not None:
            if not coincidencias:
                st.warning("No se han encontrado empresas con un perfil afín a esta licitación.")
            else:
                st.success(f"**{len(coincidencias)}** empresas encajan con esta licitación, de más a menos afín:")
                texto_licitacion = licitacion.get("titulo", "") + " " + (licitacion.get("descripcion_manual", "") or "")

                for empresa in coincidencias:
                    with st.container(border=True):
                        col_nombre, col_score = st.columns([4, 1])
                        with col_nombre:
                            st.markdown(f"**{empresa['nombre_empresa']}**  ·  `{empresa['id_empresa']}`")
                        with col_score:
                            st.markdown(f"**{round(empresa['similarity'] * 100, 1)}%**")

                        motivos = explicar_coincidencia(texto_licitacion, licitacion.get("pais"), empresa)
                        for motivo in motivos:
                            st.markdown(f"- {motivo}")

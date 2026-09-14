"""
db.py
-----
Conexión a Supabase y modelo de embeddings, cacheados con
`st.cache_resource` para no reconectar ni recargar el modelo en cada
interacción del usuario.
"""
import streamlit as st
from sentence_transformers import SentenceTransformer
from supabase import Client, create_client

from config import MODELO_EMBEDDING, SUPABASE_ANON_KEY, SUPABASE_URL


@st.cache_resource(show_spinner=False)
def obtener_cliente() -> Client:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        st.error(
            "⚠️ Faltan las credenciales de Supabase. Define `SUPABASE_URL` y "
            "`SUPABASE_ANON_KEY` en `.streamlit/secrets.toml` (producción) o "
            "como variables de entorno (desarrollo local)."
        )
        st.stop()
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


@st.cache_resource(show_spinner=False)
def obtener_encoder() -> SentenceTransformer:
    return SentenceTransformer(MODELO_EMBEDDING, device="cpu")

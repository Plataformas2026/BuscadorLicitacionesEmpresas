"""
config.py
---------
Configuración y constantes compartidas por las 3 pestañas de la app.
"""
import os

import streamlit as st


def _config(clave: str, por_defecto: str = "") -> str:
    try:
        valor = st.secrets.get(clave)
        if valor:
            return valor
    except Exception:
        pass
    return os.getenv(clave, por_defecto)


# La app SOLO lee datos: usa siempre la clave ANÓNIMA de Supabase. La
# Service Role Key (con permisos de escritura) vive exclusivamente en los
# GitHub Actions de ingesta — ver ingest/common.py.
SUPABASE_URL = _config("SUPABASE_URL")
SUPABASE_ANON_KEY = _config("SUPABASE_ANON_KEY")

# Justificación de coincidencias con IA (Pestaña 2, capa tipo RAG sobre la
# lógica de coincidencias ya existente) -- ver app/ia_explicacion.py.
# Groq: capa gratuita real, sin tarjeta de crédito (30 peticiones/min,
# 14.400/día en el momento de escribir esto), API compatible con la de
# OpenAI. Si no se configura la clave, la app sigue funcionando igual que
# hasta ahora (la Pestaña 2 no ofrece el botón de justificación con IA,
# pero toda la lógica de coincidencias determinista sigue intacta).
GROQ_API_KEY = _config("GROQ_API_KEY")
GROQ_MODELO = _config("GROQ_MODELO", "llama-3.1-8b-instant")

MODELO_EMBEDDING = "intfloat/multilingual-e5-small"  # 384 dimensiones

FUENTES_LICITACIONES = ["AfDB", "BID", "CAF", "AFD"]  # se irán añadiendo más bancos/organismos aquí

TIPOS_EMPRESA = ["Pública", "Privada"]

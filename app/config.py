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

MODELO_EMBEDDING = "intfloat/multilingual-e5-small"  # 384 dimensiones

FUENTES_LICITACIONES = ["AfDB", "BID"]  # se irán añadiendo más bancos/organismos aquí

TIPOS_EMPRESA = ["Pública", "Privada"]

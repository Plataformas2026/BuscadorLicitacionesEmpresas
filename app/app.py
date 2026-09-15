"""
app.py
------
Licitaciones & Empresas — punto de entrada de Streamlit.

Ejecutar en local:
    cd app
    streamlit run app.py

Requiere SUPABASE_URL y SUPABASE_ANON_KEY (ver README.md).
"""
import streamlit as st

from db import obtener_cliente, obtener_encoder
from styles import aplicar_estilos

import directorio
import matching
import search

st.set_page_config(
    page_title="Licitaciones & Empresas",
    page_icon="🎯",
    layout="wide",
)

aplicar_estilos()

supabase = obtener_cliente()

with st.spinner("Cargando modelo de IA..."):
    encoder = obtener_encoder()

st.title("🎯 Licitaciones & Empresas")
st.caption("Buscador de licitaciones internacionales, coincidencia inteligente y directorio de empresas.")

tab1, tab2, tab3 = st.tabs([
    "🔍 Buscador de Licitaciones",
    "🤝 Coincidencia Inteligente",
    "🏢 Directorio de Empresas",
])

with tab1:
    search.render_tab1(supabase, encoder)

with tab2:
    matching.render_tab2(supabase, encoder)

with tab3:
    directorio.render_tab3(supabase, encoder)

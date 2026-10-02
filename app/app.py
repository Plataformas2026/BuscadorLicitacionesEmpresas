"""
app.py
------
Licitaciones & Empresas — punto de entrada de Streamlit.

Ejecutar en local:
    cd app
    streamlit run app.py

Requiere SUPABASE_URL y SUPABASE_ANON_KEY.
"""

import streamlit as st

from db import obtener_cliente, obtener_encoder
from styles import aplicar_estilos

import auth
import directorio
import matching
import search


st.set_page_config(
    page_title="Licitaciones & Empresas",
    page_icon="",
    layout="wide",
)

aplicar_estilos()

# ---------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------

supabase = obtener_cliente()

# ---------------------------------------------------------
# LOGIN
# ---------------------------------------------------------

if not auth.mostrar_login(supabase):
    st.stop()

# ---------------------------------------------------------
# USUARIO AUTENTICADO
# ---------------------------------------------------------

usuario = st.session_state.get("usuario")

# Barra superior
col1, col2 = st.columns([8, 2])

with col1:
    if usuario:
        email_usuario = getattr(usuario, "email", None)
        if email_usuario:
            st.caption(f"Sesión iniciada como: {email_usuario}")

with col2:
    if st.button("Cerrar sesión", use_container_width=True):
        auth.cerrar_sesion(supabase)

# ---------------------------------------------------------
# MODELO DE IA
# ---------------------------------------------------------

with st.spinner("Cargando modelo de IA..."):
    encoder = obtener_encoder()

# ---------------------------------------------------------
# APLICACIÓN
# ---------------------------------------------------------

st.title("Licitaciones & Empresas")

st.caption(
    "Buscador de licitaciones internacionales, "
    "coincidencia inteligente y directorio de empresas."
)

tab1, tab2, tab3 = st.tabs(
    [
        "Buscador de Licitaciones",
        "Coincidencia Inteligente",
        "Directorio de Empresas",
    ]
)

with tab1:
    search.render_tab1(supabase, encoder)

with tab2:
    matching.render_tab2(supabase, encoder)

with tab3:
    directorio.render_tab3(supabase, encoder)

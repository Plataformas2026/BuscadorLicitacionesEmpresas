import streamlit as st

from db import obtener_cliente, obtener_encoder
from styles import aplicar_estilos

import directorio
import matching
import search


st.set_page_config(
    page_title="Licitaciones & Empresas",
    page_icon="",
    layout="wide",
)


# ============================================================
# LOGIN SENCILLO
# ============================================================

# ============================================================
# CERRAR SESIÓN — ESQUINA SUPERIOR DERECHA
# ============================================================

st.markdown(
    """
    <style>
    .logout-container {
        position: fixed;
        top: 15px;
        right: 20px;
        z-index: 999999;
    }

    .logout-container button {
        border-radius: 8px;
        border: 1px solid #d0d5dd;
        background: white;
        color: #344054;
        font-size: 13px;
        font-weight: 600;
        padding: 8px 16px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="logout-container">',
    unsafe_allow_html=True,
)

if st.button("Cerrar sesión"):
    st.session_state["logueado"] = False
    st.rerun()

st.markdown("</div>", unsafe_allow_html=True)

# ============================================================
# COMPROBAR LOGIN
# ============================================================

if not login():
    st.stop()


# ============================================================
# APLICACIÓN
# ============================================================

aplicar_estilos()

supabase = obtener_cliente()

with st.spinner("Cargando modelo de IA..."):
    encoder = obtener_encoder()

st.title("Licitaciones & Empresas")

st.caption(
    "Buscador de licitaciones internacionales, "
    "coincidencia inteligente y directorio de empresas."
)


# Botón de cerrar sesión
if st.button("Cerrar sesión"):
    st.session_state["logueado"] = False
    st.rerun()


tab1, tab2, tab3 = st.tabs([
    "Buscador de Licitaciones",
    "Coincidencia Inteligente",
    "Directorio de Empresas",
])


with tab1:
    search.render_tab1(supabase, encoder)

with tab2:
    matching.render_tab2(supabase, encoder)

with tab3:
    directorio.render_tab3(supabase, encoder)

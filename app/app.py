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

def login():
    if st.session_state.get("logueado"):
        return True

    st.title("🔐 Licitaciones & Empresas")
    st.write("Introduce tus credenciales para acceder.")

    usuario = st.text_input("Usuario")
    password = st.text_input("Contraseña", type="password")

    if st.button("Entrar", use_container_width=True):
        if (
            usuario == st.secrets["LOGIN_USER"]
            and password == st.secrets["LOGIN_PASSWORD"]
        ):
            st.session_state["logueado"] = True
            st.rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")

    return False


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

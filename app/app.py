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

    # ========================================================
    # ESTILOS SOLO PARA LA PANTALLA DE LOGIN
    # ========================================================

    st.markdown(
        """
        <style>

        /* Fondo del login */
        .stApp {
            background: linear-gradient(
                135deg,
                #f8fafc 0%,
                #eef2ff 100%
            );
        }

        /* Ocultar elementos de Streamlit */
        #MainMenu {
            visibility: hidden;
        }

        footer {
            visibility: hidden;
        }

        header {
            visibility: hidden;
        }

        /* Contenedor principal */
        .login-wrapper {
            max-width: 420px;
            margin: 100px auto 0 auto;
        }

        /* Logo */
        .login-logo {
            text-align: center;
            font-size: 52px;
            margin-bottom: 10px;
        }

        /* Título */
        .login-title {
            text-align: center;
            font-size: 30px;
            font-weight: 700;
            color: #172033;
            margin-bottom: 8px;
        }

        /* Subtítulo */
        .login-subtitle {
            text-align: center;
            color: #667085;
            font-size: 15px;
            margin-bottom: 28px;
        }

        /* Tarjeta */
        .login-card {
            background: white;
            padding: 32px;
            border-radius: 16px;
            border: 1px solid #e4e7ec;
            box-shadow: 0 12px 35px rgba(16, 24, 40, 0.08);
        }

        /* Etiquetas */
        .login-card label {
            font-weight: 600;
            color: #344054;
        }

        /* Inputs */
        .login-card input {
            border-radius: 8px !important;
        }

        /* Botón */
        .login-button button {
            width: 100%;
            border-radius: 8px;
            background: #315efb;
            color: white;
            border: none;
            font-weight: 600;
            min-height: 44px;
            margin-top: 10px;
        }

        .login-button button:hover {
            background: #2449d8;
            color: white;
        }

        /* Texto inferior */
        .login-footer {
            text-align: center;
            color: #98a2b3;
            font-size: 12px;
            margin-top: 20px;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )

    # ========================================================
    # LOGIN
    # ========================================================

    st.markdown(
        """
        <div class="login-wrapper">

            <div class="login-logo">
                📊
            </div>

            <div class="login-title">
                Licitaciones & Empresas
            </div>

            <div class="login-subtitle">
                Accede a tu plataforma
            </div>

            <div class="login-card">
        """,
        unsafe_allow_html=True,
    )

    usuario = st.text_input(
        "Usuario",
        placeholder="Introduce tu usuario",
    )

    password = st.text_input(
        "Contraseña",
        type="password",
        placeholder="Introduce tu contraseña",
    )

    st.markdown(
        '<div class="login-button">',
        unsafe_allow_html=True,
    )

    entrar = st.button(
        "Iniciar sesión",
        use_container_width=True,
    )

    st.markdown(
        "</div>",
        unsafe_allow_html=True,
    )

    if entrar:
        if (
            usuario == st.secrets["LOGIN_USER"]
            and password == st.secrets["LOGIN_PASSWORD"]
        ):
            st.session_state["logueado"] = True
            st.rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")

    st.markdown(
        """
            </div>

            <div class="login-footer">
                Acceso privado
            </div>

        </div>
        """,
        unsafe_allow_html=True,
    )

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

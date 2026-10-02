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
# LOGIN
# ============================================================

def login():

    if st.session_state.get("logueado", False):
        return True

    # --------------------------------------------------------
    # ESTILOS DEL LOGIN
    # --------------------------------------------------------

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

        /* Ocultar elementos de Streamlit durante el login */
        #MainMenu {
            visibility: hidden;
        }

        footer {
            visibility: hidden;
        }

        header {
            visibility: hidden;
        }

        /* Contenedor visual */
        .login-box {
            max-width: 420px;
            margin: 100px auto 0 auto;
            background: white;
            padding: 40px;
            border-radius: 16px;
            border: 1px solid #e4e7ec;
            box-shadow: 0 12px 35px rgba(16, 24, 40, 0.08);
        }

        .login-title {
            text-align: center;
            color: #172033;
            font-size: 30px;
            font-weight: 700;
            margin-bottom: 8px;
        }

        .login-subtitle {
            text-align: center;
            color: #667085;
            font-size: 15px;
            margin-bottom: 30px;
        }

        .login-label {
            color: #344054;
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 6px;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )

    # --------------------------------------------------------
    # CABECERA DEL LOGIN
    # --------------------------------------------------------

    st.markdown(
        """
        <div class="login-box">

            <div class="login-title">
                Licitaciones & Empresas
            </div>

            <div class="login-subtitle">
                Accede a tu plataforma
            </div>

        </div>
        """,
        unsafe_allow_html=True,
    )

    # Los campos se muestran debajo de la tarjeta visual
    # para evitar problemas con el HTML de Streamlit.

    usuario = st.text_input(
        "Usuario",
        placeholder="Introduce tu usuario",
    )

    password = st.text_input(
        "Contraseña",
        type="password",
        placeholder="Introduce tu contraseña",
    )

    entrar = st.button(
        "Iniciar sesión",
        use_container_width=True,
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


# ============================================================
# CERRAR SESIÓN — ESQUINA SUPERIOR DERECHA
# ============================================================

st.markdown(
    """
    <style>

    .logout-button {
        position: fixed;
        top: 15px;
        right: 20px;
        z-index: 999999;
    }

    </style>
    """,
    unsafe_allow_html=True,
)

# Usamos una columna para colocar el botón arriba a la derecha
col_izq, col_der = st.columns([9, 1])

with col_der:
    if st.button("Cerrar sesión"):
        st.session_state["logueado"] = False
        st.rerun()


# ============================================================
# TABS
# ============================================================

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

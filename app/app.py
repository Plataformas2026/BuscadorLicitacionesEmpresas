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
    # ESTILOS SOLO PARA EL LOGIN
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

        /* Ocultar elementos de Streamlit */
        #MainMenu {
            visibility: hidden;
        }

        header {
            visibility: hidden;
        }

        footer {
            visibility: hidden;
        }

        /* Centrar el contenido del formulario */
        .login-input {
            max-width: 420px;
            margin: 0 auto;
        }

        /* Botón de login */
        .login-button button {
            background-color: #006dcc;
            color: white;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            min-height: 45px;
        }

        .login-button button:hover {
            background-color: #005bb5;
            color: white;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )

    # --------------------------------------------------------
    # ESPACIO SUPERIOR
    # --------------------------------------------------------

    st.write("")
    st.write("")
    st.write("")

    # --------------------------------------------------------
    # CONTENEDOR CENTRADO
    # --------------------------------------------------------

    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:

        # Tarjeta del login
        with st.container(border=True):

            st.markdown(
                "<h1 style='text-align:center; "
                "font-size:30px; "
                "color:#172033; "
                "margin-bottom:5px;'>"
                "Licitaciones & Empresas"
                "</h1>",
                unsafe_allow_html=True,
            )

            st.markdown(
                "<p style='text-align:center; "
                "color:#667085; "
                "margin-bottom:25px;'>"
                "Accede a tu plataforma"
                "</p>",
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


# ============================================================
# CABECERA DE LA APLICACIÓN
# ============================================================

# Título a la izquierda + cerrar sesión a la derecha
col_titulo, col_logout = st.columns([8, 1])

with col_titulo:

    st.title("Licitaciones & Empresas")

    st.caption(
        "Buscador de licitaciones internacionales, "
        "coincidencia inteligente y directorio de empresas."
    )


with col_logout:

    # Pequeño espacio para alinear el botón con el título
    st.write("")

    if st.button(
        "Cerrar sesión",
        use_container_width=True,
    ):
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

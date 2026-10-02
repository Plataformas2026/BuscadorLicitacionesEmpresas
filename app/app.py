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

        /* Botón de iniciar sesión */
        .login-button button {
            background-color: #315EFB;
            color: white;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            min-height: 45px;
        }

        .login-button button:hover {
            background-color: #2449D8;
            color: white;
        }

        /* Borde de los campos */
        div[data-baseweb="input"] {
            border-radius: 8px;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )

    # ========================================================
    # ESPACIO SUPERIOR
    # ========================================================

    st.write("")
    st.write("")
    st.write("")

    # ========================================================
    # LOGO
    # ========================================================

    col_logo_1, col_logo_2, col_logo_3 = st.columns([1, 2, 1])

    with col_logo_2:
        st.image(
            "assets/logo.png",
            width=230,
        )

    # ========================================================
    # LOGIN
    # ========================================================

    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:

        with st.container(border=True):

            st.markdown(
                """
                <h1 style="
                    text-align:center;
                    font-size:30px;
                    color:#172033;
                    margin-top:10px;
                    margin-bottom:5px;
                ">
                    Licitaciones & Empresas
                </h1>

                <p style="
                    text-align:center;
                    color:#667085;
                    margin-bottom:25px;
                ">
                    Accede a tu plataforma
                </p>
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

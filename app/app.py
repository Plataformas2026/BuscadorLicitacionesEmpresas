import streamlit as st
from pathlib import Path

from db import obtener_cliente, obtener_encoder
from styles import aplicar_estilos

import directorio
import matching
import search


# ============================================================
# CONFIGURACIÓN
# ============================================================

st.set_page_config(
    page_title="Licitaciones & Empresas",
    page_icon="",
    layout="wide",
)


# ============================================================
# CARGA DEL ENCODER
# Se guarda en memoria para no volver a cargarlo en cada rerun
# ============================================================

@st.cache_resource
def cargar_encoder():
    return obtener_encoder()


# ============================================================
# LOGIN
# ============================================================

def login():

    # Si ya está logueado, no renderizamos absolutamente nada
    # del login.
    if st.session_state.get("logueado", False):
        return True

    # ========================================================
    # RUTA DEL LOGO
    # app.py está dentro de /app
    # logo.png está dentro de /assets
    # ========================================================

    logo_path = (
        Path(__file__).resolve().parent.parent
        / "assets"
        / "logo.png"
    )

    # ========================================================
    # ESTILOS SOLO PARA EL LOGIN
    # ========================================================

    st.markdown(
        """
        <style>

        /* ==================================================
           EVITAR PARPADEO DURANTE LA TRANSICIÓN
           ================================================== */

        .login-page {
            animation: loginFadeIn 0.15s ease-in;
        }

        @keyframes loginFadeIn {
            from {
                opacity: 0;
            }
            to {
                opacity: 1;
            }
        }

        /* ==================================================
           LOGO EN ESQUINA INFERIOR IZQUIERDA
           ================================================== */

        div[data-testid="stImage"] {
            position: fixed !important;
            left: 25px !important;
            bottom: 20px !important;
            z-index: 9999 !important;
            width: auto !important;
        }

        /* ==================================================
           BORDE DEL RECUADRO
           ================================================== */

        div[data-testid="stVerticalBlockBorderWrapper"] {
            border: 1.5px solid #315EFB !important;
            border-radius: 12px !important;
        }

        /* ==================================================
           BOTÓN INICIAR SESIÓN
           ================================================== */

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

        /* ==================================================
           PANTALLA DE TRANSICIÓN
           ================================================== */

        .login-loading {
            position: fixed;
            inset: 0;
            background: white;
            z-index: 999999;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-direction: column;
        }

        .login-loading-title {
            font-size: 24px;
            font-weight: 600;
            color: #172033;
            margin-bottom: 8px;
        }

        .login-loading-text {
            font-size: 14px;
            color: #667085;
        }

        .login-loading-spinner {
            width: 28px;
            height: 28px;
            margin-bottom: 18px;
            border: 3px solid #E5E7EB;
            border-top: 3px solid #315EFB;
            border-radius: 50%;
            animation: loginSpinner 0.8s linear infinite;
        }

        @keyframes loginSpinner {
            to {
                transform: rotate(360deg);
            }
        }

        </style>
        """,
        unsafe_allow_html=True,
    )

    # ========================================================
    # CONTENEDOR COMPLETO DEL LOGIN
    # ========================================================

    st.markdown(
        '<div class="login-page">',
        unsafe_allow_html=True,
    )

    # ========================================================
    # ESPACIO SUPERIOR
    # ========================================================

    st.write("")
    st.write("")

    # ========================================================
    # RECUADRO DEL LOGIN
    # ========================================================

    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:

        with st.container(border=True):

            st.markdown(
                """
                <h1 style="
                    text-align: center;
                    font-size: 30px;
                    color: #172033;
                    margin-top: 5px;
                    margin-bottom: 5px;
                ">
                    Licitaciones & Empresas
                </h1>

                <p style="
                    text-align: center;
                    color: #667085;
                    margin-bottom: 25px;
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

            # =================================================
            # COMPROBAR CREDENCIALES
            # =================================================

            if entrar:

                if (
                    usuario == st.secrets["LOGIN_USER"]
                    and password == st.secrets["LOGIN_PASSWORD"]
                ):

                    # -----------------------------------------
                    # 1. Guardamos inmediatamente el estado
                    # -----------------------------------------

                    st.session_state["logueado"] = True

                    # -----------------------------------------
                    # 2. Mostramos una pantalla completa de
                    #    transición para que NO se vea la app
                    #    parcialmente renderizada.
                    # -----------------------------------------

                    st.markdown(
                        """
                        <div class="login-loading">

                            <div class="login-loading-spinner"></div>

                            <div class="login-loading-title">
                                Accediendo...
                            </div>

                            <div class="login-loading-text">
                                Preparando tu plataforma
                            </div>

                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

                    # -----------------------------------------
                    # 3. Forzamos un nuevo renderizado
                    # -----------------------------------------

                    st.rerun()

                else:

                    st.error(
                        "Usuario o contraseña incorrectos."
                    )

    # ========================================================
    # LOGO FUERA DEL RECUADRO
    # ESQUINA INFERIOR IZQUIERDA
    # ========================================================

    st.image(
        str(logo_path),
        width=180,
    )

    st.markdown(
        "</div>",
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


# ============================================================
# CONEXIÓN A SUPABASE
# ============================================================

supabase = obtener_cliente()


# ============================================================
# CARGAR MODELO DE IA
# Se ejecuta una sola vez gracias a @st.cache_resource
# ============================================================

encoder = cargar_encoder()


# ============================================================
# CABECERA DE LA APLICACIÓN
# ============================================================

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

        # Limpiamos también posibles datos temporales
        # relacionados con la aplicación.
        st.rerun()


# ============================================================
# TABS
# ============================================================

tab1, tab2, tab3 = st.tabs([
    "Buscador de Licitaciones",
    "Coincidencia Inteligente",
    "Directorio de Empresas",
])


# ============================================================
# TAB 1
# ============================================================

with tab1:
    search.render_tab1(
        supabase,
        encoder,
    )


# ============================================================
# TAB 2
# ============================================================

with tab2:
    matching.render_tab2(
        supabase,
        encoder,
    )


# ============================================================
# TAB 3
# ============================================================

with tab3:
    directorio.render_tab3(
        supabase,
        encoder,
    )

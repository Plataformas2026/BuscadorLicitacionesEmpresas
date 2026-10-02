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
# ESTADOS INICIALES
# ============================================================

if "logueado" not in st.session_state:
    st.session_state["logueado"] = False

if "cargando_app" not in st.session_state:
    st.session_state["cargando_app"] = False


# ============================================================
# RUTA DEL LOGO
# app.py está dentro de /app
# logo.png está dentro de /assets
# ============================================================

logo_path = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "logo.png"
)


# ============================================================
# CARGAR ENCODER
#
# st.cache_resource hace que el modelo se cargue una sola vez
# y no vuelva a inicializarse en cada st.rerun().
# ============================================================

@st.cache_resource
def cargar_encoder():
    return obtener_encoder()


# ============================================================
# PANTALLA DE CARGA
# ============================================================

def mostrar_pantalla_carga():

    st.markdown(
        """
        <style>

        /* ==================================================
           OCULTAR ELEMENTOS DE STREAMLIT
           ================================================== */

        header {
            visibility: hidden;
        }

        footer {
            visibility: hidden;
        }

        /* ==================================================
           PANTALLA COMPLETA
           ================================================== */

        .loading-screen {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;

            background: #ffffff;

            z-index: 999999;

            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;

            text-align: center;
        }

        /* ==================================================
           LOGO
           ================================================== */

        .loading-logo {
            width: 180px;
            max-width: 45vw;
            margin-bottom: 35px;
        }

        /* ==================================================
           TEXTO PRINCIPAL
           ================================================== */

        .loading-title {
            font-size: 26px;
            font-weight: 600;
            color: #172033;

            margin-bottom: 8px;
        }

        /* ==================================================
           TEXTO SECUNDARIO
           ================================================== */

        .loading-subtitle {
            font-size: 15px;
            color: #667085;

            margin-bottom: 28px;
        }

        /* ==================================================
           SPINNER
           ================================================== */

        .loading-spinner {
            width: 32px;
            height: 32px;

            border: 3px solid #E5E7EB;
            border-top: 3px solid #315EFB;

            border-radius: 50%;

            animation: loading-spin 0.8s linear infinite;
        }

        @keyframes loading-spin {
            0% {
                transform: rotate(0deg);
            }

            100% {
                transform: rotate(360deg);
            }
        }

        /* ==================================================
           PEQUEÑA ANIMACIÓN DE ENTRADA
           ================================================== */

        .loading-content {
            animation: loading-fade-in 0.25s ease-out;
        }

        @keyframes loading-fade-in {
            from {
                opacity: 0;
                transform: translateY(5px);
            }

            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        </style>
        """,
        unsafe_allow_html=True,
    )

    # Convertimos la ruta del logo a URI para poder mostrarla
    # directamente dentro del HTML.
    import base64

    try:
        with open(logo_path, "rb") as f:
            logo_base64 = base64.b64encode(f.read()).decode()

        logo_src = f"data:image/png;base64,{logo_base64}"

    except Exception:
        logo_src = ""

    st.markdown(
        f"""
        <div class="loading-screen">

            <div class="loading-content">

                {
                    f'<img class="loading-logo" src="{logo_src}">'
                    if logo_src
                    else ""
                }

                <div class="loading-title">
                    Accediendo...
                </div>

                <div class="loading-subtitle">
                    Preparando tu plataforma
                </div>

                <div class="loading-spinner"></div>

            </div>

        </div>
        """,
        unsafe_allow_html=True,
    )


# ============================================================
# LOGIN
# ============================================================

def login():

    # Si ya está logueado no mostramos absolutamente nada
    # relacionado con el login.
    if st.session_state.get("logueado", False):
        return True

    # ========================================================
    # ESTILOS DEL LOGIN
    # ========================================================

    st.markdown(
        """
        <style>

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

        </style>
        """,
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
                    # GUARDAR LOGIN
                    # -----------------------------------------

                    st.session_state["logueado"] = True

                    # -----------------------------------------
                    # ACTIVAR PANTALLA DE CARGA
                    # -----------------------------------------

                    st.session_state["cargando_app"] = True

                    # -----------------------------------------
                    # VOLVER A EJECUTAR LA APP
                    # -----------------------------------------

                    st.rerun()

                else:

                    st.error(
                        "Usuario o contraseña incorrectos."
                    )

    # ========================================================
    # LOGO DEL LOGIN
    # ========================================================

    st.image(
        str(logo_path),
        width=180,
    )

    return False


# ============================================================
# FLUJO PRINCIPAL
# ============================================================


# ============================================================
# 1. SI NO ESTÁ LOGUEADO → LOGIN
# ============================================================

if not st.session_state.get("logueado", False):

    login()

    st.stop()


# ============================================================
# 2. SI ESTÁ LOGUEADO PERO ESTÁ CARGANDO → PANTALLA DE CARGA
# ============================================================

if st.session_state.get("cargando_app", False):

    # --------------------------------------------------------
    # Mostrar inmediatamente la pantalla de carga.
    #
    # Esto evita que el usuario vea:
    #
    # LOGIN → pestañas → aplicación
    #
    # y en su lugar verá:
    #
    # LOGIN → CARGANDO → aplicación
    # --------------------------------------------------------

    mostrar_pantalla_carga()

    # --------------------------------------------------------
    # Cargar recursos pesados mientras la pantalla de carga
    # permanece visible.
    # --------------------------------------------------------

    encoder = cargar_encoder()

    # --------------------------------------------------------
    # También inicializamos la conexión a Supabase aquí para
    # aprovechar la pantalla de carga.
    # --------------------------------------------------------

    supabase = obtener_cliente()

    # --------------------------------------------------------
    # Marcamos que ya terminó la carga.
    # --------------------------------------------------------

    st.session_state["cargando_app"] = False

    # --------------------------------------------------------
    # Nuevo renderizado.
    #
    # Como cargando_app ahora es False, la siguiente ejecución
    # entra directamente en la aplicación.
    # --------------------------------------------------------

    st.rerun()


# ============================================================
# 3. APLICACIÓN PRINCIPAL
# ============================================================

aplicar_estilos()


# ============================================================
# CONEXIÓN A SUPABASE
# ============================================================

supabase = obtener_cliente()


# ============================================================
# ENCODER
#
# Si ya fue cargado durante la pantalla de transición,
# st.cache_resource lo devuelve inmediatamente.
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

        # -----------------------------------------
        # Cerrar sesión
        # -----------------------------------------

        st.session_state["logueado"] = False

        # -----------------------------------------
        # Aseguramos que la próxima entrada vuelva
        # a pasar por la pantalla de carga.
        # -----------------------------------------

        st.session_state["cargando_app"] = False

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

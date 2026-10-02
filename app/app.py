import streamlit as st
from pathlib import Path
import time

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
# ESTADO INICIAL
# ============================================================

if "logueado" not in st.session_state:
    st.session_state["logueado"] = False

if "cargando_app" not in st.session_state:
    st.session_state["cargando_app"] = False


# ============================================================
# LOGIN
# ============================================================

def login():

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

            # =================================================
            # USUARIO
            # =================================================

            usuario = st.text_input(
                "Usuario",
                placeholder="Introduce tu usuario",
            )

            # =================================================
            # CONTRASEÑA
            # =================================================

            password = st.text_input(
                "Contraseña",
                type="password",
                placeholder="Introduce tu contraseña",
            )

            # =================================================
            # BOTÓN
            # =================================================

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

                    # Usuario autenticado
                    st.session_state["logueado"] = True

                    # Activar pantalla de carga
                    st.session_state["cargando_app"] = True

                    # Volver a ejecutar la aplicación
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

    return False


# ============================================================
# COMPROBAR LOGIN
# ============================================================

if not login():
    st.stop()


# ============================================================
# PANTALLA DE CARGA DESPUÉS DEL LOGIN
# ============================================================

if st.session_state.get("cargando_app", False):

    # ========================================================
    # PANTALLA BLANCA COMPLETA
    # ========================================================

    st.markdown(
        """
        <style>

        /* ==================================================
           FONDO BLANCO
           ================================================== */

        html,
        body,
        [data-testid="stApp"],
        [data-testid="stAppViewContainer"] {
            background: white !important;
        }


        /* ==================================================
           OCULTAR CONTENIDO DE LA APLICACIÓN
           ================================================== */

        [data-testid="stAppViewContainer"] > .main {
            visibility: hidden !important;
        }


        /* ==================================================
           OCULTAR SIDEBAR
           ================================================== */

        [data-testid="stSidebar"] {
            display: none !important;
        }


        /* ==================================================
           OCULTAR HEADER
           ================================================== */

        header {
            visibility: hidden !important;
        }


        /* ==================================================
           SPINNER
           ================================================== */

        [data-testid="stSpinner"] {
            visibility: visible !important;

            position: fixed !important;

            top: 50% !important;
            left: 50% !important;

            transform: translate(-50%, -50%) !important;

            z-index: 999999 !important;

            display: flex !important;

            align-items: center !important;
            justify-content: center !important;
        }


        /* ==================================================
           SPINNER GRANDE
           ================================================== */

        [data-testid="stSpinner"] svg {
            width: 70px !important;
            height: 70px !important;
        }


        /* ==================================================
           OCULTAR TEXTO DEL SPINNER
           ================================================== */

        [data-testid="stSpinner"] > div:last-child {
            display: none !important;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )


    # ========================================================
    # CARGAR RECURSOS
    # ========================================================

    inicio = time.time()

    with st.spinner(""):

        # ----------------------------------------------------
        # Conexión
        # ----------------------------------------------------

        supabase = obtener_cliente()

        # ----------------------------------------------------
        # Modelo de IA
        # ----------------------------------------------------

        encoder = obtener_encoder()

        # ----------------------------------------------------
        # Garantizar 5 segundos
        # ----------------------------------------------------

        tiempo_transcurrido = time.time() - inicio

        tiempo_restante = 5 - tiempo_transcurrido

        if tiempo_restante > 0:
            time.sleep(tiempo_restante)


    # ========================================================
    # FINALIZAR CARGA
    # ========================================================

    st.session_state["cargando_app"] = False

    st.rerun()


# ============================================================
# APLICACIÓN
# ============================================================

aplicar_estilos()


# ============================================================
# CONEXIÓN Y MODELO
# ============================================================

supabase = obtener_cliente()

encoder = obtener_encoder()


# ============================================================
# CABECERA DE LA APLICACIÓN
# ============================================================

col_titulo, col_logout = st.columns([8, 1])


with col_titulo:

    st.title(
        "Licitaciones & Empresas"
    )

    st.caption(
        "Buscador de licitaciones internacionales, "
        "coincidencia inteligente y directorio de empresas."
    )


with col_logout:

    # Pequeño espacio para alinear el botón
    # con el título

    st.write("")

    if st.button(
        "Cerrar sesión",
        use_container_width=True,
    ):

        st.session_state["logueado"] = False

        st.session_state["cargando_app"] = False

        st.rerun()


# ============================================================
# TABS
# ============================================================

tab1, tab2, tab3 = st.tabs(
    [
        "Buscador de Licitaciones",
        "Coincidencia Inteligente",
        "Directorio de Empresas",
    ]
)


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

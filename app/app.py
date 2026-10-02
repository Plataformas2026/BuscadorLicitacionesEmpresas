import streamlit as st
from pathlib import Path

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
    # ESTILOS DEL LOGIN
    # ========================================================

    st.markdown(
        """
        <style>
    
        /* ==================================================
           FRANJA MORADA SUPERIOR
           ================================================== */
    
        header[data-testid="stHeader"] {
            background-color: #6C2BD9 !important;
            height: 60px !important;
        }
    
        header[data-testid="stHeader"]::before {
            content: "";
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 60px;
            background-color: #6C2BD9;
        }
    
    
        /* ==================================================
           LOGO FIJO ABAJO A LA IZQUIERDA
           ================================================== */
    
        div[data-testid="stImage"] {
            position: fixed !important;
            left: 25px !important;
            bottom: 20px !important;
            z-index: 9999 !important;
            width: auto !important;
        }
    
    
        /* ==================================================
           BORDE DEL RECUADRO DEL LOGIN
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

                    st.session_state["logueado"] = True

                    st.rerun()

                else:

                    st.error(
                        "Usuario o contraseña incorrectos."
                    )


    # ========================================================
    # LOGO
    # FUERA DEL RECUADRO
    # ABAJO A LA IZQUIERDA
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
# APLICACIÓN
# ============================================================

aplicar_estilos()


# ============================================================
# CONEXIÓN Y MODELO DE IA
# ============================================================

supabase = obtener_cliente()

with st.spinner("Cargando modelo de IA..."):
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

    search.render_tab1(
        supabase,
        encoder,
    )


with tab2:

    matching.render_tab2(
        supabase,
        encoder,
    )


with tab3:

    directorio.render_tab3(
        supabase,
        encoder,
    )

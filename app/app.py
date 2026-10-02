import time
import streamlit as st
from pathlib import Path

from supabase_client import obtener_cliente
from embeddings import obtener_encoder
from estilos import aplicar_estilos

import directorio
import matching
import search


# ============================================================
# CONFIGURACIÓN
# ============================================================

st.set_page_config(
    page_title="Licitaciones & Empresas",
    page_icon="",
    layout="wide"
)


# ============================================================
# ESTADOS DE SESIÓN
# ============================================================

if "logueado" not in st.session_state:
    st.session_state["logueado"] = False

if "cargando_app" not in st.session_state:
    st.session_state["cargando_app"] = False


# ============================================================
# RUTAS
# ============================================================

logo_path = Path("assets/logo.png")


# ============================================================
# CARGA DEL MODELO
# ============================================================

@st.cache_resource
def cargar_encoder():
    return obtener_encoder()


# ============================================================
# LOGIN
# ============================================================

def login():

    st.markdown(
        """
        <style>

        .login-wrapper {
            min-height: 75vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }

        .login-card {
            width: 420px;
            padding: 40px;
            border-radius: 18px;
            border: 1px solid #E5E7EB;
            background: white;
            box-shadow: 0 10px 35px rgba(0,0,0,0.08);
        }

        .login-title {
            text-align: center;
            font-size: 28px;
            font-weight: 700;
            color: #111827;
            margin-bottom: 8px;
        }

        .login-subtitle {
            text-align: center;
            color: #6B7280;
            margin-bottom: 30px;
        }

        </style>
        """,
        unsafe_allow_html=True
    )

    st.markdown(
        """
        <div class="login-wrapper">
            <div class="login-card">
                <div class="login-title">
                    Licitaciones & Empresas
                </div>

                <div class="login-subtitle">
                    Accede a tu plataforma
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    # Usamos columnas para colocar el formulario centrado
    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:

        usuario = st.text_input(
            "Usuario",
            key="login_usuario"
        )

        password = st.text_input(
            "Contraseña",
            type="password",
            key="login_password"
        )

        if st.button(
            "Iniciar sesión",
            type="primary",
            use_container_width=True
        ):

            if (
                usuario == st.secrets["LOGIN_USER"]
                and password == st.secrets["LOGIN_PASSWORD"]
            ):

                st.session_state["logueado"] = True

                # Activamos la pantalla de carga
                st.session_state["cargando_app"] = True

                # Ejecutamos inmediatamente el siguiente estado
                st.rerun()

            else:
                st.error("Usuario o contraseña incorrectos.")


# ============================================================
# PANTALLA DE CARGA
# ============================================================

def mostrar_pantalla_carga():

    st.markdown(
        """
        <style>

        .loading-container {
            min-height: 75vh;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            text-align: center;
        }

        .loading-title {
            font-size: 26px;
            font-weight: 700;
            color: #111827;
            margin-top: 20px;
        }

        .loading-subtitle {
            font-size: 15px;
            color: #6B7280;
            margin-top: 8px;
        }

        .loading-logo {
            max-width: 180px;
            max-height: 100px;
            object-fit: contain;
        }

        </style>
        """,
        unsafe_allow_html=True
    )

    logo_html = ""

    if logo_path.exists():

        import base64

        with open(logo_path, "rb") as f:
            logo_base64 = base64.b64encode(f.read()).decode()

        logo_html = f"""
            <img
                class="loading-logo"
                src="data:image/png;base64,{logo_base64}"
            >
        """

    st.markdown(
        f"""
        <div class="loading-container">

            {logo_html}

            <div class="loading-title">
                Accediendo...
            </div>

            <div class="loading-subtitle">
                Preparando tu plataforma
            </div>

        </div>
        """,
        unsafe_allow_html=True
    )


# ============================================================
# FLUJO DE LOGIN
# ============================================================

if not st.session_state.get("logueado", False):

    login()

    st.stop()


# ============================================================
# CARGA DE LA APLICACIÓN
# ============================================================

if st.session_state.get("cargando_app", False):

    mostrar_pantalla_carga()

    # IMPORTANTE:
    # El spinner nativo de Streamlit sí puede mantenerse visible
    # mientras se ejecuta este bloque.

    with st.spinner("Preparando tu plataforma..."):

        inicio = time.time()

        # Cargar modelo
        encoder = cargar_encoder()

        # Conectar con Supabase
        supabase = obtener_cliente()

        # Queremos que la pantalla permanezca visible
        # al menos 5 segundos en total.
        tiempo_transcurrido = time.time() - inicio

        tiempo_restante = 5 - tiempo_transcurrido

        if tiempo_restante > 0:
            time.sleep(tiempo_restante)

    # Ya terminó la carga
    st.session_state["cargando_app"] = False

    # Entramos a la aplicación
    st.rerun()


# ============================================================
# APLICACIÓN PRINCIPAL
# ============================================================

aplicar_estilos()


# ============================================================
# CONEXIONES / RECURSOS
# ============================================================

supabase = obtener_cliente()

encoder = cargar_encoder()


# ============================================================
# CABECERA
# ============================================================

col1, col2 = st.columns([8, 1])

with col1:

    st.markdown(
        """
        <h1 style="
            margin-bottom: 0;
            color: #111827;
        ">
            Licitaciones & Empresas
        </h1>
        """,
        unsafe_allow_html=True
    )

with col2:

    if st.button(
        "Cerrar sesión",
        use_container_width=True
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
        "Directorio de Empresas"
    ]
)


# ============================================================
# TAB 1
# ============================================================

with tab1:

    search.render(
        supabase=supabase,
        encoder=encoder
    )


# ============================================================
# TAB 2
# ============================================================

with tab2:

    matching.render(
        supabase=supabase,
        encoder=encoder
    )


# ============================================================
# TAB 3
# ============================================================

with tab3:

    directorio.render(
        supabase=supabase
    )

import hmac
from pathlib import Path

import streamlit as st

from styles import aplicar_estilos


st.set_page_config(
    page_title="Licitaciones & Empresas",
    page_icon="",
    layout="wide",
)


# ============================================================
# LOGIN
# ------------------------------------------------------------
# POR QUÉ ANTES SE SOLAPABA CON LA APP
#
# Streamlit no "borra" la pantalla al relanzar el script: cada
# elemento nuevo REEMPLAZA al que ocupaba su misma posición en la
# ejecución anterior, y lo que sobra se queda en pantalla (atenuado)
# hasta que el script termina. El login emitía 5 elementos sueltos
# (estilos, 2 espacios, columnas y logo); tras st.rerun() la app
# principal solo sustituía los primeros, y la tarjeta del login y el
# logo seguían visibles durante toda la carga del modelo.
#
# LA SOLUCIÓN
#
#   1. Toda la pantalla de login (CSS incluido) es UN SOLO elemento
#      raíz (un st.container). En cuanto la app principal emite su
#      primer elemento, ese único elemento se reemplaza entero y el
#      login desaparece al instante, sin restos.
#   2. Todo el CSS del login vive dentro de ese mismo elemento: se
#      elimina con él y no puede afectar a la app principal (antes,
#      `div[data-testid="stImage"]` con position: fixed alcanzaba a
#      cualquier imagen mientras el estilo seguía en pantalla).
#   3. Los módulos pesados (sentence-transformers, supabase...) se
#      importan DESPUÉS de comprobar el login, y el modelo se precarga
#      mientras el usuario escribe sus credenciales: el login aparece
#      al instante incluso en frío y, al entrar, el modelo ya está en
#      caché (st.cache_resource) en vez de cargarse con el login a
#      medio desmontar.
# ============================================================

# True: mientras se muestra el login se carga en segundo plano (en la
# propia ejecución, ya con el formulario pintado) el modelo de IA. Pon
# False si prefieres que se cargue solo al iniciar sesión.
PRECARGAR_MODELO_EN_LOGIN = True

RUTA_LOGO = Path(__file__).resolve().parent.parent / "assets" / "logo.png"

# Todo este CSS se emite DENTRO del contenedor del login (ver
# _pantalla_login), así que desaparece con él al entrar en la app.
CSS_LOGIN = """
/* Franja superior */
header[data-testid="stHeader"] {
    background-color: #172554 !important;
    height: 60px !important;
}

/* Iconos y textos de la barra superior legibles sobre el azul marino */
header[data-testid="stHeader"] [data-testid="stToolbar"],
header[data-testid="stHeader"] [data-testid="stToolbar"] * {
    color: #FFFFFF !important;
}
header[data-testid="stHeader"] [data-testid="stToolbar"] button:hover {
    background-color: rgba(255, 255, 255, 0.14) !important;
}

/* Fondo liso, sin degradados */
.stApp {
    background-color: #F3F5FB;
}

/* La tarjeta es el propio formulario */
[data-testid="stForm"] {
    background-color: #FFFFFF;
    border: 1px solid #D5DCEF;
    border-radius: 14px;
    padding: 2rem 2rem 1.5rem 2rem;
}

/* Logo centrado dentro de la tarjeta (acotado al formulario) */
[data-testid="stForm"] [data-testid="stFullScreenFrame"] {
    display: flex;
    justify-content: center;
}

/* Etiquetas de los campos */
[data-testid="stForm"] label p {
    color: #172033;
    font-weight: 600;
}

/* Campos con colores fijos: la tarjeta es blanca también si el navegador está en modo oscuro.
   (Se listan el selector actual de Streamlit y el antiguo `data-baseweb`, por compatibilidad.) */
[data-testid="stForm"] [data-testid="stTextInputRootElement"],
[data-testid="stForm"] div[data-baseweb="input"],
[data-testid="stForm"] div[data-baseweb="base-input"] {
    background-color: #F0F2F6 !important;
    border: 1px solid #D5DCEF !important;
}
[data-testid="stForm"] input {
    color: #172033 !important;
    -webkit-text-fill-color: #172033;
    caret-color: #172033;
}
[data-testid="stForm"] input::placeholder {
    color: #667085 !important;
    -webkit-text-fill-color: #667085;
    opacity: 1;
}
[data-testid="stForm"] [data-testid="stTextInputRootElement"] button,
[data-testid="stForm"] [data-testid="stTextInputRootElement"] svg {
    color: #172033 !important;
    fill: #172033;
}

/* Foco visible en los campos */
[data-testid="stForm"] [data-testid="stTextInputRootElement"]:focus-within,
[data-testid="stForm"] div[data-baseweb="input"]:focus-within {
    border-color: #315EFB !important;
    box-shadow: 0 0 0 1px #315EFB;
}

/* Quita el aviso "Press Enter to submit form" */
[data-testid="stForm"] [data-testid="InputInstructions"] {
    display: none;
}

/* Botón Iniciar sesión (a todo el ancho sin usar parámetros obsoletos) */
[data-testid="stElementContainer"]:has([data-testid="stFormSubmitButton"]),
.element-container:has([data-testid="stFormSubmitButton"]),
[data-testid="stFormSubmitButton"],
[data-testid="stFormSubmitButton"] button {
    width: 100% !important;
}
[data-testid="stFormSubmitButton"] button {
    background-color: #315EFB;
    border: none;
    border-radius: 8px;
    min-height: 45px;
}
[data-testid="stFormSubmitButton"] button p {
    color: #FFFFFF;
    font-weight: 600;
}
[data-testid="stFormSubmitButton"] button:hover {
    background-color: #2449D8;
}
[data-testid="stFormSubmitButton"] button:focus-visible {
    outline: 2px solid #172554;
    outline-offset: 2px;
}
"""

CABECERA_LOGIN_HTML = """
<div role="heading" aria-level="1" style="
    text-align: center;
    font-size: 28px;
    font-weight: 700;
    line-height: 1.25;
    color: #172554;
    margin: 12px 0 4px 0;
">
    Licitaciones & Empresas
</div>

<p style="
    text-align: center;
    color: #667085;
    margin: 0 0 12px 0;
">
    Accede a tu plataforma
</p>
"""


def _leer_secretos_login():
    """(usuario, contraseña) de los secrets de la app, o None si no están configurados."""
    try:
        return str(st.secrets["LOGIN_USER"]), str(st.secrets["LOGIN_PASSWORD"])
    except (KeyError, FileNotFoundError):
        return None


def _credenciales_correctas(usuario: str, password: str, usuario_ok: str, password_ok: str) -> bool:
    """Comparación en tiempo constante (y segura con ñ, acentos y demás caracteres no ASCII)."""
    # `&` (no `and`) para que las dos comparaciones se evalúen siempre.
    return hmac.compare_digest(usuario.encode("utf-8"), usuario_ok.encode("utf-8")) & hmac.compare_digest(
        password.encode("utf-8"), password_ok.encode("utf-8")
    )


def _pantalla_login():
    """
    Pinta el login como UN ÚNICO elemento raíz (ver explicación arriba).
    No emitas nada fuera de este contenedor mientras el usuario no esté
    identificado: cada elemento raíz extra reaparecería como resto en
    pantalla al entrar en la aplicación.
    """
    with st.container():

        st.markdown(f"<style>{CSS_LOGIN}</style>", unsafe_allow_html=True)

        _, centro, _ = st.columns([1, 1.4, 1])

        with centro:

            with st.form("form_login", clear_on_submit=False):

                if RUTA_LOGO.exists():
                    st.image(str(RUTA_LOGO), width=150)

                st.markdown(CABECERA_LOGIN_HTML, unsafe_allow_html=True)

                usuario = st.text_input(
                    "Usuario",
                    placeholder="Introduce tu usuario",
                )

                password = st.text_input(
                    "Contraseña",
                    type="password",
                    placeholder="Introduce tu contraseña",
                )

                entrar = st.form_submit_button("Iniciar sesión", type="primary")

                if entrar:

                    secretos = _leer_secretos_login()

                    if secretos is None:

                        st.error(
                            "Falta configurar LOGIN_USER y LOGIN_PASSWORD en los secrets de la aplicación."
                        )

                    elif _credenciales_correctas(usuario, password, *secretos):

                        st.session_state["logueado"] = True

                        st.rerun()

                    else:

                        st.error("Usuario o contraseña incorrectos.")


def _precargar_modelo():
    """Calienta la caché del modelo (st.cache_resource) sin mostrar nada en pantalla."""
    try:
        from db import obtener_encoder

        obtener_encoder()
    except Exception:
        # Si falla, no se rompe el login: el error saldrá, con su traza
        # normal, al entrar en la aplicación (que vuelve a intentarlo).
        pass


def login():

    if st.session_state.get("logueado", False):
        return True

    _pantalla_login()

    if PRECARGAR_MODELO_EN_LOGIN:
        _precargar_modelo()

    return False


# ============================================================
# COMPROBAR LOGIN
# ============================================================

if not login():
    st.stop()


# ============================================================
# IMPORTS PESADOS: solo cuando el usuario ya ha entrado
# (y ya cacheados si el modelo se precargó durante el login)
# ============================================================

from db import obtener_cliente, obtener_encoder

import directorio
import estadisticas
import matching
import search


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

tab1, tab2, tab3, tab4 = st.tabs([
    "Buscador de Licitaciones",
    "Coincidencia Inteligente",
    "Directorio de Empresas",
    "Estadísticas",
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


with tab4:

    estadisticas.render_tab4(
        supabase,
    )

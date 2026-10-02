import streamlit as st


def mostrar_login(supabase):
    """
    Muestra el formulario de login/registro.
    Devuelve True si el usuario está autenticado.
    """

    # Ya está autenticado
    if st.session_state.get("autenticado", False):
        return True

    st.markdown(
        """
        <div style="text-align: center; margin-top: 60px;">
            <h1>🔐 Licitaciones & Empresas</h1>
            <p>Inicia sesión para acceder a la aplicación.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tab_login, tab_registro = st.tabs(["Iniciar sesión", "Crear cuenta"])

    # ---------------------------------------------------------
    # LOGIN
    # ---------------------------------------------------------
    with tab_login:
        with st.form("login_form"):
            email = st.text_input(
                "Email",
                placeholder="tu@email.com",
            )

            password = st.text_input(
                "Contraseña",
                type="password",
            )

            submitted = st.form_submit_button(
                "Iniciar sesión",
                use_container_width=True,
            )

        if submitted:
            if not email or not password:
                st.error("Introduce tu email y contraseña.")
                return False

            try:
                response = supabase.auth.sign_in_with_password(
                    {
                        "email": email.strip(),
                        "password": password,
                    }
                )

                if response.user:
                    st.session_state["autenticado"] = True
                    st.session_state["usuario"] = response.user
                    st.rerun()

            except Exception as e:
                st.error("Email o contraseña incorrectos.")

    # ---------------------------------------------------------
    # REGISTRO
    # ---------------------------------------------------------
    with tab_registro:
        with st.form("registro_form"):
            email_registro = st.text_input(
                "Email",
                placeholder="tu@email.com",
                key="registro_email",
            )

            password_registro = st.text_input(
                "Contraseña",
                type="password",
                key="registro_password",
            )

            password_confirmacion = st.text_input(
                "Repite la contraseña",
                type="password",
                key="registro_password_confirmacion",
            )

            submitted_registro = st.form_submit_button(
                "Crear cuenta",
                use_container_width=True,
            )

        if submitted_registro:
            if not email_registro or not password_registro:
                st.error("Completa todos los campos.")
                return False

            if password_registro != password_confirmacion:
                st.error("Las contraseñas no coinciden.")
                return False

            if len(password_registro) < 6:
                st.error("La contraseña debe tener al menos 6 caracteres.")
                return False

            try:
                response = supabase.auth.sign_up(
                    {
                        "email": email_registro.strip(),
                        "password": password_registro,
                    }
                )

                # Si Supabase requiere confirmación de email,
                # normalmente session será None.
                if response.user and response.session:
                    st.session_state["autenticado"] = True
                    st.session_state["usuario"] = response.user
                    st.rerun()
                else:
                    st.success(
                        "Cuenta creada. Revisa tu email para confirmar la cuenta."
                    )

            except Exception as e:
                st.error(f"No se pudo crear la cuenta: {e}")

    return False


def cerrar_sesion(supabase):
    """Cierra la sesión actual."""

    try:
        supabase.auth.sign_out()
    except Exception:
        pass

    st.session_state.pop("autenticado", None)
    st.session_state.pop("usuario", None)

    st.rerun()

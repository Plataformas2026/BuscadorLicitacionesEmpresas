"""
styles.py
---------
CSS compartido por toda la app: mantiene la misma línea visual que el
resto de apps de esta familia (botón azul, tarjetas con borde suave),
pero más compacta -- tipografía reducida, inputs/selectores con menos
alto, y menos margen vertical entre bloques -- pensada para una app de
3 pestañas donde cabe más información por pantalla.
"""
import streamlit as st


def aplicar_estilos():
    st.markdown(
        """
        <style>
            /* Tipografía base más pequeña en toda la app */
            html, body, [class*="css"] {
                font-size: 14px;
            }
            h1 { font-size: 1.5rem !important; }
            h2 { font-size: 1.2rem !important; }
            h3 { font-size: 1.05rem !important; }

            /* Menos aire entre bloques verticales */
            div.block-container {
                padding-top: 1.5rem;
                padding-bottom: 2rem;
            }
            div[data-testid="stVerticalBlock"] > div {
                gap: 0.4rem;
            }

            /* Inputs, selects y multiselects compactos */
            div[data-baseweb="select"] > div,
            div[data-baseweb="input"] input,
            .stTextInput input,
            .stNumberInput input {
                min-height: 34px !important;
                font-size: 13px !important;
            }
            div[data-baseweb="tag"] {
                font-size: 12px !important;
            }

            /* Botón principal: mismo azul corporativo de siempre */
            div.stButton > button:first-child {
                background-color: #0066cc;
                color: white;
                font-weight: 600;
                font-size: 13px;
                padding: 0.45rem 1rem;
                border-radius: 8px;
                border: none;
                width: 100%;
                box-shadow: 0 2px 4px rgba(0, 0, 0, 0.08);
                transition: all 0.2s ease;
            }
            div.stButton > button:first-child:hover {
                background-color: #0052a3;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.12);
                color: white;
            }

            /* Pestañas más compactas */
            button[data-baseweb="tab"] {
                font-size: 13px;
                font-weight: 600;
                padding: 0.5rem 1rem;
            }

            /* Tarjeta genérica (usada en el directorio de empresas) */
            .tarjeta {
                background-color: #f8f9fa;
                border: 1px solid #e2e2e2;
                border-radius: 10px;
                padding: 12px 14px;
                margin-bottom: 8px;
            }
            .tarjeta:hover {
                border-color: #0066cc;
                box-shadow: 0 2px 6px rgba(0, 102, 204, 0.12);
            }
            .tarjeta-titulo {
                font-weight: 700;
                font-size: 13.5px;
                margin-bottom: 2px;
            }
            .tarjeta-subtitulo {
                font-size: 12px;
                color: #5b6470;
            }
            .chip {
                display: inline-block;
                background-color: #e8f0fe;
                color: #0052a3;
                border-radius: 999px;
                padding: 1px 9px;
                font-size: 11px;
                margin: 2px 4px 0 0;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )

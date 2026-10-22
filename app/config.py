"""
config.py
---------
Configuración y constantes compartidas por las 3 pestañas de la app.
"""
import os

import streamlit as st


def _config(clave: str, por_defecto: str = "") -> str:
    try:
        valor = st.secrets.get(clave)
        if valor:
            return valor
    except Exception:
        pass
    return os.getenv(clave, por_defecto)


# La app SOLO lee datos: usa siempre la clave ANÓNIMA de Supabase. La
# Service Role Key (con permisos de escritura) vive exclusivamente en los
# GitHub Actions de ingesta — ver ingest/common.py.
SUPABASE_URL = _config("SUPABASE_URL")
SUPABASE_ANON_KEY = _config("SUPABASE_ANON_KEY")

# Justificación de coincidencias con IA (Pestaña 2, capa tipo RAG sobre la
# lógica de coincidencias ya existente) -- ver app/ia_explicacion.py.
# Groq: capa gratuita real, sin tarjeta de crédito (30 peticiones/min,
# 14.400/día en el momento de escribir esto), API compatible con la de
# OpenAI. Si no se configura la clave, la app sigue funcionando igual que
# hasta ahora (la Pestaña 2 no ofrece el botón de justificación con IA,
# pero toda la lógica de coincidencias determinista sigue intacta).
GROQ_API_KEY = _config("GROQ_API_KEY")
# GROQ_MODEL es el nombre recomendado; GROQ_MODELO se mantiene por compatibilidad.
GROQ_MODEL = _config("GROQ_MODEL") or _config("GROQ_MODELO", "openai/gpt-oss-20b")
GROQ_MODELO = GROQ_MODEL


def _entero(clave: str, por_defecto: int) -> int:
    try:
        return int(str(_config(clave, str(por_defecto))).strip())
    except ValueError:
        return por_defecto


# Reranking con Groq (segunda fase de «Coincidencia Inteligente», app/reranking_groq.py).
# Los límites del plan gratuito dependen del modelo y pueden cambiar: por eso son ajustables.
GROQ_RERANK_MAX_EMPRESAS = _entero("GROQ_RERANK_MAX_EMPRESAS", 15)   # candidatas que se analizan (mejores de los rankings)
GROQ_RERANK_TPM = _entero("GROQ_RERANK_TPM", 8000)                      # tokens/minuto del plan (entrada + salida de una petición)
GROQ_RERANK_MAX_ENTRADA_TOKENS = _entero("GROQ_RERANK_MAX_ENTRADA_TOKENS", 4800)
GROQ_RERANK_MAX_SALIDA_TOKENS = _entero("GROQ_RERANK_MAX_SALIDA_TOKENS", 3000)
GROQ_RERANK_MIN_RECOMENDADA = _entero("GROQ_RERANK_MIN_RECOMENDADA", 50)  # puntuación mínima para "recomendada"
GROQ_RERANK_TIMEOUT = _entero("GROQ_RERANK_TIMEOUT", 90)
GROQ_RERANK_REASONING = _config("GROQ_RERANK_REASONING", "low")  # low | medium | high ("" = no enviar)

MODELO_EMBEDDING = "intfloat/multilingual-e5-small"  # 384 dimensiones

FUENTES_LICITACIONES = [
    "AECID",
    "AfDB",
    "AFD",
    "BCIE",
    "BEO/IADB",
    "BID",
    "BID for the Americas APP",
    "CAF",
    "Cabo Verde - Global Tenders",
    "EC-INTPA",
    "Enabel",
    "GIZ-Satellite",
    "LuxDev",
    "PLACSP",
    "Service-Bund",
    "TED",
    "UGPE",
    "UNDP",
    "UNGM",
    "World Bank",
]

TIPOS_EMPRESA = ["Pública", "Privada"]

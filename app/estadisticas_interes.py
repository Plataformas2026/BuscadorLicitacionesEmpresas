"""
estadisticas_interes.py
-----------------------
Pestaña «Estadísticas de interés»: las mismas gráficas y tablas que
«Estadísticas de adjudicación» (`estadisticas.py`), pero alimentadas por el
Excel de licitaciones INFORMADAS a las empresas.

ORIGEN DE LOS DATOS
-------------------
El Excel «Empresas_informadas» (hoja «SEGUIMIENTO 2026») está en Google
Drive. `ingest/sync_licitaciones_informadas_drive.py` lo carga cada día en la
tabla `licitaciones_informadas` de Supabase (se crea con
`sql/crear_licitaciones_informadas.sql`), y esta pestaña lee de ahí con el
mismo cliente de Supabase que el resto de la app.

QUÉ SIGNIFICA «ÉXITO» AQUÍ
--------------------------
Éxito = que se le ha informado a una empresa de la licitación, y esta ha
mostrado interés y confirmado que encaja con su perfil. El campo que lo dice es
COMENTARIOS (en lugar de RESULTADO): `normalizacion_interes.py` lo lee y
decide si la respuesta de la empresa es de interés, de rechazo o no concluyente
(las filas sin comentario, es decir, sin respuesta registrada, no cuentan).
El desplegable del final de la pestaña muestra cómo se ha leído cada
redacción.

Correspondencia con la pestaña de adjudicación
----------------------------------------------
    EMPRESA                       -> empresa
    TITULO DE LA LICITACION       -> titulo (para las palabras clave)
    FECHA DE ENVÍO A LA EMPRESA   -> año y mes (filtros de año y de mes)
    COMENTARIOS                   -> resultado (interés confirmado / sin interés)
    ORGANISMO FINANCIADOR         -> organismo (gráfica por organismo)

Los nombres de empresa se limpian de espacios y mayúsculas; para unir además
variantes que son la misma empresa, añádelas a `ALIAS_EMPRESAS`.
"""
import pandas as pd
import streamlit as st
from supabase import Client

from estadisticas import render_estadisticas, render_interpretacion
from estadisticas_comun import PERFIL_INTERES, leer_paginado
from normalizacion_interes import clasificar_comentario

TABLA_INFORMADAS = "licitaciones_informadas"
COLUMNAS_INFORMADAS = "id, anio, mes, empresa, titulo, organismo_financiador, comentarios"

# Variantes de nombre que son la misma empresa (clave en mayúsculas -> nombre que se muestra).
ALIAS_EMPRESAS = {
    "CANARY TEK": "CANARYTEK",
    "RALEY" : "RALEY ESTUDIOS COSTEROS",
    "INNOVARIS" : "GRUPO INNOVARIS",
}

COLUMNAS = [
    "empresa", "titulo", "anio", "mes", "categoria", "regla", "resultado_original", "organismo_original",
]


def _nombre_empresa(texto) -> str:
    nombre = " ".join(str(texto or "").split())
    return ALIAS_EMPRESAS.get(nombre.upper(), nombre)


def construir_dataframe(filas: list) -> pd.DataFrame:
    """
    Filas de `licitaciones_informadas` -> DataFrame con TODAS las licitaciones informadas ya interpretadas.
    Columnas: empresa, titulo, anio (Int64), mes (Int64, 1-12), categoria (POSITIVO / NEGATIVO / IGNORADO), regla,
    resultado_original (COMENTARIOS), organismo_original.
    """
    registros = []
    for fila in filas:
        empresa = _nombre_empresa(fila.get("empresa"))
        if not empresa:
            continue
        categoria, regla = clasificar_comentario(fila.get("comentarios"))
        registros.append({
            "empresa": empresa,
            "titulo": (fila.get("titulo") or "").strip(),
            "anio": fila.get("anio"),
            "mes": fila.get("mes"),
            "categoria": categoria,
            "regla": regla,
            "resultado_original": (fila.get("comentarios") or "").strip(),
            "organismo_original": (fila.get("organismo_financiador") or "").strip(),
        })

    df = pd.DataFrame(registros, columns=COLUMNAS)
    df["anio"] = pd.to_numeric(df["anio"], errors="coerce").astype("Int64")
    df["mes"] = pd.to_numeric(df["mes"], errors="coerce").astype("Int64")
    return df


@st.cache_data(ttl=1800, show_spinner=False)
def cargar_informadas(_supabase: Client) -> tuple:
    filas = leer_paginado(lambda: _supabase.table(TABLA_INFORMADAS).select(COLUMNAS_INFORMADAS).order("id"))
    return construir_dataframe(filas), None


def _tabla_inexistente(error: Exception) -> bool:
    texto = str(error)
    return TABLA_INFORMADAS in texto and any(
        pista in texto for pista in ("PGRST205", "does not exist", "Could not find", "schema cache", "42P01")
    )


def _pie(df: pd.DataFrame, _extra):
    sin_comentario = int((df["resultado_original"] == "").sum())
    render_interpretacion(
        df,
        perfil=PERFIL_INTERES,
        campo="COMENTARIOS",
        explicacion=(
            f"{sin_comentario} sin comentario, es decir, todavía sin respuesta registrada, y el resto sin una señal "
            "clara de interés ni de rechazo, como «Respuesta de cortesía» o «Al final no se presentaron» a secas"
        ),
    )


def render_tab_interes(supabase: Client):
    """Pestaña «Estadísticas de interés»."""

    def cargar(cliente):
        try:
            return cargar_informadas(cliente)
        except Exception as error:
            if _tabla_inexistente(error):
                raise RuntimeError(
                    f"Falta la tabla `{TABLA_INFORMADAS}`. Ejecuta una vez `sql/crear_licitaciones_informadas.sql` en "
                    "el editor SQL de Supabase y lanza la sincronización diaria (Actions > Sincronizar licitaciones "
                    "informadas > Run workflow)."
                ) from error
            raise

    render_estadisticas(
        supabase,
        PERFIL_INTERES,
        cargar,
        titulo="Estadísticas de interés",
        descripcion=(
            ""
            + PERFIL_INTERES.definicion
            + " Solo cuentan las licitaciones con una respuesta clara de la empresa (columna COMENTARIOS): "
            "las que aún no tienen respuesta registrada no entran en los porcentajes."
        ),
        mensaje_vacio=(
            "Todavía no hay licitaciones informadas con una respuesta clara de la empresa (interés o rechazo) "
            "para mostrar."
        ),
        con_mes=True,
        pie=_pie,
    )

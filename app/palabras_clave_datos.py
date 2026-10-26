"""
palabras_clave_datos.py
-----------------------
Datos de «Coincidencia Inteligente»: las empresas de la tabla `empresas_palabras_clave` de Supabase, que la sincronización
(ingest/sync_palabras_clave_drive.py, workflow «Sincronizar palabras clave») llena desde Palabras_clave.xlsx de Google Drive.
Es la ÚNICA fuente de empresas de esa pestaña: no lee `empresas` (BBDD_Empresas.xlsx) ni `licitaciones_informadas`
(Seguimiento_Empresas.xlsx), y esas pestañas tampoco leen esta tabla.

Las columnas de la tabla ya tienen los nombres internos que usan los algoritmos (nombre_empresa, sector, palabras_clave,
proyectos_tipo, experiencia_paises, zona_geografica_interes, zonas_descartadas, ambito_geografico...), de modo que no hay
traducción de campos. Se añade `numero_interno` (= clave_empresa) porque así identifican a la empresa el ranking y GPT.
Este módulo no usa Streamlit.
"""
import re
import unicodedata

TABLA = "empresas_palabras_clave"
COLUMNAS = (
    "clave_empresa, nombre_empresa, sector, descripcion_sector, especialidad_referencias, descripcion_actividad, "
    "palabras_clave_referencias, palabras_clave_bbdd, palabras_clave_unificadas, palabras_clave, proyectos_tipo, "
    "experiencia_paises, zona_geografica_interes, zonas_descartadas, tamano, certificaciones, facturacion_anual, "
    "clientes_principales, ambito_geografico, importes, fecha_actualizacion"
)


def _norm(texto) -> str:
    texto = unicodedata.normalize("NFD", str(texto or "").casefold())
    return " ".join("".join(c for c in texto if unicodedata.category(c) != "Mn").split())


def a_linea(texto, separador: str = "; ") -> str:
    """Texto en una sola línea (los saltos de línea del Excel pasan a separadores)."""
    return separador.join(l.strip(" •·-•") for l in str(texto or "").split("\n") if l.strip(" •·-•"))


def trocear(texto, maximo: int = 60) -> list:
    """Lista de frases de un campo de palabras clave (separadas por coma, punto y coma, viñeta o salto de línea)."""
    vistas, resultado = set(), []
    for parte in re.split(r"[,;\n•·]+", str(texto or "")):
        parte = " ".join(parte.split()).strip(" .-–—:")
        clave = _norm(parte)
        if len(parte) >= 3 and clave not in vistas:
            vistas.add(clave)
            resultado.append(parte)
    return resultado[:maximo]


def a_empresa(fila: dict) -> dict:
    """Fila de `empresas_palabras_clave` -> empresa con las claves que usan los algoritmos."""
    empresa = dict(fila)
    empresa["numero_interno"] = str(fila["clave_empresa"])
    # Alias con los nombres de campo que ya usan los informes (sin duplicar datos)
    empresa["paises_interes"] = fila.get("zona_geografica_interes")
    empresa["notas_libres"] = fila.get("importes")
    empresa["observaciones_importantes"] = None
    return empresa


def cargar_empresas(supabase) -> list:
    """
    Todas las empresas de la tabla, ordenadas como en el Excel. Lanza ValueError con un mensaje claro si la tabla no existe,
    no es accesible o está vacía (la sincronización aún no se ha ejecutado).
    """
    filas, inicio = [], 0
    try:
        while True:
            lote = (supabase.table(TABLA).select(COLUMNAS).order("id").range(inicio, inicio + 999).execute().data or [])
            filas.extend(lote)
            if len(lote) < 1000:
                break
            inicio += 1000
    except Exception as error:  # noqa: BLE001
        raise ValueError(
            f"No se pudo leer la tabla `{TABLA}` de Supabase ({type(error).__name__}). Ejecuta sql/crear_empresas_palabras_clave.sql "
            "y lanza el workflow «Sincronizar palabras clave»."
        ) from None
    if not filas:
        raise ValueError(
            f"La tabla `{TABLA}` está vacía: lanza el workflow «Sincronizar palabras clave» (GitHub Actions > Run workflow) "
            "para cargar Palabras_clave.xlsx."
        )
    return [a_empresa(f) for f in filas]

# -*- coding: utf-8 -*-
"""
archivos_drive.py
-----------------
File ID de Google Drive de los TRES Excel de la aplicación, en un único sitio. Cada Excel alimenta SOLO lo suyo:

    Excel                        File ID (constante)            Variable de entorno / secreto            Script -> tablas de Supabase                          Pestañas
    ---------------------------  -----------------------------  ---------------------------------------  ---------------------------------------------------  -------------------------------------------
    BBDD_Empresas.xlsx           FILE_ID_BBDD_EMPRESAS          GOOGLE_DRIVE_FILE_ID                     sync_empresas_drive.py -> empresas,                  Directorio de Empresas,
                                                                                                          empresas_referencias, empresas_hojas_extra           Estadísticas de adjudicación, Fiabilidad
    Seguimiento_Empresas.xlsx    FILE_ID_SEGUIMIENTO_EMPRESAS   GOOGLE_DRIVE_FILE_ID_INFORMADAS          sync_licitaciones_informadas_drive.py ->             Estadísticas de interés
                                                                                                          licitaciones_informadas
    Palabras_clave.xlsx          FILE_ID_PALABRAS_CLAVE         GOOGLE_DRIVE_FILE_ID_PALABRAS_CLAVE      sync_palabras_clave_drive.py ->                      Coincidencia Inteligente
                                                                                                          empresas_palabras_clave

El File ID es el trozo largo de la URL de Drive:  https://drive.google.com/file/d/ESTE_ES_EL_FILE_ID/view
(o  https://docs.google.com/spreadsheets/d/ESTE_ES_EL_FILE_ID/edit  si el archivo es una hoja de Google Sheets).

CÓMO SE ELIGE EL ID: si existe la variable de entorno (secreto de GitHub Actions), manda ella; si no, se usa la constante de
este archivo. Así los workflows que ya funcionan con sus secretos no cambian, y puedes pegar aquí los IDs si prefieres
tenerlos en el código (un File ID no es una credencial: sin la cuenta de servicio nadie puede leer el archivo).
"""
import os

# Pega aquí el File ID de cada archivo. BBDD_Empresas y Seguimiento_Empresas ya funcionan con sus secretos de GitHub
# (GOOGLE_DRIVE_FILE_ID y GOOGLE_DRIVE_FILE_ID_INFORMADAS), así que pueden quedarse vacíos aquí.
FILE_ID_BBDD_EMPRESAS = ""
FILE_ID_SEGUIMIENTO_EMPRESAS = ""
FILE_ID_PALABRAS_CLAVE = ""          # <- RELLENAR: File ID de Palabras_clave.xlsx (pasos en README.md, sección «Palabras clave»)


def file_id_de(variable_entorno: str, constante: str) -> str:
    """File ID a usar: el secreto/variable de entorno si existe; si no, la constante de este archivo ('' si ninguno)."""
    return (os.environ.get(variable_entorno) or "").strip() or (constante or "").strip()

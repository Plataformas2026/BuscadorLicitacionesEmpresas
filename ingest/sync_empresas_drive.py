# -*- coding: utf-8 -*-
"""
sync_empresas_drive.py
--------------------------
Sincroniza el Excel de empresas (BBDD_Empresas_YYMMDD.xlsx o como se
llame en cada momento) almacenado en Google Drive contra Supabase.
Pensado para ejecutarse periódicamente (ver
.github/workflows/sincronizar_empresas_drive.yml): en cada ejecución
comprueba la fecha de modificación del fichero en Drive contra la última
que tenemos guardada (tabla `sync_estado`); si no ha cambiado -- y la
lógica de ingesta tampoco (ver VERSION_INGESTA) --, no hace nada.

HOJAS DEL EXCEL Y TABLAS DESTINO
-----------------------------------
- "DOSSIER COMPLETO"          -> tabla `empresas`. Solo se extraen las
                                columnas indicadas por LETRA en
                                COLUMNAS_DOSSIER (A, C, D, G, H, I, J, K,
                                L, M, N, O, P, R, T, U, W, Y, AA..AH).
- "REFERENCIAS P BÚSQUEDAS"   -> tabla `empresas_referencias` (histórico
  y "HMS"                       de licitaciones de cada empresa; es lo
                                que lee la ficha del Directorio). La fila
                                completa de cada una queda en `datos_excel`.
- TODAS las demás pestañas    -> tabla `empresas_hojas_extra`: una fila
  (visibles y ocultas)          por fila de Excel, con TODAS sus columnas en
                                `datos_excel` (cabecera real -> valor).
                                Varias son vistas derivadas de "REFERENCIAS
                                P BÚSQUEDAS" (REFERENCIAS, REF AGUA, REF
                                TIC...): sus títulos ya presentes en
                                `empresas_referencias` se marcan como
                                duplicados y no se usan en el matching;
                                solo aportan los títulos que NO están ya.

COLUMNAS POR LETRA (DOSSIER COMPLETO)
-----------------------------------------
La selección de columnas se define por la letra de columna de Excel, tal
cual está en el Excel (las columnas ocultas B, E, F, Q, S, V, X y Z no se
extraen). Como una columna insertada o movida en el Excel desplazaría las
letras en silencio, antes de leer se comprueba que las cabeceras ancla de
ANCLAS_CABECERA siguen en su sitio; si no, la sincronización se detiene
SIN tocar Supabase (para saltarse la comprobación: IGNORAR_VALIDACION_CABECERAS=1).

POR QUÉ CASI TODO SE GUARDA COMO TEXTO/JSONB
-----------------------------------------------
Varias columnas son mucho más libres de lo que su nombre sugiere (p. ej.
"ÁMBITO GEOGRÁFICO DE OPERACIÓN" contiene en la práctica listas de países
en texto libre, y "RESULTADO" tiene decenas de redacciones distintas). Por
eso este script NO fuerza esos valores a un tipo/enum limpio: los guarda
tal cual y además guarda la fila completa (cabecera real -> valor real) en
`datos_excel`, como red de seguridad.

FORZAR UNA RESINCRONIZACIÓN
-----------------------------
Aunque el Excel no haya cambiado, se resincroniza completo cuando:
  - cambia VERSION_INGESTA (cambió la lógica: columnas, texto de los
    embeddings, tablas destino), o
  - se ejecuta con FORZAR_SINCRONIZACION=true (el workflow lo expone como
    la casilla "forzar" al lanzarlo a mano).

Si CUALQUIER carga a Supabase falla, el script termina con error y NO
guarda el estado de sincronización: la siguiente ejecución lo reintenta.

Variables de entorno requeridas:
    SUPABASE_URL, SUPABASE_SERVICE_KEY
    GOOGLE_SERVICE_ACCOUNT_JSON  -- contenido COMPLETO del JSON de la
                                    cuenta de servicio de Google (como
                                    secreto de GitHub Actions)
    GOOGLE_DRIVE_FILE_ID         -- ID del fichero Excel en Drive
Opcionales:
    FORZAR_SINCRONIZACION, IGNORAR_VALIDACION_CABECERAS

La cuenta de servicio debe tener el fichero compartido con ella (basta
con permiso de "Lector") -- ver README.md para el paso a paso.
"""
import io
import json
import os
import re
import time
import unicodedata
from datetime import date, datetime

import numpy as np
import openpyxl
import pandas as pd

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    subir_en_lotes,
)


CLAVE_SYNC_ESTADO = "empresas_drive_modified_time"
CLAVE_VERSION_INGESTA = "empresas_ingesta_version"
# Súbela cada vez que cambie la LÓGICA de ingesta (columnas extraídas, texto de
# los embeddings, tablas destino): fuerza una resincronización completa aunque
# el Excel de Drive no haya cambiado.
VERSION_INGESTA = "2026-10-01.v2"
TAMANO_LOTE_SUPABASE = 20
TAMANO_LOTE_INSERCION = 200

HOJA_EMPRESAS = "DOSSIER COMPLETO"
FILA_CABECERA_EMPRESAS = 0          # cabecera en la 1ª fila

HOJA_REFERENCIAS = "REFERENCIAS P BÚSQUEDAS"
FILA_CABECERA_REFERENCIAS = 1       # cabecera en la 2ª fila (la 1ª va vacía en el Excel real)

# HMS: misma tabla destino que REFERENCIAS P BÚSQUEDAS (empresas_referencias),
# mismas cabeceras de columna exactas -- solo cambia la hoja y la fila de
# cabecera. La columna "EMPRESA" de esta hoja vale literalmente "HMS" en
# todas las filas: la búsqueda por prefijo ya existente
# (_buscar_empresa_por_prefijo_o_alias) la enlaza sola con "HMS INTELLIGENCE"
# en 'DOSSIER COMPLETO' (nombre_idx.startswith(primera_palabra)), sin
# necesitar ningún caso especial.
HOJA_HMS = "HMS"
FILA_CABECERA_HMS = 9                # cabecera en la fila 10 (filas 1-9 vacías en el Excel real)

IDIOMAS_PALABRAS_CLAVE = ["es", "en", "fr", "pt"]

# ------------------------------------------------------------------
# DOSSIER COMPLETO: columnas que se extraen, por LETRA de Excel
# -> campo de la tabla `empresas` (None = solo va a `datos_excel`).
# ------------------------------------------------------------------
# OJO: la columna K (DESCRIPCIÓN BREVE DE LA ACTIVIDAD) NO estaba en la lista
# original de letras pedidas, pero es visible en el Excel y es el texto
# principal del perfil (matching, embeddings y ficha). Se ha mantenido; para
# excluirla, borra la línea "K" de este diccionario (descripcion_actividad
# quedará a NULL).
COLUMNAS_DOSSIER = {
    "A": "numero_interno",             # ID empresas TB
    "C": "sector",                     # SECTOR
    "D": "subsector",                  # SUBSECTOR
    "G": "nombre_empresa",             # EMPRESA
    "H": "cif",                        # CIF
    "I": "cnae",                       # CNAE
    "J": "web",                        # WEB
    "K": "descripcion_actividad",      # DESCRIPCIÓN BREVE DE LA ACTIVIDAD QUE DESARROLLA
    "L": "palabras_clave",             # PALABRAS CLAVE
    "M": "proyectos_tipo",             # PRINCIPALES PROYECTOS TIPO
    "N": "experiencia_paises",         # EXPERIENCIA PAÍSES
    "O": "zona_geografica_interes",    # ZONA GEOGRÁFICA y PAÍSES DE INTERÉS (unificadas en el Excel)
    "P": "paises_interes",             # PAÍSES DE INTERÉS
    "R": "tamano",                     # TAMAÑO (MICRO/PYME/GRANDE)
    "T": "certificaciones",            # CERTIFICACIONES (ISO, sectoriales, etc.)
    "U": "facturacion_anual",          # FACTURACIÓN ANUAL (USD/EUR)
    "W": "clientes_principales",       # CLIENTES PRINCIPALES
    "Y": "ambito_geografico",          # ÁMBITO GEOGRÁFICO DE OPERACIÓN
    "AA": "notas_libres",              # importe mínimo / máximo
    "AB": "contacto_nombre",           # NOMBRE
    "AC": "contacto_cargo",            # CARGO
    "AD": "contacto_email",            # E-MAIL
    "AE": "palabras_clave_en",         # PALABRAS CLAVE EN INGLÉS
    "AF": "palabras_clave_fr",         # PALABRAS CLAVE EN FRANCÉS
    "AG": "palabras_clave_pt",         # PALABRAS CLAVE EN PORTUGUÉS
    "AH": None,                        # (sin cabecera; vacía en el Excel actual)
}

# Columnas de `empresas` que dejan de rellenarse (vienen de columnas ocultas
# del Excel que ya no se extraen). Se envían a NULL para que la tabla refleje
# exactamente lo que hay en las columnas extraídas.
CAMPOS_EMPRESA_SIN_ORIGEN = (
    "tipo_empresa", "sector_secundario", "pais", "registro_oficial_proveedores",
    "experiencia_contratos_similares", "capacidad_tecnica", "preferencias_licitaciones",
)

# Comprobación anti-desplazamiento: letra -> inicio esperado de la cabecera
# (normalizada: minúsculas y sin tildes).
ANCLAS_CABECERA = {
    "C": "sector",
    "G": "empresa",
    "L": "palabras clave",
    "AE": "palabras clave en ingles",
}

MAPEO_REFERENCIAS = {
    "sector": "SECTOR",
    "nombre_empresa_excel": "EMPRESA",
    "tipo_proyecto": "TIPO PROYECTO",
    "pais": "PAÍS Country",
    "organismo_financiador": "ORGANISMO FINANCIADOR",
    "agencia_ejecutora": "AGENCIA EJECUTORA /Name of Client",
    "titulo": "TITULO (Assignment name)",
    "descripcion_trabajos": "BREVE DESCRIPCIÓN TRABAJOS",
    "fecha": "FECHA",
    "duracion": "Duración Duration of assignment (months):",
    "importe": "IMPORTE Approx. value of the contract (in current US$):",
    "resultado": "RESULTADO (ADJUDICADA/NO ADJUDICADA/SIN INFORMACIÓN)",
    "descripcion_empresa": "DESCRIPCIÓN DE LA EMPRESA",
    "palabras_clave": "PALABRAS CLAVE",
}

CAMPOS_LISTA_EMPRESAS = {
    "palabras_clave", "palabras_clave_en", "palabras_clave_fr", "palabras_clave_pt",
    "proyectos_tipo", "experiencia_paises", "zona_geografica_interes", "paises_interes",
}
# Campos geográficos: al trocear la lista no se corta por comas dentro de paréntesis.
CAMPOS_LISTA_CON_PARENTESIS = {"experiencia_paises", "zona_geografica_interes", "paises_interes"}
CAMPOS_COMPARABLES_EMPRESAS = (
    "nombre_empresa", "sector", "sector_secundario", "subsector", "tipo_empresa",
    "cif", "cnae", "web", "descripcion_actividad", "pais", "tamano",
    "ambito_geografico", "facturacion_anual",
)


# ------------------------------------------------------------------
# Mapeo explícito de alias (Variante normalizada -> Nombre exacto en Dossier Completo)
# ------------------------------------------------------------------
ALIAS_EMPRESAS = {
    "ctsi": "CANARIAS TECNOLÓGICA Y SI",
    "ctsi canarias tecnologica y sistemas de informacion": "CANARIAS TECNOLÓGICA Y SI",
    "canarias tecnologica y si": "CANARIAS TECNOLÓGICA Y SI",
    "canarias tecnologica y si": "CANARIAS TECNOLÓGICA Y SI",
    "ctsi (canarias tecnologica y sistemas de informacion)": "CANARIAS TECNOLÓGICA Y SI",
    "cocosolutions": "COCO SOLUTIONS",
    "coco solutions": "COCO SOLUTIONS",
    "grupo evm": "GRUPO EVM",
    "evm": "GRUPO EVM",
    "acosta ing y wet ingenieria": "GRUPO ACOSTA (ACOSTA ING Y WET INGENIERÍA)",
    "acosta": "GRUPO ACOSTA (ACOSTA ING Y WET INGENIERÍA)",
    "acosta wet": "GRUPO ACOSTA (ACOSTA ING Y WET INGENIERÍA)",
    "grupo acosta acosta ing y wet ingenieria": "GRUPO ACOSTA (ACOSTA ING Y WET INGENIERÍA)",
    "smart linking": "SMART LINKING",
    "smartlinking": "SMART LINKING",
    "axionet": "AXIONNET",
    "axionnet": "AXIONNET",
    "2raestudio ingenieria y arquitectura": "2RA STUDIO",
    "2ra studio": "2RA STUDIO",
}


# ------------------------------------------------------------------
# Google Drive
# ------------------------------------------------------------------
def obtener_servicio_drive():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credenciales_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not credenciales_json:
        raise RuntimeError("Falta la variable de entorno GOOGLE_SERVICE_ACCOUNT_JSON.")

    info = json.loads(credenciales_json)
    credenciales = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=credenciales)


def obtener_metadata_archivo(servicio, file_id: str) -> dict:
    return servicio.files().get(fileId=file_id, fields="modifiedTime, name, mimeType").execute()


def descargar_excel(servicio, file_id: str, mime_type: str) -> io.BytesIO:
    if mime_type == "application/vnd.google-apps.spreadsheet":
        contenido = servicio.files().export_media(
            fileId=file_id,
            mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ).execute()
    else:
        contenido = servicio.files().get_media(fileId=file_id).execute()
    return io.BytesIO(contenido)


# ------------------------------------------------------------------
# Estado de sincronización (tabla sync_estado)
# ------------------------------------------------------------------
def _leer_estado(supabase, clave: str):
    respuesta = supabase.table("sync_estado").select("valor").eq("clave", clave).limit(1).execute()
    return respuesta.data[0]["valor"] if respuesta.data else None


def _guardar_estado(supabase, clave: str, valor: str):
    supabase.table("sync_estado").upsert({"clave": clave, "valor": valor}, on_conflict="clave").execute()


def obtener_ultima_modificacion_conocida(supabase) -> str:
    return _leer_estado(supabase, CLAVE_SYNC_ESTADO)


def guardar_ultima_modificacion(supabase, valor: str):
    _guardar_estado(supabase, CLAVE_SYNC_ESTADO, valor)


# ------------------------------------------------------------------
# Utilidades de lectura/normalización
# ------------------------------------------------------------------
def _normalizar_cabecera(texto) -> str:
    texto = str(texto).strip().lower()
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def _normalizar_nombre_empresa(texto: str) -> str:
    """Normaliza un nombre de empresa eliminando tildes, mayúsculas, espacios extra y símbolos comunes."""
    if not texto:
        return ""
    t = str(texto).strip().lower()
    t = "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _es_nulo(valor) -> bool:
    if valor is None:
        return True
    try:
        return bool(pd.isna(valor))
    except (TypeError, ValueError):
        return False


def _valor_json_seguro(valor):
    """Convierte un valor de celda (que puede ser numpy/pandas) a algo serializable en JSON."""
    if _es_nulo(valor):
        return None
    if isinstance(valor, (pd.Timestamp, datetime, date)):
        return valor.isoformat()
    if isinstance(valor, np.integer):
        return int(valor)
    if isinstance(valor, np.floating):
        return float(valor)
    if isinstance(valor, np.bool_):
        return bool(valor)
    return valor


def _fila_a_json(fila: pd.Series) -> dict:
    """La fila COMPLETA del Excel, cabecera real -> valor real (ver docstring del módulo)."""
    return {str(clave): _valor_json_seguro(valor) for clave, valor in fila.items()}


def _valor_texto(fila: pd.Series, columna: str):
    if not columna or columna not in fila.index:
        return None
    valor = fila[columna]
    if _es_nulo(valor):
        return None
    if isinstance(valor, (pd.Timestamp, datetime)):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))  # evita "2018.0" cuando en realidad es un año
    texto = str(valor).strip()
    return texto if texto else None


def _dividir_lista(valor, respetar_parentesis: bool = False) -> list:
    if _es_nulo(valor):
        return []
    texto = str(valor).strip()
    if not texto:
        return []

    # el Excel usa indistintamente coma, punto y coma o salto de línea
    if respetar_parentesis:
        # Para los campos geográficos: no cortar por las comas que van DENTRO de
        # un paréntesis ("España (Tenerife, La Gomera)" es un solo elemento, no
        # dos fragmentos sueltos que luego darían falsas coincidencias de país).
        partes, actual, nivel = [], [], 0
        for caracter in texto:
            if caracter in "([{":
                nivel += 1
            elif caracter in ")]}":
                nivel = max(0, nivel - 1)
            if nivel == 0 and caracter in ",;\n":
                partes.append("".join(actual))
                actual = []
            else:
                actual.append(caracter)
        partes.append("".join(actual))
        if nivel == 0:  # paréntesis equilibrados; si no, se cae al corte simple de abajo
            return [p.strip() for p in partes if p.strip()]

    return [p.strip() for p in re.split(r"[,;\n]", texto) if p.strip()]


def _detectar_ocultos(buffer_excel: io.BytesIO, hoja: str) -> tuple:
    """
    Devuelve (columnas_ocultas, filas_ocultas): las letras de columna
    de Excel ("B", "E"...) y los números de fila de Excel marcados como
    ocultos en esa hoja. pandas no expone esta información -- hace
    falta leer el propio libro con openpyxl para consultarla.
    read_only=False a propósito: en modo solo lectura, openpyxl no
    siempre rellena column_dimensions/row_dimensions con la visibilidad
    real.
    """
    posicion_previa = buffer_excel.tell()
    buffer_excel.seek(0)
    libro = openpyxl.load_workbook(buffer_excel, read_only=False, data_only=True)
    hoja_wb = libro[hoja]

    columnas_ocultas = {
        letra for letra, dim in hoja_wb.column_dimensions.items() if dim.hidden
    }
    filas_ocultas = {
        numero for numero, dim in hoja_wb.row_dimensions.items() if dim.hidden
    }

    libro.close()
    buffer_excel.seek(posicion_previa)
    return columnas_ocultas, filas_ocultas


def _leer_hoja(
    buffer_excel: io.BytesIO, hoja: str, fila_cabecera: int,
    excluir_ocultos: bool = False, excluir_columnas_ocultas: bool = True,
) -> pd.DataFrame:
    df = pd.read_excel(buffer_excel, sheet_name=hoja, header=fila_cabecera, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]

    if not excluir_ocultos:
        return df

    columnas_ocultas, filas_ocultas = _detectar_ocultos(buffer_excel, hoja)

    if columnas_ocultas and excluir_columnas_ocultas:
        indices_ocultos = {
            openpyxl.utils.column_index_from_string(letra) - 1
            for letra in columnas_ocultas
        }
        nombres_columnas_ocultas = [
            df.columns[i] for i in sorted(indices_ocultos) if i < len(df.columns)
        ]
        if nombres_columnas_ocultas:
            df = df.drop(columns=nombres_columnas_ocultas)
            print(f"'{hoja}': se ignoran columnas ocultas del Excel: {nombres_columnas_ocultas}", flush=True)

    if filas_ocultas:
        # fila de Excel = índice de fila del DataFrame + fila_cabecera + 2
        # (fila_cabecera es 0-indexado, como el `header=` de pandas; +1
        # por la propia cabecera, +1 porque Excel empieza a contar en 1)
        indices_filas_ocultas = {
            numero_excel - fila_cabecera - 2 for numero_excel in filas_ocultas
        }
        filas_a_quitar = [i for i in indices_filas_ocultas if i in df.index]
        if filas_a_quitar:
            df = df.drop(index=filas_a_quitar)
            print(f"'{hoja}': se ignoran {len(filas_a_quitar)} filas ocultas del Excel.", flush=True)

    return df


# ------------------------------------------------------------------
# Lectura de "DOSSIER COMPLETO" -> empresas (columnas por LETRA)
# ------------------------------------------------------------------
def _validar_cabeceras_ancla(df: pd.DataFrame):
    """Detiene la ejecución si una columna insertada/movida en el Excel ha desplazado las letras."""
    if os.environ.get("IGNORAR_VALIDACION_CABECERAS", "").strip().lower() in {"1", "true", "yes", "si", "sí"}:
        return

    incidencias = []
    for letra, esperado in ANCLAS_CABECERA.items():
        indice = openpyxl.utils.column_index_from_string(letra) - 1
        encontrado = df.columns[indice] if indice < len(df.columns) else None
        if encontrado is None or not _normalizar_cabecera(encontrado).startswith(esperado):
            incidencias.append(f"columna {letra}: se esperaba una cabecera que empiece por '{esperado}' y hay {encontrado!r}")

    if incidencias:
        raise RuntimeError(
            f"La estructura de '{HOJA_EMPRESAS}' no coincide con COLUMNAS_DOSSIER (¿se ha insertado o "
            "movido una columna en el Excel?): " + " | ".join(incidencias) + ". No se ha tocado Supabase. "
            "Ajusta COLUMNAS_DOSSIER/ANCLAS_CABECERA, o define IGNORAR_VALIDACION_CABECERAS=1 para saltar esta comprobación."
        )


def leer_empresas(buffer_excel: io.BytesIO) -> list:
    # Columnas ocultas: NO se excluyen aquí (la selección es por letra, y quitar
    # columnas desplazaría las posiciones). Las filas ocultas sí se ignoran, como antes.
    df = _leer_hoja(
        buffer_excel, HOJA_EMPRESAS, FILA_CABECERA_EMPRESAS,
        excluir_ocultos=True, excluir_columnas_ocultas=False,
    )
    if df.empty:
        return []

    _validar_cabeceras_ancla(df)

    columnas_por_letra = {}
    for letra, clave in COLUMNAS_DOSSIER.items():
        indice = openpyxl.utils.column_index_from_string(letra) - 1
        if indice < len(df.columns):
            columnas_por_letra[letra] = df.columns[indice]
        else:
            print(
                f"Aviso: '{HOJA_EMPRESAS}' no tiene datos en la columna {letra} "
                f"({clave or 'sin campo asociado'}); se trata como vacía.",
                flush=True,
            )

    letra_numero_interno = next(l for l, c in COLUMNAS_DOSSIER.items() if c == "numero_interno")
    columna_numero_interno = columnas_por_letra.get(letra_numero_interno)
    if columna_numero_interno is None:
        raise RuntimeError(f"'{HOJA_EMPRESAS}' no tiene la columna {letra_numero_interno} (numero_interno).")

    empresas = []

    for _, fila in df.iterrows():
        numero_interno = _valor_texto(fila, columna_numero_interno)
        if not numero_interno:
            continue  # sin número interno no hay forma fiable de identificar la fila

        empresa = {}
        datos_excel = {}
        for letra, clave in COLUMNAS_DOSSIER.items():
            columna = columnas_por_letra.get(letra)
            if columna is None:
                if clave:
                    empresa[clave] = [] if clave in CAMPOS_LISTA_EMPRESAS else None
                continue

            if clave in CAMPOS_LISTA_EMPRESAS:
                empresa[clave] = _dividir_lista(fila.get(columna), respetar_parentesis=clave in CAMPOS_LISTA_CON_PARENTESIS)
            elif clave:
                empresa[clave] = _valor_texto(fila, columna)

            clave_json = columna if columna not in datos_excel else f"{columna} ({letra})"
            datos_excel[clave_json] = _valor_json_seguro(fila.get(columna))

        empresa["numero_interno"] = numero_interno
        for campo in CAMPOS_EMPRESA_SIN_ORIGEN:
            empresa[campo] = None
        empresa["datos_excel"] = datos_excel
        empresas.append(empresa)

    return empresas


# ------------------------------------------------------------------
# Lectura de "REFERENCIAS P BÚSQUEDAS" -> empresas_referencias
# ------------------------------------------------------------------
def _normalizar_resultado(texto: str) -> str:
    if not texto:
        return "desconocido"
    t = _normalizar_cabecera(texto)
    if any(p in t for p in ("no adjudicad", "no seleccionad", "eliminad", "no pasa", "rechazad", "descartad")):
        return "no_adjudicada"
    if "adjudicad" in t:  
        return "adjudicada"
    return "desconocido"


def _buscar_empresa_por_prefijo_o_alias(nombre_original: str, indice_nombres: dict) -> tuple:
    """Busca mediante alias explícitos, coincidencia exacta normalizada y prefijos inteligentes.
       Devuelve (numero_interno, motivo_falla)"""
    if not nombre_original:
        return None, "Nombre de empresa vacío en la referencia"

    nombre_norm = _normalizar_nombre_empresa(nombre_original)

    # 1. Comprobar si existe un alias explícito predefinido en ALIAS_EMPRESAS
    if nombre_norm in ALIAS_EMPRESAS:
        nombre_objetivo_real = _normalizar_nombre_empresa(ALIAS_EMPRESAS[nombre_norm])
        if nombre_objetivo_real in indice_nombres:
            return indice_nombres[nombre_objetivo_real], None
        else:
            return None, f"Alias encontrado ('{ALIAS_EMPRESAS[nombre_norm]}'), pero no existe en el índice de 'DOSSIER COMPLETO'"

    # 2. Coincidencia exacta normalizada
    if nombre_norm in indice_nombres:
        return indice_nombres[nombre_norm], None

    # 3. Coincidencia por prefijo o palabra principal (ej: ROMPEI ENERGY -> ROMPEI)
    palabras = nombre_norm.split()
    if not palabras:
        return None, "Nombre normalizado sin palabras"
    
    primera_palabra = palabras[0]
    if len(primera_palabra) < 3:  # Evita prefijos demasiado cortos como "la", "el", "de"
        if len(palabras) > 1:
            primera_palabra = f"{palabras[0]} {palabras[1]}"
        else:
            return None, f"Primera palabra demasiado corta y sin secundaria: '{primera_palabra}'"

    for nombre_idx, num_interno in indice_nombres.items():
        if nombre_idx.startswith(primera_palabra) or primera_palabra.startswith(nombre_idx):
            return num_interno, None

    return None, f"No se encontró coincidencia exacta, alias ni prefijo compatible para '{nombre_original}' (normalizado: '{nombre_norm}')"


# Campos que se muestran en la ficha de la empresa (ver app/directorio.py,
# tabla de "Referencias de licitaciones": SECTOR, TIPO PROYECTO, AGENCIA
# EJECUTORA, TITULO, FECHA, IMPORTE, RESULTADO). Si NINGUNO de estos tiene
# contenido, la fila del Excel solo traía el nombre de la empresa y nada
# mas -- no aporta ninguna referencia real, asi que se descarta (antes se
# guardaba igual y se contaba en el "N referencias" de la ficha, dando un
# recuento que no coincidia con las filas realmente visibles).
CAMPOS_REFERENCIA_MOSTRADOS = (
    "sector", "tipo_proyecto", "agencia_ejecutora", "titulo", "fecha", "importe", "resultado",
)


def _referencia_totalmente_vacia(referencia: dict) -> bool:
    return all(not referencia.get(campo) for campo in CAMPOS_REFERENCIA_MOSTRADOS)


def leer_referencias(
    buffer_excel: io.BytesIO,
    indice_nombres_empresa: dict,
    hoja: str = HOJA_REFERENCIAS,
    fila_cabecera: int = FILA_CABECERA_REFERENCIAS,
) -> list:
    """
    Lee una hoja de referencias/histórico de licitaciones con el mismo
    formato de columnas que "REFERENCIAS P BÚSQUEDAS" (ver
    MAPEO_REFERENCIAS) -- se usa tanto para esa hoja como para "HMS"
    (mismas cabeceras exactas, solo cambia la hoja y la fila de
    cabecera; ver HOJA_HMS/FILA_CABECERA_HMS). A diferencia de
    'DOSSIER COMPLETO', aquí NUNCA se excluyen filas/columnas ocultas
    -- se pidió explícitamente conservar todo lo de estas hojas, esté
    oculto o no en el Excel.
    """
    df = _leer_hoja(buffer_excel, hoja, fila_cabecera)
    if df.empty:
        return []

    faltantes = [c for c in MAPEO_REFERENCIAS.values() if c not in df.columns]
    if faltantes:
        print(f"Aviso: no se han encontrado estas columnas en '{hoja}': {faltantes}", flush=True)

    referencias = []
    sin_match = 0
    vacias_descartadas = 0

    for _, fila in df.iterrows():
        nombre_excel = _valor_texto(fila, MAPEO_REFERENCIAS["nombre_empresa_excel"])
        if not nombre_excel:
            continue  

        referencia = {"nombre_empresa_excel": nombre_excel}
        for clave, columna in MAPEO_REFERENCIAS.items():
            if clave == "nombre_empresa_excel":
                continue
            if clave == "palabras_clave":
                referencia[clave] = _dividir_lista(fila.get(columna)) if columna in df.columns else []
            else:
                referencia[clave] = _valor_texto(fila, columna) if columna in df.columns else None

        if _referencia_totalmente_vacia(referencia):
            vacias_descartadas += 1
            continue  # solo tenia el nombre de la empresa, ninguna informacion real de la licitacion

        referencia["resultado_normalizado"] = _normalizar_resultado(referencia.get("resultado"))
        referencia["datos_excel"] = _fila_a_json(fila)

        # Búsqueda inteligente con impresión de motivos detallados si falla
        numero_interno, motivo = _buscar_empresa_por_prefijo_o_alias(nombre_excel, indice_nombres_empresa)
        
        referencia["numero_interno"] = numero_interno
        if not numero_interno:
            sin_match += 1
            print(f"[SIN MATCH] Empresa en referencia ('{hoja}'): '{nombre_excel}' -> Motivo: {motivo}", flush=True)

        referencias.append(referencia)

    if vacias_descartadas:
        print(
            f"ℹ️ {vacias_descartadas} filas de '{hoja}' descartadas por no tener ningún "
            f"dato relleno salvo el nombre de la empresa.",
            flush=True,
        )

    if sin_match:
        print(
            f"ℹ️ {sin_match}/{len(referencias)} filas de '{hoja}' no se han podido enlazar "
            f"con ninguna empresa de '{HOJA_EMPRESAS}' por nombre (quedan con numero_interno=NULL, "
            f"pero se guardan igualmente).",
            flush=True,
        )

    return referencias


# ------------------------------------------------------------------
# Resto de pestañas -> empresas_hojas_extra (TODAS las columnas)
# ------------------------------------------------------------------
HOJAS_YA_TRATADAS = {HOJA_EMPRESAS, HOJA_REFERENCIAS, HOJA_HMS}

# Pestañas SIN fila de cabecera: la columna B lleva el nombre de la empresa
# (solo en la primera fila) y la columna C un título de proyecto por fila.
CONFIG_HOJAS_LISTA_TITULOS = {
    "REF ECOS": {"columna_empresa": "B", "columna_titulo": "C"},
    "REF TEMA": {"columna_empresa": "B", "columna_titulo": "C"},
    "REF CODEXCA": {"columna_empresa": "B", "columna_titulo": "C"},
    "REF ACOSTA WET": {"columna_empresa": "B", "columna_titulo": "C"},
}
LIMITE_FILAS_BUSQUEDA_CABECERA = 25   # la cabecera es la primera fila (de las N primeras) con >= 3 textos
MINIMO_CELDAS_CABECERA = 3


def _valor_celda_extra(valor):
    if _es_nulo(valor):
        return None
    if isinstance(valor, (datetime, pd.Timestamp)):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, float) and valor.is_integer():
        return int(valor)  # evita "2018.0" cuando en realidad es un año
    if isinstance(valor, str):
        return valor.strip() or None
    return valor


def _valor_en(valores: list, indice):
    if indice is None or indice < 0 or indice >= len(valores):
        return None
    return valores[indice]


def _leer_filas_hoja(hoja_wb) -> list:
    """[(nº de fila de Excel, [valores por columna desde la A])] -- coordenadas exactas, sin depender de pandas."""
    return [
        (numero, [_valor_celda_extra(v) for v in fila])
        for numero, fila in enumerate(hoja_wb.iter_rows(values_only=True), start=1)
    ]


def _detectar_fila_cabecera(filas: list):
    for numero, valores in filas[:LIMITE_FILAS_BUSQUEDA_CABECERA]:
        celdas = [v for v in valores if v is not None]
        if len(celdas) >= MINIMO_CELDAS_CABECERA and all(isinstance(v, str) for v in celdas):
            return numero
    return None


def _claves_unicas(cabecera: list) -> list:
    """Cabecera real de cada columna; si se repite, se le añade .1, .2...; si no tiene, 'Columna X'."""
    claves, vistos = [], {}
    for i, titulo in enumerate(cabecera):
        base = str(titulo).strip() if titulo is not None else f"Columna {openpyxl.utils.get_column_letter(i + 1)}"
        repeticiones = vistos.get(base, 0)
        vistos[base] = repeticiones + 1
        claves.append(base if repeticiones == 0 else f"{base}.{repeticiones}")
    return claves


def _registros_con_cabecera(hoja: str, filas: list, fila_cabecera: int) -> list:
    cabecera = filas[fila_cabecera - 1][1]
    claves = _claves_unicas(cabecera)
    indice_empresa = next(
        (i for i, t in enumerate(cabecera) if t is not None and _normalizar_cabecera(t) == "empresa"), None
    )
    indices_titulos = [
        i for i, t in enumerate(cabecera) if t is not None and _normalizar_cabecera(t).startswith("titulo")
    ]

    registros = []
    for numero, valores in filas[fila_cabecera:]:
        datos = {}
        for i, clave in enumerate(claves):
            valor = _valor_en(valores, i)
            if cabecera[i] is None:
                if valor is not None:  # columna sin cabecera: solo se guarda si trae dato
                    datos[clave] = valor
            else:
                datos[clave] = valor
        for i in range(len(cabecera), len(valores)):  # celdas a la derecha de la cabecera
            if valores[i] is not None:
                datos[f"Columna {openpyxl.utils.get_column_letter(i + 1)}"] = valores[i]

        nombre_empresa = _valor_en(valores, indice_empresa)
        clave_empresa = claves[indice_empresa] if indice_empresa is not None else None
        if nombre_empresa is not None and _normalizar_cabecera(nombre_empresa) == "empresa":
            continue  # fila que repite la cabecera dentro de los datos (p. ej. cada "Parte N" de 'lista para chatgpt')
        if not any(v is not None for k, v in datos.items() if k != clave_empresa):
            continue  # fila vacía, o solo con el nombre de la empresa: no aporta nada

        titulos = [str(valores[i]) for i in indices_titulos if _valor_en(valores, i) is not None]
        registros.append({
            "hoja": hoja, "fila_excel": numero,
            "nombre_empresa_excel": str(nombre_empresa) if nombre_empresa is not None else None,
            "titulos": titulos, "datos_excel": datos,
        })
    return registros


def _registros_lista_titulos(hoja: str, filas: list, columna_empresa: str, columna_titulo: str) -> list:
    indice_empresa = openpyxl.utils.column_index_from_string(columna_empresa) - 1
    indice_titulo = openpyxl.utils.column_index_from_string(columna_titulo) - 1

    registros, empresa_actual = [], None
    for numero, valores in filas:
        nombre = _valor_en(valores, indice_empresa)
        titulo = _valor_en(valores, indice_titulo)
        if nombre is not None:
            empresa_actual = str(nombre)  # solo aparece en la primera fila de cada bloque
        if titulo is None:
            continue
        registros.append({
            "hoja": hoja, "fila_excel": numero, "nombre_empresa_excel": empresa_actual,
            "titulos": [str(titulo)], "datos_excel": {"EMPRESA": empresa_actual, "TITULO": str(titulo)},
        })
    return registros


def _registros_sin_cabecera(hoja: str, filas: list) -> list:
    registros = []
    for numero, valores in filas:
        datos = {
            f"Columna {openpyxl.utils.get_column_letter(i + 1)}": v
            for i, v in enumerate(valores) if v is not None
        }
        if datos:
            registros.append({
                "hoja": hoja, "fila_excel": numero, "nombre_empresa_excel": None,
                "titulos": [], "datos_excel": datos,
            })
    return registros


def leer_hojas_extra(buffer_excel: io.BytesIO, indice_nombres_empresa: dict, titulos_principales: set) -> list:
    """
    Todas las pestañas salvo DOSSIER COMPLETO / REFERENCIAS P BÚSQUEDAS / HMS
    (visibles y ocultas), con TODAS sus columnas. Cada fila queda enlazada con
    su empresa por nombre (mismo criterio que las referencias) y con sus
    títulos marcados como "nuevos" si no aparecen ya en las referencias
    principales (`titulos_principales`: títulos normalizados).
    """
    buffer_excel.seek(0)
    libro = openpyxl.load_workbook(buffer_excel, read_only=False, data_only=True)
    registros = []
    try:
        for nombre_hoja in libro.sheetnames:
            if nombre_hoja in HOJAS_YA_TRATADAS:
                continue
            hoja_wb = libro[nombre_hoja]
            filas = _leer_filas_hoja(hoja_wb)

            if nombre_hoja in CONFIG_HOJAS_LISTA_TITULOS:
                config = CONFIG_HOJAS_LISTA_TITULOS[nombre_hoja]
                de_la_hoja, modo = _registros_lista_titulos(nombre_hoja, filas, **config), "lista de títulos"
            else:
                fila_cabecera = _detectar_fila_cabecera(filas)
                if fila_cabecera:
                    de_la_hoja, modo = _registros_con_cabecera(nombre_hoja, filas, fila_cabecera), f"cabecera en fila {fila_cabecera}"
                else:
                    de_la_hoja, modo = _registros_sin_cabecera(nombre_hoja, filas), "sin cabecera (columnas por letra)"

            estado = "oculta" if hoja_wb.sheet_state != "visible" else "visible"
            print(f"  · '{nombre_hoja}' ({estado}, {modo}): {len(de_la_hoja)} filas", flush=True)
            registros.extend(de_la_hoja)
    finally:
        libro.close()

    enlaces, sin_enlace = {}, set()
    for registro in registros:
        nombre = registro["nombre_empresa_excel"]
        if nombre:
            if nombre not in enlaces:
                enlaces[nombre] = _buscar_empresa_por_prefijo_o_alias(nombre, indice_nombres_empresa)[0]
            registro["numero_interno"] = enlaces[nombre]
            if enlaces[nombre] is None:
                sin_enlace.add(nombre)
        else:
            registro["numero_interno"] = None

        nuevos = [t for t in registro["titulos"] if _normalizar_nombre_empresa(t) not in titulos_principales]
        registro["titulos_nuevos"] = nuevos
        registro["aporta_titulos"] = bool(nuevos)

    if sin_enlace:
        ejemplos = ", ".join(sorted(sin_enlace)[:10])
        print(
            f"ℹ️ {len(sin_enlace)} nombres de empresa de las demás pestañas no se han podido enlazar con "
            f"'{HOJA_EMPRESAS}' (quedan con numero_interno=NULL, pero se guardan igualmente): {ejemplos}"
            + ("…" if len(sin_enlace) > 10 else ""),
            flush=True,
        )

    return registros


# ------------------------------------------------------------------
# Texto y embeddings por idioma
# ------------------------------------------------------------------
def _lista_a_texto(valor) -> str:
    if isinstance(valor, list):
        return ", ".join(str(v).strip() for v in valor if v and str(v).strip())
    return str(valor).strip() if valor else ""


def _recortar_para_embedding(texto: str, maximo: int) -> str:
    texto = re.sub(r"\s+", " ", texto or "").strip()
    return texto if len(texto) <= maximo else texto[:maximo].rstrip() + "…"


def construir_texto_por_idioma(empresa: dict, idioma: str) -> str:
    """
    Texto del perfil que se convierte en embedding. El modelo (e5-small) solo
    lee ~512 tokens, así que cada sección tiene un tope de caracteres y van
    ordenadas de más a menos importante para el matching contra licitaciones.
    Se incorporan (respecto a la versión anterior): sector/subsector,
    experiencia y zona/países de interés, ámbito geográfico, clientes
    principales y certificaciones. No entran datos que solo meterían ruido en
    la similitud (CIF, CNAE, web, contacto, tamaño, importes).
    """
    columna_palabras = "palabras_clave" if idioma == "es" else f"palabras_clave_{idioma}"

    sector = " / ".join(filter(None, [empresa.get("sector"), empresa.get("subsector")]))
    zona = ", ".join(filter(None, [
        _lista_a_texto(empresa.get("zona_geografica_interes")),
        _lista_a_texto(empresa.get("paises_interes")),
    ]))

    secciones = [  # (etiqueta o None, contenido, máximo de caracteres)
        (None, empresa.get("nombre_empresa"), 120),
        ("Sector", sector, 200),
        (None, empresa.get("descripcion_actividad"), 600),
        ("Proyectos tipo", _lista_a_texto(empresa.get("proyectos_tipo")), 400),
        ("Palabras clave", _lista_a_texto(empresa.get(columna_palabras)), 400),
        ("Experiencia en países", _lista_a_texto(empresa.get("experiencia_paises")), 150),
        ("Zona y países de interés", zona, 200),
        ("Ámbito geográfico", empresa.get("ambito_geografico"), 100),
        ("Clientes principales", empresa.get("clientes_principales"), 120),
        ("Certificaciones", empresa.get("certificaciones"), 100),
    ]

    lineas = []
    for etiqueta, contenido, maximo in secciones:
        contenido = _recortar_para_embedding(str(contenido) if contenido else "", maximo)
        if contenido:
            lineas.append(f"{etiqueta}: {contenido}" if etiqueta else contenido)
    return "\n".join(lineas)


def calcular_textos_y_embeddings(empresa: dict) -> dict:
    resultado = {}
    for idioma in IDIOMAS_PALABRAS_CLAVE:
        columna_palabras = "palabras_clave" if idioma == "es" else f"palabras_clave_{idioma}"
        sufijo_campo = "" if idioma == "es" else f"_{idioma}"

        if idioma != "es" and not empresa.get(columna_palabras):
            resultado[f"texto_completo{sufijo_campo}"] = None
            resultado[f"embedding{sufijo_campo}"] = None
            continue

        texto = construir_texto_por_idioma(empresa, idioma)
        resultado[f"texto_completo{sufijo_campo}"] = texto or None
        resultado[f"embedding{sufijo_campo}"] = generar_embedding(texto) if texto else None

    return resultado


# ------------------------------------------------------------------
# Carga a Supabase
# ------------------------------------------------------------------
def _recargar_tabla(supabase, tabla: str, registros: list, errores: list, max_intentos: int = 3) -> int:
    """
    Borra el contenido de `tabla` e inserta `registros` por lotes (con
    reintentos). Cualquier lote que falle de forma definitiva se anota en
    `errores` -- el script termina entonces con error y NO guarda el estado
    de sincronización, de modo que la siguiente ejecución lo reintenta.
    """
    supabase.table(tabla).delete().neq("id", 0).execute()

    insertados = 0
    for i in range(0, len(registros), TAMANO_LOTE_INSERCION):
        lote = registros[i:i + TAMANO_LOTE_INSERCION]
        numero_lote = i // TAMANO_LOTE_INSERCION + 1
        insertado = False
        for intento in range(1, max_intentos + 1):
            try:
                supabase.table(tabla).insert(lote).execute()
                insertados += len(lote)
                insertado = True
                print(f"Progreso '{tabla}': {insertados}/{len(registros)}...", flush=True)
                break
            except Exception as error:
                print(f"Aviso: intento {intento}/{max_intentos} fallido en el lote {numero_lote} de '{tabla}': {error}", flush=True)
                ultimo_error = error
                if intento < max_intentos:
                    time.sleep(2 * intento)
        if not insertado:
            errores.append(f"'{tabla}': lote {numero_lote} no insertado ({ultimo_error})")
            break  # no tiene sentido seguir cargando una tabla que ya ha quedado incompleta
    return insertados


def _escribir_resumen(lineas: list):
    """Resumen visible en la pestaña de la ejecución de GitHub Actions (si estamos en Actions)."""
    ruta = os.environ.get("GITHUB_STEP_SUMMARY")
    if not ruta:
        return
    with open(ruta, "a", encoding="utf-8") as fichero:
        fichero.write("### Sincronización de empresas\n\n" + "\n".join(f"- {linea}" for linea in lineas) + "\n")


# ------------------------------------------------------------------
# Ejecución principal
# ------------------------------------------------------------------
def ejecutar_sincronizacion():
    print("=" * 100, flush=True)
    print("SINCRONIZACIÓN DE EMPRESAS — Google Drive -> Supabase", flush=True)
    print("=" * 100, flush=True)

    file_id = os.environ.get("GOOGLE_DRIVE_FILE_ID")
    if not file_id:
        raise RuntimeError("Falta la variable de entorno GOOGLE_DRIVE_FILE_ID.")

    supabase = obtener_cliente_supabase()
    servicio_drive = obtener_servicio_drive()

    metadata = obtener_metadata_archivo(servicio_drive, file_id)
    modificado_en = metadata["modifiedTime"]
    print(f"Fichero: {metadata.get('name')}  ·  Última modificación en Drive: {modificado_en}", flush=True)

    ultimo_conocido = obtener_ultima_modificacion_conocida(supabase)
    version_guardada = _leer_estado(supabase, CLAVE_VERSION_INGESTA)

    motivo_forzado = None
    if os.environ.get("FORZAR_SINCRONIZACION", "").strip().lower() in {"1", "true", "yes", "si", "sí"}:
        motivo_forzado = "se ha pedido forzar la sincronización"
    elif version_guardada != VERSION_INGESTA:
        motivo_forzado = f"ha cambiado la lógica de ingesta ({version_guardada or 'sin versión previa'} -> {VERSION_INGESTA})"

    if ultimo_conocido == modificado_en and not motivo_forzado:
        print("No hay cambios desde la última sincronización. Nada que hacer.", flush=True)
        return

    if motivo_forzado:
        print(f"Sincronización completa: {motivo_forzado}. Descargando...", flush=True)
    else:
        print("Se ha detectado un cambio (o es la primera sincronización). Descargando...", flush=True)
    buffer_excel = descargar_excel(servicio_drive, file_id, metadata.get("mimeType", ""))

    # ---- 1) LEER todo antes de tocar Supabase: si el Excel no se puede leer, la base queda intacta ----
    empresas = leer_empresas(buffer_excel)
    print(f"Empresas leídas de '{HOJA_EMPRESAS}': {len(empresas)}", flush=True)
    if not empresas:
        raise RuntimeError("No se ha podido leer ninguna empresa del Excel. Se aborta sin tocar Supabase.")

    indice_nombres = {
        _normalizar_nombre_empresa(e["nombre_empresa"]): e["numero_interno"]
        for e in empresas if e.get("nombre_empresa")
    }

    referencias = leer_referencias(buffer_excel, indice_nombres, HOJA_REFERENCIAS, FILA_CABECERA_REFERENCIAS)
    print(f"Referencias leídas de '{HOJA_REFERENCIAS}': {len(referencias)}", flush=True)
    referencias_hms = leer_referencias(buffer_excel, indice_nombres, HOJA_HMS, FILA_CABECERA_HMS)
    print(f"Referencias leídas de '{HOJA_HMS}': {len(referencias_hms)}", flush=True)
    referencias += referencias_hms

    titulos_principales = {
        _normalizar_nombre_empresa(r["titulo"]) for r in referencias if r.get("titulo")
    }
    print("Leyendo el resto de pestañas (todas sus columnas)...", flush=True)
    hojas_extra = leer_hojas_extra(buffer_excel, indice_nombres, titulos_principales)
    print(f"Filas leídas del resto de pestañas: {len(hojas_extra)}", flush=True)

    # ---- 2) Embeddings ----
    print("Generando embeddings (español siempre; en/fr/pt solo si hay palabras clave propias)...", flush=True)
    for empresa in empresas:
        empresa.update(calcular_textos_y_embeddings(empresa))

    # ---- 3) CARGAR ----
    errores = []

    subidas = subir_en_lotes(supabase, "empresas", "numero_interno", empresas, tamano_lote=TAMANO_LOTE_SUPABASE)
    print(f"Empresas sincronizadas: {subidas}/{len(empresas)}", flush=True)
    if subidas != len(empresas):
        errores.append(f"'empresas': solo se han subido {subidas} de {len(empresas)}")

    numeros_actuales = {e["numero_interno"] for e in empresas}
    respuesta_existentes = supabase.table("empresas").select("id, numero_interno").execute()
    ids_a_borrar = [f["id"] for f in respuesta_existentes.data if f["numero_interno"] not in numeros_actuales]
    if ids_a_borrar:
        for i in range(0, len(ids_a_borrar), 100):
            supabase.table("empresas").delete().in_("id", ids_a_borrar[i:i + 100]).execute()
        print(f"Empresas retiradas (ya no están en el Excel): {len(ids_a_borrar)}", flush=True)

    referencias_insertadas = hojas_insertadas = 0
    if referencias:
        print("Recargando 'empresas_referencias' (borrado + inserción completa)...", flush=True)
        referencias_insertadas = _recargar_tabla(supabase, "empresas_referencias", referencias, errores)
        print(f"Referencias sincronizadas: {referencias_insertadas}/{len(referencias)}", flush=True)

    if hojas_extra:
        print("Recargando 'empresas_hojas_extra' (borrado + inserción completa)...", flush=True)
        hojas_insertadas = _recargar_tabla(supabase, "empresas_hojas_extra", hojas_extra, errores)
        print(f"Filas del resto de pestañas sincronizadas: {hojas_insertadas}/{len(hojas_extra)}", flush=True)

    resumen = [
        f"Empresas: {subidas}/{len(empresas)} (retiradas: {len(ids_a_borrar)})",
        f"Referencias: {referencias_insertadas}/{len(referencias)}",
        f"Resto de pestañas: {hojas_insertadas}/{len(hojas_extra)} filas",
    ]

    if errores:
        _escribir_resumen(resumen + [f"❌ {e}" for e in errores])
        raise RuntimeError(
            "Sincronización INCOMPLETA (no se guarda el estado, se reintentará en la próxima ejecución): "
            + " | ".join(errores)
        )

    guardar_ultima_modificacion(supabase, modificado_en)
    _guardar_estado(supabase, CLAVE_VERSION_INGESTA, VERSION_INGESTA)
    print("Estado de sincronización actualizado.", flush=True)
    _escribir_resumen(resumen + ["✅ Sincronización completa"])


if __name__ == "__main__":
    ejecutar_sincronizacion()

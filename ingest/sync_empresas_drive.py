# -*- coding: utf-8 -*-
"""
sync_empresas_drive.py
--------------------------
Sincroniza el Excel de empresas (BBDD_Empresas_260909.xlsx o como se
llame en cada momento) almacenado en Google Drive contra las tablas
`empresas` y `empresas_referencias` de Supabase. Pensado para ejecutarse
periódicamente (ver .github/workflows/sincronizar_empresas_drive.yml):
en cada ejecución comprueba la fecha de modificación del fichero en
Drive contra la última que tenemos guardada (tabla `sync_estado`); si no
ha cambiado, no hace nada.

DOS HOJAS DEL EXCEL, DOS TABLAS
-----------------------------------
- "DOSSIER COMPLETO"         -> tabla `empresas` (perfil de cada empresa)
- "REFERENCIAS P BÚSQUEDAS"    -> tabla `empresas_referencias` (histórico
                                de licitaciones en las que ha
                                participado cada empresa)

El Excel real tiene más hojas ("REFERENCIAS", "REF AGUA", "REF TURISMO",
"lista para chatgpt", "PALABRAS CLAVE PARA BÚSQUEDAS"...) que, tras
revisarlas, son vistas derivadas/de trabajo de esas dos hojas
principales (subconjuntos filtrados por sector, o exports para pegar en
un chat) -- no se ingieren para no duplicar datos. Si alguna de ellas SÍ
tiene información que no esté en las dos hojas principales, dímelo y lo
ajusto.

POR QUÉ CASI TODO SE GUARDA COMO TEXTO/JSONB
-----------------------------------------------
Al leer el Excel real se vio que varias columnas son mucho más libres de
lo que su nombre sugiere -- p. ej. "ÁMBITO GEOGRÁFICO DE OPERACIÓN
(Local/Regional/Nacional/Internacional)" en la práctica contiene listas
de países en texto libre, y "RESULTADO (ADJUDICADA/NO ADJUDICADA/SIN
INFORMACIÓN)" tiene decenas de redacciones distintas. Por eso este
script NO intenta forzar esos valores a un tipo/enum limpio: los guarda
tal cual, y además guarda la FILA COMPLETA de cada hoja (cabecera real
-> valor real) en una columna `datos_excel` JSONB, como red de
seguridad para no perder nunca ninguna anotación atípica.

5 IDIOMAS DE PALABRAS CLAVE: en el Excel real solo se han encontrado 4
columnas de idioma ("PALABRAS CLAVE", "PALABRAS CLAVE EN INGLÉS", "...EN
FRANCÉS", "...EN PORTUGUÉS") -- si hay un 5º idioma en otra hoja o con
otro nombre de cabecera, dímelo y lo añado a MAPEO_EMPRESAS; el resto
del script (embeddings por idioma, búsqueda) ya está preparado para
soportar más idiomas sin cambios estructurales.

Variables de entorno requeridas:
    SUPABASE_URL, SUPABASE_SERVICE_KEY
    GOOGLE_SERVICE_ACCOUNT_JSON  -- contenido COMPLETO del JSON de la
                                    cuenta de servicio de Google (como
                                    secreto de GitHub Actions)
    GOOGLE_DRIVE_FILE_ID         -- ID del fichero Excel en Drive

La cuenta de servicio debe tener el fichero compartido con ella (basta
con permiso de "Lector") -- ver README.md para el paso a paso.
"""
import io
import json
import os
import re
import unicodedata
from datetime import date, datetime

import numpy as np
import pandas as pd

from common import (
    generar_embedding,
    obtener_cliente_supabase,
    subir_en_lotes,
)

CLAVE_SYNC_ESTADO = "empresas_drive_modified_time"
TAMANO_LOTE_SUPABASE = 20

HOJA_EMPRESAS = "DOSSIER COMPLETO"
FILA_CABECERA_EMPRESAS = 0          # cabecera en la 1ª fila

HOJA_REFERENCIAS = "REFERENCIAS P BÚSQUEDAS"
FILA_CABECERA_REFERENCIAS = 1       # cabecera en la 2ª fila (la 1ª va vacía en el Excel real)

IDIOMAS_PALABRAS_CLAVE = ["es", "en", "fr", "pt"]  # ver nota "5 IDIOMAS" arriba

# clave interna -> cabecera REAL confirmada contra BBDD_Empresas_260909.xlsx
MAPEO_EMPRESAS = {
    "nombre_empresa": "EMPRESA",
    "sector": "SECTOR",
    "sector_secundario": "SECTOR.1",  # 2ª columna también titulada "SECTOR" en el Excel (ver docstring)
    "subsector": "SUBSECTOR",
    "tipo_empresa": "EMPRESA PÚBLICA / CLÚSTER / ASOCIACIÓN / PRIVADA",
    "cif": "CIF",
    "cnae": "CNAE",
    "web": "WEB",
    "descripcion_actividad": "DESCRIPCIÓN BREVE DE LA ACTIVIDAD QUE DESARROLLA",
    "palabras_clave": "PALABRAS CLAVE",
    "proyectos_tipo": "PRINCIPALES PROYECTOS TIPO",
    "experiencia_paises": "EXPERIENCIA PAÍSES",
    "zona_geografica_interes": "ZONA GEOGRÁFICA DE INTERÉS",
    "paises_interes": "PAÍSES DE INTERÉS",
    "pais": "PAÍS",
    "tamano": "TAMAÑO (MICRO/PYME/GRANDE)",
    "registro_oficial_proveedores": "REGISTRO OFICIAL DE PROVEEDORES (SI/NO)",
    "certificaciones": "CERTIFICACIONES (ISO, sectoriales, etc.)",
    "facturacion_anual": "FACTURACIÓN ANUAL (USD/EUR)",
    "experiencia_contratos_similares": "EXPERIENCIA EN CONTRATOS SIMILARES (Sí/No)",
    "clientes_principales": "CLIENTES PRINCIPALES",
    "capacidad_tecnica": "CAPACIDAD TÉCNICA (equipos, tecnología, etc.)",
    "ambito_geografico": "ÁMBITO GEOGRÁFICO DE OPERACIÓN (Local/Regional/Nacional/Internacional)",
    "preferencias_licitaciones": "PREFERENCIAS LICITACIONES (Monto mínimo/máximo, sector, etc.)",
    "contacto_nombre": "NOMBRE",
    "contacto_cargo": "CARGO",
    "contacto_email": "E-MAIL",
    "palabras_clave_en": "PALABRAS CLAVE EN INGLÉS",
    "palabras_clave_fr": "PALABRAS CLAVE EN FRANCÉS",
    "palabras_clave_pt": "PALABRAS CLAVE EN PORTUGUÉS",
}
COLUMNA_NUMERO_INTERNO = "Unnamed: 0"   # columna A, sin cabecera
COLUMNA_ID_EMPRESA = "ID"
COLUMNA_NOTAS_LIBRES = "Unnamed: 26"    # comentarios sueltos del analista, sin cabecera

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
CAMPOS_COMPARABLES_EMPRESAS = (
    "nombre_empresa", "sector", "sector_secundario", "subsector", "tipo_empresa",
    "cif", "cnae", "web", "descripcion_actividad", "pais", "tamano",
    "ambito_geografico", "facturacion_anual",
)


# ------------------------------------------------------------------
# Mapeo explícito de alias (Variante normalizada -> Nombre exacto en Dossier Completo)
# ------------------------------------------------------------------
ALIAS_EMPRESAS = {
    "canarias tecnologica y si": "CANARIAS TECNOLÓGICA Y SI",
    "ctsi (canarias tecnologica y sistemas de informacion)": "CANARIAS TECNOLÓGICA Y SI",
    "cocosolutions": "COCO SOLUTIONS",
    "coco solutions": "COCO SOLUTIONS",
    "grupo evm": "GRUPO EVM",
    "evm": "GRUPO EVM",
    "acosta ing y wet ingenieria": "GRUPO ACOSTA (ACOSTA ING Y WET INGENIERÍA)",
    "acosta": "GRUPO ACOSTA (ACOSTA ING Y WET INGENIERÍA)",
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
def obtener_ultima_modificacion_conocida(supabase) -> str:
    respuesta = (
        supabase.table("sync_estado").select("valor").eq("clave", CLAVE_SYNC_ESTADO).limit(1).execute()
    )
    return respuesta.data[0]["valor"] if respuesta.data else None


def guardar_ultima_modificacion(supabase, valor: str):
    supabase.table("sync_estado").upsert(
        {"clave": CLAVE_SYNC_ESTADO, "valor": valor}, on_conflict="clave"
    ).execute()


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


def _dividir_lista(valor) -> list:
    if _es_nulo(valor):
        return []
    texto = str(valor).strip()
    if not texto:
        return []
    # el Excel usa indistintamente coma, punto y coma o salto de línea
    return [p.strip() for p in re.split(r"[,;\n]", texto) if p.strip()]


def _leer_hoja(buffer_excel: io.BytesIO, hoja: str, fila_cabecera: int) -> pd.DataFrame:
    df = pd.read_excel(buffer_excel, sheet_name=hoja, header=fila_cabecera, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]
    return df


# ------------------------------------------------------------------
# Lectura de "DOSSIER COMPLETO" -> empresas
# ------------------------------------------------------------------
def leer_empresas(buffer_excel: io.BytesIO) -> list:
    df = _leer_hoja(buffer_excel, HOJA_EMPRESAS, FILA_CABECERA_EMPRESAS)
    if df.empty:
        return []

    faltantes = [c for c in list(MAPEO_EMPRESAS.values()) + [COLUMNA_ID_EMPRESA] if c not in df.columns]
    if faltantes:
        print(f"Aviso: no se han encontrado estas columnas en '{HOJA_EMPRESAS}': {faltantes}", flush=True)

    empresas = []
    ids_generados = 0

    for _, fila in df.iterrows():
        numero_interno = _valor_texto(fila, COLUMNA_NUMERO_INTERNO)
        if not numero_interno:
            continue  # sin número interno no hay forma fiable de identificar la fila

        id_empresa = _valor_texto(fila, COLUMNA_ID_EMPRESA)
        if not id_empresa:
            id_empresa = f"SIN_ID_{numero_interno}"
            ids_generados += 1

        empresa = {
            "numero_interno": numero_interno,
            "id_empresa": id_empresa,
        }
        for clave, columna in MAPEO_EMPRESAS.items():
            if columna not in df.columns:
                empresa[clave] = [] if clave in CAMPOS_LISTA_EMPRESAS else None
            elif clave in CAMPOS_LISTA_EMPRESAS:
                empresa[clave] = _dividir_lista(fila.get(columna))
            else:
                empresa[clave] = _valor_texto(fila, columna)

        empresa["notas_libres"] = _valor_texto(fila, COLUMNA_NOTAS_LIBRES)
        empresa["datos_excel"] = _fila_a_json(fila)
        empresas.append(empresa)

    if ids_generados:
        print(f"ℹ️ {ids_generados} empresas sin 'ID' en el Excel: se les asignó un ID estable 'SIN_ID_<número interno>'.", flush=True)

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
       Devuelve (id_empresa, motivo_falla)"""
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

    for nombre_idx, id_emp in indice_nombres.items():
        if nombre_idx.startswith(primera_palabra) or primera_palabra.startswith(nombre_idx):
            return id_emp, None

    return None, f"No se encontró coincidencia exacta, alias ni prefijo compatible para '{nombre_original}' (normalizado: '{nombre_norm}')"


def leer_referencias(buffer_excel: io.BytesIO, indice_nombres_empresa: dict) -> list:
    df = _leer_hoja(buffer_excel, HOJA_REFERENCIAS, FILA_CABECERA_REFERENCIAS)
    if df.empty:
        return []

    faltantes = [c for c in MAPEO_REFERENCIAS.values() if c not in df.columns]
    if faltantes:
        print(f"Aviso: no se han encontrado estas columnas en '{HOJA_REFERENCIAS}': {faltantes}", flush=True)

    referencias = []
    sin_match = 0

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

        referencia["resultado_normalizado"] = _normalizar_resultado(referencia.get("resultado"))
        referencia["datos_excel"] = _fila_a_json(fila)

        # Búsqueda inteligente con impresión de motivos detallados si falla
        id_empresa, motivo = _buscar_empresa_por_prefijo_o_alias(nombre_excel, indice_nombres_empresa)
        
        referencia["id_empresa"] = id_empresa
        if not id_empresa:
            sin_match += 1
            print(f"⚠️ [SIN MATCH] Empresa en referencia: '{nombre_excel}' -> Motivo: {motivo}", flush=True)

        referencias.append(referencia)

    if sin_match:
        print(
            f"ℹ️ {sin_match}/{len(referencias)} filas de referencias no se han podido enlazar "
            f"con ninguna empresa de '{HOJA_EMPRESAS}' por nombre (quedan con id_empresa=NULL, "
            f"pero se guardan igualmente).",
            flush=True,
        )

    return referencias


# ------------------------------------------------------------------
# Texto y embeddings por idioma
# ------------------------------------------------------------------
def construir_texto_por_idioma(empresa: dict, idioma: str) -> str:
    columna_palabras = "palabras_clave" if idioma == "es" else f"palabras_clave_{idioma}"
    partes = []
    if empresa.get("nombre_empresa"):
        partes.append(empresa["nombre_empresa"])
    if empresa.get("descripcion_actividad"):
        partes.append(empresa["descripcion_actividad"])
    if empresa.get("proyectos_tipo"):
        partes.append(", ".join(empresa["proyectos_tipo"]))
    palabras = empresa.get(columna_palabras) or []
    if palabras:
        partes.append(", ".join(palabras))
    return "\n".join(partes)


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
    if ultimo_conocido == modificado_en:
        print("No hay cambios desde la última sincronización. Nada que hacer.", flush=True)
        return

    print("Se ha detectado un cambio (o es la primera sincronización). Descargando...", flush=True)
    buffer_excel = descargar_excel(servicio_drive, file_id, metadata.get("mimeType", ""))

    # ---------------- Empresas ----------------
    empresas = leer_empresas(buffer_excel)
    print(f"Empresas leídas de '{HOJA_EMPRESAS}': {len(empresas)}", flush=True)

    if not empresas:
        print("No se ha podido leer ninguna empresa del Excel. Se aborta sin tocar Supabase.", flush=True)
        return

    print("Generando embeddings (español siempre; en/fr/pt solo si hay palabras clave propias)...", flush=True)
    for empresa in empresas:
        empresa.update(calcular_textos_y_embeddings(empresa))

    subidas = subir_en_lotes(supabase, "empresas", "id_empresa", empresas, tamano_lote=TAMANO_LOTE_SUPABASE)
    print(f"Empresas sincronizadas: {subidas}/{len(empresas)}", flush=True)

    ids_actuales = {e["id_empresa"] for e in empresas}
    respuesta_existentes = supabase.table("empresas").select("id, id_empresa").execute()
    ids_a_borrar = [f["id"] for f in respuesta_existentes.data if f["id_empresa"] not in ids_actuales]
    if ids_a_borrar:
        for i in range(0, len(ids_a_borrar), 100):
            supabase.table("empresas").delete().in_("id", ids_a_borrar[i:i + 100]).execute()
        print(f"Empresas retiradas (ya no están en el Excel): {len(ids_a_borrar)}", flush=True)

    # ---------------- Referencias ----------------
    indice_nombres = {
        _normalizar_nombre_empresa(e["nombre_empresa"]): e["id_empresa"]
        for e in empresas if e.get("nombre_empresa")
    }
    referencias = leer_referencias(buffer_excel, indice_nombres)
    print(f"Referencias leídas de '{HOJA_REFERENCIAS}': {len(referencias)}", flush=True)

    if referencias:
        print("Recargando 'empresas_referencias' (borrado + inserción completa)...", flush=True)
        supabase.table("empresas_referencias").delete().neq("id", 0).execute()

        tamano_lote = 200
        insertadas = 0
        for i in range(0, len(referencias), tamano_lote):
            lote = referencias[i:i + tamano_lote]
            try:
                supabase.table("empresas_referencias").insert(lote).execute()
                insertadas += len(lote)
                print(f"Progreso referencias: {insertadas}/{len(referencias)}...", flush=True)
            except Exception as error:
                print(f"Error insertando lote de referencias {i // tamano_lote + 1}: {error}", flush=True)
        print(f"Referencias sincronizadas: {insertadas}/{len(referencias)}", flush=True)

    guardar_ultima_modificacion(supabase, modificado_en)
    print("Estado de sincronización actualizado.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

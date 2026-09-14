# -*- coding: utf-8 -*-
"""
sync_empresas_drive.py
--------------------------
Sincroniza el Excel de empresas almacenado en Google Drive contra la
tabla `empresas` de Supabase. Pensado para ejecutarse periódicamente
(ver .github/workflows/sincronizar_empresas_drive.yml): en cada
ejecución comprueba la fecha de modificación del fichero en Drive contra
la última que tenemos guardada (tabla `sync_estado`); si no ha cambiado,
no hace nada.

AVISO SOBRE EL MAPEO DE COLUMNAS
-----------------------------------
La captura de referencia que se compartió solo mostraba 2 columnas: una
SIN cabecera con un número correlativo (p. ej. 74) y la columna "ID" con
el código real de empresa (p. ej. "ENT_90"). Esas dos SÍ están
confirmadas y se leen por posición: la primera columna del Excel siempre
es el número interno, y la columna cuya cabecera es exactamente "ID"
siempre es el identificador real -- ninguna de las dos se regenera.

El resto de columnas (Sector, Subsector, Web, Contacto...) están
mapeadas por NOMBRE de cabecera en `MAPEO_COLUMNAS`, a partir de la
lista de campos que describiste, pero sin haber visto esas cabeceras
reales. Si el Excel usa nombres distintos a los que aquí se prueban,
solo hay que añadir la variante real a la lista correspondiente de
`MAPEO_COLUMNAS` -- el resto del script no necesita cambios. En
particular, no había ninguna columna explícita para el NOMBRE de la
empresa en la lista de campos que diste (solo aparecía "Nombre" dentro
del bloque de "Contacto"); se asume que existe una columna de nombre de
empresa con alguno de los encabezados típicos ("Empresa", "Nombre de la
empresa"...) -- confirma o ajusta esto en MAPEO_COLUMNAS.

Variables de entorno requeridas:
    SUPABASE_URL, SUPABASE_SERVICE_KEY
    GOOGLE_SERVICE_ACCOUNT_JSON  -- contenido COMPLETO del JSON de la
                                    cuenta de servicio de Google (como
                                    secreto de GitHub Actions)
    GOOGLE_DRIVE_FILE_ID          -- ID del fichero Excel en Drive (se
                                     saca de su URL para compartir)

La cuenta de servicio debe tener el fichero compartido con ella (basta
con permiso de "Lector") -- ver README.md para el paso a paso.
"""
import io
import json
import os
import re
import unicodedata

import pandas as pd

from common import generar_embedding, obtener_cliente_supabase, subir_en_lotes

CLAVE_SYNC_ESTADO = "empresas_drive_modified_time"
TAMANO_LOTE_SUPABASE = 20

# clave interna -> posibles cabeceras en el Excel (se comparan ya
# normalizadas: minúsculas y sin acentos). Añade aquí cualquier variante
# real que use vuestro fichero.
MAPEO_COLUMNAS = {
    "nombre_empresa": ["empresa", "nombre de la empresa", "nombre comercial", "razon social", "nombre empresa"],
    "sector": ["sector"],
    "subsector": ["subsector"],
    "tipo_empresa": ["tipo de empresa", "tipo empresa", "tipo"],
    "cif": ["cif", "nif"],
    "web": ["web", "pagina web", "sitio web", "url"],
    "descripcion_actividad": ["descripcion de la actividad", "descripcion actividad", "descripcion", "actividad"],
    "palabras_clave": ["palabras clave", "keywords"],
    "proyectos_tipo": ["proyectos tipo", "tipo de proyectos", "tipos de proyecto"],
    "experiencia_paises": ["experiencia en paises", "paises de experiencia", "experiencia paises", "paises"],
    "zona_geografica": ["zona geografica", "zonas geograficas", "region"],
    "tamano": ["tamano"],
    "facturacion_anual": ["facturacion anual", "facturacion"],
    "contacto_nombre": ["nombre contacto", "nombre del contacto", "contacto"],
    "contacto_cargo": ["cargo", "cargo contacto", "puesto"],
    "contacto_email": ["e-mail", "email", "correo electronico", "correo"],
}

CAMPOS_LISTA = {"palabras_clave", "proyectos_tipo", "experiencia_paises", "zona_geografica"}
CAMPOS_COMPARABLES = (
    "nombre_empresa", "sector", "subsector", "tipo_empresa", "web",
    "descripcion_actividad", "tamano", "facturacion_anual",
)


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
        # Google Sheets nativo: hay que exportarlo a .xlsx
        contenido = servicio.files().export_media(
            fileId=file_id,
            mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ).execute()
    else:
        # Ya es un .xlsx subido tal cual
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
# Lectura y normalización del Excel
# ------------------------------------------------------------------
def _normalizar_cabecera(texto) -> str:
    texto = str(texto).strip().lower()
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def _mapear_columnas(df: pd.DataFrame) -> dict:
    cabeceras_normalizadas = {_normalizar_cabecera(c): c for c in df.columns}
    mapa_resuelto = {}
    for clave_interna, alternativas in MAPEO_COLUMNAS.items():
        for alternativa in alternativas:
            columna_real = cabeceras_normalizadas.get(_normalizar_cabecera(alternativa))
            if columna_real:
                mapa_resuelto[clave_interna] = columna_real
                break
    return mapa_resuelto


def _dividir_lista(valor) -> list:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return []
    texto = str(valor).strip()
    if not texto:
        return []
    return [p.strip() for p in re.split(r"[,;|]", texto) if p.strip()]


def _valor_texto(fila: pd.Series, columna) -> str:
    if not columna:
        return None
    valor = fila.get(columna)
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    texto = str(valor).strip()
    return texto if texto else None


def leer_empresas_desde_excel(buffer_excel: io.BytesIO) -> list:
    df = pd.read_excel(buffer_excel, dtype=object)
    if df.empty:
        return []

    columna_numero_interno = df.columns[0]  # primera columna, tal cual (ver aviso del docstring)
    mapa = _mapear_columnas(df)

    # La columna "ID" se busca por nombre exacto (normalizado), no por
    # posición, para no depender de que sea siempre la segunda columna.
    cabeceras_normalizadas = {_normalizar_cabecera(c): c for c in df.columns}
    columna_id = cabeceras_normalizadas.get("id")
    if not columna_id:
        raise RuntimeError(
            "No se ha encontrado una columna llamada 'ID' en el Excel. "
            "Esa columna es obligatoria: es el identificador único de cada empresa."
        )

    print(f"Columnas reconocidas: {mapa}", flush=True)
    faltantes = [c for c in MAPEO_COLUMNAS if c not in mapa]
    if faltantes:
        print(f"⚠️ No se han encontrado columnas para: {faltantes} (revisa MAPEO_COLUMNAS si son necesarias).", flush=True)

    empresas = []
    for _, fila in df.iterrows():
        id_empresa = _valor_texto(fila, columna_id)
        if not id_empresa:
            continue  # sin ID no se puede identificar la empresa de forma fiable

        numero_interno = None
        valor_numero = fila.get(columna_numero_interno)
        if valor_numero is not None and not (isinstance(valor_numero, float) and pd.isna(valor_numero)):
            try:
                numero_interno = int(valor_numero)
            except (ValueError, TypeError):
                numero_interno = None

        empresa = {
            "numero_interno": numero_interno,
            "id_empresa": id_empresa,
        }
        for clave in MAPEO_COLUMNAS:
            columna = mapa.get(clave)
            if clave in CAMPOS_LISTA:
                empresa[clave] = _dividir_lista(fila.get(columna)) if columna else []
            else:
                empresa[clave] = _valor_texto(fila, columna)

        empresas.append(empresa)

    return empresas


# ------------------------------------------------------------------
# Texto para el embedding
# ------------------------------------------------------------------
def construir_texto_completo(empresa: dict) -> str:
    partes = []
    if empresa.get("nombre_empresa"):
        partes.append(f"Empresa: {empresa['nombre_empresa']}")
    if empresa.get("sector"):
        detalle = empresa["sector"]
        if empresa.get("subsector"):
            detalle += f" / {empresa['subsector']}"
        partes.append(f"Sector: {detalle}")
    if empresa.get("descripcion_actividad"):
        partes.append(empresa["descripcion_actividad"])
    if empresa.get("palabras_clave"):
        partes.append("Palabras clave: " + ", ".join(empresa["palabras_clave"]))
    if empresa.get("proyectos_tipo"):
        partes.append("Proyectos tipo: " + ", ".join(empresa["proyectos_tipo"]))
    if empresa.get("experiencia_paises"):
        partes.append("Experiencia en países: " + ", ".join(empresa["experiencia_paises"]))
    if empresa.get("zona_geografica"):
        partes.append("Zona geográfica: " + ", ".join(empresa["zona_geografica"]))
    return "\n".join(partes)


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

    empresas = leer_empresas_desde_excel(buffer_excel)
    print(f"Empresas leídas del Excel: {len(empresas)}", flush=True)

    if not empresas:
        print("El Excel no contiene filas aprovechables (¿falta la columna 'ID'?). No se sincroniza nada.", flush=True)
        return

    print("Generando/actualizando embeddings y subiendo a Supabase...", flush=True)
    for empresa in empresas:
        texto_completo = construir_texto_completo(empresa)
        empresa["texto_completo"] = texto_completo
        empresa["embedding"] = generar_embedding(texto_completo) if texto_completo else None

    subidas = subir_en_lotes(
        supabase, "empresas", "id_empresa", empresas, tamano_lote=TAMANO_LOTE_SUPABASE
    )
    print(f"Empresas sincronizadas: {subidas}/{len(empresas)}", flush=True)

    guardar_ultima_modificacion(supabase, modificado_en)
    print("Estado de sincronización actualizado.", flush=True)


if __name__ == "__main__":
    ejecutar_sincronizacion()

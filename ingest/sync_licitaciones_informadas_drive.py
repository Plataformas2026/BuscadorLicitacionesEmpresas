# -*- coding: utf-8 -*-
"""
sync_licitaciones_informadas_drive.py
-------------------------------------
Sincroniza el Excel de seguimiento de licitaciones INFORMADAS a las empresas
(«Empresas_informadas», hoja «SEGUIMIENTO 2026») de Google Drive contra la
tabla `licitaciones_informadas` de Supabase. Es lo que lee la pestaña
«Estadísticas de interés».

Se ejecuta una vez al día (.github/workflows/sincronizar_licitaciones_informadas.yml)
y, igual que sync_empresas_drive.py, solo hace trabajo real si el Excel ha
cambiado en Drive desde la última vez (se compara `modifiedTime`, guardado en
la tabla `sync_estado`) o si ha cambiado la lógica de este script
(`VERSION_INGESTA`). Si CUALQUIER carga falla, termina con error y no guarda el
estado: la siguiente ejecución lo reintenta.

QUÉ LEE
-------
- Todas las hojas cuyo nombre empieza por «SEGUIMIENTO» (así, cuando llegue
  «SEGUIMIENTO 2027», se incorpora sola).
- La cabecera se busca en las primeras filas (la fila con EMPRESA y
  COMENTARIOS), y las columnas se localizan por su NOMBRE, no por su letra:
  si reordenas columnas no se rompe; si renombras una de las necesarias, se
  detiene SIN tocar Supabase.
- Cuentan las filas con EMPRESA y con un MES válido (Enero...Diciembre). Las
  tablas de totales que hay debajo en la hoja («SUBTOTAL», «OBJETIVO MIN»...)
  no tienen mes válido y se descartan.
- Año y mes salen de «FECHA DE ENVÍO A LA EMPRESA»; si una fila no la tiene, el
  mes sale de la columna MES y el año, del nombre de la hoja.

CÓMO PROBARLO SIN SUPABASE NI GOOGLE
-----------------------------------
    python sync_licitaciones_informadas_drive.py --archivo Empresas_informadas.xlsx
Lee el Excel local, enseña un resumen y no escribe nada.

Variables de entorno (modo normal):
    SUPABASE_URL, SUPABASE_SERVICE_KEY
    GOOGLE_SERVICE_ACCOUNT_JSON   -- el mismo secreto que usa sync_empresas_drive.py
    GOOGLE_DRIVE_FILE_ID_INFORMADAS -- ID del Excel de seguimiento en Drive
Opcional: FORZAR_SINCRONIZACION=true
"""
import io
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime

CLAVE_SYNC_ESTADO = "informadas_drive_modified_time"
CLAVE_VERSION_INGESTA = "informadas_ingesta_version"
# Súbela cuando cambie la LÓGICA de este script: fuerza una resincronización aunque el Excel no cambie.
VERSION_INGESTA = "2026-10-08.v1"

TABLA = "licitaciones_informadas"
PREFIJO_HOJA = "seguimiento"
FILAS_BUSCANDO_CABECERA = 40
TAMANO_LOTE_INSERCION = 200

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

# campo de la tabla -> cabecera del Excel (comparadas sin tildes, mayúsculas ni espacios sobrantes)
CABECERAS = {
    "mes": "MES",
    "sector": "SECTOR",
    "tecnico": "TECNICO/A",
    "empresa": "EMPRESA",
    "titulo": "TITULO DE LA LICITACION",
    "agencia_ejecutora": "AGENCIA EJECUTORA",
    "organismo_financiador": "ORGANISMO FINANCIADOR",
    "pais": "PAIS",
    "fecha_envio": "FECHA DE ENVIO A LA EMPRESA",
    "fecha_limite": "FECHA LIMITE",
    "comentarios": "COMENTARIOS",
    "links": "LINKS",
}
OBLIGATORIAS = ("mes", "empresa", "titulo", "fecha_envio", "comentarios")


# ------------------------------------------------------------------
# Lectura del Excel (sin dependencias de Supabase ni de Google)
# ------------------------------------------------------------------
def _normalizar(texto) -> str:
    texto = unicodedata.normalize("NFD", str(texto or "").casefold())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", texto).strip()


def _texto(valor):
    if valor is None:
        return None
    if isinstance(valor, (datetime, date)):
        return valor.strftime("%Y-%m-%d")
    texto = " ".join(str(valor).split())  # una sola línea, sin espacios sobrantes
    return texto or None


def _a_fecha(valor):
    """Fecha de Excel (o texto dd/mm/aaaa o aaaa-mm-dd) -> `date`; None si no es una fecha."""
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor or "").strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(texto[:10], formato).date()
        except ValueError:
            continue
    return None


def _buscar_cabecera(filas: list):
    """-> (índice de la fila de cabecera, {campo: índice de columna}) o None si la hoja no tiene la tabla."""
    esperadas = {_normalizar(v): k for k, v in CABECERAS.items()}
    for i, fila in enumerate(filas[:FILAS_BUSCANDO_CABECERA]):
        columnas = {}
        for j, valor in enumerate(fila):
            campo = esperadas.get(_normalizar(valor))
            if campo and campo not in columnas:
                columnas[campo] = j
        if "empresa" in columnas and "comentarios" in columnas:
            return i, columnas
    return None


def leer_registros(origen) -> list:
    """
    `origen`: ruta o BytesIO del Excel -> lista de dicts listos para la tabla.
    Lanza RuntimeError si no encuentra hojas «SEGUIMIENTO», la cabecera o columnas obligatorias.
    """
    import openpyxl

    libro = openpyxl.load_workbook(origen, read_only=True, data_only=True)
    registros, hojas_leidas = [], []
    for hoja in libro.worksheets:
        if not _normalizar(hoja.title).startswith(PREFIJO_HOJA):
            continue
        filas = [list(f) for f in hoja.iter_rows(values_only=True)]
        encontrada = _buscar_cabecera(filas)
        if not encontrada:
            raise RuntimeError(
                f"En la hoja «{hoja.title}» no se encuentra la cabecera (una fila con EMPRESA y COMENTARIOS)."
            )
        fila_cabecera, columnas = encontrada
        faltan = [CABECERAS[c] for c in OBLIGATORIAS if c not in columnas]
        if faltan:
            raise RuntimeError(f"En la hoja «{hoja.title}» faltan las columnas: {', '.join(faltan)}.")

        anio_hoja = re.search(r"(20\d\d)", hoja.title)
        anio_hoja = int(anio_hoja.group(1)) if anio_hoja else None

        for numero, fila in enumerate(filas[fila_cabecera + 1:], start=fila_cabecera + 2):  # nº de fila en Excel
            def celda(campo):
                j = columnas.get(campo)
                return fila[j] if j is not None and j < len(fila) else None

            mes_texto = _normalizar(celda("mes"))
            empresa = _texto(celda("empresa"))
            if mes_texto not in MESES or not empresa:
                continue  # totales, leyendas, filas vacías

            fecha_envio = _a_fecha(celda("fecha_envio"))
            registros.append({
                "hoja": hoja.title,
                "fila": numero,
                "anio": fecha_envio.year if fecha_envio else anio_hoja,
                "mes": fecha_envio.month if fecha_envio else MESES[mes_texto],
                "sector": _texto(celda("sector")),
                "tecnico": _texto(celda("tecnico")),
                "empresa": empresa,
                "titulo": _texto(celda("titulo")),
                "agencia_ejecutora": _texto(celda("agencia_ejecutora")),
                "organismo_financiador": _texto(celda("organismo_financiador")),
                "pais": _texto(celda("pais")),
                "fecha_envio": fecha_envio.isoformat() if fecha_envio else None,
                "fecha_limite": (lambda d: d.isoformat() if d else None)(_a_fecha(celda("fecha_limite"))),
                "comentarios": _texto(celda("comentarios")),
                "links": _texto(celda("links")),
            })
        hojas_leidas.append(hoja.title)

    if not hojas_leidas:
        raise RuntimeError(f"El Excel no tiene ninguna hoja cuyo nombre empiece por «{PREFIJO_HOJA.upper()}».")
    return registros


# ------------------------------------------------------------------
# Google Drive
# ------------------------------------------------------------------
def obtener_servicio_drive():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credenciales_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not credenciales_json:
        raise RuntimeError("Falta la variable de entorno GOOGLE_SERVICE_ACCOUNT_JSON.")
    credenciales = service_account.Credentials.from_service_account_info(
        json.loads(credenciales_json), scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=credenciales)


def descargar_excel(servicio, file_id: str, mime_type: str) -> io.BytesIO:
    """Sirve tanto para un Google Sheets (se exporta a .xlsx) como para un .xlsx subido a Drive."""
    if mime_type == "application/vnd.google-apps.spreadsheet":
        contenido = servicio.files().export_media(
            fileId=file_id, mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ).execute()
    else:
        contenido = servicio.files().get_media(fileId=file_id).execute()
    return io.BytesIO(contenido)


# ------------------------------------------------------------------
# Supabase
# ------------------------------------------------------------------
def _leer_estado(supabase, clave: str):
    respuesta = supabase.table("sync_estado").select("valor").eq("clave", clave).limit(1).execute()
    return respuesta.data[0]["valor"] if respuesta.data else None


def _guardar_estado(supabase, clave: str, valor: str):
    supabase.table("sync_estado").upsert({"clave": clave, "valor": valor}, on_conflict="clave").execute()


def _recargar_tabla(supabase, registros: list, errores: list, max_intentos: int = 3) -> int:
    """Vacía la tabla e inserta los registros por lotes con reintentos; los fallos definitivos van a `errores`."""
    supabase.table(TABLA).delete().neq("id", 0).execute()
    insertados = 0
    for i in range(0, len(registros), TAMANO_LOTE_INSERCION):
        lote = registros[i:i + TAMANO_LOTE_INSERCION]
        ultimo_error = None
        for intento in range(1, max_intentos + 1):
            try:
                supabase.table(TABLA).insert(lote).execute()
                insertados += len(lote)
                print(f"Progreso '{TABLA}': {insertados}/{len(registros)}...", flush=True)
                break
            except Exception as error:
                ultimo_error = error
                print(f"Aviso: intento {intento}/{max_intentos} fallido en '{TABLA}': {error}", flush=True)
                if intento < max_intentos:
                    time.sleep(2 * intento)
        else:
            errores.append(f"'{TABLA}': lote {i // TAMANO_LOTE_INSERCION + 1} no insertado ({ultimo_error})")
            break
    return insertados


def _escribir_resumen(lineas: list):
    ruta = os.environ.get("GITHUB_STEP_SUMMARY")
    if ruta:
        with open(ruta, "a", encoding="utf-8") as fichero:
            fichero.write("### Sincronización de licitaciones informadas\n\n" + "\n".join(f"- {l}" for l in lineas) + "\n")


def _resumen_de(registros: list) -> list:
    con_comentario = sum(1 for r in registros if r["comentarios"])
    anios = sorted({r["anio"] for r in registros if r["anio"]})
    return [
        f"Filas leídas: {len(registros)} (con comentarios: {con_comentario})",
        f"Años: {', '.join(map(str, anios)) or '—'}",
    ]


def ejecutar_sincronizacion():
    print("=" * 100, flush=True)
    print("SINCRONIZACIÓN DE LICITACIONES INFORMADAS — Google Drive -> Supabase", flush=True)
    print("=" * 100, flush=True)

    file_id = os.environ.get("GOOGLE_DRIVE_FILE_ID_INFORMADAS")
    if not file_id:
        raise RuntimeError("Falta la variable de entorno GOOGLE_DRIVE_FILE_ID_INFORMADAS.")

    from common import obtener_cliente_supabase

    supabase = obtener_cliente_supabase()
    servicio = obtener_servicio_drive()

    metadata = servicio.files().get(fileId=file_id, fields="modifiedTime, name, mimeType").execute()
    modificado_en = metadata["modifiedTime"]
    print(f"Fichero: {metadata.get('name')}  ·  Última modificación en Drive: {modificado_en}", flush=True)

    ultimo_conocido = _leer_estado(supabase, CLAVE_SYNC_ESTADO)
    version_guardada = _leer_estado(supabase, CLAVE_VERSION_INGESTA)

    motivo = None
    if os.environ.get("FORZAR_SINCRONIZACION", "").strip().lower() in {"1", "true", "yes", "si", "sí"}:
        motivo = "se ha pedido forzar la sincronización"
    elif version_guardada != VERSION_INGESTA:
        motivo = f"ha cambiado la lógica de ingesta ({version_guardada or 'sin versión previa'} -> {VERSION_INGESTA})"

    if ultimo_conocido == modificado_en and not motivo:
        print("No hay cambios desde la última sincronización. Nada que hacer.", flush=True)
        return

    print(f"Descargando ({motivo or 'cambio detectado o primera sincronización'})...", flush=True)
    buffer_excel = descargar_excel(servicio, file_id, metadata.get("mimeType", ""))

    # Se lee TODO antes de tocar Supabase: si el Excel no se puede leer, la base queda intacta.
    registros = leer_registros(buffer_excel)
    if not registros:
        raise RuntimeError("No se ha leído ninguna fila válida del Excel. Se aborta sin tocar Supabase.")
    resumen = _resumen_de(registros)
    print("\n".join(resumen), flush=True)

    errores = []
    insertados = _recargar_tabla(supabase, registros, errores)
    resumen.append(f"Filas sincronizadas: {insertados}/{len(registros)}")

    if errores:
        _escribir_resumen(resumen + [f"❌ {e}" for e in errores])
        raise RuntimeError("Sincronización INCOMPLETA (se reintentará en la próxima ejecución): " + " | ".join(errores))

    _guardar_estado(supabase, CLAVE_SYNC_ESTADO, modificado_en)
    _guardar_estado(supabase, CLAVE_VERSION_INGESTA, VERSION_INGESTA)
    print("Estado de sincronización actualizado.", flush=True)
    _escribir_resumen(resumen + ["✅ Sincronización completa"])


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--archivo":
        datos = leer_registros(sys.argv[2])
        print("\n".join(_resumen_de(datos)))
        for muestra in datos[:3]:
            print(muestra)
    else:
        ejecutar_sincronizacion()

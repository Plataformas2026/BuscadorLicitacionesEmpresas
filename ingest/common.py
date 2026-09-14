# -*- coding: utf-8 -*-
"""
common.py
---------
Utilidades compartidas por los scripts de ingesta (ingesta_afdb.py,
sync_empresas_drive.py). Mismo patrón que en el resto de proyectos de
esta familia:

  - Conexión a Supabase con la Service Role Key (permisos de escritura;
    la RLS solo deja leer a la clave anónima que usa la app -- ver
    sql/schema.sql y app/db.py).
  - Carga perezosa del modelo de embeddings: los scripts que no generan
    embeddings (ninguno de los actuales los necesita para leer, pero se
    deja preparado por si se añade un futuro script de solo-lectura) no
    se ven obligados a tener instalado sentence-transformers/PyTorch.
  - Subida en lotes con reintentos, y consulta de lo ya existente por
    lotes (para no traer tablas enteras a memoria).
"""
import os
import time
from functools import lru_cache

from supabase import Client, create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

NOMBRE_MODELO_EMBEDDING = "intfloat/multilingual-e5-small"  # 384 dimensiones


def obtener_cliente_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "Faltan las variables de entorno SUPABASE_URL o SUPABASE_SERVICE_KEY."
        )
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


@lru_cache(maxsize=1)
def _obtener_encoder():
    from sentence_transformers import SentenceTransformer

    print(f"Cargando modelo de embeddings '{NOMBRE_MODELO_EMBEDDING}'...", flush=True)
    return SentenceTransformer(NOMBRE_MODELO_EMBEDDING, device="cpu")


def generar_embedding(texto_completo: str) -> list:
    """El prefijo 'passage: ' es obligatorio con los modelos E5 (ver app/search.py)."""
    encoder = _obtener_encoder()
    return encoder.encode(f"passage: {texto_completo}").tolist()


def obtener_registros_existentes(supabase: Client, tabla: str, columna_clave: str, columnas: tuple, claves: list) -> dict:
    """
    Consulta solo los registros de `tabla` cuya `columna_clave` está en
    `claves` (en vez de traer la tabla entera), devolviendo un
    diccionario {clave: fila}.
    """
    registros = {}
    if not claves:
        return registros

    tamano_lote_in = 300
    claves_unicas = list(dict.fromkeys(claves))

    for i in range(0, len(claves_unicas), tamano_lote_in):
        trozo = claves_unicas[i:i + tamano_lote_in]
        respuesta = (
            supabase.table(tabla)
            .select(", ".join(columnas))
            .in_(columna_clave, trozo)
            .execute()
        )
        registros.update({fila[columna_clave]: fila for fila in respuesta.data})

    return registros


def subir_en_lotes(supabase: Client, tabla: str, on_conflict: str, registros: list, tamano_lote: int = 25, max_intentos: int = 3) -> int:
    total = len(registros)
    subidos = 0

    for i in range(0, total, tamano_lote):
        lote = registros[i:i + tamano_lote]
        numero_lote = i // tamano_lote + 1

        for intento in range(1, max_intentos + 1):
            try:
                supabase.table(tabla).upsert(lote, on_conflict=on_conflict).execute()
                subidos += len(lote)
                print(f"Progreso: {subidos}/{total} registros sincronizados en '{tabla}'...", flush=True)
                break
            except Exception as error:
                print(f"⚠️ Intento {intento}/{max_intentos} fallido para el lote {numero_lote} de '{tabla}': {error}", flush=True)
                if intento < max_intentos:
                    time.sleep(2 * intento)
                else:
                    print(f"❌ Error definitivo subiendo el lote {numero_lote} de '{tabla}'.", flush=True)

    return subidos

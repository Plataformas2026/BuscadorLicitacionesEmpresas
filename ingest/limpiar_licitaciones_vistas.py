# -*- coding: utf-8 -*-
"""
limpiar_licitaciones_vistas.py
----------------------------------
Elimina de `licitaciones_internacionales` las licitaciones marcadas como
"Visto" hace más de 3 días -- SALVO que también estén "Guardadas"
(Favoritos): guardar una licitación la protege del borrado automático,
tal como se pidió ("para que no se pierda").

Pensado para ejecutarse a diario (ver
.github/workflows/limpiar_licitaciones_vistas.yml).

Variables de entorno requeridas: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Ejecución local: python limpiar_licitaciones_vistas.py
"""
from datetime import datetime, timedelta, timezone

from common import obtener_cliente_supabase

DIAS_ANTES_DE_BORRAR = 3


def ejecutar_limpieza():
    print("=" * 100, flush=True)
    print("LIMPIEZA DE LICITACIONES VISTAS (más de 3 días, no guardadas)", flush=True)
    print("=" * 100, flush=True)

    limite = (datetime.now(timezone.utc) - timedelta(days=DIAS_ANTES_DE_BORRAR)).isoformat()
    print(f"Se eliminan las marcadas como 'Visto' antes de: {limite}", flush=True)

    supabase = obtener_cliente_supabase()

    respuesta = (
        supabase.table("licitaciones_internacionales")
        .select("id, codigo_unico, titulo, visto_en")
        .eq("visto", True)
        .eq("guardado", False)  # las guardadas NUNCA se borran automáticamente
        .lt("visto_en", limite)
        .execute()
    )
    candidatas = respuesta.data or []

    if not candidatas:
        print("No hay licitaciones que cumplan el criterio de borrado.", flush=True)
        return

    print(f"Licitaciones a eliminar: {len(candidatas)}", flush=True)
    for fila in candidatas:
        print(f"  - {fila['codigo_unico']}: {fila['titulo'][:80]}", flush=True)

    ids = [fila["id"] for fila in candidatas]
    for i in range(0, len(ids), 100):
        supabase.table("licitaciones_internacionales").delete().in_("id", ids[i:i + 100]).execute()

    print(f"\nEliminadas {len(candidatas)} licitaciones vistas caducadas.", flush=True)


if __name__ == "__main__":
    ejecutar_limpieza()

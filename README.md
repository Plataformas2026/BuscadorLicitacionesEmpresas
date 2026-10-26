# Licitaciones & Empresas

App de 3 pestañas: buscador de licitaciones internacionales, coincidencia
inteligente (licitación → empresas) y directorio de empresas.  

```
.
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── .streamlit/secrets.toml.example
├── sql/
│   └── schema.sql                          # tablas, JSONB, RLS, funciones RPC
├── app/                                     # Streamlit (solo lectura)
│   ├── app.py                               # entrypoint, st.tabs()
│   ├── config.py
│   ├── db.py
│   ├── styles.py                            # CSS compacto compartido
│   ├── search.py                            # Pestaña 1 — Buscador de Licitaciones
│   ├── matching.py                          # Pestaña 2 — Coincidencia Inteligente
│   └── directorio.py                        # Pestaña 3 — Directorio de Empresas
├── ingest/                                  # scripts de ingesta (backend, escritura)
│   ├── common.py
│   ├── ingesta_afdb.py
|   ├── ...                     
│   ├── limpiar_licitaciones_vistas.py       # Pestaña 1: borrado a los 3 días de "Visto"
│   ├── archivos_drive.py                    # File ID de los 3 Excel de Drive
│   ├── sync_empresas_drive.py               # BBDD_Empresas.xlsx -> Directorio y Estadísticas de adjudicación
│   ├── sync_licitaciones_informadas_drive.py # Seguimiento_Empresas.xlsx -> Estadísticas de interés
│   └── sync_palabras_clave_drive.py         # Palabras_clave.xlsx -> Coincidencia Inteligente
└── .github/workflows/
    ├── sincronizar_afdb.yml
    ├──  ...
    ├── limpiar_licitaciones_vistas.yml      # diario
    ├── sincronizar_empresas_drive.yml       # BBDD_Empresas.xlsx (diario)
    ├── sincronizar_licitaciones_informadas.yml # Seguimiento_Empresas.xlsx (diario)
    └── sincronizar_palabras_clave.yml       # Palabras_clave.xlsx (diario)
```


```


## Excel de Google Drive: qué alimenta cada uno

| Excel | File ID (secreto de GitHub) | Script → tablas de Supabase | Funcionalidad |
|---|---|---|---|
| `BBDD_Empresas.xlsx` | `GOOGLE_DRIVE_FILE_ID` | `sync_empresas_drive.py` → `empresas`, `empresas_referencias`, `empresas_hojas_extra` | Directorio de Empresas, Estadísticas de adjudicación (y la lista de empresas de Fiabilidad) |
| `Seguimiento_Empresas.xlsx` | `GOOGLE_DRIVE_FILE_ID_INFORMADAS` | `sync_licitaciones_informadas_drive.py` → `licitaciones_informadas` | Estadísticas de interés |
| `Palabras_clave.xlsx` | `GOOGLE_DRIVE_FILE_ID_PALABRAS_CLAVE` | `sync_palabras_clave_drive.py` → `empresas_palabras_clave` | Coincidencia Inteligente |

Los tres comparten la cuenta de servicio de Google (`GOOGLE_SERVICE_ACCOUNT_JSON`) y las claves de Supabase. Cada Excel escribe solo en
sus tablas: ninguno toca las de los otros.

### Palabras clave: puesta en marcha (una sola vez)
1. En Drive, abre `Palabras_clave.xlsx` → *Compartir* y añade como **Lector** el email de la cuenta de servicio (el campo `client_email` del
   JSON de `GOOGLE_SERVICE_ACCOUNT_JSON`). Sin esto la sincronización falla con «File not found».
2. Copia su **File ID**: es el trozo largo de la URL `https://drive.google.com/file/d/ESTE_ES_EL_FILE_ID/view`
   (en una hoja de Google Sheets: `https://docs.google.com/spreadsheets/d/ESTE_ES_EL_FILE_ID/edit`).
3. En GitHub: *Settings → Secrets and variables → Actions → New repository secret* → nombre `GOOGLE_DRIVE_FILE_ID_PALABRAS_CLAVE`, valor el File ID.
   (Alternativa: pegarlo en `FILE_ID_PALABRAS_CLAVE` de `ingest/archivos_drive.py`.)
4. En Supabase → *SQL Editor*, ejecuta `sql/crear_empresas_palabras_clave.sql`.
5. En GitHub → *Actions → Sincronizar palabras clave → Run workflow* (marca «forzar»). Comprueba en el resumen del job que lee 101 empresas.
6. Abre la pestaña Coincidencia Inteligente y lanza una búsqueda. A partir de ahí el workflow diario recoge los cambios del Excel; para verlos al
   momento, lánzalo a mano (la app refresca la tabla como mucho cada 5 minutos).

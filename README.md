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
│   └── sync_empresas_drive.py               # Pestañas 2/3: Google Drive -> Supabase
└── .github/workflows/
    ├── sincronizar_afdb.yml
    ├──  ...
    ├── limpiar_licitaciones_vistas.yml      # diario
    └── sincronizar_empresas_drive.yml       # cada 30 min (ver sección Google Drive)
```


```

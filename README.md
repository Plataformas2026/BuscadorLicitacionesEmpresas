# Licitaciones & Empresas

App de 3 pestañas: buscador de licitaciones internacionales, coincidencia
inteligente (licitación → empresas) y directorio de empresas. Misma
arquitectura gratuita que el resto de proyectos de esta familia:
Streamlit Community Cloud + Supabase + GitHub Actions.

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
│   ├── ingesta_afdb.py                      # Pestaña 1: scraping del AfDB
│   ├── limpiar_licitaciones_vistas.py       # Pestaña 1: borrado a los 3 días de "Visto"
│   └── sync_empresas_drive.py               # Pestañas 2/3: Google Drive -> Supabase
└── .github/workflows/
    ├── sincronizar_afdb.yml                 # diario
    ├── limpiar_licitaciones_vistas.yml      # diario
    └── sincronizar_empresas_drive.yml       # cada 30 min (ver sección Google Drive)
```

Principio de siempre: **la app solo lee, `ingest/` es lo único que
escribe/borra** -- con una única excepción deliberada: la propia app
puede marcar/desmarcar "Visto" y "Guardado" en tiempo real (ver sección
7). Por eso hay dos claves de Supabase (`SUPABASE_ANON_KEY` para la app,
`SUPABASE_SERVICE_KEY` para la ingesta) y RLS que solo deja `SELECT` (y,
solo sobre esas 4 columnas, `UPDATE`) a la clave anónima.


## 1. Supabase

Ejecuta [`sql/schema.sql`](sql/schema.sql) en **SQL Editor -> New
query**. Crea/recrea:

- `licitaciones_internacionales` (Pestaña 1, sin cambios respecto a
  antes).
- `empresas` y `empresas_referencias` (Pestañas 2 y 3) — **rediseñadas a
  fondo** tras analizar tu Excel real (`BBDD_Empresas_260909.xlsx`).
  Cada una combina columnas estructuradas (para filtrar/ordenar rápido)
  con una columna `datos_excel JSONB` que guarda la fila COMPLETA tal
  cual viene del Excel, cabecera real -> valor real, como red de
  seguridad para no perder nunca ninguna anotación atípica.
- Funciones RPC de búsqueda semántica (una por caso de uso: licitaciones,
  empresas-para-una-licitación-ya-en-BD, empresas-por-embedding-directo
  para licitaciones pegadas a mano, empresas del directorio con
  selección de idioma) y dos funciones auxiliares (`obtener_referencias_empresa`,
  `obtener_opciones_filtro_empresas`).

**Importante**: dado que el cambio de esquema es grande y no había datos
reales cargados todavía, el script **recrea** `empresas` y
`empresas_referencias` desde cero (`drop + create`). Si ya subiste datos
de prueba que quieras conservar, dímelo antes de ejecutarlo y preparo una
migración incremental en su lugar.

Credenciales: **Project Settings -> API** -> `Project URL` ->
`SUPABASE_URL`; clave `anon` `public` -> `SUPABASE_ANON_KEY` (para la
app); clave `service_role` -> `SUPABASE_SERVICE_KEY` (para la ingesta —
trátala como una contraseña de administrador).


## 2. La app de Streamlit

```bash
pip install -r requirements.txt
cd app
streamlit run app.py
```

Credenciales vía `.streamlit/secrets.toml` (copia
`.streamlit/secrets.toml.example`) o variables de entorno. La app usa
**siempre** la clave anónima.


## 3. Google Drive — cómo compartir el Excel con la app

La sincronización usa una **cuenta de servicio** de Google (no tu cuenta
personal), para que pueda ejecutarse sola desde GitHub Actions sin pedir
un login interactivo cada vez.

1. Ve a [Google Cloud Console](https://console.cloud.google.com/) ->
   crea un proyecto (o usa uno existente) -> **APIs & Services -> Library**
   -> busca **Google Drive API** -> actívala.
2. **APIs & Services -> Credentials -> Create Credentials -> Service
   Account**. Ponle un nombre (p. ej. `licitaciones-empresas-drive`) y
   termina el asistente (no hace falta darle ningún rol de proyecto).
3. Entra en la cuenta de servicio recién creada -> pestaña **Keys** ->
   **Add key -> Create new key -> JSON**. Se descarga un fichero
   `.json`: ese es el valor completo de `GOOGLE_SERVICE_ACCOUNT_JSON`
   (cópialo tal cual, como una única línea, al crear el secreto de
   GitHub Actions).
4. Copia el campo `"client_email"` de ese JSON (algo como
   `licitaciones-empresas-drive@tu-proyecto.iam.gserviceaccount.com`).
5. En Google Drive, **clic derecho sobre el Excel -> Compartir**, y
   comparte el fichero con ese `client_email` con permiso de
   **Lector** (no hace falta más).
6. El `GOOGLE_DRIVE_FILE_ID` es la parte de la URL del fichero entre
   `/d/` y `/view`:
   `https://drive.google.com/file/d/`**`ESTE_ES_EL_ID`**`/view`.

Añade `GOOGLE_SERVICE_ACCOUNT_JSON` y `GOOGLE_DRIVE_FILE_ID` (junto con
`SUPABASE_URL`/`SUPABASE_SERVICE_KEY`) como *Secrets* del repositorio en
**Settings -> Secrets and variables -> Actions**.

El workflow `sincronizar_empresas_drive.yml` comprueba cada 30 minutos si
el fichero ha cambiado (`modifiedTime`, guardado en la tabla
`sync_estado`); si no ha cambiado, la ejecución es casi instantánea y no
toca Supabase. GitHub Actions no ofrece triggers por webhook de Drive en
el plan gratuito -- para eso haría falta exponer un endpoint HTTPS
propio -- así que el sondeo periódico es la forma gratuita de acercarse
a "sincronizar en cuanto cambie". Ajusta la frecuencia del `cron` si
quieres detectarlo antes, a cambio de más minutos consumidos.


## 4. Qué hojas del Excel se ingieren, y por qué

Tu Excel real tiene 15 hojas. Se ingieren dos:

| Hoja | Tabla destino | Contenido |
|---|---|---|
| `DOSSIER COMPLETO` | `empresas` | Perfil de cada empresa (100 filas) |
| `REFERENCIAS P BÚSQUEDAS` | `empresas_referencias` | Histórico de licitaciones por empresa (1202 filas aprovechables) |

Las demás (`REFERENCIAS` más corta, `REF AGUA`, `REF TURISMO`, `REF TIC`,
`REF CONSULTO`, `REF TRANSP Y OTROS`, `REF MMAA`, `REF ECOS`, `REF TEMA`,
`REF CODEXCA`, `REF ACOSTA WET`, `lista para chatgpt`, `PALABRAS CLAVE
PARA BÚSQUEDAS`) se han revisado y son **vistas derivadas** de esas dos
hojas principales -- subconjuntos filtrados por sector, o exports de
trabajo para pegar en un chat --, no datos nuevos. Si alguna sí tiene
información que no esté en las dos hojas principales, dímelo y ajusto
`ingest/sync_empresas_drive.py` para incluirla.

**Dos cosas que quedaron ambiguas** al leer tu Excel real (te las señalé
también en el chat):
1. Hay **dos columnas tituladas literalmente "SECTOR"** en `DOSSIER
   COMPLETO`: la primera es una taxonomía numerada de 6 valores (p. ej.
   "2. TURISMO, PROMOCIÓN Y MARKETING"), la segunda (en naranja en tu
   Excel) tiene valores como "CONSULTORÍA ESTRATÉGICA" o "TIC -
   Ciberseguridad". Se guarda en `sector_secundario`, sin usarla como
   filtro (tu lista de filtros no la menciona) -- si tiene un
   significado concreto, dímelo y le pongo mejor nombre.
2. El enlace por nombre entre `empresas_referencias` y `empresas`
   (necesario porque la hoja de referencias no trae el "ID", solo el
   nombre de la empresa) solo encuentra coincidencia exacta (sin
   distinguir mayúsculas/tildes) para el **65%** de las filas (786/1202)
   -- el resto queda con `id_empresa = NULL` pero se guarda igualmente
   (nunca se descarta). Si quieres, puedo añadir una coincidencia más
   flexible (por subcadena, o tolerante a "S.L."/"SL"/"S.A." al final)
   como siguiente paso.

**Sobre los "5 idiomas"**: en el Excel real solo se han encontrado 4
columnas de idioma para las palabras clave (`PALABRAS CLAVE` en español
+ `...EN INGLÉS` + `...EN FRANCÉS` + `...EN PORTUGUÉS`). Si hay una 5ª
en otra hoja o con otra cabecera, dímelo -- añadirla es solo una entrada
más en `MAPEO_EMPRESAS` y una columna de embedding más; el resto del
código ya está preparado para ese caso.


## 5. Búsqueda semántica multilingüe (Pestaña 3)

Cada empresa tiene, además del embedding en español (usado también por
la Pestaña 2), un embedding en inglés/francés/portugués -- pero **solo
si esa empresa tiene palabras clave propias en ese idioma** (si no, se
deja a `NULL` y la búsqueda cae automáticamente al embedding en español
en vez de gastar cómputo generando uno idéntico). La detección de idioma
de la consulta del usuario usa `langdetect` (gratuita, sin API, sin
conexión a internet) -- ver `app/directorio.py:detectar_idioma`.


## 6. AfDB (Pestaña 1)

### Fuente (corregida)

La versión anterior scrapeaba una página equivocada
(`/en/documents/category/specific-procurement-notices`, filtrando solo
títulos que empezaran literalmente por "SPN -"/"GPN -") -- por eso solo
subía 1 registro: casi ningún aviso real usa ese prefijo. La fuente
correcta, confirmada contra la página real, es:

    https://www.afdb.org/en/projects-and-operations/procurement?page=N

Un listado mucho más grande (+12.000 documentos) con tipos de aviso muy
variados: AMI (Appel à Manifestation d'Intérêt), AAO (Appel d'Offres
Ouvert), IFB (Invitation For Bids), EOI (Expression Of Interest), PPM,
SPN, GPN... `ingest/ingesta_afdb.py` reconoce cualquier prefijo de 2 a 6
letras mayúsculas seguido de " - país - ..." (no solo SPN/GPN), y
descarta explícitamente los avisos de resultado/adjudicación
("Attribution de..."/"Contract Award...": ya no son oportunidades
abiertas).

### Fecha de cierre (deadline) -- heurística de mejor esfuerzo

Ni el listado ni la ficha HTML de cada aviso traen la fecha de cierre en
ningún campo estructurado (se comprobó descargando una ficha real) --
solo aparece dentro del PDF adjunto, como texto libre ("...must be
submitted... no later than 13 March 2026...", "...au plus tard le...").
Por cada aviso dentro de la ventana de sincronización, el script:

1. Descarga la ficha HTML (para la descripción y la URL del PDF/documento adjunto).
2. Si hay un PDF, descarga sus 3 primeras páginas y busca frases
   habituales que anuncian el plazo, en inglés, francés y portugués
   (`FRASES_DISPARADORAS_FECHA_CIERRE` en el código), y si encuentra una,
   intenta leer una fecha justo después con `dateutil.parser` (traduciendo
   antes los nombres de mes en francés/portugués, que `dateutil` no
   reconoce por defecto).
3. Descarta cualquier fecha que no caiga entre hoy y +3 años: mejor no
   guardar `fecha_limite` que guardar una fecha sin sentido por una mala
   lectura del PDF.

Es una heurística sobre texto libre en varios idiomas y formatos de PDF
distintos -- no va a acertar siempre; se ha probado contra frases reales
de avisos ya publicados por el AfDB (ver las pruebas que se entregan
aparte), no hay garantía de cobertura al 100%. Cuando no se detecta, la
app muestra "Cierre: No detectada" en vez de inventar un dato.

Por el volumen de peticiones que esto añade (ficha + PDF por cada aviso
candidato), hay un tope `MAX_DETALLES_POR_EJECUCION` (80 por defecto):
si hay más candidatos en la ventana de 3 días, el resto se recoge en la
siguiente ejecución programada.


## 7. "Visto" y "Guardado" (Pestaña 1)

Única parte de la app con permiso de ESCRITURA desde el navegador (con
la clave anónima) -- ver `sql/schema.sql`, que concede `UPDATE` **solo**
sobre las columnas `visto`/`visto_en`/`guardado`/`guardado_en`
(comprobado con un `SET ROLE anon` real: intentar tocar cualquier otra
columna falla con "permission denied"). Como la app no tiene login, este
estado es compartido por todo el que la use, no por usuario individual.

- **Visto**: desaparece al instante de la vista principal "Buscar" y
  pasa a la sección "Vistas". `ingest/limpiar_licitaciones_vistas.py`
  (a diario, ver el workflow correspondiente) la borra a los 3 días de
  marcarse -- **salvo que también esté "Guardada"**. Se puede desmarcar
  en cualquier momento mientras no se haya borrado.
- **Guardado**: pasa a "Favoritos" de forma permanente; nunca se borra
  automáticamente, y protege del borrado por "Visto" aunque ambas
  estén marcadas a la vez. Se puede desmarcar en cualquier momento.


## 8. Coincidencia Inteligente (Pestaña 2)

Además de la similitud semántica, `explicar_coincidencia()` en
`app/matching.py` compara, con heurísticas de solapamiento de texto (sin
LLM, nada inventado):

- `PRINCIPALES PROYECTOS TIPO` y `DESCRIPCIÓN BREVE DE LA ACTIVIDAD` de
  la empresa frente al título + descripción de la licitación.
- `PREFERENCIAS LICITACIONES` de la empresa.
- Títulos de licitaciones antiguas de la empresa (tabla
  `empresas_referencias`, cargada desde la hoja "REFERENCIAS P
  BÚSQUEDAS" del Excel -- ver Sección 4): si alguna se parece a la
  licitación actual, se muestra junto con si fue adjudicada o no.

`preferencias_licitaciones` se ha añadido a las funciones RPC
`buscar_empresas_para_licitacion` y `buscar_empresas_por_embedding` en
`sql/schema.sql` (antes no se devolvía). Es un cambio aditivo: ninguna
columna ni función existente se ha eliminado ni renombrado.

Cada empresa candidata incluye también una tabla comparando el lugar de
la licitación contra los 4 campos geográficos ya definidos en el
Directorio de Empresas (Experiencia países, Zona geográfica de interés,
Países de interés, Ámbito geográfico), marcando "Coincide" / "No
coincide" / "Sin datos" en cada uno.


## 9. Probar en local

```bash
pip install -r requirements.txt
export SUPABASE_URL="https://TU-PROYECTO.supabase.co"
export SUPABASE_SERVICE_KEY="tu_clave_service_role"
export GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
export GOOGLE_DRIVE_FILE_ID="tu_id_de_archivo"

cd ingest
python ingesta_afdb.py
python sync_empresas_drive.py
python limpiar_licitaciones_vistas.py
```

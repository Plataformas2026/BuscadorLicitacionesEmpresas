"""
matching.py
-----------
Pestaña 2 (Coincidencia Inteligente: Licitación -> Empresas), sobre la base de datos de empresas del Excel
(Palabras_clave.xlsx en Google Drive -> workflow «Sincronizar palabras clave» -> tabla `empresas_palabras_clave` de Supabase; ver palabras_clave_datos.py), que es la única fuente de empresas de esta pestaña. No usa `empresas` (BBDD_Empresas.xlsx) ni `licitaciones_informadas`.

Flujo al pulsar «Buscar» (dos fases separadas e independientes):
  FASE 1 (ranking_embeddings.py): UN solo ranking por embeddings. La similitud usa EXCLUSIVAMENTE nombre de la empresa,
          sector, palabras clave extraídas de su descripción y principales proyectos tipo. Se conservan las 15 mejores.
  FASE 2 (reranking_groq.py): GPT (Groq) evalúa esas 15 y las reordena por adecuación real (técnica pesa mucho más que la
          geografía). Solo se excluye una empresa si el país/zona de la licitación figura expresamente entre sus zonas
          descartadas; la falta de información geográfica nunca penaliza ni excluye.
Pantalla: primero las empresas por puntuación de GPT (con justificación); debajo, el ranking original por embeddings con sus
posiciones y las palabras que puntuaron; y una tabla para comparar ambas posiciones. Las excluidas siguen apareciendo en el
historial de embeddings (marcadas) pero no como recomendaciones. Si GPT falla, la fase 1 se sigue mostrando.

La búsqueda de la licitación (por enlace, código o texto) sigue usando Supabase, como antes.
"""
import hashlib
import json
import re

import pandas as pd
import streamlit as st
from sentence_transformers import SentenceTransformer
from supabase import Client

import palabras_clave_datos
import ranking_embeddings
import reranking_groq
from config import RANKING_TOP_N
from search import buscar_semantica

PATRON_URL = re.compile(r"^https?://", re.IGNORECASE)


def _completar_campos_licitacion(supabase: Client, candidatos: list) -> list:
    """
    La RPC de búsqueda semántica (buscar_licitaciones_internacionales) no
    tiene por qué devolver `categoria`, `organismo` ni `fuente_origen`:
    se completan, en UNA sola consulta, los que falten en los candidatos.
    Si la consulta falla, se devuelven tal cual (sin categoría).
    """
    faltan = [
        c for c in candidatos
        if c.get("codigo_unico") and any(k not in c for k in ("categoria", "organismo", "fuente_origen"))
    ]
    if not faltan:
        return candidatos
    try:
        respuesta = (
            supabase.table("licitaciones_internacionales")
            .select("codigo_unico, categoria, organismo, fuente_origen")
            .in_("codigo_unico", [c["codigo_unico"] for c in faltan])
            .execute()
        )
    except Exception:
        return candidatos

    extra = {f["codigo_unico"]: f for f in (respuesta.data or [])}
    for candidato in faltan:
        fila = extra.get(candidato["codigo_unico"], {})
        for campo in ("categoria", "organismo", "fuente_origen"):
            candidato.setdefault(campo, fila.get(campo))
    return candidatos


def localizar_licitacion(supabase: Client, encoder: SentenceTransformer, entrada: str) -> list:
    """
    Devuelve una lista de licitaciones candidatas para la entrada del
    usuario. Si hay un match EXACTO por enlace o por codigo_unico,
    devuelve solo ese (confianza total). Si no, hace una búsqueda
    semántica por el texto y devuelve los primeros candidatos para que el
    usuario elija.
    """
    entrada = entrada.strip()
    if not entrada:
        return []

    columnas = (
        "codigo_unico, titulo, descripcion, pais, organismo, categoria, tipo_aviso, fuente_origen, "
        "url_oficial, fecha_publicacion, fecha_limite"
    )

    # 1. Match exacto por enlace
    if PATRON_URL.match(entrada):
        respuesta = (
            supabase.table("licitaciones_internacionales")
            .select(columnas)
            .eq("url_oficial", entrada)
            .limit(1)
            .execute()
        )
        if respuesta.data:
            return respuesta.data

    # 2. Match exacto por codigo_unico (con o sin mayúsculas)
    respuesta = (
        supabase.table("licitaciones_internacionales")
        .select(columnas)
        .ilike("codigo_unico", entrada)
        .limit(1)
        .execute()
    )
    if respuesta.data:
        return respuesta.data

    # 3. Búsqueda semántica por título/texto -> varios candidatos a elegir
    candidatos = buscar_semantica(supabase, encoder, entrada, match_threshold=0.15, match_count=8)
    return _completar_campos_licitacion(supabase, candidatos[:8])


# ------------------------------------------------------------------
# Empresas (tabla empresas_palabras_clave <- Palabras_clave.xlsx) e índice de embeddings
# ------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def _empresas_cacheadas(_supabase: Client) -> list:
    """Lee la tabla (como mucho cada 5 min): así una actualización del Excel se refleja sola tras la sincronización."""
    return palabras_clave_datos.cargar_empresas(_supabase)


@st.cache_resource(show_spinner="Calculando los embeddings de las empresas...")
def _indice(huella: str, _empresas: list, _encoder):
    return ranking_embeddings.construir_indice(_encoder, _empresas)


def _base_empresas(supabase, encoder):
    """(empresas, índice de embeddings). El índice se recalcula solo cuando cambian los datos (huella del contenido)."""
    empresas = _empresas_cacheadas(supabase)
    huella = hashlib.sha1(json.dumps([[e["clave_empresa"], e["fecha_actualizacion"]] for e in empresas]).encode()).hexdigest()
    return empresas, _indice(huella, empresas, encoder)


# ------------------------------------------------------------------
# Interfaz de la Pestaña 2
# ------------------------------------------------------------------
def _resetear_seleccion():
    st.session_state.tab2_candidatos = None
    st.session_state.tab2_licitacion_elegida = None
    st.session_state.tab2_coincidencias = None


def _firma(licitacion: dict) -> str:
    return "|".join(str(licitacion.get(k) or "") for k in ("codigo_unico", "titulo", "descripcion_manual", "categoria", "pais")) \
        + f"|{RANKING_TOP_N}"


def _fmt(valor) -> str:
    return f"{valor:.1f}".rstrip("0").rstrip(".") if isinstance(valor, float) else str(valor)


def _cambio(pos_gpt: int, pos_emb: int) -> str:
    if pos_gpt < pos_emb:
        return f"▲ sube {pos_emb - pos_gpt}"
    if pos_gpt > pos_emb:
        return f"▼ baja {pos_gpt - pos_emb}"
    return "= igual"


def _fase1(supabase, encoder, licitacion: dict) -> dict:
    """Ranking por embeddings de las `RANKING_TOP_N` mejores + explicación de palabras. Sin GPT."""
    empresas, indice = _base_empresas(supabase, encoder)
    por_id = {e["numero_interno"]: e for e in empresas}
    filas, vector = ranking_embeddings.ranking(encoder, indice, licitacion, top=RANKING_TOP_N)
    for f in filas:
        f["explicacion"] = ranking_embeddings.explicar(encoder, por_id[f["numero_interno"]], licitacion, vector)
    return {"firma": _firma(licitacion), "filas": filas, "total_empresas": len(empresas)}


def _fase2(supabase, licitacion: dict, fase1: dict, encoder) -> dict:
    empresas, _ = _base_empresas(supabase, encoder)
    por_id = {e["numero_interno"]: e for e in empresas}
    return reranking_groq.reordenar(licitacion, [por_id[f["numero_interno"]] for f in fase1["filas"]])


def _informe(clave: str, licitacion: dict, empresa: dict, fila: dict, evaluacion: dict, encoder):
    """Botón «Generar informe más completo» de una empresa (otra petición a GPT, solo bajo demanda)."""
    informes = st.session_state.setdefault("tab2_informes", {})
    firma = _firma(licitacion) + "|" + clave
    if st.button("📄 Generar informe más completo", key=f"tab2_rerank_amp_{clave}"):
        with st.spinner("GPT está redactando el informe completo..."):
            posicion = {"posicion": fila["posicion"], "puntuacion_embeddings": fila["puntuacion"]}
            informes[firma] = reranking_groq.ampliar_explicacion(
                licitacion, empresa, posicion,
                {"puntuacion": evaluacion["puntuacion"], "tecnica": evaluacion["tecnica"], "bonus_geo": evaluacion["bonus_geo"],
                 "justificacion": evaluacion["justificacion"]})
    if firma in informes:
        texto, error = informes[firma]
        if error:
            st.error(f"No se pudo generar el informe: {error}")
        else:
            st.markdown(texto)


def _seccion_gpt(licitacion, fase1, res, por_id, filas_por_id, encoder):
    st.markdown("### 🤖 Empresas recomendadas (ordenadas por GPT)")
    if res is None:
        return
    if not res["ok"]:
        st.error(f"No se pudo reordenar con GPT: {res['error']}")
        if res.get("detalle_error"):
            with st.expander("Detalle técnico"):
                st.code(res["detalle_error"])
        if st.button("🔄 Reintentar la reordenación con GPT", key="tab2_rerank_reintentar"):
            st.session_state.tab2_rerank_pendiente = True
            st.rerun()
        st.info("El ranking por embeddings (más abajo) sigue siendo válido.")
        return
    st.caption(
        f"Puntuación = adecuación técnica (actividad y proyectos tipo) + una ventaja geográfica mínima (máx. {reranking_groq.BONUS_GEO_MAXIMO}). "
        f"Modelo: {res['modelo']}."
    )
    for aviso in res["avisos"]:
        st.warning(aviso)
    if not res["ranking"]:
        st.warning("Ninguna de las empresas candidatas es una recomendación válida.")
    for e in res["ranking"]:
        fila = filas_por_id[e["numero_interno"]]
        empresa = por_id[e["numero_interno"]]
        with st.container(border=True):
            st.markdown(
                f"**{e['posicion']}. {empresa['nombre_empresa']}** · **{_fmt(e['puntuacion'])} pts** "
                f"(técnica {_fmt(e['tecnica'])}" + (f" + geografía {e['bonus_geo']}" if e["bonus_geo"] else "") + ")"
            )
            st.caption(f"Embeddings: nº {fila['posicion']} ({_fmt(fila['puntuacion'])} pts) · {_cambio(e['posicion'], fila['posicion'])}")
            st.write(e["justificacion"])
            for aviso in e["avisos"]:
                st.caption("⚠️ " + aviso)
            _informe(e["numero_interno"], licitacion, empresa, fila, e, encoder)
    if res["excluidas"]:
        st.markdown("#### ⛔ Excluidas geográficamente (no son recomendaciones válidas)")
        for e in res["excluidas"]:
            fila = filas_por_id[e["numero_interno"]]
            st.error(f"**{por_id[e['numero_interno']]['nombre_empresa']}** (nº {fila['posicion']} en embeddings): {e['motivo_exclusion']}")
    for clave, motivo in res["no_evaluadas"]:
        st.caption(f"Sin evaluar: {por_id[clave]['nombre_empresa']} — {motivo}")


def _tabla_comparativa(fase1, res, por_id) -> pd.DataFrame:
    gpt = {e["numero_interno"]: e for e in (res["ranking"] if res and res["ok"] else [])}
    excl = {e["numero_interno"]: e for e in (res["excluidas"] if res and res["ok"] else [])}
    filas = []
    for f in fase1["filas"]:
        i = f["numero_interno"]
        fila = {"Empresa": por_id[i]["nombre_empresa"], "Pos. embeddings": f["posicion"], "Puntuación embeddings": f["puntuacion"]}
        if res and res["ok"]:
            if i in gpt:
                fila.update({"Pos. GPT": gpt[i]["posicion"], "Puntuación GPT": gpt[i]["puntuacion"],
                             "Cambio": _cambio(gpt[i]["posicion"], f["posicion"])})
            elif i in excl:
                fila.update({"Pos. GPT": None, "Puntuación GPT": None, "Cambio": "⛔ excluida"})
            else:
                fila.update({"Pos. GPT": None, "Puntuación GPT": None, "Cambio": "sin evaluar"})
        filas.append(fila)
    return pd.DataFrame(filas)


def _seccion_embeddings(fase1, res, por_id):
    st.markdown("### 🔢 Ranking original por embeddings")
    st.caption(
        "Similitud (×100) entre la licitación y cada empresa, usando solo su nombre, sector, palabras clave extraídas de la "
        "descripción y principales proyectos tipo. La lista de palabras es una aproximación para entender el resultado; "
        "no modifica la puntuación."
    )
    st.dataframe(_tabla_comparativa(fase1, res, por_id), hide_index=True, use_container_width=True)
    excl = {e["numero_interno"]: e for e in (res["excluidas"] if res and res["ok"] else [])}
    gpt = {e["numero_interno"]: e for e in (res["ranking"] if res and res["ok"] else [])}
    for f in fase1["filas"]:
        i = f["numero_interno"]
        marca = " · ⛔ excluida geográficamente" if i in excl else (f" · GPT: nº {gpt[i]['posicion']}" if i in gpt else "")
        with st.expander(f"{f['posicion']}. {por_id[i]['nombre_empresa']} — {_fmt(f['puntuacion'])} pts{marca}"):
            if i in excl:
                st.error(excl[i]["motivo_exclusion"])
            ex = f["explicacion"]
            hay = False
            for campo, palabras in ex["literales"].items():
                st.markdown(f"**{ranking_embeddings.ETIQUETAS_CAMPO[campo]}** — palabras en común con la licitación: " + ", ".join(palabras))
                hay = True
            if ex["frases_literales"]:
                st.markdown("**Palabras clave que aparecen en la licitación:** " + "; ".join(ex["frases_literales"]))
                hay = True
            if ex["frases"]:
                st.markdown("**Palabras clave más afines por significado:** " + "; ".join(f"{p} ({_fmt(s)})" for p, s in ex["frases"]))
                hay = True
            if not hay:
                st.caption("Sin palabras en común; la similitud viene del significado global del texto.")


def _resultados(supabase, licitacion, encoder):
    fase1 = st.session_state.get("tab2_fase1")
    if not fase1 or fase1["firma"] != _firma(licitacion):
        return
    empresas, _ = _base_empresas(supabase, encoder)
    por_id = {e["numero_interno"]: e for e in empresas}
    filas_por_id = {f["numero_interno"]: f for f in fase1["filas"]}

    if st.session_state.pop("tab2_rerank_pendiente", False):
        if reranking_groq.disponible():
            with st.spinner(f"GPT está evaluando las {len(fase1['filas'])} empresas..."):
                st.session_state.tab2_rerank = {"firma": fase1["firma"], "res": _fase2(supabase, licitacion, fase1, encoder)}
        else:
            st.session_state.tab2_rerank = {"firma": fase1["firma"], "res": reranking_groq.reordenar(licitacion, [])}
    rr = st.session_state.get("tab2_rerank")
    res = rr["res"] if rr and rr["firma"] == fase1["firma"] else None

    st.success(f"Fase 1: {len(fase1['filas'])} empresas más parecidas de {fase1['total_empresas']} en la base de datos.")
    st.caption("Origen de las empresas: Palabras_clave.xlsx (tabla `empresas_palabras_clave` de Supabase).")
    _seccion_gpt(licitacion, fase1, res, por_id, filas_por_id, encoder)
    st.divider()
    _seccion_embeddings(fase1, res, por_id)


def render_tab2(supabase: Client, encoder: SentenceTransformer):
    st.subheader("Coincidencia Inteligente: Licitación → Empresas")
    st.caption(
        "Introduce el título, el enlace oficial o el identificador de una licitación. "
        "Si no está en nuestra base de datos, también puedes describirla directamente."
    )

    for clave, valor in (
        ("tab2_candidatos", None),
        ("tab2_licitacion_elegida", None),
        ("tab2_coincidencias", None),
    ):
        if clave not in st.session_state:
            st.session_state[clave] = valor

    entrada = st.text_input(
        "Título, enlace o identificador de la licitación",
        placeholder="ej. AFDB-spn-namibia-... · https://www.afdb.org/... · perforación de pozos en Namibia",
        key="tab2_entrada",
        on_change=_resetear_seleccion,
    )

    if st.button("Buscar licitación", key="tab2_buscar_licitacion", use_container_width=True):
        with st.spinner("Buscando la licitación..."):
            st.session_state.tab2_candidatos = localizar_licitacion(supabase, encoder, entrada)
            st.session_state.tab2_licitacion_elegida = None
            st.session_state.tab2_coincidencias = None

    candidatos = st.session_state.tab2_candidatos

    if candidatos:
        if len(candidatos) == 1:
            st.session_state.tab2_licitacion_elegida = candidatos[0]
            st.success(f"Licitación identificada: **{candidatos[0]['titulo']}**")
        else:
            opciones = {f"{c['titulo']}  ·  {c.get('pais') or 's/país'}": c for c in candidatos}
            etiqueta_elegida = st.selectbox(
                "Se han encontrado varias licitaciones parecidas — elige la correcta:",
                list(opciones.keys()),
                key="tab2_selector_candidato",
            )
            st.session_state.tab2_licitacion_elegida = opciones[etiqueta_elegida]

    elif candidatos is not None:
        st.warning("No se ha encontrado ninguna licitación parecida en nuestra base de datos.")

    with st.expander("¿No está en la base de datos? Descríbela directamente"):
        titulo_manual = st.text_input("Título de la licitación", key="tab2_titulo_manual")
        descripcion_manual = st.text_area("Descripción / objeto del contrato", key="tab2_descripcion_manual", height=80)
        categoria_manual = st.text_input("Categoría (opcional)", key="tab2_categoria_manual")
        pais_manual = st.text_input("País (opcional; permite valorar la geografía y las exclusiones)", key="tab2_pais_manual")
        if st.button("Usar esta descripción", key="tab2_usar_manual"):
            if titulo_manual.strip():
                st.session_state.tab2_licitacion_elegida = {
                    "titulo": titulo_manual,
                    "pais": pais_manual.strip() or None,
                    "codigo_unico": None,
                    "descripcion_manual": descripcion_manual,
                    "categoria": categoria_manual.strip() or None,
                }
                st.session_state.tab2_coincidencias = None
            else:
                st.warning("Escribe al menos un título.")

    licitacion = st.session_state.tab2_licitacion_elegida

    if licitacion:
        st.markdown(f"**Licitación seleccionada:** {licitacion['titulo']}")
        if licitacion.get("categoria"):
            st.caption(f"Categoría: {licitacion['categoria']}")
        if licitacion.get("pais"):
            st.caption(f"País: {licitacion['pais']}")

        if st.button("🔎 Buscar", key="tab2_buscar_empresas", use_container_width=True):
            st.session_state.pop("tab2_rerank", None)
            st.session_state.pop("tab2_fase1", None)
            try:
                with st.spinner("Calculando el ranking por embeddings..."):
                    st.session_state.tab2_fase1 = _fase1(supabase, encoder, licitacion)
                st.session_state.tab2_rerank_pendiente = True
            except ValueError as error:
                st.error(str(error))
        try:
            _resultados(supabase, licitacion, encoder)
        except ValueError as error:
            st.error(str(error))

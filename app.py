# ======================================================================
#  app.py — Gestion des plans de table pour un loto
#  ======================================================================
#  Fonctionnalités :
#  - Réservations : nom, téléphone (obligatoire), email (option), cartons, places
#  - Stockage en base de données SQLite
#  - Allocation automatique des tables (8 max par table par défaut)
#  - Génération du plan de salle (graphique)
#  - Coloration avancée selon taux d’occupation
#  - Export CSV (via dataframe) + Export PDF du plan
#  - Recherche par nom ou téléphone (accent-insensible, fuzzy)
# ======================================================================

import math
import re
import sqlite3
from datetime import datetime
from math import ceil
from typing import List, Dict, Tuple, Optional

import pandas as pd
import streamlit as st
import plotly.graph_objects as go

# PDF
from io import BytesIO
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape, portrait
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    _REPORTLAB_OK = True
except Exception as _e:
    _REPORTLAB_OK = False
    _REPORTLAB_ERR = _e

# ======================================================================
#  Base de données SQLite
# ======================================================================
DB_PATH = "loto.db"


def create_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            places INTEGER NOT NULL,
            cartons INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


create_database()


def insert_reservation(res_id, name, phone, email, places, cartons):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO reservations (id, name, phone, email, places, cartons, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (res_id, name, phone, email, places, cartons, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def next_res_id(prefix: str = "RES", width: int = 5) -> str:
    """
    Génère le prochain identifiant de réservation au format RES00001, RES00002, ...
    - Ne considère que les IDs commençant par le préfixe
    - Verrouille la base (BEGIN IMMEDIATE) pour éviter les collisions
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")  # verrou court
        cur.execute("SELECT id FROM reservations WHERE id LIKE ?;", (prefix + '%',))
        rows = cur.fetchall()

        if not rows:
            new_id = f"{prefix}{1:0{width}d}"
        else:
            nums = []
            for (rid,) in rows:
                if rid.startswith(prefix):
                    try:
                        nums.append(int(rid[len(prefix):]))
                    except ValueError:
                        pass
            last = max(nums) if nums else 0
            new_id = f"{prefix}{last + 1:0{width}d}"

        conn.commit()
        return new_id

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_reservations():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, phone, email, places, cartons
        FROM reservations
    """)
    data = cursor.fetchall()
    conn.close()
    return data


def delete_reservation(res_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM reservations WHERE id = ?", (res_id,))
    conn.commit()
    conn.close()


# ======================================================================
#  Interface Streamlit – configuration générale
# ======================================================================
st.set_page_config(
    page_title="Plans de table - Loto",
    page_icon="🎟️",
    layout="wide",
)

st.sidebar.image("logo.png", width=180)
st.title("🎟️ Gestion des plans de table (Loto)")
st.caption("Tables de 8 personnes max • Capacité totale 300 personnes")


# ======================================================================
#  Paramètres de la salle
# ======================================================================
st.sidebar.header("Paramètres")

TABLE_CAPACITY = st.sidebar.number_input(
    "Capacité par table",
    min_value=2, max_value=20, value=8, step=1
)

MAX_PEOPLE = st.sidebar.number_input(
    "Capacité totale",
    min_value=20, max_value=1000, value=300, step=10
)

override_tables = st.sidebar.checkbox("Définir manuellement le nombre de tables")
manual_tables: Optional[int] = None
if override_tables:
    manual_tables = st.sidebar.number_input(
        "Nombre de tables imposé",
        min_value=1, max_value=400, value=10, step=1
    )

# --- Confirmation de réinitialisation (via modal) ---
if "ask_confirm_reset" not in st.session_state:
    st.session_state.ask_confirm_reset = False

# 1) Clic sur le bouton -> on demande confirmation
if st.sidebar.button("🧹 Réinitialiser les réservations"):
    st.session_state.ask_confirm_reset = True

# 2) Si on demande confirmation, on affiche un popup (modal)
if st.session_state.get("ask_confirm_reset"):
    # Si vous avez Streamlit >= 1.30 : st.modal est disponible
    try:
        use_modal = hasattr(st, "modal")
    except Exception:
        use_modal = False

    def _render_confirmation_ui():
        # (Optionnel) proposer une sauvegarde CSV avant suppression
        try:
            _rows_for_backup = get_reservations()
            if _rows_for_backup:
                _df_backup = pd.DataFrame(
                    _rows_for_backup,
                    columns=["ID", "Réservant", "Téléphone", "Email", "Places", "Cartons"]
                )
                _csv_bytes = _df_backup.to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "📥 Télécharger une sauvegarde CSV des réservations",
                    data=_csv_bytes,
                    file_name=f"reservations_backup_{datetime.now():%Y%m%d_%H%M}.csv",
                    mime="text/csv",
                    help="Recommandé avant suppression"
                )
        except Exception:
            pass

        st.warning(
            "⚠️ Cette action va **supprimer définitivement toutes les réservations** "
            "de la base `loto.db`. Cette opération est **irréversible**."
        )
        col_cancel, col_confirm = st.columns(2)
        with col_cancel:
            if st.button("❌ Annuler", key="cancel_reset"):
                st.session_state.ask_confirm_reset = False
                st.rerun()
        with col_confirm:
            if st.button("✅ Oui, supprimer tout", type="primary", key="confirm_reset"):
                try:
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM reservations")
                    conn.commit()
                    conn.close()
                    st.session_state.ask_confirm_reset = False
                    st.success("Toutes les réservations ont été supprimées.")
                    st.rerun()
                except Exception as e:
                    st.session_state.ask_confirm_reset = False
                    st.error(f"Erreur lors de la suppression : {e}")

    if use_modal:
        with st.modal("Confirmer la réinitialisation"):
            _render_confirmation_ui()
    else:
        # Fallback sans modal (anciennes versions de Streamlit)
        st.subheader("Confirmer la réinitialisation")
        _render_confirmation_ui()


# ======================================================================
#  Validation téléphone + email
# ======================================================================
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE_FR = re.compile(r"^(?:\+33\s?|0)(?:[1-9])(?:[\s\-]?\d{2}){4}$")
PHONE_RE_ALT = re.compile(r"^\+?\d{8,15}$")


def is_phone_valid(phone: str) -> bool:
    p = phone.strip()
    return bool(PHONE_RE_FR.match(p) or PHONE_RE_ALT.match(p))


# ======================================================================
#  Recherche réservants : normalisation & fuzzy
# ======================================================================
import unicodedata
from difflib import SequenceMatcher


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def _norm_name(s: str) -> str:
    s = _strip_accents(s).lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_phone(s: str) -> str:
    s = (s or "").replace("+33", "0")
    return re.sub(r"\D+", "", s)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(a=a, b=b).ratio()


def search_reservations(
    name: Optional[str] = None,
    phone: Optional[str] = None,
    fuzzy: bool = True,
    threshold: float = 0.75,
    limit: int = 100,
) -> list:
    """
    Recherche de réservants par nom (accent-insensible, partiel, fuzzy) et/ou téléphone (partiel).
    Retourne les meilleurs résultats triés par score décroissant.
    """
    # 1) Récupération en base
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""SELECT id, name, phone, email, places, cartons, created_at FROM reservations""")
    rows = cur.fetchall()
    conn.close()

    qn = _norm_name(name) if name else None
    qp = _norm_phone(phone) if phone else None

    results = []
    for (rid, rname, rphone, remail, rplaces, rcartons, rcreated) in rows:
        nname = _norm_name(rname or "")
        nphone = _norm_phone(rphone or "")

        name_score = 0.0
        phone_score = 0.0
        matched_by = None

        # Nom
        if qn:
            if qn in nname:
                name_score = 1.0 if qn == nname else 0.9
            elif fuzzy:
                name_score = _ratio(qn, nname)
            if name_score >= threshold:
                matched_by = "name"

        # Téléphone (partiel >= 3 chiffres)
        if qp:
            if len(qp) >= 3 and qp in nphone:
                phone_score = 1.0 if qp == nphone else 0.95
                matched_by = "phone" if not matched_by else "both"

        include = False
        score = 0.0
        if qn and qp:
            include = (name_score >= threshold) or (phone_score >= 0.8)
            score = (name_score * 0.6 + phone_score * 0.4)
            if matched_by == "both":
                score += 0.05
        elif qn:
            include = (name_score >= threshold)
            score = name_score
        elif qp:
            include = (len(qp) >= 3 and qp in nphone)
            score = phone_score or (1.0 if qp == nphone else 0.85)

        if include:
            results.append({
                "ID": rid,
                "Réservant": rname,
                "Téléphone": rphone,
                "Email": remail,
                "Places": rplaces,
                "Cartons": rcartons,
                "Créée le": rcreated,
                "matched_by": matched_by or ("name" if qn else "phone"),
                "score": round(min(score, 1.0), 4),
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:max(0, limit)]


# ======================================================================
#  Fractionnement d'une réservation > TABLE_CAPACITY
# ======================================================================
def split_reservations(reservations: List[Dict], cap: int) -> List[Dict]:
    chunks = []
    for r in reservations:
        rem = int(r["places"])
        part = 1
        while rem > cap:
            chunks.append({
                "id": f"{r['id']}-{part}",
                "name": r["name"],
                "phone": r["phone"],
                "email": r["email"],
                "cartons": r["cartons"],
                "places": cap,
            })
            rem -= cap
            part += 1

        if rem > 0:
            chunks.append({
                "id": f"{r['id']}-{part}",
                "name": r["name"],
                "phone": r["phone"],
                "email": r["email"],
                "cartons": r["cartons"],
                "places": rem,
            })
    return chunks


# ======================================================================
#  Allocation automatique des tables
# ======================================================================
def allocate_tables(reservations: List[Dict], cap: int, max_people: int, manual_tables: int | None):
    total_people = sum(int(r["places"]) for r in reservations)

    if total_people == 0:
        return [], {"total_people": 0, "tables": 0, "capacity": 0, "free": 0, "fill_rate": 0, "unplaced": []}

    if total_people > max_people:
        return [], {"error": f"Capacité totale dépassée : {total_people} > {max_people}", "unplaced": []}

    chunks = split_reservations(reservations, cap)
    chunks.sort(key=lambda x: x["places"], reverse=True)

    needed_tables = math.ceil(total_people / cap)
    T = manual_tables if manual_tables else needed_tables

    tables = [{"table_no": i + 1, "items": [], "free": cap} for i in range(T)]
    unplaced = []

    for c in chunks:
        placed = False
        for t in tables:
            if t["free"] >= c["places"]:
                t["items"].append(c)
                t["free"] -= c["places"]
                placed = True
                break

        if not placed:
            if manual_tables:
                unplaced.append(c)
            else:
                new_no = len(tables) + 1
                tables.append({"table_no": new_no, "items": [c], "free": cap - c["places"]})

    capacity = len(tables) * cap
    return tables, {
        "total_people": total_people,
        "tables": len(tables),
        "capacity": capacity,
        "free": sum(t["free"] for t in tables),
        "fill_rate": total_people / capacity,
        "unplaced": unplaced
    }


# ======================================================================
#  FONCTION : Plan de salle (graphique) + COLORATION AVANCÉE
# ======================================================================
def build_hall_plan(
    tables: list,
    cols: int = 3,
    circular: bool = False,
    table_size: float = 40,
    h_gap: float = 0.02,
    v_gap: float = 0.04,
    font_size: int = 10,
) -> go.Figure:

    n = len(tables)
    if n == 0:
        fig = go.Figure()
        fig.add_annotation(text="Aucune table à afficher", showarrow=False)
        return fig

    rows = ceil(n / cols)

    width = cols * (table_size + h_gap) + h_gap
    height = rows * (table_size + v_gap) + v_gap

    fig = go.Figure()

    # -----------------------------
    # COLORATION AVANCÉE
    # -----------------------------
    def occupancy_color(free, cap=TABLE_CAPACITY):
        used = (cap - free) / cap
        if used <= 0.40:
            return "#b2f2bb"   # vert
        elif used <= 0.70:
            return "#fff3bf"   # jaune
        elif used <= 0.90:
            return "#ffd8a8"   # orange
        else:
            return "#ffa8a8"   # rouge

    # -----------------------------
    # Rendu des tables
    # -----------------------------
    for idx, t in enumerate(tables):

        r = idx // cols
        c = idx % cols

        cx = h_gap + table_size / 2 + c * (table_size + h_gap)
        cy = height - (v_gap + table_size / 2 + r * (table_size + v_gap))

        x0, y0 = cx - table_size / 2, cy - table_size / 2
        x1, y1 = cx + table_size / 2, cy + table_size / 2

        # couleur selon taux occupation
        color = occupancy_color(t["free"])

        fig.add_shape(
            type="rect",
            x0=x0, y0=y0, x1=x1, y1=y1,
            fillcolor=color,
            line=dict(color="#1c7ed6", width=3),
        )

        # --------------------------------------
        #   TEXTE 3 LIGNES PAR RÉSERVATION
        # --------------------------------------
        lines = [f"<b>Table {t['table_no']}</b>"]  # titre

        items = t.get("items", [])
        if items:
            for it in items:
                name = str(it.get("name", "")).strip()
                phone = str(it.get("phone", "")).strip()
                places = int(it.get("places", 0))

                if len(name) > 22:
                    name = name[:21] + "…"

                block = (
                    name + "<br>" +
                    (f"📞 {phone}" if phone else "") + "<br>" +
                    f"{places} pers."
                )

                lines.append(block)
        else:
            lines.append("Libre")

        text = "<br><br>".join(lines)

        fig.add_annotation(
            x=cx, y=cy,
            text=text,
            showarrow=False,
            font=dict(size=font_size, color="#222"),
            align="center",
        )

    # -----------------------------
    # Axes & Layout
    # -----------------------------
    fig.update_xaxes(visible=False, range=[0, width])
    fig.update_yaxes(visible=False, range=[0, height], scaleanchor="x", scaleratio=1)

    fig.update_layout(
        height=min(1400, int(350 * rows)),
        margin=dict(l=20, r=20, t=40, b=20),
        title=f"Plan de salle – {len(tables)} table(s)",
        plot_bgcolor="white",
    )

    return fig


# ======================================================================
#  Export PDF du plan (A4, grille auto)
# ======================================================================
def generate_table_plan_pdf_bytes(
    tables: list[dict],
    title: str = "Plan de salle - Loto",
    subtitle: Optional[str] = None,
    orientation: str = "landscape",  # "landscape" ou "portrait"
    grid_cols: int = 4,
    margins_mm: int = 10,
    show_phones: bool = False,
) -> BytesIO:
    """
    Génère un PDF en mémoire représentant le plan des tables.
    - tables : liste de dicts comme renvoyée par allocate_tables (table_no, items[], free)
    - show_phones : inclure les numéros (sinon masqués)
    """
    if not _REPORTLAB_OK:
        raise RuntimeError(
            "Le module reportlab est requis pour l'export PDF. "
            "Installe-le : pip install reportlab"
        ) from _REPORTLAB_ERR

    buf = BytesIO()
    ps = A4
    ps = landscape(ps) if orientation == "landscape" else portrait(ps)
    c = canvas.Canvas(buf, pagesize=ps)
    width, height = ps

    margin = margins_mm * mm
    content_w = width - 2 * margin
    content_h = height - 2 * margin

    # En-têtes
    c.setTitle(title)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, height - margin + 2*mm, title)
    c.setFont("Helvetica", 10)
    sub = subtitle or f"Édité le {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    c.drawString(margin, height - margin - 3*mm, sub)

    # Placement en grille
    n = len(tables)
    cols = max(1, grid_cols)
    rows = int(math.ceil(n / cols))
    gap = 6 * mm

    cell_w = (content_w - (cols - 1) * gap) / cols
    cell_h = (content_h - (rows - 1) * gap) / rows

    def mask_phone(s: str) -> str:
        digits = re.sub(r"\D+", "", s or "")
        if len(digits) >= 10:
            return f"{digits[:2]}••••{digits[-2:]}"
        return s or ""

    # Dessin
    idx = 0
    for r in range(rows):
        for ccol in range(cols):
            if idx >= n:
                break
            t = tables[idx]
            idx += 1

            x = margin + ccol * (cell_w + gap)
            y = height - margin - (r + 1) * cell_h - r * gap

            # Cartouche table
            c.setFillColor(colors.whitesmoke)
            c.setStrokeColor(colors.black)
            c.rect(x, y, cell_w, cell_h, fill=1, stroke=1)

            # Titre table
            c.setFillColor(colors.black)
            c.setFont("Helvetica-Bold", 12)
            c.drawString(x + 4*mm, y + cell_h - 7*mm, f"Table {t['table_no']}")

            # Occupation (à droite)
            try:
                used = TABLE_CAPACITY - int(t.get("free", 0))
            except Exception:
                used = sum(int(it.get("places", 0)) for it in t.get("items", []))
            cap_txt = f"{used}/{TABLE_CAPACITY} places"
            c.setFont("Helvetica", 9)
            tw = c.stringWidth(cap_txt, "Helvetica", 9)
            c.drawString(x + cell_w - 4*mm - tw, y + cell_h - 7*mm, cap_txt)

            # ------------------------------
            # Détail réservations (3 lignes)
            # ------------------------------
            y_text = y + cell_h - 14*mm
            c.setFont("Helvetica", 10)
            items = t.get("items", [])

            if not items:
                c.setFont("Helvetica-Oblique", 9)
                c.setFillColor(colors.grey)
                c.drawString(x + 4*mm, y_text, "(aucune réservation)")
                c.setFillColor(colors.black)
                c.setFont("Helvetica", 10)
            else:
                for it in items:
                    nm = (it.get("name") or "").strip()
                    ph = (it.get("phone") or "").strip()
                    ## if not show_phones and ph:
                    ##   ph = mask_phone(ph)
                    places = int(it.get("places", 0))

                    # Ligne 1 : nom
                    c.drawString(x + 4*mm, y_text, f"• {nm}")
                    y_text -= 5*mm

                    # Ligne 2 : nombre de personnes
                    c.drawString(x + 4*mm, y_text, f"{places} pers.")
                    y_text -= 5*mm

                    # Ligne 3 : téléphone (option)
                    if ph:
                        c.drawString(x + 4*mm, y_text, ph)
                        y_text -= 5*mm

                    # Espace entre réservants
                    y_text -= 2*mm

                    # Nouvelle page si débord
                    if y_text < y + 6*mm:
                        c.showPage()
                        # En-tête sur la nouvelle page
                        c.setFont("Helvetica-Bold", 18)
                        c.drawString(margin, height - margin + 2*mm, title)
                        c.setFont("Helvetica", 10)
                        c.drawString(margin, height - margin - 3*mm, sub)
                        # Reprise du texte
                        y_text = height - margin - 20*mm
                        c.setFont("Helvetica", 10)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf
# ======================================================================
#  Formulaire d'ajout de réservation
# ======================================================================
with st.form("add_res_form", clear_on_submit=True):
    st.subheader("➕ Ajouter une réservation")

    name = st.text_input("Nom du réservant *")
    phone = st.text_input("Numéro de Téléphone *", placeholder="Exemple de saisie :    +33 6 12 34 56 78    ou    06 88 41 06 36")
    email = st.text_input("Adresse Email (option)")
    places = st.number_input("Nombre de places *", min_value=1, max_value=MAX_PEOPLE, value=2)
    cartons = st.number_input("Nombre de cartons", min_value=0, max_value=100, value=0)

    submitted = st.form_submit_button("Ajouter")
    if submitted:
        if not name.strip():
            st.error("Nom obligatoire")
        elif not is_phone_valid(phone):
            st.error("Téléphone invalide")
        elif email.strip() and not EMAIL_RE.match(email.strip()):
            st.error("Email invalide")
        else:
            # Génère un ID propre, sans collision
            res_id = next_res_id()

            try:
                insert_reservation(
                    res_id,
                    name.strip(),
                    phone.strip(),
                    email.strip(),
                    int(places),
                    int(cartons)
                )
                st.success(f"Réservation ajoutée (ID {res_id}).")
                st.rerun()

            except sqlite3.IntegrityError:
                st.error("Erreur : impossible d'insérer l'ID (collision). Réessayez.")


# ======================================================================
#  Liste des réservations + suppression
# ======================================================================
st.subheader("📋 Réservations")

rows = get_reservations()
if not rows:
    st.info("Aucune réservation.")
else:
    df = pd.DataFrame(rows, columns=["ID", "Réservant", "Téléphone", "Email", "Places", "Cartons"])
    st.dataframe(df, use_container_width=True)

    id_to_del = st.selectbox("ID à supprimer", df["ID"].tolist())
    if st.button("🗑️ Supprimer"):
        delete_reservation(id_to_del)
        st.success("Supprimé")
        st.rerun()


# ======================================================================
#  Recherche de réservants (nom / téléphone)
# ======================================================================
st.subheader("🔎 Rechercher un réservant")

with st.form("search_form"):
    qname = st.text_input("Nom (partiel, accents tolérés)")
    qphone = st.text_input("Téléphone (partiel, ex: 61234)")
    colA, colB, colC = st.columns([1, 1, 1])
    with colA:
        fuzzy = st.checkbox("Tolérance approximative (fuzzy)", value=True)
    with colB:
        threshold = st.slider("Seuil fuzzy", 0.5, 1.0, 0.75, 0.01)
    with colC:
        limit = st.number_input("Max résultats", min_value=1, max_value=500, value=100, step=1)

    do_search = st.form_submit_button("Rechercher")
    if do_search:
        if not qname and not qphone:
            st.warning("Renseigne le nom et/ou le téléphone.")
        else:
            matches = search_reservations(
                name=qname or None,
                phone=qphone or None,
                fuzzy=fuzzy,
                threshold=threshold,
                limit=limit
            )
            if not matches:
                st.info("Aucun résultat.")
            else:
                sdf = pd.DataFrame(matches)
                st.dataframe(sdf.drop(columns=["matched_by", "score"]), use_container_width=True)
                with st.expander("Voir les scores de correspondance (debug)"):
                    st.dataframe(pd.DataFrame(matches), use_container_width=True)


# ======================================================================
#  Génération du plan de tables
# ======================================================================
st.subheader("🪑 Générer le plan")

if st.button("⚙️ Cliquez ici pour générer le plan de la salle"):
    reservations = [
        {
            "id": r[0], "name": r[1], "phone": r[2],
            "email": r[3], "places": r[4], "cartons": r[5]
        }
        for r in rows
    ]
    tables, stats = allocate_tables(reservations, TABLE_CAPACITY, MAX_PEOPLE, manual_tables)

    if "error" in stats:
        st.error(stats["error"])
    else:
        st.session_state.plan = {"tables": tables, "stats": stats}
        st.success("Plan généré")


# ======================================================================
#  AFFICHAGE : Plan graphique
# ======================================================================
st.subheader("🗺️ Plan graphique de la salle")

if st.session_state.get("plan"):
    tables = st.session_state["plan"]["tables"]

    fig = build_hall_plan(
        tables,
        cols=4,
        circular=False,
        table_size=40,    # échelle cohérente avec la fonction
        font_size=12
    )

    st.plotly_chart(fig, use_container_width=True)

else:
    st.info("Aucun plan généré.")


# ======================================================================
#  Impression PDF du plan
# ======================================================================
st.subheader("🖨️ Impression PDF du plan des tables")

if st.session_state.get("plan"):
    tables = st.session_state["plan"]["tables"]

    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        orientation = st.selectbox("Orientation", ["Paysage", "Portrait"], index=0)
    with col2:
        grid_cols = st.number_input("Colonnes (grille auto)", min_value=1, max_value=12, value=4, step=1)
    with col3:
        show_phones = st.checkbox("Inclure les numéros de téléphone", value=False)

    if not _REPORTLAB_OK:
        st.warning("Le module reportlab n'est pas installé. Installe-le avec : `pip install reportlab`")
    else:
        if st.button("📄 Générer le PDF"):
            buf = generate_table_plan_pdf_bytes(
                tables,
                title="Plan de salle - Loto",
                subtitle=datetime.now().strftime("Export du %d/%m/%Y à %H:%M"),
                orientation="landscape" if orientation == "Paysage" else "portrait",
                grid_cols=grid_cols,
                show_phones=show_phones,
            )
            st.download_button(
                "📥 Télécharger le PDF",
                data=buf,
                file_name=f"plan_tables_{datetime.now():%Y%m%d_%H%M}.pdf",
                mime="application/pdf",
            )
else:
    st.info("Aucun plan généré.")

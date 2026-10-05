"""
Warehouse Zone Map Editor
=========================
A Streamlit app for turning a movable/non-movable BaseMap grid into an
annotated ZoneMap (Storage, Pick, Load, Induct, Build, Traffic, Queue areas),
backed by MongoDB for storage of the source BaseMap(s) and versioned ZoneMap(s).

Station IDs: Storage/Stow, Pick, Load (Put), Induct and Build areas can carry an
optional Station ID (set in the "Station ID" field next to the Palette
heading). When a Station ID is entered (and is not blank or 0), it is appended
to the zone code written into the grid, so a Stow Area painted with ID 22 is
stored as "S22" instead of "S".

Run with:
    streamlit run app.py

Configuration (all optional, set via environment variables):
    MONGO_URI         - connection string (default: mongodb://localhost:27017)
    MONGO_DB          - database name (default: warehouse_mapping)
    MONGO_COLLECTION  - default source collection, holding BaseMap docs (default: base_maps)

MONGO_URI and MONGO_DB are not shown in the UI. The source collection (where
"Start from a Base Map" looks for documents) and target collection (where
ZoneMap versions are saved) are both plain text fields the user can edit in
section 1; source collection defaults to MONGO_COLLECTION above but can be
pointed at any other collection — including the target collection — to pull
in a previously saved Zone Map as a new starting point.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
from pymongo import MongoClient
from pymongo.errors import PyMongoError

try:
    import mongomock
    MONGOMOCK_AVAILABLE = True
except ImportError:
    MONGOMOCK_AVAILABLE = False


# --------------------------------------------------------------------------
# Palette / domain constants
# --------------------------------------------------------------------------

# code -> (label, color). Order here drives legend + palette-picker order.
PALETTE: dict[str, tuple[str, str]] = {
    "S": ("Storage / Stow Areas", "#F4A300"),
    "P": ("Pick Stations", "#2ECC71"),
    "L": ("Load (Put) Stations", "#3498DB"),
    "I": ("Induct Area", "#E53935"),
    "B": ("Build Area", "#8B5A2B"),
    "T": ("Traffic Lanes", "#9B59B6"),
    "Q": ("Queue Areas", "#F1C40F"),
    "1": ("Legal / Movable (Base)", "#FAFAFA"),
    "0": ("Illegal / Not Movable", "#8B8B8B"),
}
VALID_CODES = set(PALETTE.keys())

# Zone types that can carry a Station ID. When an ID is supplied, the grid
# value becomes <code><id> (e.g. "S22") instead of just <code> ("S").
STATION_ID_CODES = {"S", "P", "L", "I", "B"}
STATION_ID_MAX_LEN = 5

# The sample BaseMap supplied with this app (0 = non-movable, 1 = movable).
SAMPLE_BASE_MAP = [
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
]
SAMPLE_BASE_MAP_ID = "warehouse-01-base"

DEFAULT_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DEFAULT_DB = os.environ.get("MONGO_DB", "warehouse_mapping")
DEFAULT_SOURCE_COLL = os.environ.get("MONGO_COLLECTION", "base_maps")
DEFAULT_TARGET_COLL = "target_logical_maps"

UNDO_LIMIT = 25

# --------------------------------------------------------------------------
# Password gate
#
# Only a salted SHA-256 hash of the password is stored below — never the
# plaintext — so reading this source file does not reveal the password.
# To change the password: pick a new one, compute
#   hashlib.sha256((AUTH_SALT + "<new-password>").encode("utf-8")).hexdigest()
# and paste the result into AUTH_PASSWORD_HASH. There is no way to recover
# the plaintext password from the hash.
# --------------------------------------------------------------------------
AUTH_SALT = "wz-map-editor-v1:"
AUTH_PASSWORD_HASH = "991fdc06262e948761a83356d06c92de274d19d8fe55a5484e50bea484ef91a9"


def _check_password(candidate: str) -> bool:
    digest = hashlib.sha256((AUTH_SALT + candidate).encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, AUTH_PASSWORD_HASH)


def password_gate() -> None:
    """Block the rest of the app until the correct password is entered.

    Renders nothing but a password prompt (and, after a wrong attempt, a
    "bad password" message) until authenticated for this browser session,
    then lets the caller continue. Calls st.stop() internally so no app
    content is ever built before authentication succeeds.
    """
    if st.session_state.get("_authenticated"):
        return

    st.session_state.setdefault("_auth_failed", False)

    if st.session_state["_auth_failed"]:
        st.write("bad password")

    with st.form("_auth_form", clear_on_submit=True):
        pw = st.text_input("Password", type="password", label_visibility="collapsed", placeholder="Password")
        submitted = st.form_submit_button("Enter")

    if submitted:
        if _check_password(pw):
            st.session_state["_authenticated"] = True
            st.session_state["_auth_failed"] = False
            st.rerun()
        else:
            st.session_state["_auth_failed"] = True
            st.rerun()

    st.stop()


# --------------------------------------------------------------------------
# Mongo helpers
# --------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_client(uri: str, use_mock: bool):
    """Cached Mongo client. A new (uri, use_mock) pair creates a new client."""
    if use_mock:
        return mongomock.MongoClient()
    return MongoClient(uri, serverSelectionTimeoutMS=4000)


def test_connection(client, use_mock: bool) -> tuple[bool, str]:
    if use_mock:
        return True, "Using in-memory demo database (mongomock). Data will not persist."
    try:
        client.admin.command("ping")
        return True, "Connected to MongoDB."
    except PyMongoError as exc:
        return False, f"Could not reach MongoDB: {exc}"
    except Exception as exc:  # noqa: BLE001 - surface any driver error to the UI
        return False, f"Could not reach MongoDB: {exc}"


_COLLECTION_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,255}$")


def is_valid_collection_name(name: str) -> bool:
    """Conservative validity check for a MongoDB collection name: non-empty,
    no '$' or null bytes, not a reserved 'system.*' name, and restricted to a
    safe character set (letters, digits, '_', '-', '.')."""
    if not name:
        return False
    if name.startswith("system."):
        return False
    return bool(_COLLECTION_NAME_RE.match(name))


def to_str_grid(raw_grid: list[list]) -> list[list[str]]:
    """Normalize a grid (ints or strings) to a grid of upper-case strings."""
    return [[str(v).strip().upper() for v in row] for row in raw_grid]


# --------------------------------------------------------------------------
# Zone code / Station ID helpers
# --------------------------------------------------------------------------

def base_code(v: str) -> str | None:
    """Zone type for a grid value, or None if the value isn't valid.

    Plain palette codes ("S", "T", "1", ...) map to themselves. For zone types
    that support Station IDs (S, P, L, I), a value like "S22" or "P3" also
    maps to its leading type code ("S", "P")."""
    if v in PALETTE:
        return v
    if (
        v
        and v[0] in STATION_ID_CODES
        and 1 < len(v) <= 1 + STATION_ID_MAX_LEN
        and v[1:].isascii()
        and v[1:].isalnum()
    ):
        return v[0]
    return None


def normalize_station_id(raw: str | None) -> tuple[str, str | None]:
    """Clean up the Station ID field. Returns (station_id, error).

    A blank value, or a value that is zero ("0", "00", ...), means "no
    Station ID" and comes back as "". Otherwise the ID is upper-cased and
    must be letters/digits only."""
    s = (raw or "").strip().upper()
    if not s:
        return "", None
    if not (s.isascii() and s.isalnum()):
        return "", "Station ID may only contain letters and digits."
    if s.isdigit() and int(s) == 0:
        return "", None
    return s, None


def make_cell_code(zone_type: str, station_id: str) -> str:
    """Value written into the grid: '<type><id>' when the type supports
    Station IDs and one was supplied (e.g. 'S22'), otherwise just '<type>'."""
    if station_id and zone_type in STATION_ID_CODES:
        return f"{zone_type}{station_id}"
    return zone_type


def validate_grid(grid: list[list[str]], allowed: set[str]) -> list[str]:
    problems = []
    if not grid or not isinstance(grid, list):
        return ["Grid is empty."]
    width = len(grid[0])
    for r, row in enumerate(grid):
        if len(row) != width:
            problems.append(f"Row {r} has length {len(row)}, expected {width} (ragged grid).")
        for c, v in enumerate(row):
            # Accept plain codes, and ID-suffixed codes like "S22" whose
            # leading zone type is allowed.
            if v not in allowed and base_code(v) not in allowed:
                problems.append(f"Cell (row {r}, col {c}) has invalid value '{v}'.")
    if len(problems) > 8:
        problems = problems[:8] + [f"...and {len(problems) - 8} more issue(s)."]
    return problems


def base_map_display_id(doc: dict) -> str:
    """Human-readable identifier for a document shown in the "Start from a
    Base Map" picker.

    Docs may be an actual BaseMap, from this app's own sample/seed format
    ('mapId') or the fleet-manager schema ('fmModuleMapId') — or, since the
    source collection can be pointed at a target (ZoneMap) collection to
    reuse a previously saved map as a new starting point, a ZoneMap document
    (identified by its 'metadata.mapName').
    """
    meta = doc.get("metadata") or {}
    return str(
        doc.get("fmModuleMapId")
        or doc.get("mapId")
        or meta.get("mapName")
        or doc.get("_id", "unknown")
    )


def base_map_option_label(doc: dict) -> str:
    meta = doc.get("metadata") or {}
    parts = [base_map_display_id(doc)]
    if doc.get("fleetManagerId"):
        parts.append(str(doc["fleetManagerId"]))
    if doc.get("name"):
        parts.append(str(doc["name"]))
    if meta.get("versionId") is not None:
        parts.append(f"v{meta['versionId']}")
    return " — ".join(parts)


def extract_base_map_grid(doc: dict) -> list[list] | None:
    """BaseMap grids may be stored under 'mapGrid' (fleet-manager schema)
    or 'grid' (this app's original sample-map schema)."""
    grid = doc.get("mapGrid")
    if grid is None:
        grid = doc.get("grid")
    return grid


def list_base_maps(db, source_coll: str) -> list[dict]:
    try:
        cursor = db[source_coll].find({}, {"grid": 0, "mapGrid": 0})
        docs = list(cursor)
        docs.sort(key=base_map_display_id)
        return docs
    except PyMongoError:
        return []


def load_base_map(db, source_coll: str, doc_id) -> dict | None:
    try:
        return db[source_coll].find_one({"_id": doc_id})
    except PyMongoError:
        return None


def list_zone_map_names(db, target_coll: str) -> list[str]:
    try:
        return sorted(db[target_coll].distinct("metadata.mapName"))
    except PyMongoError:
        return []


def list_zone_map_versions(db, target_coll: str, map_name: str) -> list[dict]:
    try:
        cursor = (
            db[target_coll]
            .find({"metadata.mapName": map_name}, {"grid": 0})
            .sort("metadata.versionId", -1)
        )
        return list(cursor)
    except PyMongoError:
        return []


def load_zone_map(db, target_coll: str, doc_id) -> dict | None:
    try:
        return db[target_coll].find_one({"_id": doc_id})
    except PyMongoError:
        return None


def generate_blank_grid(width: int, height: int, fill_code: str = "1") -> list[list[str]]:
    """Build a height x width grid filled with a single code (default '1' =
    Legal / Movable), for starting a new map of arbitrary size from scratch."""
    return [[fill_code for _ in range(width)] for _ in range(height)]


def build_base_map_document(grid: list[list[str]], map_id: str, name: str = "") -> dict:
    """BaseMap JSON in this app's own schema (mapId/grid) — also readable back
    via extract_base_map_grid()/base_map_display_id(), which understand both
    this schema and the fleet-manager 'fmModuleMapId'/'mapGrid' schema."""
    height = len(grid)
    width = len(grid[0]) if height else 0
    return {
        "mapId": map_id,
        "mapType": "BaseMap",
        "name": name or map_id,
        "width": width,
        "height": height,
        "grid": grid,
        "createdDate": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def seed_sample_base_map(db, source_coll: str) -> tuple[bool, str]:
    try:
        if db[source_coll].find_one({"mapId": SAMPLE_BASE_MAP_ID}):
            return False, "Sample base map already exists in this collection."
        doc = {
            "mapId": SAMPLE_BASE_MAP_ID,
            "mapType": "BaseMap",
            "name": "Warehouse 01 - Sample Base Layout",
            "width": len(SAMPLE_BASE_MAP[0]),
            "height": len(SAMPLE_BASE_MAP),
            "grid": SAMPLE_BASE_MAP,
            "createdDate": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        db[source_coll].insert_one(doc)
        return True, "Sample base map inserted."
    except PyMongoError as exc:
        return False, f"Could not seed sample base map: {exc}"


def build_zone_map_document(
    grid: list[list[str]],
    map_name: str,
    source_map_id: str,
    notes: str,
    change_date_text: str,
) -> dict:
    height = len(grid)
    width = len(grid[0]) if height else 0
    return {
        "metadata": {
            "versionId": int(time.time()),  # unix timestamp (epoch) of creation
            "changeDate": change_date_text,  # plain text
            "mapType": "ZoneMap",
            "mapName": map_name,
            "sourceMapId": source_map_id,
            "width": width,
            "height": height,
            "notes": notes,
        },
        "grid": grid,
    }


def save_zone_map(db, target_coll: str, document: dict) -> tuple[bool, str]:
    try:
        result = db[target_coll].insert_one(copy.deepcopy(document))
        return True, str(result.inserted_id)
    except PyMongoError as exc:
        return False, f"Could not save zone map: {exc}"


# --------------------------------------------------------------------------
# Grid / chart helpers
# --------------------------------------------------------------------------

def make_figure(
    grid: list[list[str]],
    dragmode: str = "select",
    max_width_px: int = 1000,
    max_height_px: int = 650,
):
    rows = len(grid)
    cols = len(grid[0]) if rows else 0

    xs, ys, types, hover = [], [], [], []
    for r in range(rows):
        for c in range(cols):
            raw = grid[r][c]
            # Colour/legend by zone type; a Station ID suffix (e.g. "S22")
            # only shows up in the hover text.
            if raw in PALETTE:
                t, sid = raw, ""
            else:
                t = base_code(raw)
                sid = raw[1:] if t else ""
                t = t or "0"
            label = PALETTE[t][0]
            xs.append(c)
            ys.append(r)
            types.append(t)
            tip = f"Row (Y): {r}<br>Col (X): {c}<br>Type: {t} — {label}"
            if sid:
                tip += f"<br>Station ID: {sid}"
            hover.append(tip)

    df = pd.DataFrame({"x": xs, "y": ys, "type": types, "hover": hover})

    color_map = {code: color for code, (_, color) in PALETTE.items()}
    category_order = list(PALETTE.keys())

    fig = px.scatter(
        df,
        x="x",
        y="y",
        color="type",
        color_discrete_map=color_map,
        category_orders={"type": category_order},
        custom_data=["hover"],
    )
    fig.update_traces(
        hovertemplate="%{customdata[0]}<extra></extra>",
        marker=dict(symbol="square", line=dict(width=1, color="rgba(0,0,0,0.25)")),
        selected=dict(marker=dict(opacity=1)),
        unselected=dict(marker=dict(opacity=0.55)),
    )

    # Fixed chrome around the plot area: left/right/top/bottom margins, plus a
    # reserved column to the right for the legend. Sizing fig_width/fig_height
    # to exactly this chrome + the plot pixels (no extra padding) keeps the
    # container tight to the actual map — important because the axes are
    # locked to a 1:1 scale, so any slack we leave in one dimension just
    # becomes dead space once that lock shrinks the plotting domain to fit.
    MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 10, 10, 10, 10
    LEGEND_RESERVE_PX = 190
    MIN_CELL_PX, MAX_CELL_PX = 4, 34

    # Fit-to-bounding-box sizing: pick the largest square cell size that keeps
    # BOTH the full width (including margins + legend) under max_width_px AND
    # the full height under max_height_px. This is computed directly from the
    # grid's own aspect ratio, so a wide-but-short grid (e.g. 100x32) ends up
    # wide-but-short on screen too, instead of being forced into a fixed
    # height that leaves dead space above/below once the 1:1 scale lock
    # shrinks the plotting domain to match. Previously a hard 12px/cell floor
    # made fig_width balloon past the real container width on wide grids,
    # which is what caused that leftover vertical whitespace.
    width_budget = max(max_width_px - MARGIN_L - MARGIN_R - LEGEND_RESERVE_PX, MIN_CELL_PX)
    height_budget = max(max_height_px - MARGIN_T - MARGIN_B, MIN_CELL_PX)
    cell_w = width_budget / max(cols, 1)
    cell_h = height_budget / max(rows, 1)
    cell_px = max(MIN_CELL_PX, min(MAX_CELL_PX, cell_w, cell_h))

    fig_width = int(round(cell_px * cols + MARGIN_L + MARGIN_R + LEGEND_RESERVE_PX))
    fig_height = int(round(cell_px * rows + MARGIN_T + MARGIN_B))

    fig.update_traces(marker=dict(size=cell_px * 0.92))
    fig.update_layout(
        width=fig_width,
        height=fig_height,
        margin=dict(l=MARGIN_L, r=MARGIN_R, t=MARGIN_T, b=MARGIN_B),
        dragmode=dragmode,
        legend_title_text="Type",
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    tick_step = 1 if max(rows, cols) <= 40 else 5
    fig.update_xaxes(
        range=[-0.5, cols - 0.5],
        tickmode="linear",
        tick0=0,
        dtick=tick_step,
        title="X (col)",
        showgrid=False,
        zeroline=False,
        constrain="domain",
    )
    fig.update_yaxes(
        range=[rows - 0.5, -0.5],  # row 0 at the top, like the source grid
        tickmode="linear",
        tick0=0,
        dtick=tick_step,
        title="Y (row)",
        showgrid=False,
        zeroline=False,
        scaleanchor="x",
        scaleratio=1,
        constrain="domain",
    )
    return fig, fig_width, fig_height


def extract_selected_cells(event, rows: int, cols: int) -> list[tuple[int, int]]:
    cells: set[tuple[int, int]] = set()
    if not event:
        return []
    points = (event.get("selection") or {}).get("points") or []
    for p in points:
        cx, cy = p.get("x"), p.get("y")
        if cx is None or cy is None:
            continue
        col, row = int(round(float(cx))), int(round(float(cy)))
        if 0 <= row < rows and 0 <= col < cols:
            cells.add((row, col))
    return sorted(cells)


def push_undo():
    st.session_state.undo_stack.append(copy.deepcopy(st.session_state.grid))
    if len(st.session_state.undo_stack) > UNDO_LIMIT:
        st.session_state.undo_stack.pop(0)


def legend_chips_html() -> str:
    chips = []
    for code, (label, color) in PALETTE.items():
        text_color = "#111" if code in ("1",) else "#fff" if code not in ("Q",) else "#111"
        chips.append(
            f'<span style="display:inline-flex;align-items:center;margin:2px 8px 2px 0;'
            f'padding:2px 8px;border-radius:12px;background:{color};color:{text_color};'
            f'font-size:12px;border:1px solid rgba(0,0,0,0.15)">{code} · {label}</span>'
        )
    return "<div>" + "".join(chips) + "</div>"


# --------------------------------------------------------------------------
# Streamlit app
# --------------------------------------------------------------------------

def init_state():
    defaults = {
        "grid": None,
        "grid_meta": {},
        "undo_stack": [],
        "use_mock": False,
        "map_width_px": 1000,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def connection_controls() -> tuple[object, str, str, str, bool]:
    """Section 1: MongoDB connection. Rendered in the main column (no sidebar)
    so the full page width is available for a wide map."""
    with st.expander("1. MongoDB connection", expanded=False):
        st.caption(
            "Connection URI and database are configured via environment variables "
            "and are not shown here."
        )
        uri = DEFAULT_URI
        db_name = DEFAULT_DB

        st.session_state.setdefault("source_coll_override", DEFAULT_SOURCE_COLL)
        col_src, col_tgt = st.columns(2)
        with col_src:
            raw_source_coll = st.text_input(
                "Source collection (BaseMaps)",
                value=st.session_state.source_coll_override,
                help=(
                    f"Where 'Start from a Base Map' looks for documents. Defaults to "
                    f"'{DEFAULT_SOURCE_COLL}' (from the MONGO_COLLECTION environment "
                    "variable), but you can point this at any other collection — for "
                    "example your target collection — to pull in a previously saved "
                    "Zone Map as a new starting point."
                ),
            )
        st.session_state.source_coll_override = raw_source_coll
        source_coll = raw_source_coll.strip() or DEFAULT_SOURCE_COLL
        if not is_valid_collection_name(source_coll):
            st.error(
                f"'{source_coll}' isn't a valid MongoDB collection name — falling back "
                f"to '{DEFAULT_SOURCE_COLL}'."
            )
            source_coll = DEFAULT_SOURCE_COLL

        with col_tgt:
            target_coll = st.text_input(
                "Target collection (ZoneMaps)", value=DEFAULT_TARGET_COLL,
                help="Where new ZoneMap versions are saved. Defaults to 'target_logical_maps'; "
                     "type any collection name you'd like to use instead.",
            )

        col_a, col_b = st.columns(2)
        if col_a.button("Test connection", width="stretch"):
            client = get_client(uri, False)
            ok, msg = test_connection(client, False)
            (st.success if ok else st.error)(msg)
            if not ok and not MONGOMOCK_AVAILABLE:
                st.info("Install `mongomock` to try the app without a real MongoDB instance.")

        if MONGOMOCK_AVAILABLE:
            st.session_state.use_mock = col_b.toggle(
                "Demo mode", value=st.session_state.use_mock,
                help="Use an in-memory database instead of the configured connection. Nothing is persisted.",
            )
        else:
            st.session_state.use_mock = False

        use_mock = st.session_state.use_mock
        client = get_client(uri if not use_mock else "mock", use_mock)
        if use_mock:
            st.caption("🧪 Demo mode: in-memory MongoDB (mongomock). Data resets on restart.")
        db = client[db_name]
    return db, source_coll, target_coll, uri, use_mock


def load_controls(db, source_coll: str, target_coll: str):
    """Section 2: load a Base Map or a saved Zone Map. Rendered in the main
    column, above the map, using two side-by-side expanders to stay compact."""
    st.subheader("2. Load a map")
    col1, col2, col3 = st.columns(3)

    with col1:
        with st.expander("Start from a Base Map", expanded=st.session_state.grid is None):
            if st.button("Seed sample BaseMap into MongoDB"):
                ok, msg = seed_sample_base_map(db, source_coll)
                (st.success if ok else st.warning)(msg)

            base_maps = list_base_maps(db, source_coll)
            if base_maps:
                options = {base_map_option_label(d): d["_id"] for d in base_maps}
                choice = st.selectbox("Available base maps", list(options.keys()))
                if st.button("Load base map", type="primary"):
                    doc = load_base_map(db, source_coll, options[choice])
                    if doc is None:
                        st.error("Could not reload that base map document.")
                    else:
                        raw_grid = extract_base_map_grid(doc)
                        if raw_grid is None:
                            st.error(
                                "That base map document has no 'mapGrid' or 'grid' field, "
                                "so it can't be loaded. Check that it's a valid BaseMap document."
                            )
                        else:
                            grid = to_str_grid(raw_grid)
                            # Use the full palette here (not just {"0","1"}): the
                            # source collection can now be pointed at a target
                            # (ZoneMap) collection, whose grids carry the full set
                            # of zone codes (S/P/L/I/B/T/Q, optionally with a Station
                            # ID suffix such as "S22") rather than just 0/1.
                            problems = validate_grid(grid, VALID_CODES)
                            if problems:
                                st.error("Base map failed validation:\n\n" + "\n".join(problems))
                            else:
                                map_id = base_map_display_id(doc)
                                st.session_state.grid = grid
                                st.session_state.grid_meta = {
                                    "mapName": map_id,
                                    "sourceMapId": map_id,
                                }
                                st.session_state.undo_stack = []
                                st.session_state["_backup_stale"] = True
                                st.rerun()
            else:
                st.caption(
                    "No documents found in this collection yet. Seed the sample above, "
                    "insert your own BaseMap document with a 'fmModuleMapId' (or 'mapId') "
                    "and a 'mapGrid' (or 'grid') field, or change the source collection "
                    "above (section 1) to point at your target collection to reuse a "
                    "previously saved Zone Map."
                )

    with col2:
        with st.expander("...or continue a saved Zone Map"):
            names = list_zone_map_names(db, target_coll)
            if names:
                map_name = st.selectbox("Map name", names, key="zm_name_select")
                versions = list_zone_map_versions(db, target_coll, map_name)
                if versions:
                    v_options = {
                        f'v{d["metadata"]["versionId"]} — {d["metadata"].get("changeDate", "")}': d["_id"]
                        for d in versions
                    }
                    v_choice = st.selectbox("Version", list(v_options.keys()), key="zm_version_select")
                    if st.button("Load this version", type="primary"):
                        doc = load_zone_map(db, target_coll, v_options[v_choice])
                        if doc is None:
                            st.error("Could not reload that zone map document.")
                        elif "grid" not in doc:
                            st.error(
                                "That zone map document has no 'grid' field, so it can't be "
                                "loaded. Check that it's a valid ZoneMap document."
                            )
                        else:
                            grid = to_str_grid(doc["grid"])
                            problems = validate_grid(grid, VALID_CODES)
                            if problems:
                                st.error("Saved zone map failed validation:\n\n" + "\n".join(problems))
                            else:
                                st.session_state.grid = grid
                                st.session_state.grid_meta = {
                                    "mapName": doc["metadata"].get("mapName", map_name),
                                    "sourceMapId": doc["metadata"].get("sourceMapId", ""),
                                }
                                st.session_state.undo_stack = []
                                st.session_state["_backup_stale"] = True
                                st.rerun()
            else:
                st.caption("No saved zone maps yet. Save one below once you've made edits.")

    with col3:
        with st.expander("...or generate a blank grid", expanded=False):
            st.caption(
                "Enter a width and height to generate a new dummy map of that exact "
                "size, filled entirely with '1' (Legal / Movable). You can then paint "
                "zones onto it, or save/download it as-is."
            )
            gc1, gc2 = st.columns(2)
            gen_width = gc1.number_input("Width (X, columns)", min_value=1, max_value=2000, value=400, step=1)
            gen_height = gc2.number_input("Height (Y, rows)", min_value=1, max_value=2000, value=128, step=1)

            cell_count = int(gen_width) * int(gen_height)
            if cell_count > 50_000:
                st.warning(
                    f"That's {cell_count:,} cells — generation and rendering will work, "
                    "but may be slow. Consider a smaller size if the app feels sluggish."
                )

            if st.button("Generate and load into editor", type="primary"):
                grid = generate_blank_grid(int(gen_width), int(gen_height), fill_code="1")
                map_id = f"generated-{int(gen_width)}x{int(gen_height)}"
                st.session_state.grid = grid
                st.session_state.grid_meta = {
                    "mapName": map_id,
                    "sourceMapId": map_id,
                }
                st.session_state.undo_stack = []
                st.session_state["_backup_stale"] = True
                st.rerun()

            gen_map_id = f"generated-{int(gen_width)}x{int(gen_height)}"
            preview_doc = build_base_map_document(
                generate_blank_grid(min(int(gen_width), 3), min(int(gen_height), 3), fill_code="1"),
                gen_map_id,
            )
            preview_doc["width"], preview_doc["height"] = int(gen_width), int(gen_height)
            st.caption("BaseMap JSON shape (grid truncated for preview):")
            st.json(preview_doc, expanded=False)

            full_doc = build_base_map_document(
                generate_blank_grid(int(gen_width), int(gen_height), fill_code="1"), gen_map_id
            )
            dl1, dl2 = st.columns(2)
            with dl1:
                st.download_button(
                    "⬇️ Download as BaseMap JSON",
                    data=json.dumps(full_doc, indent=2),
                    file_name=f"{gen_map_id}.json",
                    mime="application/json",
                    width="stretch",
                )
            with dl2:
                if st.button("Insert as new BaseMap into MongoDB", width="stretch"):
                    try:
                        if db[source_coll].find_one({"mapId": full_doc["mapId"]}):
                            st.warning(
                                f"A BaseMap with mapId '{full_doc['mapId']}' already exists "
                                "in this collection; nothing was inserted."
                            )
                        else:
                            db[source_coll].insert_one(dict(full_doc))
                            st.success(f"Inserted BaseMap '{full_doc['mapId']}' into MongoDB.")
                    except PyMongoError as exc:
                        st.error(f"Could not insert BaseMap: {exc}")


def save_controls(db, target_coll: str):
    """Section 3: save a new Zone Map version. Rendered in the main column,
    below the map."""
    st.subheader("3. Save")
    if st.session_state.grid is None:
        st.caption("Load a map first.")
        return

    meta = st.session_state.grid_meta
    col1, col2 = st.columns(2)
    with col1:
        map_name = st.text_input("Map name", value=meta.get("mapName", "warehouse-map"))
        change_date = st.text_input(
            "Change date (plain text)",
            value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    with col2:
        source_map_id = st.text_input("Source map ID", value=meta.get("sourceMapId", ""))
        notes = st.text_area("Notes (optional)", value="", height=68)

    document = build_zone_map_document(
        st.session_state.grid, map_name, source_map_id, notes, change_date
    )

    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        if st.button("💾 Save new version to MongoDB", type="primary", width="stretch"):
            ok, info = save_zone_map(db, target_coll, document)
            if ok:
                st.session_state.grid_meta["mapName"] = map_name
                st.session_state.grid_meta["sourceMapId"] = source_map_id
                st.session_state["_last_save_msg"] = (
                    f"Saved as versionId={document['metadata']['versionId']} (_id={info})"
                )
                st.rerun()
            else:
                st.error(info)
    with btn_col2:
        st.download_button(
            "⬇️ Download this version as JSON",
            data=json.dumps(document, indent=2),
            file_name=f'{map_name}_v{document["metadata"]["versionId"]}.json',
            mime="application/json",
            width="stretch",
        )

    if st.session_state.get("_last_save_msg"):
        st.success(st.session_state.pop("_last_save_msg"))


def main_editor():
    grid = st.session_state.grid
    rows, cols = len(grid), len(grid[0])

    # "Palette" heading with the Station ID field sitting to its right.
    pal_col, sid_col, _pal_spacer = st.columns([1.2, 1.4, 6], vertical_alignment="bottom")
    with pal_col:
        st.subheader("Palette")
    with sid_col:
        raw_station_id = st.text_input(
            "Station ID",
            max_chars=STATION_ID_MAX_LEN,
            key="station_id_input",
            help=(
                "Optional. For Storage/Stow, Pick, Load (Put), Induct and Build areas, a "
                "Station ID that isn't blank or 0 is appended to the zone code "
                "written into the map — e.g. a Stow Area with ID 22 is stored as "
                "'S22' instead of 'S'. Ignored for Traffic, Queue and base cells."
            ),
        )
    station_id, station_id_error = normalize_station_id(raw_station_id)
    if station_id_error:
        st.error(station_id_error)

    codes = list(PALETTE.keys())
    apply_type = st.radio(
        "Type to paint",
        codes,
        format_func=lambda c: f"{c} — {PALETTE[c][0]}",
        horizontal=True,
        label_visibility="collapsed",
    )
    apply_code = make_cell_code(apply_type, station_id)
    st.markdown(legend_chips_html(), unsafe_allow_html=True)

    st.subheader("Map")

    # The chart's current selection is stored in session_state under its own
    # widget key ("grid_chart") as soon as the user interacts with it, and
    # that happens BEFORE Streamlit reruns the script — so it's already
    # available here, ahead of rendering the chart itself. That lets us put
    # the Apply/Undo buttons in the same row as the mode toggle, above the
    # map, instead of a separate row that adds vertical space.
    prior_event = st.session_state.get("grid_chart")
    selected_cells = extract_selected_cells(prior_event, rows, cols)

    mode_col, apply_col, undo_col = st.columns([2, 1.4, 1.1], gap="medium")
    with mode_col:
        mode_label = st.radio(
            "Map mode",
            ["Select cells", "Zoom / Pan"],
            horizontal=True,
            label_visibility="collapsed",
            help=(
                "Select cells: click a cell, or drag a box / lasso to select multiple cells. "
                "Zoom / Pan: drag to zoom into an area, or scroll to zoom in/out. Use the "
                "toolbar's 'Reset axes' button (or double-click the map) to zoom back out."
            ),
        )
    with apply_col:
        apply_clicked = st.button(
            f"Apply '{apply_code}' to selection ({len(selected_cells)})",
            disabled=(not selected_cells) or bool(station_id_error),
            type="primary",
            width="stretch",
        )
    with undo_col:
        undo_clicked = st.button(
            "Undo last change",
            disabled=not st.session_state.undo_stack,
            width="stretch",
        )
    dragmode = "select" if mode_label == "Select cells" else "zoom"
    st.caption(
        "**Select cells** mode: click a cell, or drag a box / lasso to select multiple "
        "cells, then use **Apply to selection** above. **Zoom / Pan** mode: drag to zoom "
        "into an area, scroll to zoom, or use the toolbar to pan / reset the view. You can "
        "also target an exact rectangle by coordinates further down."
    )
    st.session_state.map_width_px = st.slider(
        "Max map display width (px)",
        min_value=500,
        max_value=1800,
        value=st.session_state.map_width_px,
        step=50,
        help=(
            "The map is sized to fit within this width while keeping cells square. "
            "If you still see empty space to the side or the map looks compressed, "
            "lower this to roughly match your browser window's width; raise it on a "
            "wider monitor. This mainly matters for grids much wider than they are tall."
        ),
    )
    fig, fig_w, fig_h = make_figure(
        grid, dragmode=dragmode, max_width_px=st.session_state.map_width_px
    )
    event = st.plotly_chart(
        fig,
        key="grid_chart",
        on_select="rerun",
        width=fig_w,
        height=fig_h,
        config={"displaylogo": False, "scrollZoom": True, "displayModeBar": True},
    )
    # Refresh selected_cells from this run's event too, so that if the user's
    # click both changed the selection AND landed on this same rerun as an
    # Apply/Undo click, downstream logic still sees the latest selection.
    selected_cells = extract_selected_cells(event, rows, cols) or selected_cells

    if apply_clicked and selected_cells:
        push_undo()
        for r, c in selected_cells:
            st.session_state.grid[r][c] = apply_code
        st.rerun()

    if undo_clicked and st.session_state.undo_stack:
        st.session_state.grid = st.session_state.undo_stack.pop()
        st.rerun()

    with st.expander("Apply to a rectangle by coordinates"):
        rc1, rc2, rc3, rc4, rc5 = st.columns([1, 1, 1, 1, 1.3])
        row_start = rc1.number_input("Row start (Y)", 0, rows - 1, 0)
        row_end = rc2.number_input("Row end (Y)", 0, rows - 1, rows - 1)
        col_start = rc3.number_input("Col start (X)", 0, cols - 1, 0)
        col_end = rc4.number_input("Col end (X)", 0, cols - 1, cols - 1)
        if rc5.button(f"Apply '{apply_code}' to rectangle", disabled=bool(station_id_error)):
            push_undo()
            r0, r1 = sorted((int(row_start), int(row_end)))
            c0, c1_ = sorted((int(col_start), int(col_end)))
            for r in range(r0, r1 + 1):
                for c in range(c0, c1_ + 1):
                    st.session_state.grid[r][c] = apply_code
            st.rerun()

    with st.expander("Reset"):
        if st.button("Revert all edits back to the originally loaded map"):
            st.session_state.grid = to_str_grid(st.session_state.get("_loaded_grid_backup", grid))
            st.rerun()

    with st.expander("Cell type counts"):
        # Counted by zone type, so "S", "S1" and "S22" all count as Storage.
        counts: dict[str, int] = {code: 0 for code in PALETTE}
        for row in grid:
            for v in row:
                counts[base_code(v) or "0"] += 1
        counts_df = pd.DataFrame(
            [{"Code": c, "Label": PALETTE[c][0], "Count": n} for c, n in counts.items()]
        )
        st.dataframe(counts_df, hide_index=True, width="stretch")

    with st.expander("Raw grid JSON"):
        st.json(grid)

    with st.expander("Document that will be saved to MongoDB"):
        preview = build_zone_map_document(
            grid,
            st.session_state.grid_meta.get("mapName", "warehouse-map"),
            st.session_state.grid_meta.get("sourceMapId", ""),
            "",
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        st.json(preview)


def main():
    st.set_page_config(page_title="Warehouse Zone Map Editor", layout="wide")
    password_gate()  # blocks (st.stop()) until the correct password is entered

    st.title("🏭 Warehouse Zone Map Editor")
    st.caption(
        "Load a BaseMap, paint zones (Storage, Pick, Load, Induct, Build, Traffic, Queue) onto it, "
        "and save versioned ZoneMaps to MongoDB."
    )

    init_state()

    db, source_coll, target_coll, uri, use_mock = connection_controls()
    load_controls(db, source_coll, target_coll)

    if st.session_state.grid is None:
        st.info("👆 Load a Base Map or a saved Zone Map above to begin editing.")
        return

    if "_loaded_grid_backup" not in st.session_state or st.session_state.get("_backup_stale", True):
        st.session_state["_loaded_grid_backup"] = copy.deepcopy(st.session_state.grid)
        st.session_state["_backup_stale"] = False

    st.divider()
    main_editor()
    st.divider()
    save_controls(db, target_coll)


if __name__ == "__main__":
    main()

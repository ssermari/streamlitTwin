import copy

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import pymongo
from pymongo.errors import PyMongoError
import time
from datetime import datetime, timezone

# ── Page Config ────────────────────────────────────────────────────────────────
st.set_page_config(layout="wide", page_title="Radiant Digital Twin", page_icon="📦")
st.title("Radiant Digital Twin")

st.markdown("""
<style>
    .block-container { padding-top: 2rem; }
    .hwm-box {
        background: #1a1a2e;
        border-left: 4px solid #00d4ff;
        border-radius: 6px;
        padding: 0.6rem 1rem;
        color: #00d4ff;
        font-family: monospace;
        font-size: 1rem;
        margin-bottom: 1rem;
    }
</style>
""", unsafe_allow_html=True)

# ── Constants ──────────────────────────────────────────────────────────────────
STEPS = 8            # interpolation sub-frames per event (digitalTwin* topics)
PATH_STEPS = 4       # interpolation sub-frames per path step (pathPlanningEvents* topics)

# Logical grid convention (matches mapApp.py / the Zone Map Editor): base unit
# is 2'x2', rendered at 8x8 resolution per base unit, so each logical grid
# unit/cell = (2 * 12) / 8 = 3 inches. Pallets are 52" square. Carrier
# positions coming out of MongoDB are assumed to already be expressed in
# these same logical units — i.e. one position unit = one grid cell — so no
# separate pixel/image scale factor is needed anymore: whatever Zone Map is
# loaded below simply *is* the coordinate space robots are plotted in.
BASE_UNIT_FT              = 2
LOGICAL_RES_PER_BASE_UNIT = 8
INCHES_PER_UNIT           = (BASE_UNIT_FT * 12) / LOGICAL_RES_PER_BASE_UNIT  # 3.0
PALLET_SIZE_INCHES        = 52
PALLET_SIZE_UNITS         = PALLET_SIZE_INCHES / INCHES_PER_UNIT  # ~17.33 grid cells

# Where saved Zone Maps live — same default target collection mapApp.py
# writes to. Override via st.secrets["MONGO_ZONE_COLLECTION"] if needed.
DEFAULT_ZONE_MAP_COLLECTION = "target_logical_maps"

# Collections with any of these prefixes are offered as event sources.
DIGITAL_TWIN_PREFIX       = "digitalTwin"
PATH_PLANNING_PREFIX      = "pathPlanningEvents"
EVENT_COLLECTION_PREFIXES = (DIGITAL_TWIN_PREFIX, PATH_PLANNING_PREFIX)

DEFAULT_MAP_DISPLAY_WIDTH = 1000
MAP_DISPLAY_MAX_HEIGHT_PX = 700

# code -> (label, color). Identical to mapApp.py's PALETTE — this is the same
# palette baked into every Zone Map, so the grid we render here and the
# legend above it read exactly the same as in the Zone Map Editor.
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


# ── MongoDB Connection ─────────────────────────────────────────────────────────
@st.cache_resource
def get_mongo_client():
    return pymongo.MongoClient(st.secrets["MONGO_URI"])

try:
    mongo = get_mongo_client()
    db = mongo[st.secrets["MONGO_DB"]]
except Exception as e:
    st.error(f"MongoDB connection failed: {e}")
    st.stop()

ZONE_MAP_COLLECTION = st.secrets.get("MONGO_ZONE_COLLECTION", DEFAULT_ZONE_MAP_COLLECTION)


# ── Zone Map (grid) helpers — ported from mapApp.py ───────────────────────────
def normalize_cell(v) -> str:
    """Upper-case a cell value and drop any Station ID suffix: 'P1', 'S22',
    'b7' -> 'P', 'S', 'B'. Only a zone letter followed by extra letters/digits
    is trimmed; plain codes and the base cells '0' / '1' are left untouched
    (so a value like '10' is not mistaken for a suffixed code)."""
    s = str(v).strip().upper()
    if len(s) > 1 and s[0].isalpha() and s[0] in PALETTE and s[1:].isalnum():
        return s[0]
    return s


def to_str_grid(raw_grid: list[list]) -> list[list[str]]:
    return [[normalize_cell(v) for v in row] for row in raw_grid]


def validate_grid(grid: list[list[str]], allowed: set[str]) -> list[str]:
    problems = []
    if not grid or not isinstance(grid, list):
        return ["Grid is empty."]
    width = len(grid[0])
    for r, row in enumerate(grid):
        if len(row) != width:
            problems.append(f"Row {r} has length {len(row)}, expected {width} (ragged grid).")
        for c, v in enumerate(row):
            if v not in allowed:
                problems.append(f"Cell (row {r}, col {c}) has invalid value '{v}'.")
    if len(problems) > 8:
        problems = problems[:8] + [f"...and {len(problems) - 8} more issue(s)."]
    return problems


def list_zone_map_names(db, coll: str) -> list[str]:
    try:
        return sorted(db[coll].distinct("metadata.mapName"))
    except PyMongoError:
        return []


def list_zone_map_versions(db, coll: str, map_name: str) -> list[dict]:
    try:
        cursor = (
            db[coll]
            .find({"metadata.mapName": map_name}, {"grid": 0})
            .sort("metadata.versionId", -1)
        )
        return list(cursor)
    except PyMongoError:
        return []


def load_zone_map(db, coll: str, doc_id) -> dict | None:
    try:
        return db[coll].find_one({"_id": doc_id})
    except PyMongoError:
        return None


def list_event_collections(db) -> list[str]:
    """Collections available as an event source: anything named
    'digitalTwin*' or 'pathPlanningEvents*', so new topics show up
    automatically without code changes."""
    try:
        names = db.list_collection_names()
    except PyMongoError:
        return []
    return sorted(n for n in names if n.startswith(EVENT_COLLECTION_PREFIXES))


def is_path_collection(name: str) -> bool:
    return name.startswith(PATH_PLANNING_PREFIX)


# ── Map-version helpers ───────────────────────────────────────────────────────
def norm_version(v):
    """Normalise a map version so 3, 3.0, '3' and 'v3' all compare equal."""
    if v is None:
        return None
    s = str(v).strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except ValueError:
        pass
    return s


def version_mismatch_summary(events, loaded_version):
    """Returns (number of events whose map_version != loaded map's version,
    sorted list of the distinct map_version values seen on those events)."""
    want = norm_version(loaded_version)
    bad = [e for e in events if norm_version(e.get("map_version")) != want]
    seen = sorted({("missing" if norm_version(e.get("map_version")) is None
                    else norm_version(e.get("map_version"))) for e in bad})
    return len(bad), seen


# ── Session State ──────────────────────────────────────────────────────────────
def init_state():
    defaults = {
        "grid": None,
        "grid_key": None,
        "map_version": None,
        "current_pos": None,
        "playing": False,
        "frame_id": 0,
        "log_lines": [],
        "map_width_px": DEFAULT_MAP_DISPLAY_WIDTH,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()


def create_center_positions(cols, rows, robot_ids=None):
    if robot_ids is None:
        robot_ids = []
    return pd.DataFrame({
        "robot_id": robot_ids,
        "x": [cols / 2] * len(robot_ids),
        "y": [rows / 2] * len(robot_ids),
    })


# ── 1. Setup: warehouse grid ───────────────────────────────────────────────────
st.subheader("1. Setup")

zm_names = list_zone_map_names(db, ZONE_MAP_COLLECTION)
if not zm_names:
    st.warning(
        f"No saved Zone Maps found in the '{ZONE_MAP_COLLECTION}' collection. "
        "Save one from the Zone Map Editor (mapApp.py) first."
    )
    st.stop()

zc1, zc2, zc3 = st.columns([2, 2, 1])
with zc1:
    zm_map_name = st.selectbox("Map name", zm_names, key="zm_name_select")
with zc2:
    zm_versions = list_zone_map_versions(db, ZONE_MAP_COLLECTION, zm_map_name)
    if zm_versions:
        v_options = {
            f'v{d["metadata"]["versionId"]} — {d["metadata"].get("changeDate", "")}': d["_id"]
            for d in zm_versions
        }
        zm_version_choice = st.selectbox("Version", list(v_options.keys()), key="zm_version_select")
    else:
        v_options = {}
        st.caption("No versions found for that map.")
with zc3:
    st.write("")
    load_grid_clicked = st.button("Load this version", type="primary", use_container_width=True)

if load_grid_clicked and v_options:
    doc = load_zone_map(db, ZONE_MAP_COLLECTION, v_options[zm_version_choice])
    if doc is None:
        st.error("Could not reload that zone map document.")
    elif "grid" not in doc:
        st.error("That zone map document has no 'grid' field, so it can't be loaded.")
    else:
        grid = to_str_grid(doc["grid"])
        problems = validate_grid(grid, VALID_CODES)
        if problems:
            st.error("Zone map failed validation:\n\n" + "\n".join(problems))
        else:
            rows, cols = len(grid), len(grid[0])
            st.session_state.grid = grid
            st.session_state.grid_key = f'{zm_map_name}_v{doc["metadata"]["versionId"]}'
            st.session_state.map_version = doc["metadata"]["versionId"]
            st.session_state.current_pos = create_center_positions(cols, rows)
            st.session_state.playing = False
            st.session_state.frame_id = 0
            st.session_state.log_lines = []
            st.session_state.pop("_base_fig_key", None)
            st.rerun()

if st.session_state.grid is None:
    st.info("👆 Pick a map name/version above and click **Load this version** to begin.")
    st.stop()

grid = st.session_state.grid
GRID_ROWS, GRID_COLS = len(grid), len(grid[0])
LOADED_MAP_VERSION = st.session_state.map_version
st.caption(
    f"Loaded **{st.session_state.grid_key}** (map_version {LOADED_MAP_VERSION}) — "
    f"{GRID_COLS}×{GRID_ROWS} cells "
    f"({GRID_COLS * INCHES_PER_UNIT / 12:.0f}' × {GRID_ROWS * INCHES_PER_UNIT / 12:.0f}' "
    f"at {INCHES_PER_UNIT:.0f}\" per cell). Carriers are {PALLET_SIZE_INCHES}\" square "
    f"(~{PALLET_SIZE_UNITS:.1f} cells)."
)

# ── Legend ─────────────────────────────────────────────────────────────────────
st.markdown(legend_chips_html(), unsafe_allow_html=True)


# ── Helpers ────────────────────────────────────────────────────────────────────
def epoch_to_str(epoch_ms):
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(epoch_ms)


def event_time_ms(doc) -> int:
    """Event time in epoch ms. digitalTwin* docs carry timestamp_epoch;
    pathPlanningEvents* docs carry created_at / updated_at (datetimes)."""
    ts = doc.get("timestamp_epoch")
    if ts is not None:
        try:
            return int(ts)
        except (TypeError, ValueError):
            pass
    dt = doc.get("created_at") or doc.get("updated_at")
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    return 0


def sort_spec(path_mode: bool):
    if path_mode:
        return [("created_at", pymongo.DESCENDING), ("_id", pymongo.DESCENDING)]
    return [("timestamp_epoch", pymongo.DESCENDING)]


def fetch_events(col, n, path_mode=False):
    cursor = col.find({}, {"_id": 0}).sort(sort_spec(path_mode)).limit(n)
    return list(cursor)


def doc_to_df(doc):
    """Carrier positions are read straight through — they're already in the
    same logical grid units (1 unit = 1 cell) as the loaded Zone Map, so no
    pixel/image scale factor applies here anymore."""
    rows = []
    for c in doc.get("carriers", []):
        try:
            rows.append({
                "robot_id": c["carrier_id"],
                "x": c["position"]["x"],
                "y": c["position"]["y"],
            })
        except Exception as e:
            print(f"BAD CARRIER: {e}")
    if rows:
        return pd.DataFrame(rows)
    return st.session_state.current_pos.copy()


def build_path_tracks(events, loaded_version):
    """pathPlanningEvents* pipeline, in this order:
       1. events = the N most recent docs already loaded,
       2. keep only events whose map_version matches the loaded map,
       3. de-duplicate by carrier_id, keeping the latest event per carrier,
       4. turn each remaining event's 'path' ([x, y] steps) into a track.
    Returns (tracks, stats) where tracks = {carrier_id: [(x, y), ...]}."""
    want = norm_version(loaded_version)
    matching = [e for e in events if norm_version(e.get("map_version")) == want]

    # events arrive newest-first; reverse so ties resolve toward the newest
    latest = {}
    for e in sorted(reversed(matching), key=event_time_ms):
        cid = e.get("carrier_id")
        if cid is None:
            continue
        latest[cid] = e  # later (newer) overwrites earlier

    tracks = {}
    for cid, e in latest.items():
        pts = []
        for p in (e.get("path") or []):
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                pts.append((float(p[0]), float(p[1])))
        if not pts:
            sp = e.get("start_position")
            if isinstance(sp, (list, tuple)) and len(sp) >= 2:
                pts.append((float(sp[0]), float(sp[1])))
        if pts:
            tracks[cid] = pts

    stats = {
        "loaded": len(events),
        "matching": len(matching),
        "mismatched": len(events) - len(matching),
        "duplicates": len(matching) - len(latest),
        "carriers": len(tracks),
    }
    return tracks, stats


# ── Rendering ──────────────────────────────────────────────────────────────────
def build_grid_traces(grid):
    """One Scattergl trace per zone code (not one point-per-cell trace with a
    per-point color array) — cheaper to build and to redraw, and lets each
    trace use a single solid marker color instead of an array."""
    rows = len(grid)
    cols = len(grid[0]) if rows else 0
    by_code = {code: {"x": [], "y": []} for code in PALETTE}
    for r in range(rows):
        row = grid[r]
        for c in range(cols):
            code = row[c] if row[c] in PALETTE else "0"
            by_code[code]["x"].append(c)
            by_code[code]["y"].append(r)

    traces = []
    for code, (label, color) in PALETTE.items():
        xs, ys = by_code[code]["x"], by_code[code]["y"]
        if not xs:
            continue
        traces.append(go.Scattergl(
            x=xs, y=ys, mode="markers",
            marker=dict(symbol="square", color=color, line=dict(width=0)),
            hoverinfo="skip", showlegend=False, name=f"{code} · {label}",
        ))
    return traces, rows, cols


def make_base_figure(grid, max_width_px, max_height_px=MAP_DISPLAY_MAX_HEIGHT_PX):
    traces, rows, cols = build_grid_traces(grid)
    fig = go.Figure(data=traces)

    MARGIN = 10
    MIN_CELL_PX, MAX_CELL_PX = 1, 34
    width_budget = max(max_width_px - 2 * MARGIN, MIN_CELL_PX)
    height_budget = max(max_height_px - 2 * MARGIN, MIN_CELL_PX)
    cell_w = width_budget / max(cols, 1)
    cell_h = height_budget / max(rows, 1)
    cell_px = max(MIN_CELL_PX, min(MAX_CELL_PX, cell_w, cell_h))

    fig_width = int(round(cell_px * cols + 2 * MARGIN))
    fig_height = int(round(cell_px * rows + 2 * MARGIN))

    fig.update_traces(marker=dict(size=cell_px * 0.92))
    fig.update_layout(
        width=fig_width,
        height=fig_height,
        margin=dict(l=MARGIN, r=MARGIN, t=MARGIN, b=MARGIN),
        showlegend=False,
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    fig.update_xaxes(
        range=[-0.5, cols - 0.5], visible=False,
        showgrid=False, zeroline=False, constrain="domain",
    )
    fig.update_yaxes(
        range=[rows - 0.5, -0.5], visible=False,  # row 0 at the top, matching mapApp.py
        showgrid=False, zeroline=False,
        scaleanchor="x", scaleratio=1, constrain="domain",
    )
    return fig, fig_width, fig_height, cell_px


def get_base_figure():
    """The zone grid itself never changes frame-to-frame — only the robot
    overlay does — so the (potentially tens of thousands of cells) grid
    trace is built once per (grid, display width) and cached, instead of
    being rebuilt on every animation sub-frame."""
    key = (st.session_state.grid_key, st.session_state.map_width_px)
    if st.session_state.get("_base_fig_key") != key:
        fig, fw, fh, cell_px = make_base_figure(grid, st.session_state.map_width_px)
        st.session_state["_base_fig"] = fig
        st.session_state["_base_fig_dims"] = (fw, fh, cell_px)
        st.session_state["_base_fig_key"] = key
    fw, fh, cell_px = st.session_state["_base_fig_dims"]
    return st.session_state["_base_fig"], fw, fh, cell_px


def render_frame(placeholder, df, label=""):
    st.session_state.frame_id += 1
    base_fig, fig_w, fig_h, cell_px = get_base_figure()
    fig = copy.deepcopy(base_fig)

    # Pallet footprint sized in grid cells (52" / 3" per cell), overlaid
    # directly in the grid's own x=col / y=row coordinate space.
    w = h = PALLET_SIZE_UNITS
    for _, row in df.iterrows():
        fig.add_shape(
            type="rect",
            x0=row["x"] - w / 2, x1=row["x"] + w / 2,
            y0=row["y"] - h / 2, y1=row["y"] + h / 2,
            fillcolor="red",
            line=dict(color="white", width=2),
            xref="x", yref="y",
        )
        fig.add_annotation(
            x=row["x"], y=row["y"],
            text=str(row["robot_id"]),
            showarrow=False,
            font=dict(family="Arial Black", size=max(9, min(14, cell_px)), color="black"),
            xref="x", yref="y",
        )

    if label:
        fig.update_layout(
            margin=dict(l=10, r=10, t=30, b=10),
            title=dict(text=label, x=0.01, font=dict(color="#00d4ff", size=13)),
        )

    placeholder.plotly_chart(
        fig,
        use_container_width=False,
        theme=None,
        config={"displayModeBar": False},
        key=f"warehouse_map_{st.session_state.frame_id}",
    )


# ── 2. Playback: event collection + controls ─────────────────────────────────
st.subheader("2. Playback")

event_colls = list_event_collections(db)
if not event_colls:
    st.warning(
        "No collections found with the "
        + " or ".join(f"'{p}*'" for p in EVENT_COLLECTION_PREFIXES)
        + " prefix."
    )
    st.stop()

def default_event_collection(names: list[str]) -> str:
    """Default topic: a pathPlanningEvents* collection (the exact name
    'pathPlanningEvents' if it exists, otherwise the first such match), then
    the MONGO_COLLECTION secret, then whatever comes first."""
    if PATH_PLANNING_PREFIX in names:
        return PATH_PLANNING_PREFIX
    path_names = [n for n in names if n.startswith(PATH_PLANNING_PREFIX)]
    if path_names:
        return path_names[0]
    preferred = st.secrets.get("MONGO_COLLECTION")
    return preferred if preferred in names else names[0]


# The choice lives in session state under an explicit key so it survives
# reruns (map loads, slider moves, Play/Stop). It is only (re)initialised when
# nothing valid is stored yet.
if st.session_state.get("event_coll_select") not in event_colls:
    st.session_state["event_coll_select"] = default_event_collection(event_colls)

selected_coll_name = st.selectbox(
    "Event collection (" + " / ".join(f"{p}*" for p in EVENT_COLLECTION_PREFIXES) + ")",
    event_colls,
    key="event_coll_select",
)
col = db[selected_coll_name]
IS_PATH_MODE = is_path_collection(selected_coll_name)
st.caption(
    "Mode: **path planning** — one event per carrier; all carriers' moves play in lock-step."
    if IS_PATH_MODE else
    "Mode: **digital twin** — each event is a snapshot of all carriers."
)

# Switching collections: stop any playback and drop the previous collection's
# event log / robots so stale output from the old topic can't be mistaken for
# the new one.
if st.session_state.get("_active_event_coll") != selected_coll_name:
    st.session_state["_active_event_coll"] = selected_coll_name
    st.session_state.playing = False
    st.session_state.log_lines = []
    st.session_state.current_pos = create_center_positions(GRID_COLS, GRID_ROWS)

# ── High Water Mark ────────────────────────────────────────────────────────────
hwm_doc = col.find_one(
    {},
    {"timestamp_epoch": 1, "created_at": 1, "updated_at": 1, "map_version": 1, "_id": 0},
    sort=sort_spec(IS_PATH_MODE),
)
if hwm_doc:
    hwm = event_time_ms(hwm_doc)
    st.markdown(
        f'<div class="hwm-box">⬆ HIGH WATER MARK &nbsp;|&nbsp; '
        f'<b>{epoch_to_str(hwm)}</b> &nbsp;·&nbsp; epoch&nbsp;{hwm} '
        f'&nbsp;·&nbsp; <span style="opacity:0.7">{selected_coll_name}</span></div>',
        unsafe_allow_html=True,
    )

    # Map version check: the loaded map must match the events' map_version.
    latest_ver = hwm_doc.get("map_version")
    if latest_ver is None:
        st.info(
            f"The latest event in '{selected_coll_name}' has no map_version field, "
            f"so it can't be confirmed against the loaded map (v{LOADED_MAP_VERSION})."
        )
    elif norm_version(latest_ver) != norm_version(LOADED_MAP_VERSION):
        st.warning(
            f"⚠ Map version mismatch: the latest event in '{selected_coll_name}' "
            f"was produced for map_version **{latest_ver}**, but the loaded map is "
            f"version **{LOADED_MAP_VERSION}** ({st.session_state.grid_key}). "
            f"Robots may not line up with this map."
        )
else:
    st.warning(f"No documents found in '{selected_coll_name}'.")

# ── Playback controls ──────────────────────────────────────────────────────────
ctrl1, ctrl2, ctrl3, ctrl4 = st.columns([3, 3, 1, 1])

with ctrl1:
    batch_limit = st.slider(
        "Events to load & replay",
        min_value=1, max_value=100, value=25, step=1,
    )

with ctrl2:
    speed_level = st.select_slider(
        "Playback speed",
        options=["Slow", "Normal", "Fast", "Turbo"],
        value="Normal",
    )
    frame_delay = {"Slow": 0.2, "Normal": 0.07, "Fast": 0.02, "Turbo": 0.0}[speed_level]

with ctrl3:
    st.write("")
    play_btn = st.button("▶ Play", type="primary", use_container_width=True)

with ctrl4:
    st.write("")
    stop_btn = st.button("⏹ Stop", use_container_width=True)

st.session_state.map_width_px = st.slider(
    "Max map display width (px)",
    min_value=400,
    max_value=2200,
    value=st.session_state.map_width_px,
    step=50,
    help="The map's height is derived automatically from the grid's own "
         "row/column count, so it never looks stretched.",
)

# ── Placeholders ───────────────────────────────────────────────────────────────
chart_placeholder  = st.empty()
status_placeholder = st.empty()
warn_placeholder   = st.empty()
events_placeholder = st.empty()
log_placeholder    = st.empty()

# Always render current position on load / rerun
render_frame(chart_placeholder, st.session_state.current_pos)

# ── Button State ───────────────────────────────────────────────────────────────
if play_btn:
    st.session_state.playing = True
if stop_btn:
    st.session_state.playing = False

# ── re-render log on every rerun so it survives Stop ─────────────────────────
if st.session_state.log_lines:
    log_placeholder.markdown(
        "**Event Log**\n\n"
        + "\n\n".join(f"- {line}" for line in st.session_state.log_lines)
    )


def play_digital_twin(events):
    """Original behaviour: each event is a snapshot of all carriers; play the
    snapshots back one after another, interpolating between them."""
    status_placeholder.info(f"Playing back {len(events)} event(s)…")

    sorted_events = sorted(events, key=lambda x: x['timestamp_epoch'])

    n_bad, seen = version_mismatch_summary(sorted_events, LOADED_MAP_VERSION)
    if n_bad:
        warn_placeholder.warning(
            f"⚠ {n_bad} of {len(sorted_events)} event(s) have a map_version "
            f"({', '.join(seen)}) that doesn't match the loaded map "
            f"(version {LOADED_MAP_VERSION})."
        )

    all_robot_ids = sorted({
        c["carrier_id"]
        for doc in sorted_events
        for c in doc.get("carriers", [])
    })

    st.session_state.current_pos = create_center_positions(GRID_COLS, GRID_ROWS, all_robot_ids)
    st.session_state.log_lines = []

    for i, doc in enumerate(sorted_events):
        if not st.session_state.playing:
            status_placeholder.warning("⏹ Playback stopped.")
            break

        incoming_df = doc_to_df(doc)
        start_df  = st.session_state.current_pos.set_index("robot_id").sort_index()
        target_df = incoming_df.set_index("robot_id").sort_index()

        full_index = start_df.index.union(target_df.index)
        start_df  = start_df.reindex(full_index)
        target_df = target_df.reindex(full_index)

        start_df["x"] = start_df["x"].fillna(target_df["x"])
        start_df["y"] = start_df["y"].fillna(target_df["y"])
        target_df["x"] = target_df["x"].fillna(start_df["x"])
        target_df["y"] = target_df["y"].fillna(start_df["y"])

        ts_label = (
            f"Event {i+1}/{len(events)}"
            f"  ·  {epoch_to_str(doc.get('timestamp_epoch', 0))}"
        )

        for step in range(1, STEPS + 1):
            alpha = step / STEPS
            frame_df = pd.DataFrame({
                "robot_id": start_df.index,
                "x": (start_df["x"] + (target_df["x"] - start_df["x"]) * alpha).values,
                "y": (start_df["y"] + (target_df["y"] - start_df["y"]) * alpha).values,
            })
            render_frame(chart_placeholder, frame_df, label=ts_label)
            time.sleep(frame_delay)

        st.session_state.current_pos = target_df.reset_index()

        robot_ids = ", ".join(str(r) for r in target_df.index.tolist())
        st.session_state.log_lines.append(
            f"**{ts_label}** — robots: `{robot_ids}`"
        )
        log_placeholder.markdown(
            "**Event Log**\n\n"
            + "\n\n".join(f"- {line}" for line in st.session_state.log_lines)
        )

    if st.session_state.playing:
        st.session_state.playing = False
        status_placeholder.success(
            f"✅ Playback complete — {len(events)} events replayed.  "
            f"Last: {epoch_to_str(max(event_time_ms(e) for e in events))}"
        )


def fmt_created(dt) -> str:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    return "" if dt is None else str(dt)


def path_events_table(events, loaded_version) -> pd.DataFrame:
    """One row per event that was fetched (newest first), showing exactly what
    the playback is about to use: created_at, map_version (and whether it
    matches the loaded map), path length, start/end points and how many
    distinct points the path visits."""
    want = norm_version(loaded_version)
    rows = []
    for e in events:
        pts = [tuple(p[:2]) for p in (e.get("path") or [])
               if isinstance(p, (list, tuple)) and len(p) >= 2]
        rows.append({
            "carrier_id": e.get("carrier_id"),
            "created_at": fmt_created(e.get("created_at")),
            "map_version": str(e.get("map_version")),
            "matches_map": norm_version(e.get("map_version")) == want,
            "path_points": len(pts),
            "start": f"({pts[0][0]:g}, {pts[0][1]:g})" if pts else "",
            "end": f"({pts[-1][0]:g}, {pts[-1][1]:g})" if pts else "",
            "distinct_points": len(set(pts)),
        })
    return pd.DataFrame(rows)


def play_path_planning(events):
    """pathPlanningEvents*: one event per carrier, each holding that carrier's
    whole 'path'. After the version filter and per-carrier de-duplication,
    step 0 of every carrier is shown together, then step 1 of every carrier,
    and so on. A carrier whose path is shorter than the longest one waits at
    its final position while the others finish."""
    events_placeholder.dataframe(
        path_events_table(events, LOADED_MAP_VERSION),
        hide_index=True, use_container_width=True,
    )
    tracks, stats = build_path_tracks(events, LOADED_MAP_VERSION)

    if stats["mismatched"]:
        warn_placeholder.warning(
            f"⚠ {stats['mismatched']} of {stats['loaded']} loaded event(s) have a "
            f"map_version that doesn't match the loaded map "
            f"(version {LOADED_MAP_VERSION}) and were skipped."
        )

    if not tracks:
        status_placeholder.warning(
            f"No playable events: 0 of {stats['loaded']} loaded event(s) match "
            f"map_version {LOADED_MAP_VERSION}."
            if stats["matching"] == 0 else
            "The matching events contain no usable carrier_id / path data."
        )
        st.session_state.playing = False
        return

    carrier_ids = sorted(tracks.keys(), key=str)
    n_steps = max(len(tracks[c]) for c in carrier_ids)

    status_placeholder.info(
        f"Loaded {stats['loaded']} event(s) → {stats['matching']} match map version "
        f"{LOADED_MAP_VERSION} → {stats['duplicates']} duplicate(s) removed → "
        f"{stats['carriers']} carrier(s), up to {n_steps - 1} move(s)."
    )

    def pos_at(cid, k):
        pts = tracks[cid]
        return pts[min(k, len(pts) - 1)]

    def frame_at(k_from, k_to, alpha):
        xs, ys = [], []
        for cid in carrier_ids:
            x0, y0 = pos_at(cid, k_from)
            x1, y1 = pos_at(cid, k_to)
            xs.append(x0 + (x1 - x0) * alpha)
            ys.append(y0 + (y1 - y0) * alpha)
        return pd.DataFrame({"robot_id": carrier_ids, "x": xs, "y": ys})

    st.session_state.log_lines = []
    for cid in carrier_ids:
        pts = tracks[cid]
        n_distinct = len(set(pts))
        st.session_state.log_lines.append(
            f"**{cid}** — {len(pts) - 1} move(s), "
            f"({pts[0][0]:g}, {pts[0][1]:g}) → ({pts[-1][0]:g}, {pts[-1][1]:g})"
            + ("  ⚠ never leaves its start point" if n_distinct == 1
               else f"  ·  {n_distinct} distinct points")
        )
    log_placeholder.markdown(
        "**Event Log**\n\n"
        + "\n\n".join(f"- {line}" for line in st.session_state.log_lines)
    )

    # Step 0: everyone at their start position.
    start_df = frame_at(0, 0, 0.0)
    st.session_state.current_pos = start_df
    label = f"Step 0/{n_steps - 1}  ·  {len(carrier_ids)} carrier(s)  ·  map v{LOADED_MAP_VERSION}"
    render_frame(chart_placeholder, start_df, label=label)
    time.sleep(frame_delay)

    # Steps 1..N: every carrier makes its k-th move at the same time.
    stopped = False
    for k in range(1, n_steps):
        if not st.session_state.playing:
            stopped = True
            status_placeholder.warning("⏹ Playback stopped.")
            break

        label = (
            f"Step {k}/{n_steps - 1}  ·  {len(carrier_ids)} carrier(s)"
            f"  ·  map v{LOADED_MAP_VERSION}"
        )
        for sub in range(1, PATH_STEPS + 1):
            frame_df = frame_at(k - 1, k, sub / PATH_STEPS)
            render_frame(chart_placeholder, frame_df, label=label)
            time.sleep(frame_delay)
        st.session_state.current_pos = frame_at(k, k, 1.0)

    if not stopped and st.session_state.playing:
        st.session_state.playing = False
        status_placeholder.success(
            f"✅ Playback complete — {len(carrier_ids)} carrier(s), "
            f"{n_steps - 1} step(s) replayed simultaneously."
        )


# ── playback ──────────────────────────────────────────────────────────────────
if st.session_state.playing:
    # Clear the previous run's log/warnings up front, so a run that ends early
    # (e.g. nothing matches the map version) can't leave an old log on screen.
    st.session_state.log_lines = []
    log_placeholder.empty()
    warn_placeholder.empty()
    events_placeholder.empty()
    status_placeholder.info(f"Loading events from '{selected_coll_name}'...")
    events = fetch_events(col, batch_limit, path_mode=IS_PATH_MODE)
    if not events:
        status_placeholder.warning("No MongoDB events found.")
        st.session_state.playing = False
    elif IS_PATH_MODE:
        play_path_planning(events)
    else:
        play_digital_twin(events)

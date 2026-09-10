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
STEPS = 8

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

# Only collections with this prefix are offered as event sources.
EVENT_COLLECTION_PREFIX = "digitalTwin"

DEFAULT_MAP_DISPLAY_WIDTH = 1200
MAP_DISPLAY_MAX_HEIGHT_PX = 700

# code -> (label, color). Identical to mapApp.py's PALETTE — this is the same
# palette baked into every Zone Map, so the grid we render here and the
# legend above it read exactly the same as in the Zone Map Editor.
PALETTE: dict[str, tuple[str, str]] = {
    "S": ("Storage / Stow Areas", "#F4A300"),
    "P": ("Pick Stations", "#2ECC71"),
    "L": ("Load (Put) Stations", "#3498DB"),
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
def to_str_grid(raw_grid: list[list]) -> list[list[str]]:
    return [[str(v).strip().upper() for v in row] for row in raw_grid]


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
    'digitalTwin*', so new topics show up automatically without code changes."""
    try:
        names = db.list_collection_names()
    except PyMongoError:
        return []
    return sorted(n for n in names if n.startswith(EVENT_COLLECTION_PREFIX))


# ── Session State ──────────────────────────────────────────────────────────────
def init_state():
    defaults = {
        "grid": None,
        "grid_key": None,
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


# ── 1. Load the warehouse grid (same picker as mapApp.py's Zone Map loader) ───
st.subheader("1. Load the warehouse grid")

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
st.caption(
    f"Loaded **{st.session_state.grid_key}** — {GRID_COLS}×{GRID_ROWS} cells "
    f"({GRID_COLS * INCHES_PER_UNIT / 12:.0f}' × {GRID_ROWS * INCHES_PER_UNIT / 12:.0f}' "
    f"at {INCHES_PER_UNIT:.0f}\" per cell). Carriers are {PALLET_SIZE_INCHES}\" square "
    f"(~{PALLET_SIZE_UNITS:.1f} cells)."
)

# ── Legend ─────────────────────────────────────────────────────────────────────
st.markdown(legend_chips_html(), unsafe_allow_html=True)

# ── 2. Choose the event source ─────────────────────────────────────────────────
st.subheader("2. Choose the event source")
event_colls = list_event_collections(db)
if not event_colls:
    st.warning(f"No collections found with the '{EVENT_COLLECTION_PREFIX}*' prefix.")
    st.stop()

default_idx = 0
preferred = st.secrets.get("MONGO_COLLECTION")
if preferred in event_colls:
    default_idx = event_colls.index(preferred)

selected_coll_name = st.selectbox(
    f"Event collection ({EVENT_COLLECTION_PREFIX}*)",
    event_colls,
    index=default_idx,
)
col = db[selected_coll_name]


# ── Helpers ────────────────────────────────────────────────────────────────────
def epoch_to_str(epoch_ms):
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(epoch_ms)


def fetch_events(col, n):
    return list(
        col.find({}, {"_id": 0})
           .sort("timestamp_epoch", pymongo.DESCENDING)
           .limit(n)
    )


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


# ── High Water Mark ────────────────────────────────────────────────────────────
hwm_doc = col.find_one(
    {}, {"timestamp_epoch": 1, "_id": 0},
    sort=[("timestamp_epoch", pymongo.DESCENDING)]
)
if hwm_doc:
    hwm = hwm_doc["timestamp_epoch"]
    st.markdown(
        f'<div class="hwm-box">⬆ HIGH WATER MARK &nbsp;|&nbsp; '
        f'<b>{epoch_to_str(hwm)}</b> &nbsp;·&nbsp; epoch&nbsp;{hwm} '
        f'&nbsp;·&nbsp; <span style="opacity:0.7">{selected_coll_name}</span></div>',
        unsafe_allow_html=True,
    )
else:
    st.warning(f"No documents found in '{selected_coll_name}'.")

# ── 3. Playback controls ───────────────────────────────────────────────────────
st.subheader("3. Playback")
ctrl1, ctrl2, ctrl3, ctrl4 = st.columns([3, 3, 1, 1])

with ctrl1:
    batch_limit = st.slider(
        "Events to replay",
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

# ── playback ──────────────────────────────────────────────────────────────────
if st.session_state.playing:
    status_placeholder.info("Loading MongoDB events...")
    events = fetch_events(col, batch_limit)
    if not events:
        status_placeholder.warning("No MongoDB events found.")
        st.session_state.playing = False
    else:
        status_placeholder.info(f"Playing back {len(events)} event(s)…")

        sorted_events = sorted(events, key=lambda x: x['timestamp_epoch'])

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
                f"Last: {epoch_to_str(events[-1].get('timestamp_epoch', 0))}"
            )

import json

import streamlit as st
import streamlit.components.v1 as components
import plotly.graph_objects as go
import plotly.offline
import pandas as pd
import pymongo
from pymongo.errors import PyMongoError
import calendar
import re
import time
from datetime import datetime, timezone

# ── Page Config ────────────────────────────────────────────────────────────────
st.set_page_config(layout="wide", page_title="Radiant Digital Twin", page_icon="📦")

st.markdown("""
<style>
    .block-container { padding-top: 1rem; padding-bottom: 1rem; }
    h1 { margin-top: 0; margin-bottom: 0.25rem; padding-top: 0; padding-bottom: 0; }
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

st.title("Radiant Digital Twin")

# ── Constants ──────────────────────────────────────────────────────────────────
# Playback now runs in the browser (see PLAYER_HTML), which interpolates
# between keyframes on every animation frame, so there are no per-sub-frame
# server round trips. SPEED_MS is the time one move (one keyframe -> the next)
# takes; digitalTwin snapshots are further apart, so they get a multiplier.
SPEED_MS = {"Slow": 600, "Normal": 300, "Fast": 120, "Turbo": 40}
DIGITAL_TWIN_STEP_MULT = 3
PLAYER_BAR_PX = 36

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

# Colour of everything drawn *outside* the map's own coordinates (margins and
# any spare width beside the map), so a small map doesn't look like open floor.
OUTSIDE_MAP_COLOR = "#3a3a3a"

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
            st.session_state.player_payload = None
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
        # exact integer math (no float rounding), so equal timestamps compare equal
        return calendar.timegm(dt.utctimetuple()) * 1000 + dt.microsecond // 1000
    return 0


def sort_spec(path_mode: bool):
    if path_mode:
        # Newest created_at first. A planner run often writes every carrier's
        # event with the *same* created_at (to the millisecond), so ties are
        # broken by _id ASCENDING (insertion order): "latest 10" of a 20-carrier
        # run is then c1..c10, not an arbitrary or reversed subset.
        return [("created_at", pymongo.DESCENDING), ("_id", pymongo.ASCENDING)]
    return [("timestamp_epoch", pymongo.DESCENDING)]


def natural_key(s):
    """Sort key so c2 comes before c10."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


def fetch_events(col, n, path_mode=False):
    # Path events keep their _id: it is the tie-break when several events
    # (or several events for one carrier) share the same created_at.
    projection = None if path_mode else {"_id": 0}
    cursor = col.find({}, projection).sort(sort_spec(path_mode)).limit(int(n))
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


def build_path_tracks(events, loaded_version, max_moves=None, allow_mismatch=False):
    """pathPlanningEvents* pipeline, in this order:
       1. events = the docs already loaded (and narrowed to the chosen created_at),
       2. keep only events whose map_version matches the loaded map (unless
          allow_mismatch is set, in which case every event is used),
       3. de-duplicate by carrier_id, keeping the latest event per carrier
          (newest created_at; ties broken by the larger _id),
       4. turn each remaining event's 'path' ([x, y] steps) into a track,
          keeping only the first `max_moves` moves (max_moves + 1 points,
          i.e. the start point plus that many moves) when max_moves is set.
    Returns (tracks, stats) where tracks = {carrier_id: [(x, y), ...]}."""
    want = norm_version(loaded_version)
    n_match = sum(1 for e in events if norm_version(e.get("map_version")) == want)
    matching = (list(events) if allow_mismatch
                else [e for e in events if norm_version(e.get("map_version")) == want])

    # Oldest -> newest, so a later event for the same carrier overwrites an
    # earlier one and the newest survives. ObjectId hex strings sort in
    # insertion order, which settles created_at ties.
    latest = {}
    for e in sorted(matching, key=lambda e: (event_time_ms(e), str(e.get("_id", "")))):
        cid = e.get("carrier_id")
        if cid is None:
            continue
        latest[cid] = e

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
        if pts and max_moves is not None:
            pts = pts[: int(max_moves) + 1]
        if pts:
            tracks[cid] = pts

    stats = {
        "loaded": len(events),
        "matching": n_match,                     # events that match the loaded map
        "mismatched": len(events) - n_match,     # events that don't
        "used": len(matching),                   # events actually played from
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
        plot_bgcolor="white",           # the map itself
        paper_bgcolor=OUTSIDE_MAP_COLOR,  # everything outside the map's coordinates
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
    key = (st.session_state.grid_key, st.session_state.map_width_px, OUTSIDE_MAP_COLOR)
    if st.session_state.get("_base_fig_key") != key:
        fig, fw, fh, cell_px = make_base_figure(grid, st.session_state.map_width_px)
        st.session_state["_base_fig"] = fig
        st.session_state["_base_fig_dims"] = (fw, fh, cell_px)
        st.session_state["_base_fig_key"] = key
    fw, fh, cell_px = st.session_state["_base_fig_dims"]
    return st.session_state["_base_fig"], fw, fh, cell_px


def get_base_fig_json() -> str:
    """The grid figure serialised once per (grid, display width) and cached."""
    key = st.session_state.get("_base_fig_key")
    if st.session_state.get("_base_fig_json_key") != key:
        st.session_state["_base_fig_json"] = st.session_state["_base_fig"].to_json()
        st.session_state["_base_fig_json_key"] = key
    return st.session_state["_base_fig_json"]


# Self-contained page shown in an iframe. The grid is drawn once by Plotly;
# the robots are painted on a transparent <canvas> laid over the plot, using
# the plot's own axis ranges to convert grid cells to pixels, from a
# requestAnimationFrame loop. Plotly is not touched per frame (redrawing its
# shapes/annotations every frame is what made playback slow), and nothing is
# re-sent from Streamlit per frame, so the map never blanks/strobes. The
# overlay repaints itself if the user zooms or resets the plot.
PLAYER_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<script src="__PLOTLY_SRC__"></script>
<style>
  html,body{margin:0;background:__OUTSIDE_MAP_COLOR__;font-family:system-ui,Arial,sans-serif}
  #bar{display:flex;align-items:center;gap:8px;padding:0 8px;height:30px;
       box-sizing:border-box;background:#1a1a2e;color:#00d4ff;
       font:13px ui-monospace,Menlo,Consolas,monospace}
  #bar button{background:#00d4ff;color:#1a1a2e;border:0;border-radius:4px;
       padding:2px 10px;font-size:13px;cursor:pointer}
  #sc{flex:1;min-width:80px}
  #lb{white-space:nowrap}
  #wrap{position:relative}
  #ov{position:absolute;left:0;top:0;pointer-events:none}
</style></head><body>
<div id="bar"><button id="pp" title="Pause / play">&#9208;</button>
<button id="rs" title="Restart">&#8634;</button>
<input id="sc" type="range" min="0" max="0" step="any" value="0">
<span id="lb"></span></div>
<div id="wrap"><div id="plot"></div><canvas id="ov"></canvas></div>
<script>
const FIG = __FIG_JSON__;
const P = __PAYLOAD_JSON__;
(function () {
  const gd = document.getElementById('plot');
  const pp = document.getElementById('pp'), rs = document.getElementById('rs');
  const sc = document.getElementById('sc'), lb = document.getElementById('lb');
  const ids = P.ids, K = P.pos, labels = P.labels;
  const n = ids.length, nk = K.length, half = P.half;
  sc.max = Math.max(nk - 1, 0);
  if (nk < 2) { document.getElementById('bar').style.display = 'none'; }

  const cv = document.getElementById('ov'), ctx = cv.getContext('2d');
  function sizeCanvas() {
    const W = gd._fullLayout.width, H = gd._fullLayout.height;
    const d = window.devicePixelRatio || 1;
    cv.width = Math.round(W * d); cv.height = Math.round(H * d);
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
    ctx.setTransform(d, 0, 0, d, 0, 0);
  }
  // Paint every robot as a pallet-sized red square with its id, converting
  // grid coordinates (x = column, y = row, row 0 at the top) to pixels with
  // the plot's current axis ranges, so it stays correct when zoomed.
  function paint(pos) {
    const fl = gd._fullLayout, xa = fl.xaxis, ya = fl.yaxis;
    const xr0 = xa.range[0], xr1 = xa.range[1], yr0 = ya.range[0], yr1 = ya.range[1];
    const sx = xa._length / (xr1 - xr0), sy = ya._length / (yr1 - yr0);
    const hx = half * Math.abs(sx), hy = half * Math.abs(sy);
    ctx.clearRect(0, 0, fl.width, fl.height);
    ctx.save();
    ctx.beginPath(); ctx.rect(xa._offset, ya._offset, xa._length, ya._length); ctx.clip();
    ctx.lineWidth = 2; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = '900 ' + P.font + 'px "Arial Black", Arial, sans-serif';
    for (let i = 0; i < n; i++) {
      const px = xa._offset + (pos[i][0] - xr0) * sx;
      const py = ya._offset + ya._length * (yr1 - pos[i][1]) / (yr1 - yr0);
      ctx.fillStyle = 'red'; ctx.strokeStyle = 'white';
      ctx.fillRect(px - hx, py - hy, 2 * hx, 2 * hy);
      ctx.strokeRect(px - hx, py - hy, 2 * hx, 2 * hy);
      ctx.fillStyle = 'black'; ctx.fillText(ids[i], px, py);
    }
    ctx.restore();
  }
  function posAt(t) {
    const i = Math.min(Math.floor(t), nk - 1), j = Math.min(i + 1, nk - 1), a = t - i;
    const out = new Array(n);
    for (let c = 0; c < n; c++) {
      const p = K[i][c], q = K[j][c];
      out[c] = [p[0] + (q[0] - p[0]) * a, p[1] + (q[1] - p[1]) * a];
    }
    return out;
  }

  let t = 0, playing = false, last = null, shown = -1;
  function setBtn() { pp.innerHTML = playing ? '&#9208;' : '&#9654;'; }
  function draw() {
    paint(posAt(t));
    const k = Math.min(Math.round(t), nk - 1);
    if (k !== shown) { shown = k; lb.textContent = labels[k] || ''; }
    sc.value = t;
  }
  function tick(ts) {
    if (!playing) { last = null; return; }
    if (last === null) last = ts;
    const dt = Math.min(ts - last, 100);   // don't jump after a hidden tab
    last = ts;
    t += dt / P.step_ms;
    if (t >= nk - 1) { t = nk - 1; playing = false; setBtn(); }
    draw();
    if (playing) requestAnimationFrame(tick);
  }
  function play() {
    if (nk < 2) return;
    if (t >= nk - 1) t = 0;
    playing = true; last = null; setBtn(); requestAnimationFrame(tick);
  }
  pp.onclick = function () { if (playing) { playing = false; setBtn(); } else { play(); } };
  rs.onclick = function () { t = 0; draw(); play(); };
  sc.oninput = function () { playing = false; setBtn(); t = parseFloat(sc.value); draw(); };

  Plotly.newPlot(gd, FIG.data, FIG.layout,
                 {displayModeBar: false, responsive: false}).then(function () {
    sizeCanvas(); draw(); setBtn();
    // repaint the overlay when the user zooms / resets the plot
    gd.on('plotly_relayout', function () { sizeCanvas(); draw(); });
    if (P.autoplay) play();
  });
})();
</script></body></html>"""


def render_player(placeholder):
    """Show the map + robots. With a stored run (`player_payload`) the browser
    plays it; otherwise it just shows the robots at their current positions."""
    _, fig_w, fig_h, cell_px = get_base_figure()
    payload = st.session_state.get("player_payload")
    if payload is None:
        cp = st.session_state.current_pos
        payload = {
            "ids": [str(r) for r in cp["robot_id"]],
            "pos": [[[float(x), float(y)] for x, y in zip(cp["x"], cp["y"])]],
            "labels": [""],
            "step_ms": SPEED_MS["Normal"],
            "autoplay": False,
        }
    payload = dict(payload, half=PALLET_SIZE_UNITS / 2, font=max(9, min(14, cell_px)))

    def js(s: str) -> str:
        return s.replace("</", "<\\/")

    page = (
        PLAYER_HTML
        .replace("__OUTSIDE_MAP_COLOR__", OUTSIDE_MAP_COLOR)
        .replace("__PLOTLY_SRC__",
                 f"https://cdn.plot.ly/plotly-{plotly.offline.get_plotlyjs_version()}.min.js")
        .replace("__FIG_JSON__", js(get_base_fig_json()))
        .replace("__PAYLOAD_JSON__", js(json.dumps(payload)))
    )
    with placeholder.container():
        components.html(page, height=int(fig_h) + PLAYER_BAR_PX + 4, scrolling=True)


# ── 2. Playback: event collection + controls ─────────────────────────────────
# "Playback" heading with the number of events to pull sitting to its right.
pb_col, ev_col, mm_col, _pb_spacer = st.columns(
    [1.3, 1.5, 2.4, 4.6], vertical_alignment="bottom")
with pb_col:
    st.subheader("2. Playback")
with ev_col:
    events_to_load = st.number_input(
        "Events to load",
        min_value=1, max_value=1000, value=30, step=1,
        key="events_to_load",
        help="How many of the most recent events (newest created_at first) to pull "
             "from the selected topic. For pathPlanningEvents, duplicates are then "
             "removed by carrier_id, keeping only the latest event per carrier.",
    )
events_to_load = int(events_to_load)
with mm_col:
    allow_mismatch = st.checkbox(
        "Allow map mismatch playback",
        value=False,
        key="allow_map_mismatch",
        help="Off (default): only events whose map_version matches the loaded map "
             "are played; any that don't are skipped. On: play every loaded event "
             "regardless of its map_version.",
    )

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

# Event collection and the created_at filter sit side by side, each about a
# third of the page width (the created_at dropdown is filled in further down,
# once the topic is known, because its choices come from that topic).
coll_col, ts_col, _coll_spacer = st.columns([3.6, 3.6, 2.8])
with coll_col:
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
    st.session_state.player_payload = None
    st.session_state.log_lines = []
    st.session_state["created_at_select"] = "ALL"
    st.session_state.current_pos = create_center_positions(GRID_COLS, GRID_ROWS)


# ── created_at filter: choices come from the events retrieved from the topic ─
def fmt_ms(ms: int) -> str:
    if not ms:
        return "(no timestamp)"
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{ms % 1000:03d} UTC"


def event_matches_map(e) -> bool:
    return norm_version(e.get("map_version")) == norm_version(LOADED_MAP_VERSION)


try:
    # Same query/sort/limit as the real playback fetch, but only the fields
    # needed here, so this stays cheap on every rerun.
    _peek = list(
        col.find({}, {"_id": 0, "created_at": 1, "updated_at": 1,
                      "timestamp_epoch": 1, "map_version": 1})
           .sort(sort_spec(IS_PATH_MODE)).limit(events_to_load)
    )
except PyMongoError:
    _peek = []

ts_info: dict[int, list[int]] = {}       # timestamp (ms) -> [events, events matching the map]
for _e in _peek:
    _row = ts_info.setdefault(event_time_ms(_e), [0, 0])
    _row[0] += 1
    _row[1] += int(event_matches_map(_e))

ts_options = ["ALL"] + list(ts_info.keys())          # newest first, ALL on top
if st.session_state.get("created_at_select") not in ts_options:
    st.session_state["created_at_select"] = "ALL"


def fmt_ts_option(o) -> str:
    if o == "ALL":
        return "ALL"
    n, m = ts_info.get(o, [0, 0])
    return f"{fmt_ms(o)}  ·  {n} event(s), {m} match map"


with ts_col:
    ts_choice = st.selectbox(
        "created_at" if IS_PATH_MODE else "Event timestamp",
        ts_options,
        key="created_at_select",
        format_func=fmt_ts_option,
        help="The distinct timestamps among the events loaded from this topic "
             "(the number set in 'Events to load'). ALL plays every loaded event; "
             "picking one plays only the events with exactly that timestamp.",
    )
ts_filter = None if ts_choice == "ALL" else int(ts_choice)

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
    moves_to_play = st.slider(
        "Number of moves to play",
        min_value=25, max_value=1000, value=175, step=5,
        disabled=not IS_PATH_MODE,
        help="pathPlanningEvents only: how many moves along each carrier's path to "
             "play back (all carriers move together, one move at a time). "
             "Not used for digitalTwin topics, which replay whole snapshots.",
    )

with ctrl2:
    speed_level = st.select_slider(
        "Playback speed",
        options=["Slow", "Normal", "Fast", "Turbo"],
        value="Normal",
    )
    step_ms = SPEED_MS[speed_level]

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
# The legend is declared as its own element right here, immediately after the
# map's placeholder, so it always renders just below the map — regardless of
# when render_player() below actually fills the map placeholder's content.
chart_placeholder  = st.empty()
st.markdown(legend_chips_html(), unsafe_allow_html=True)
status_placeholder = st.empty()
warn_placeholder   = st.empty()
events_placeholder = st.empty()
log_placeholder    = st.empty()

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


def show_log():
    if st.session_state.log_lines:
        log_placeholder.markdown(
            "**Event Log**\n\n"
            + "\n\n".join(f"- {line}" for line in st.session_state.log_lines)
        )


def set_final_positions(ids, last_pos):
    st.session_state.current_pos = pd.DataFrame({
        "robot_id": ids,
        "x": [p[0] for p in last_pos],
        "y": [p[1] for p in last_pos],
    })


def prep_digital_twin(events):
    """digitalTwin*: each event is a snapshot of carriers. Build one keyframe
    per event (plus a starting keyframe with every robot at the map centre);
    carriers missing from a snapshot hold their last position, and a carrier
    listed twice in one snapshot keeps its last entry. Returns the payload the
    browser player animates, or None."""
    sorted_events = sorted(events, key=lambda x: x["timestamp_epoch"])

    n_bad, seen = version_mismatch_summary(sorted_events, LOADED_MAP_VERSION)
    if n_bad and allow_mismatch:
        warn_placeholder.warning(
            f"⚠ Playing {n_bad} of {len(sorted_events)} event(s) whose map_version "
            f"({', '.join(seen)}) doesn't match the loaded map "
            f"(version {LOADED_MAP_VERSION}) — allowed by 'Allow map mismatch playback'."
        )
    elif n_bad:
        warn_placeholder.warning(
            f"⚠ {n_bad} of {len(sorted_events)} event(s) have a map_version "
            f"({', '.join(seen)}) that doesn't match the loaded map "
            f"(version {LOADED_MAP_VERSION}) and were skipped. Tick 'Allow map "
            f"mismatch playback' to include them."
        )
        sorted_events = [e for e in sorted_events if event_matches_map(e)]
        if not sorted_events:
            status_placeholder.warning(
                f"No playable events: none of the loaded events match "
                f"map_version {LOADED_MAP_VERSION}."
            )
            return None

    ids = sorted({str(c["carrier_id"]) for doc in sorted_events
                  for c in doc.get("carriers", []) if "carrier_id" in c},
                 key=natural_key)
    if not ids:
        status_placeholder.warning("The loaded events contain no carriers.")
        return None

    cur = {cid: [GRID_COLS / 2, GRID_ROWS / 2] for cid in ids}
    snap = lambda: [list(cur[cid]) for cid in ids]
    keyframes, labels = [snap()], ["Start"]
    log_lines = []
    for i, doc in enumerate(sorted_events):
        in_doc = set()
        for c in doc.get("carriers", []):
            try:
                cid = str(c["carrier_id"])
                cur[cid] = [float(c["position"]["x"]), float(c["position"]["y"])]
                in_doc.add(cid)
            except Exception as e:  # noqa: BLE001
                print(f"BAD CARRIER: {e}")
        label = (f"Event {i + 1}/{len(sorted_events)}  ·  "
                 f"{epoch_to_str(doc.get('timestamp_epoch', 0))}")
        keyframes.append(snap())
        labels.append(label)
        log_lines.append(
            f"**{label}** — robots: `{', '.join(sorted(in_doc, key=natural_key))}`")

    st.session_state.log_lines = log_lines
    set_final_positions(ids, keyframes[-1])
    status_placeholder.info(
        f"▶ Playing {len(sorted_events)} event(s) for {len(ids)} carrier(s) in the "
        f"player below. Pause, restart or scrub with the bar above the map."
    )
    return {"ids": ids, "pos": keyframes, "labels": labels,
            "step_ms": step_ms * DIGITAL_TWIN_STEP_MULT, "autoplay": True}


def prep_path_planning(events):
    """pathPlanningEvents*: one event per carrier, each holding that carrier's
    whole 'path'. After the version filter and per-carrier de-duplication,
    keyframe k holds every carrier's position after k moves, so all carriers
    move together (a shorter path waits at its last point). Steps where no
    carrier moves at all are dropped. Returns the payload the browser player
    animates, or None."""
    events_placeholder.dataframe(
        path_events_table(events, LOADED_MAP_VERSION),
        hide_index=True, use_container_width=True,
    )
    tracks, stats = build_path_tracks(
        events, LOADED_MAP_VERSION, max_moves=moves_to_play, allow_mismatch=allow_mismatch)

    if stats["mismatched"] and allow_mismatch:
        warn_placeholder.warning(
            f"⚠ Playing {stats['mismatched']} of {stats['loaded']} loaded event(s) whose "
            f"map_version doesn't match the loaded map (version {LOADED_MAP_VERSION}) — "
            f"allowed by 'Allow map mismatch playback'."
        )
    elif stats["mismatched"]:
        warn_placeholder.warning(
            f"⚠ {stats['mismatched']} of {stats['loaded']} loaded event(s) have a "
            f"map_version that doesn't match the loaded map "
            f"(version {LOADED_MAP_VERSION}) and were skipped. Tick 'Allow map "
            f"mismatch playback' to include them."
        )

    if not tracks:
        status_placeholder.warning(
            f"No playable events: 0 of {stats['loaded']} loaded event(s) match "
            f"map_version {LOADED_MAP_VERSION}."
            if stats["used"] == 0 else
            "The events contain no usable carrier_id / path data."
        )
        return None

    ids = sorted(tracks.keys(), key=natural_key)
    n_steps = max(len(tracks[c]) for c in ids)

    def pos_at(cid, k):
        pts = tracks[cid]
        return list(pts[min(k, len(pts) - 1)])

    keyframes, labels, skipped = [], [], 0
    for k in range(n_steps):
        frame = [pos_at(cid, k) for cid in ids]
        if keyframes and frame == keyframes[-1]:
            skipped += 1          # nobody moved this step: nothing to show
            continue
        keyframes.append(frame)
        labels.append(f"Step {k}/{n_steps - 1}  ·  {len(ids)} carrier(s)  ·  map v{LOADED_MAP_VERSION}")

    log_lines = []
    for cid in ids:
        pts = tracks[cid]
        n_distinct = len(set(pts))
        log_lines.append(
            f"**{cid}** — {len(pts) - 1} move(s), "
            f"({pts[0][0]:g}, {pts[0][1]:g}) → ({pts[-1][0]:g}, {pts[-1][1]:g})"
            + ("  ⚠ doesn't move in these moves" if n_distinct == 1
               else f"  ·  {n_distinct} distinct points")
        )
    st.session_state.log_lines = log_lines
    set_final_positions(ids, keyframes[-1])

    status_placeholder.info(
        f"Loaded {stats['loaded']} event(s) → "
        + (f"{stats['used']} used (map check off) → " if allow_mismatch else
           f"{stats['matching']} match map version {LOADED_MAP_VERSION} → ")
        + f"{stats['duplicates']} duplicate(s) removed → "
        f"{stats['carriers']} carrier(s), {n_steps - 1} move(s) each (limit "
        f"{moves_to_play}); {skipped} idle step(s) skipped. Playing in the player "
        f"below: pause, restart or scrub with the bar above the map."
    )
    return {"ids": ids, "pos": keyframes, "labels": labels,
            "step_ms": step_ms, "autoplay": True}


# ── Button state ───────────────────────────────────────────────────────────────
if play_btn:
    st.session_state.playing = True
if stop_btn:
    st.session_state.playing = False
    st.session_state.player_payload = None   # back to a static map (final positions)

# ── Playback: fetch + build the run *before* drawing the map ─────────────────
if st.session_state.playing:
    # Playback itself now happens in the browser, so this only prepares it.
    st.session_state.playing = False
    st.session_state.log_lines = []
    log_placeholder.empty()
    warn_placeholder.empty()
    events_placeholder.empty()
    status_placeholder.info(f"Loading events from '{selected_coll_name}'...")
    events = fetch_events(col, events_to_load, path_mode=IS_PATH_MODE)
    no_events_msg = None
    if not events:
        no_events_msg = "No MongoDB events found."
    elif ts_filter is not None:
        events = [e for e in events if event_time_ms(e) == ts_filter]
        if not events:
            no_events_msg = (
                f"None of the newest {events_to_load} event(s) have timestamp "
                f"{fmt_ms(ts_filter)} any more — pick a timestamp again.")
    payload = None
    if not events:
        status_placeholder.warning(no_events_msg)
    elif IS_PATH_MODE:
        payload = prep_path_planning(events)
    else:
        payload = prep_digital_twin(events)
    if payload is not None:
        payload["run_id"] = time.time_ns()   # new run -> the player restarts
        st.session_state.player_payload = payload

# ── Map ───────────────────────────────────────────────────────────────────────
render_player(chart_placeholder)

# Event log survives reruns (Stop, slider moves, ...)
show_log()

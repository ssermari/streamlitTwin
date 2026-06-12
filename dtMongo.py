import streamlit as st
import plotly.express as px
from PIL import Image
import pandas as pd
import pymongo
import time
import base64
from io import BytesIO
from datetime import datetime, timezone

# ── Page Config ────────────────────────────────────────────────────────────────
#st.set_page_config(layout="wide")
st.st.set_page_config(layout="wide", page_title="Radiant Digital Twin", page_icon="📦")
st.title("USP Digital Twin")

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
STEPS           = 8
WH_WIDTH_UNITS  = 400
WH_HEIGHT_UNITS = 144

# ── MongoDB Connection ─────────────────────────────────────────────────────────
@st.cache_resource
def get_mongo_client():
    return pymongo.MongoClient(st.secrets["MONGO_URI"])

try:
    mongo = get_mongo_client()
    db    = mongo[st.secrets["MONGO_DB"]]
    col   = db[st.secrets["MONGO_COLLECTION"]]
except Exception as e:
    st.error(f"MongoDB connection failed: {e}")
    st.stop()

# ── Warehouse Image ────────────────────────────────────────────────────────────
@st.cache_resource
def get_warehouse_image():
    img = Image.open("wh.png")
    buf = BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return img, f"data:image/png;base64,{b64}"

img, img_b64          = get_warehouse_image()
img_width, img_height = img.size

scale_x = img_width  / WH_WIDTH_UNITS
scale_y = img_height / WH_HEIGHT_UNITS

def scale_coords(unit_x, unit_y):
    return unit_x * scale_x, unit_y * scale_y

# ── Session State ──────────────────────────────────────────────────────────────
def create_center_positions(robot_ids=None):
    if robot_ids is None:
        robot_ids = []
    return pd.DataFrame({
        "robot_id": robot_ids,
        "x": [img_width  / 2] * len(robot_ids),
        "y": [img_height / 2] * len(robot_ids),
    })

if "current_pos" not in st.session_state:
    st.session_state.current_pos = create_center_positions()
if "playing" not in st.session_state:
    st.session_state.playing = False
if "frame_id" not in st.session_state:
    st.session_state.frame_id = 0
if "log_lines" not in st.session_state:
    st.session_state.log_lines = []    

# ── Helpers ────────────────────────────────────────────────────────────────────
def epoch_to_str(epoch_ms):
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(epoch_ms)

def fetch_events(n):
    return list(
        col.find({}, {"_id": 0})
           .sort("timestamp_epoch", pymongo.DESCENDING)
           .limit(n)
    )

def doc_to_df(doc):
    rows = []
    for c in doc.get("carriers", []):
        try:
            px_val, py_val = scale_coords(
                c["position"]["x"],
                c["position"]["y"]
            )
            rows.append({
                "robot_id": c["carrier_id"],
                "x": px_val,
                "y": py_val
            })
        except Exception as e:
            print(f"BAD CARRIER: {e}")
    if rows:
        return pd.DataFrame(rows)
    return st.session_state.current_pos.copy()

# ── Rendering ──────────────────────────────────────────────────────────────────
def render_frame(placeholder, df, label=""):
    st.session_state.frame_id += 1
    fig = px.scatter(
        df, x="x", y="y",
        range_x=[0, img_width], range_y=[0, img_height]
    )
    # Invisible scatter — just anchors the plot axes
    fig.update_traces(
        marker=dict(size=0, color="rgba(0,0,0,0)"),
    )

    # Draw a rectangle for each robot
    w, h = 90, 90
    for _, row in df.iterrows():
        fig.add_shape(
            type="rect",
            x0=row["x"] - w / 2, x1=row["x"] + w / 2,
            y0=row["y"] - h / 2, y1=row["y"] + h / 2,
            fillcolor="red",
            line=dict(color="white", width=2),
            xref="x", yref="y",
        )
        # Annotation renders above shapes
        fig.add_annotation(
            x=row["x"], y=row["y"],
            text=str(row["robot_id"]),
            showarrow=False,
            font=dict(family="Arial Black", size=14, color="black"),
            xref="x", yref="y",
        )

    fig.add_layout_image(dict(
        source=img_b64, xref="x", yref="y",
        x=0, y=img_height,
        sizex=img_width, sizey=img_height,
        sizing="stretch", opacity=0.8, layer="below",
    ))
    fig.update_layout(
        width=900,
        height=350,
        margin=dict(l=0, r=0, t=30 if label else 0, b=0),
        xaxis_visible=False, yaxis_visible=False,
        xaxis=dict(range=[0, img_width], scaleanchor=None, constrain="domain"),
        yaxis=dict(range=[0, img_height], constrain="domain"),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        title=dict(
            text=label, x=0.01,
            font=dict(color="#00d4ff", size=13)
        ) if label else None,
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
        f'<b>{epoch_to_str(hwm)}</b> &nbsp;·&nbsp; epoch&nbsp;{hwm}</div>',
        unsafe_allow_html=True,
    )
else:
    st.warning("No documents found in collection.")

# ── Top Controls ───────────────────────────────────────────────────────────────
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
    frame_delay = {"Slow": 0.1, "Normal": 0.07, "Fast": 0.02, "Turbo": 0.0}[speed_level]

with ctrl3:
    st.write("")
    play_btn = st.button("▶ Play", type="primary", use_container_width=True)

with ctrl4:
    st.write("")
    stop_btn = st.button("⏹ Stop", use_container_width=True)

# ── Placeholders ───────────────────────────────────────────────────────────────
chart_placeholder  = st.empty()
st.caption("Grids are 2'x2' base units.  Carriers are 52"x52".  Logical map resolution is 8x8 per base unit.") 
status_placeholder = st.empty()
log_placeholder    = st.empty()  

# Always render current position on load / rerun
render_frame(chart_placeholder, st.session_state.current_pos)

# ── Button State ───────────────────────────────────────────────────────────────
if play_btn:
    st.session_state.playing = True
if stop_btn:
    st.session_state.playing = False

# ── Playback Loop ──────────────────────────────────────────────────────────────

# ── re-render log on every rerun so it survives Stop ─────────────────────────
if st.session_state.log_lines:
    log_placeholder.markdown(
        "**Event Log**\n\n"
        + "\n\n".join(f"- {line}" for line in st.session_state.log_lines)
    )

# ── playback ──────────────────────────────────────────────────────────────────
if st.session_state.playing:
    status_placeholder.info("Loading MongoDB events...")
    events = fetch_events(batch_limit)
    if not events:
        status_placeholder.warning("No MongoDB events found.")
        st.session_state.playing = False
    else:
        status_placeholder.info(f"Playing back {len(events)} event(s)…")

        # Re-order the list oldest to newest
        sorted_events = sorted(events, key=lambda x: x['timestamp_epoch'])

        # Discover all robot IDs present across this batch of events
        all_robot_ids = sorted({
            c["carrier_id"]
            for doc in sorted_events
            for c in doc.get("carriers", [])
        })

        # Reset carriers to center for a clean start
        st.session_state.current_pos = create_center_positions(all_robot_ids)
        # Fresh log each time Play is hit
        st.session_state.log_lines = []

        for i, doc in enumerate(sorted_events):
            if not st.session_state.playing:
                status_placeholder.warning("⏹ Playback stopped.")
                break

            incoming_df = doc_to_df(doc)
            # Index on robot_id so LERP aligns correctly
            start_df  = st.session_state.current_pos.set_index("robot_id").sort_index()
            target_df = incoming_df.set_index("robot_id").sort_index()

            # Union of robots known so far and robots in this event
            full_index = start_df.index.union(target_df.index)
            start_df  = start_df.reindex(full_index)
            target_df = target_df.reindex(full_index)

            # New robots: no prior position -> appear directly at target (no LERP)
            start_df["x"] = start_df["x"].fillna(target_df["x"])
            start_df["y"] = start_df["y"].fillna(target_df["y"])
            # Robots missing from this event: hold their previous position
            target_df["x"] = target_df["x"].fillna(start_df["x"])
            target_df["y"] = target_df["y"].fillna(start_df["y"])

            ts_label = (
                f"Event {i+1}/{len(events)}"
                f"  ·  {epoch_to_str(doc.get('timestamp_epoch', 0))}"
            )

            # LERP animation
            for step in range(1, STEPS + 1):
                alpha    = step / STEPS
                frame_df = pd.DataFrame({
                    "robot_id": start_df.index,
                    "x": (start_df["x"] + (target_df["x"] - start_df["x"]) * alpha).values,
                    "y": (start_df["y"] + (target_df["y"] - start_df["y"]) * alpha).values,
                })
                render_frame(chart_placeholder, frame_df, label=ts_label)
                time.sleep(frame_delay)

            # Advance current position for next event
            st.session_state.current_pos = target_df.reset_index()

            # Append log entry after each event finishes
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

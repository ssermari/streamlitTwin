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
st.set_page_config(layout="wide")
st.title("USP Digital Twin")

st.markdown("""
<style>
    .block-container { padding-top: 2rem; }
    iframe { margin-top: 0px !important; }
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

# ── Sidebar Controls ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Simulation Settings")
    batch_limit = st.slider("Max Events to Process", 1, 100, 25)
    speed_level = st.slider("Movement Speed", 1, 5, 3)
    frame_delay = 0.1 / speed_level  # 1=slow(0.1s), 5=fast(0.02s)

# ── Constants ──────────────────────────────────────────────────────────────────
STEPS           = 20
WH_WIDTH_UNITS  = 400
WH_HEIGHT_UNITS = 128

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

img, img_b64        = get_warehouse_image()
img_width, img_height = img.size

scale_x = img_width  / WH_WIDTH_UNITS
scale_y = img_height / WH_HEIGHT_UNITS

def scale_coords(unit_x, unit_y):
    return unit_x * scale_x, unit_y * scale_y

# ── Session State ──────────────────────────────────────────────────────────────
def create_center_positions():
    return pd.DataFrame({
        "robot_id": [f"carrier-{i}" for i in range(1, 11)],
        "x": [img_width  / 2] * 10,
        "y": [img_height / 2] * 10,
    })

if "current_pos" not in st.session_state:
    st.session_state.current_pos = create_center_positions()
if "playing" not in st.session_state:
    st.session_state.playing = False
if "frame_id" not in st.session_state:
    st.session_state.frame_id = 0

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
           .sort("timestamp_epoch", pymongo.ASCENDING)
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
        df, x="x", y="y", text="robot_id",
        range_x=[0, img_width], range_y=[0, img_height]
    )
    fig.update_traces(
        marker=dict(size=18, color="red", line=dict(width=2, color="white")),
        textposition="top center",
        textfont=dict(family="Arial Black", size=14, color="darkblue"),
    )
    fig.add_layout_image(dict(
        source=img_b64, xref="x", yref="y",
        x=0, y=img_height, sizex=img_width, sizey=img_height,
        sizing="stretch", opacity=0.8, layer="below",
    ))
    fig.update_layout(
        width=1200, height=450,
        margin=dict(l=0, r=0, t=30 if label else 0, b=0),
        xaxis_visible=False, yaxis_visible=False,
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

# ── Controls ───────────────────────────────────────────────────────────────────
# FIX: original had st.columns([1, 1, 4]) returning 3 cols but only 2 were unpacked
c1, c2 = st.columns([1, 1])

with c1:
    play_btn = st.button("▶ Play", type="primary")
with c2:
    stop_btn = st.button("⏹ Stop")

# ── Placeholders ───────────────────────────────────────────────────────────────
chart_placeholder  = st.empty()
status_placeholder = st.empty()

# Always render current position on load
render_frame(chart_placeholder, st.session_state.current_pos)

# ── Button State ───────────────────────────────────────────────────────────────
if play_btn:
    st.session_state.playing = True
if stop_btn:
    st.session_state.playing = False

# ── Playback Loop ──────────────────────────────────────────────────────────────
if st.session_state.playing:

    status_placeholder.info("Loading MongoDB events...")

    # FIX: original referenced undefined `n_events` — now correctly uses `batch_limit` from sidebar
    events = fetch_events(batch_limit)

    if not events:
        status_placeholder.warning("No MongoDB events found.")
        st.session_state.playing = False

    else:
        status_placeholder.info(f"Playing back {len(events)} event(s)…")

        # Reset carriers to center for a clean playback every time
        st.session_state.current_pos = create_center_positions()

        for i, doc in enumerate(events):

            if not st.session_state.playing:
                status_placeholder.warning("⏹ Playback stopped.")
                break

            incoming_df = doc_to_df(doc)

            # Index on robot_id so LERP aligns correctly across frames
            start_df  = st.session_state.current_pos.set_index("robot_id").sort_index()
            target_df = incoming_df.set_index("robot_id").sort_index()

            # Carry forward robots missing from this event
            for rid in start_df.index:
                if rid not in target_df.index:
                    target_df.loc[rid] = start_df.loc[rid]
            target_df = target_df.sort_index()

            ts_label = (
                f"Event {i+1}/{len(events)}"
                f"  ·  {epoch_to_str(doc.get('timestamp_epoch', 0))}"
            )

            # LERP animation — frame_delay driven by sidebar speed slider
            for step in range(1, STEPS + 1):
                alpha    = step / STEPS
                frame_df = pd.DataFrame({
                    "robot_id": start_df.index,
                    "x": (start_df["x"] + (target_df["x"] - start_df["x"]) * alpha).values,
                    "y": (start_df["y"] + (target_df["y"] - start_df["y"]) * alpha).values,
                })
                render_frame(chart_placeholder, frame_df, label=ts_label)
                # FIX: original hardcoded STEP_SLEEP constant — now uses sidebar speed
                time.sleep(frame_delay)

            # Advance current position so next event starts from here
            st.session_state.current_pos = target_df.reset_index()

        if st.session_state.playing:
            st.session_state.playing = False
            status_placeholder.success(
                f"✅ Playback complete — {len(events)} events replayed.  "
                f"Last: {epoch_to_str(events[-1].get('timestamp_epoch', 0))}"
            )

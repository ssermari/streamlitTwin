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

# ── Constants ──────────────────────────────────────────────────────────────────
STEPS       = 15    # LERP interpolation steps between frames
STEP_SLEEP  = 0.03  # seconds per interpolation step

WH_WIDTH_UNITS  = 400
WH_HEIGHT_UNITS = 128

# ── 1. MongoDB client ──────────────────────────────────────────────────────────
@st.cache_resource
def get_mongo_client():
    return pymongo.MongoClient(st.secrets["MONGO_URI"])

try:
    mongo = get_mongo_client()
    db    = mongo[st.secrets["MONGO_DB"]]
    col   = db[st.secrets["MONGO_COLLECTION"]]
except Exception as e:
    st.error(f"Failed to connect to MongoDB: {e}")
    st.stop()

# ── 2. Image loading & scaling ─────────────────────────────────────────────────
@st.cache_resource
def get_warehouse_image():
    img = Image.open("wh.png")
    buf = BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return img, f"data:image/png;base64,{b64}"

img, img_b64 = get_warehouse_image()
img_width, img_height = img.size

scale_x = img_width  / WH_WIDTH_UNITS
scale_y = img_height / WH_HEIGHT_UNITS

def scale_coords(unit_x, unit_y):
    return unit_x * scale_x, unit_y * scale_y

# ── 3. Session state defaults ──────────────────────────────────────────────────
if "current_pos" not in st.session_state:
    st.session_state.current_pos = pd.DataFrame({
        "robot_id": [f"carrier-{i}" for i in range(1, 11)],
        "x": [img_width  / 2] * 10,
        "y": [img_height / 2] * 10,
    })
if "frame_count" not in st.session_state:
    st.session_state.frame_count = 0
if "run_id" not in st.session_state:
    st.session_state.run_id = 0

# ── 4. Helper functions ────────────────────────────────────────────────────────
def epoch_to_str(epoch_ms) -> str:
    """Convert millisecond epoch to a readable UTC string."""
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d  %H:%M:%S UTC")
    except Exception:
        return str(epoch_ms)

def fetch_events(n: int) -> list:
    """
    Read the N most-recent docs sorted by timestamp_epoch DESC,
    then reverse so playback runs oldest → newest.
    """
    docs = list(
        col.find({}, {"_id": 0})
           .sort("timestamp_epoch", pymongo.DESCENDING)
           .limit(n)
    )
    docs.reverse()  # oldest first for playback
    return docs

def doc_to_df(doc: dict) -> pd.DataFrame:
    """Convert a MongoDB document to a DataFrame of carrier positions."""
    rows = []
    for c in doc.get("carriers", []):
        px, py = scale_coords(c["position"]["x"], c["position"]["y"])
        rows.append({"robot_id": c["carrier_id"], "x": px, "y": py})
    if rows:
        return pd.DataFrame(rows)
    return st.session_state.current_pos.copy()

def render_frame(placeholder, df: pd.DataFrame, label: str = ""):
    """Render a single animation frame to the given placeholder."""
    st.session_state.frame_count += 1
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
            text=label,
            font=dict(color="#00d4ff", size=13),
            x=0.01
        ) if label else None,
        transition_duration=30,
    )
    placeholder.plotly_chart(
        fig,
        use_container_width=False,
        theme=None,
        config={"displayModeBar": False},
        key=f"map_{st.session_state.run_id}_{st.session_state.frame_count}",
    )

# ── 5. High-water mark ─────────────────────────────────────────────────────────
hwm_doc = col.find_one(
    {},
    {"timestamp_epoch": 1, "_id": 0},
    sort=[("timestamp_epoch", pymongo.DESCENDING)]
)
if hwm_doc:
    hwm_val = hwm_doc["timestamp_epoch"]
    st.markdown(
        f'<div class="hwm-box">⬆ HIGH WATER MARK &nbsp;|&nbsp; '
        f'<b>{epoch_to_str(hwm_val)}</b> &nbsp;·&nbsp; epoch&nbsp;{hwm_val}</div>',
        unsafe_allow_html=True,
    )
else:
    st.warning("No documents found in collection.")

# ── 6. Controls ────────────────────────────────────────────────────────────────
ctrl_col1, ctrl_col2 = st.columns([1, 4])
with ctrl_col1:
    n_events = st.number_input(
        "Events to replay",
        min_value=1,
        max_value=100,
        value=25,
        step=1,
        help="How many past snapshots to animate (1–100)",
    )
with ctrl_col2:
    st.write("")
    st.write("")
    run_playback = st.button("▶  Load & Play Back", type="primary")

# ── 7. Chart & status placeholders ────────────────────────────────────────────
chart_placeholder  = st.empty()
status_placeholder = st.empty()

# Always render current position on page load / rerun
render_frame(chart_placeholder, st.session_state.current_pos)

# ── 8. Playback loop ───────────────────────────────────────────────────────────
if run_playback:
    st.session_state.run_id += 1

    # Reset carriers to center so playback always starts fresh
    st.session_state.current_pos = pd.DataFrame({
        "robot_id": [f"carrier-{i}" for i in range(1, 11)],
        "x": [img_width  / 2] * 10,
        "y": [img_height / 2] * 10,
    })

    with st.spinner(f"Loading {n_events} events from MongoDB…"):
        events = fetch_events(int(n_events))

    if not events:
        status_placeholder.warning("No events returned from the collection.")
    else:
        status_placeholder.info(f"Playing back {len(events)} event(s)…")

        for i, doc in enumerate(events):
            target_df = doc_to_df(doc)

            # Capture start INSIDE the loop so each move chains from the previous



            # Align robots by robot_id before interpolation
            start_df = st.session_state.current_pos.set_index("robot_id")
            target_df = target_df.set_index("robot_id")

            # Ensure both contain same carriers
            target_df = target_df.reindex(start_df.index).fillna(start_df)

            start_df = start_df.sort_index()
            target_df = target_df.sort_index()


            ts_label  = (
                f"Event {i + 1} / {len(events)}"
                f"  ·  {epoch_to_str(doc.get('timestamp_epoch', 0))}"
            )

            # Smooth LERP from current position → target position

            for step in range(1, STEPS + 1):
                alpha = step / STEPS
                interp_x = start_df["x"] + (target_df["x"] - start_df["x"]) * alpha
                interp_y = start_df["y"] + (target_df["y"] - start_df["y"]) * alpha

                frame_df = pd.DataFrame({
                    "robot_id": start_df.index,
                    "x": interp_x.values,
                    "y": interp_y.values,
                })
                render_frame(chart_placeholder, frame_df, label=ts_label)
                time.sleep(STEP_SLEEP)

            # Advance current position to target before next event

            st.session_state.current_pos = (
                target_df.reset_index()
            ) 

        status_placeholder.success(
            f"✅ Playback complete — {len(events)} events replayed.  "
            f"Last: {epoch_to_str(events[-1].get('timestamp_epoch', 0))}"
        )

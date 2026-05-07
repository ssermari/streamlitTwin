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
    .block-container {
        padding-top: 2rem;
    }

    iframe {
        margin-top: 0px !important;
    }

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
STEPS = 20
STEP_SLEEP = 0.03

WH_WIDTH_UNITS = 400
WH_HEIGHT_UNITS = 128

# ── MongoDB Connection ─────────────────────────────────────────────────────────
@st.cache_resource
def get_mongo_client():
    return pymongo.MongoClient(st.secrets["MONGO_URI"])

try:
    mongo = get_mongo_client()

    db = mongo[st.secrets["MONGO_DB"]]

    col = db[st.secrets["MONGO_COLLECTION"]]

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

img, img_b64 = get_warehouse_image()

img_width, img_height = img.size

# ── Coordinate Scaling ─────────────────────────────────────────────────────────
scale_x = img_width / WH_WIDTH_UNITS
scale_y = img_height / WH_HEIGHT_UNITS

def scale_coords(unit_x, unit_y):

    return (
        unit_x * scale_x,
        unit_y * scale_y
    )

# ── Session State ──────────────────────────────────────────────────────────────
def create_center_positions():

    return pd.DataFrame({
        "robot_id": [f"carrier-{i}" for i in range(1, 11)],
        "x": [img_width / 2] * 10,
        "y": [img_height / 2] * 10,
    })

if "current_pos" not in st.session_state:

    st.session_state.current_pos = create_center_positions()

if "playing" not in st.session_state:

    st.session_state.playing = False

# ── Helpers ────────────────────────────────────────────────────────────────────
def epoch_to_str(epoch_ms):

    try:

        dt = datetime.fromtimestamp(
            int(epoch_ms) / 1000,
            tz=timezone.utc
        )

        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    except Exception:

        return str(epoch_ms)

def fetch_events(n):

    docs = list(
        col.find({}, {"_id": 0})
        .sort("timestamp_epoch", pymongo.ASCENDING)
        .limit(n)
    )

    return docs

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

            print("BAD CARRIER:", e)

    if rows:

        return pd.DataFrame(rows)

    return st.session_state.current_pos.copy()

# ── Rendering ──────────────────────────────────────────────────────────────────
def render_frame(placeholder, df, label="", frame_id=0):

    fig = px.scatter(
        df,
        x="x",
        y="y",
        text="robot_id",
        range_x=[0, img_width],
        range_y=[0, img_height]
    )

    fig.update_traces(
        marker=dict(
            size=18,
            color="red",
            line=dict(
                width=2,
                color="white"
            )
        ),
        textposition="top center",
        textfont=dict(
            family="Arial Black",
            size=14,
            color="darkblue"
        )
    )

    fig.add_layout_image(
        dict(
            source=img_b64,
            xref="x",
            yref="y",
            x=0,
            y=img_height,
            sizex=img_width,
            sizey=img_height,
            sizing="stretch",
            opacity=0.8,
            layer="below"
        )
    )

    fig.update_layout(
        width=1200,
        height=450,
        margin=dict(
            l=0,
            r=0,
            t=30 if label else 0,
            b=0
        ),
        xaxis_visible=False,
        yaxis_visible=False,
        title=dict(
            text=label,
            x=0.01,
            font=dict(
                color="#00d4ff",
                size=13
            )
        ) if label else None
    )

    placeholder.plotly_chart(
        fig,
        use_container_width=False,
        theme=None,
        config={
            "displayModeBar": False
        },
        key=f"warehouse_map_{frame_id}"
    )

# ── High Water Mark ────────────────────────────────────────────────────────────
hwm_doc = col.find_one(
    {},
    {"timestamp_epoch": 1, "_id": 0},
    sort=[("timestamp_epoch", pymongo.DESCENDING)]
)

if hwm_doc:

    hwm = hwm_doc["timestamp_epoch"]

    st.markdown(
        f"""
        <div class="hwm-box">
            ⬆ HIGH WATER MARK |
            <b>{epoch_to_str(hwm)}</b>
            · epoch {hwm}
        </div>
        """,
        unsafe_allow_html=True
    )

else:

    st.warning("No documents found in collection.")

# ── Controls ───────────────────────────────────────────────────────────────────
c1, c2, c3 = st.columns([1, 1, 4])

with c1:

    n_events = st.number_input(
        "Events",
        min_value=1,
        max_value=500,
        value=25
    )

with c2:

    st.write("")
    st.write("")

    play_btn = st.button(
        "▶ Play",
        type="primary"
    )

with c3:

    st.write("")
    st.write("")

    stop_btn = st.button("⏹ Stop")

# ── Placeholders ───────────────────────────────────────────────────────────────
chart_placeholder = st.empty()

status_placeholder = st.empty()

# ── Initial Render ─────────────────────────────────────────────────────────────
render_frame(
    chart_placeholder,
    st.session_state.current_pos,
    frame_id="initial"
)

# ── Control Logic ──────────────────────────────────────────────────────────────
if play_btn:

    st.session_state.playing = True

if stop_btn:

    st.session_state.playing = False

# ── Playback Loop ──────────────────────────────────────────────────────────────
if st.session_state.playing:

    status_placeholder.info(
        "Loading MongoDB events..."
    )

    events = fetch_events(int(n_events))

    if not events:

        status_placeholder.warning(
            "No MongoDB events found."
        )

    else:

        status_placeholder.success(
            f"Loaded {len(events)} events."
        )

        # Reset robots to center
        st.session_state.current_pos = create_center_positions()

        # Iterate MongoDB Events
        for i, doc in enumerate(events):

            if not st.session_state.playing:
                break

            incoming_df = doc_to_df(doc)

            # ── Align Current vs Target Positions ────────────────────────
            start_df = (
                st.session_state.current_pos
                .set_index("robot_id")
                .sort_index()
            )

            target_df = (
                incoming_df
                .set_index("robot_id")
                .sort_index()
            )

            # Add missing robots
            for rid in start_df.index:

                if rid not in target_df.index:

                    target_df.loc[rid] = start_df.loc[rid]

            target_df = target_df.sort_index()

            # ── Timestamp Label ──────────────────────────────────────────
            ts_label = (
                f"Event {i+1}/{len(events)}"
                f" · "
                f"{epoch_to_str(doc.get('timestamp_epoch', 0))}"
            )

            # ── Smooth Animation ─────────────────────────────────────────
            for step in range(1, STEPS + 1):

                alpha = step / STEPS

                interp_x = (
                    start_df["x"] +
                    (target_df["x"] - start_df["x"]) * alpha
                )

                interp_y = (
                    start_df["y"] +
                    (target_df["y"] - start_df["y"]) * alpha
                )

                frame_df = pd.DataFrame({
                    "robot_id": start_df.index,
                    "x": interp_x.values,
                    "y": interp_y.values
                })
                
                render_frame(
                    chart_placeholder,
                    frame_df,
                    ts_label,
                    frame_id=f"{i}_{step}"
                )   


                time.sleep(STEP_SLEEP)

            # ── Save Current Position ───────────────────────────────────
            st.session_state.current_pos = (
                target_df.reset_index()
            )

        status_placeholder.success(
            f"✅ Playback Complete — "
            f"{len(events)} events replayed."
        )

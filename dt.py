import streamlit as st
import plotly.express as px
from PIL import Image
import pandas as pd
import numpy as np
import time
import boto3
import json

st.set_page_config(layout="wide")
st.title("USP Digital Twin")

# 1. CSS for Layout Cleanup
st.markdown("""
    <style>
        .block-container { padding-top: 2rem; } 
        iframe { margin-top: 0px !important; }
    </style>
""", unsafe_allow_html=True)

# 2. Setup Authenticated AWS Client (Cached)
@st.cache_resource
def get_sqs_client():
    return boto3.client(
        "sqs",
        aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
        region_name=st.secrets.get("AWS_DEFAULT_REGION", "us-east-1")
    )

sqs = get_sqs_client()
#QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/886812109001/wms-queue.fifo'
QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/886812109001/wms-events-q'

# 3. Setup Image & Scaling
image_path = "wh.png" 
img = Image.open(image_path)
img_width, img_height = img.size

# Physical dimensions of your warehouse grid (matches your simulator)
WH_WIDTH_UNITS = 400  
WH_HEIGHT_UNITS = 128 

scale_x = img_width / WH_WIDTH_UNITS
scale_y = img_height / WH_HEIGHT_UNITS

def scale_coords(unit_x, unit_y):
    pixel_x = unit_x * scale_x
    pixel_y = unit_y * scale_y 
    return pixel_x, pixel_y

# 4. Initialize Session State
if 'current_pos' not in st.session_state:
    st.session_state.current_pos = pd.DataFrame({
        'robot_id': [f'carrier-{i}' for i in range(1, 11)],
        'x': [img_width/2] * 10,
        'y': [img_height/2] * 10
    })

if 'frame_count' not in st.session_state:
    st.session_state.frame_count = 0

def get_new_target():
    """Polls SQS for a single message and deletes it to unblock FIFO."""
    try:
        response = sqs.receive_message(
            QueueUrl=QUEUE_URL, 
            MaxNumberOfMessages=20, 
            WaitTimeSeconds=1
        )
        
        messages = response.get('Messages', [])
        if messages:
            msg = messages[0]
            st.toast("✅ Data received from SQS!", icon="🤖")
            
            # Delete message so the next one in the FIFO group can be read
            sqs.delete_message(
                QueueUrl=QUEUE_URL, 
                ReceiptHandle=msg['ReceiptHandle']
            )

            body = json.loads(msg['Body'])
            carrier_data = []
            for c in body.get('carriers', []):
                px, py = scale_coords(c['position']['x'], c['position']['y'])
                carrier_data.append({
                    'robot_id': c['carrier_id'], 
                    'x': px, 
                    'y': py
                })
            return pd.DataFrame(carrier_data)
            
    except Exception as e:
        print(f"SQS Error: {e}")
        
    return st.session_state.current_pos

# 5. Smoothing Fragment
@st.fragment
def run_tracker():
    chart_placeholder = st.empty()
    steps = 15  # Number of interpolation steps (higher = smoother, lower = faster)
    
    def render_frame(df):
        """Standardizes the rendering of the Plotly map."""
        st.session_state.frame_count += 1
        
        fig = px.scatter(df, x="x", y="y", text="robot_id", 
                         range_x=[0, img_width], range_y=[0, img_height])
        
        fig.update_traces(
            marker=dict(size=18, color='red', line=dict(width=2, color='white')),
            textposition="top center",
            textfont=dict(family="Arial Black", size=14, color="darkblue")
        )
        
        fig.add_layout_image(dict(
            source=img, xref="x", yref="y", x=0, y=img_height,
            sizex=img_width, sizey=img_height, sizing="stretch", opacity=0.8, layer="below"
        ))
        
        fig.update_layout(
            width=1200, height=450, 
            margin=dict(l=0, r=0, t=0, b=0),
            xaxis_visible=False, yaxis_visible=False,
            transition_duration=30
        )
        
        chart_placeholder.plotly_chart(
            fig, 
            width="content", 
            theme=None,
            config={'displayModeBar': False},
            key=f"map_frame_{st.session_state.frame_count}"
        )

    # Initial render: show map immediately using current session state
    render_frame(st.session_state.current_pos)

    while True:
        target_pos = get_new_target()
        
        # Check if the new data is different from current position
        if not target_pos.equals(st.session_state.current_pos):
            start_pos = st.session_state.current_pos.copy()
            
            # Animation loop (Linear Interpolation)
            for step in range(1, steps + 1):
                alpha = step / steps
                interim_df = start_pos.copy()
                interim_df['x'] = start_pos['x'] + (target_pos['x'] - start_pos['x']) * alpha
                interim_df['y'] = start_pos['y'] + (target_pos['y'] - start_pos['y']) * alpha
                
                render_frame(interim_df)
                time.sleep(0.03) 
            
            st.session_state.current_pos = target_pos
        else:
            # If no data is in queue, wait slightly to reduce CPU load
            time.sleep(0.5) 

# Start the live digital twin
run_tracker()


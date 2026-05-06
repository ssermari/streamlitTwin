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

# 2. Setup AWS Client & Configuration
# Boto3 will automatically use st.secrets for these keys if provided
try:
    sqs = boto3.client(
        "sqs",
        aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
        region_name=st.secrets.get("AWS_DEFAULT_REGION", "us-east-1")
    )
except Exception:
    st.error("AWS Credentials not found. Please add them to .streamlit/secrets.toml")

QUEUE_URL = 'https://amazonaws.com'

# 3. Setup Image & Scaling
image_path = "wh.png" 
img = Image.open(image_path)
width, height = img.size

# Constants for scaling (Adjust these based on your warehouse meters/cells)
WH_WIDTH_UNITS = 400  # Total physical width of map in your telemetry units
WH_HEIGHT_UNITS = 128 # Total physical height of map in your telemetry units

scale_x = width / WH_WIDTH_UNITS
scale_y = height / WH_HEIGHT_UNITS

def scale_coords(unit_x, unit_y):
    pixel_x = unit_x * scale_x
    # If telemetry (0,0) is bottom-left, uncomment the line below to flip for image pixels
    # pixel_y = height - (unit_y * scale_y)
    pixel_y = unit_y * scale_y 
    return pixel_x, pixel_y

# 4. Initialize Session State (Starting positions)
if 'current_pos' not in st.session_state:
    st.session_state.current_pos = pd.DataFrame({
        'robot_id': [f'carrier-{i}' for i in range(1, 11)],
        'x': [width/2] * 10,
        'y': [height/2] * 10
    })

def get_new_target():
    try:
        # Long polling (WaitTimeSeconds) reduces costs and CPU usage
        response = sqs.receive_message(QueueUrl=QUEUE_URL, MaxNumberOfMessages=1, WaitTimeSeconds=1)
        if 'Messages' in response:
            msg = response['Messages'][0]
            body = json.loads(msg['Body'])
            sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg['ReceiptHandle'])

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
        # Fail silently to keep the animation running with last known positions
        pass
    return st.session_state.current_pos

# 5. Smoothing Fragment
@st.fragment

def run_tracker():
    # Create the single "slot" for the chart
    chart_placeholder = st.empty()
    steps = 15  
    

    # Create a counter at the very top of your fragment
    frame_counter = 0

    while True:
        # ... your interpolation logic ...
        for step in range(1, steps + 1):
            frame_counter += 1  # Increment this every single frame
        
            # ... your fig setup ...
           # Use the placeholder to update, but provide a unique key for every frame
            placeholder.plotly_chart(
                fig, 
                use_container_width=False, 
                theme=None,
                config={'displayModeBar': False},
                key=f"warehouse_map_{frame_counter}" # Dynamic unique key
            )

            time.sleep(0.03)


        st.session_state.current_pos = target_pos


run_tracker()


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

st.markdown("""
    <style>
        /* Adjust 2rem higher or lower to fix the title chopping */
        .block-container { padding-top: 2rem; } 
        
        /* Ensures the chart doesn't have a huge gap above it */
        iframe { margin-top: 0px !important; }
    </style>
""", unsafe_allow_html=True)

QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/886812109001/wms-queue.fifo'

# 1. Setup Image
image_path = "wh.png" 
img = Image.open(image_path)
width, height = img.size

# Constants for your specific warehouse
WH_WIDTH_CELLS = 100*4  # Physical width of the floor
WH_HEIGHT_CELLS = 32*4 # Physical height of the floor

# Scale factors: pixels per meter
scale_x = width / WH_WIDTH_CELLS
scale_y = height / WH_HEIGHT_CELLS

def scale_coords(meter_x, meter_y):
    # Convert meters to pixels
    pixel_x = meter_x * scale_x
    # Flip Y if your robot origin is bottom-left but image is top-left
    #pixel_y = height - (meter_y * scale_y) 
    return pixel_x, pixel_y


# 2. Initialize Session State for Positions
if 'current_pos' not in st.session_state:
    st.session_state.current_pos = pd.DataFrame({
        'robot_id': ['Carrier_01', 'Carrier_02', 'Carrier_03'],
        'x': [width/2] * 3,
        'y': [height/2] * 3
    })


def aws_conn():
    client = boto3.client(
        "sqs",
        aws_access_key_id=acc_key,
        aws_secret_access_key=sec_key,
        region_name="us-east-1"
    )
    return client


def get_new_target():
    sqs = boto3.client("sqs", region_name="us-east-1")
    try:
        response = sqs.receive_message(QueueUrl=QUEUE_URL, MaxNumberOfMessages=1)
        if 'Messages' in response:
            msg = response['Messages'][0]
            body = json.loads(msg['Body'])
            sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg['ReceiptHandle'])

            carrier_data = []
            for c in body.get('carriers', []):
                # Apply scaling here
                px, py = scale_coords(c['position']['x'], c['position']['y'])
                carrier_data.append({
                    'robot_id': c['carrier_id'],
                    'x': px,
                    'y': py
                })
            return pd.DataFrame(carrier_data)
    except Exception as e:
        st.error(f"SQS Error: {e}")
    return st.session_state.current_pos


# 3. Smoothing Fragment
@st.fragment
def run_tracker():
    placeholder = st.empty()
    steps = 10  # Number of intermediate frames between telematics updates
    
    while True:
        target_pos = get_new_target()
        start_pos = st.session_state.current_pos.copy()

        # Animate the transition between start and target
        for step in range(1, steps + 1):
            alpha = step / steps  # Progress percentage (0.0 to 1.0)
            
            # Linear Interpolation formula: Start + (Target - Start) * Alpha
            interim_df = start_pos.copy()
            interim_df['x'] = start_pos['x'] + (target_pos['x'] - start_pos['x']) * alpha
            interim_df['y'] = start_pos['y'] + (target_pos['y'] - start_pos['y']) * alpha

            # Create the figure
            fig = px.scatter(interim_df, x="x", y="y", text="robot_id", 
                 range_x=[0, width], range_y=[0, height])

            # Customize dots and labels for visibility
            fig.update_traces(
                # Make dots bigger and red
                marker=dict(
                    size=18, 
                    color='red', 
                    line=dict(width=2, color='white') # White outline helps them pop
                ),
                # Style the text labels
                textposition="top center",
                textfont=dict(
                    family="Arial Black",
                    size=14,
                    color="darkblue" # Change label to dark blue
                )
            )


            fig.add_layout_image(dict(
                source=img, xref="x", yref="y", x=0, y=height,
                sizex=width, sizey=height, sizing="stretch", opacity=0.8, layer="below"
            ))

            fig.update_layout(width=1200, height=450, margin=dict(l=0, r=0, t=0, b=0),
                              xaxis_visible=False, yaxis_visible=False,
                              transition_duration=50) # Tell Plotly to animate markers


            # Create a stable container with a fixed height and width

            with placeholder.container():
                # Single call, no extra HTML wrappers needed here
                st.plotly_chart(
                    fig, 
                    use_container_width=False, 
                    # Set theme to None to prevent Streamlit from adding extra padding
                    theme=None,
                    config={'displayModeBar': False}
                )
                
                # 3. Close the HTML "box"
                #st.markdown('</div>', unsafe_allow_html=True)

            time.sleep(0.05) # Control the "frame rate"

        st.session_state.current_pos = target_pos

run_tracker()


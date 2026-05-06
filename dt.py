import streamlit as st
import plotly.express as px
from PIL import Image
import pandas as pd
import numpy as np
import time

st.set_page_config(layout="wide")
st.title("Warehouse Digital Twin - Smooth Tracking")

# Force the app to use the full top space and remove padding
st.markdown("""
    <style>
        .block-container { padding-top: 1rem; padding-bottom: 0rem; }
        .stPlotlyChart { margin-top: -30px; }
    </style>
""", unsafe_allow_html=True)


# 1. Setup Image
image_path = "wh.png" 
img = Image.open(image_path)
width, height = img.size

# 2. Initialize Session State for Positions
if 'current_pos' not in st.session_state:
    st.session_state.current_pos = pd.DataFrame({
        'robot_id': ['Carrier_01', 'Carrier_02', 'Carrier_03'],
        'x': [width/2] * 3,
        'y': [height/2] * 3
    })

def get_new_target():
    # Simulate receiving new telematics data
    return pd.DataFrame({
        'robot_id': ['Carrier_01', 'Carrier_02', 'Carrier_03'],
        'x': [np.random.randint(0, width) for _ in range(3)],
        'y': [np.random.randint(0, height) for _ in range(3)]
    })

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

            fig.update_layout(width=800, height=300, margin=dict(l=0, r=0, t=0, b=0),
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



import streamlit as st
import plotly.express as px
from PIL import Image
import pandas as pd
import random

st.set_page_config(layout="wide")
st.title("Warehouse Digital Twin - Live Robot Tracking")

# 1. Load your floor plan image
# Replace 'warehouse_floor.png' with your actual file path
image_path = "wh.png" 
img = Image.open(image_path)
width, height = img.size

# 2. Mock Telematics Data (Replace this with your MQTT/SQL stream)
def get_telematics():
    return pd.DataFrame({
        'robot_id': ['Carrier_01', 'Carrier_02', 'Carrier_03'],
        'x': [random.randint(0, width) for _ in range(3)],
        'y': [random.randint(0, height) for _ in range(3)]
    })

# 3. Create the Visualization
placeholder = st.empty()

# Simple loop to simulate real-time updates
for i in range(100):
    df = get_telematics()
    
    # Create a Plotly figure
    fig = px.scatter(df, x="x", y="y", text="robot_id", 
                     range_x=[0, width], range_y=[0, height])

    # Add the image as a background
    fig.add_layout_image(
        dict(
            source=img,
            xref="x", yref="y",
            x=0, y=height, # Image starts at top-left
            sizex=width, sizey=height,
            sizing="stretch",
            opacity=0.8,
            layer="below")
    )

    # Clean up the UI
    fig.update_layout(
        width=800, height=600,
        margin=dict(l=0, r=0, t=0, b=0),
        xaxis_visible=False, yaxis_visible=False
    )
    fig.update_traces(marker=dict(size=12, color='red', line=dict(width=2, color='White')))

    with placeholder.container():
        st.plotly_chart(fig, use_container_width=True)
    
    st.cache_data.clear() # Clear cache if pulling from a live DB


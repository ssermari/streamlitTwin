import streamlit as st
import boto3
from botocore.exceptions import NoCredentialsError

# --- Configuration ---
# Replace with your actual queue URL
QUEUE_URL = 'https://sqs.us-east-1.amazonaws.com/886812109001/wms-queue.fifo'
REGION = 'us-east-1'

st.title("AWS SQS Message Reader")

# --- Initialize SQS Client ---
@st.cache_resource

def get_sqs_client():
    # Pass secrets directly into the client inside the cached function
    return boto3.client(
        "sqs",
        aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
        region_name=st.secrets.get("AWS_DEFAULT_REGION", REGION)
    )


try:
    sqs = get_sqs_client()
except Exception as e:
    st.error(f"Failed to initialize AWS client. Check your secrets: {e}")
    st.stop() # Stops execution if client can't be created


# --- Functions ---
def fetch_messages():
    """Fetches messages from SQS."""
    try:
        response = sqs.receive_message(
            QueueUrl=QUEUE_URL,
            MaxNumberOfMessages=5,
            WaitTimeSeconds=5,  # Enable long polling
            AttributeNames=['All']
        )
        return response.get('Messages', [])
    except Exception as e:
        st.error(f"Error connecting to SQS: {e}")
        return []

def delete_message(receipt_handle):
    """Deletes a message from SQS."""
    sqs.delete_message(
        QueueUrl=QUEUE_URL,
        ReceiptHandle=receipt_handle
    )

# --- UI Components ---
if st.button("Check for New Messages"):
    with st.spinner("Polling SQS..."):
        messages = fetch_messages()
        
        if not messages:
            st.info("No messages in queue.")
        else:
            st.success(f"Found {len(messages)} message(s)!")
            
            for msg in messages:
                with st.expander(f"Message ID: {msg['MessageId']}"):
                    st.write(msg['Body'])
                    
                    # Delete button for each message
                    if st.button("Delete Message", key=msg['ReceiptHandle']):
                        delete_message(msg['ReceiptHandle'])
                        st.rerun() # Refresh to update state

# Optional: Automatic polling every 10 seconds
if st.checkbox("Auto-poll"):
    st.rerun() 


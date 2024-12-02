import os
from anthropic import AsyncAnthropic
from fastapi import FastAPI, Request, Response
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from pydantic import BaseModel
import os
import json
from dotenv import load_dotenv
import re
import httpx
from typing import Optional

# Load environment variables
load_dotenv()

# Initialize the FastAPI app
app = FastAPI()

# Secret Management
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
BACKEND_API_KEY = os.getenv("BACKEND_API_KEY")

# Initialize the Slack client
slack_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))

# Initialize the Slack Signature Verifier
signature_verifier = SignatureVerifier(os.getenv("SLACK_SIGNING_SECRET"))

# Initialize bot user_id
bot_id = slack_client.auth_test()['user_id'] 

# Track event IDs to ignore duplicates
processed_event_ids = set()

class SlackEvent(BaseModel):
    type: str
    user: str
    text: str
    channel: str

#### FUNCTIONS ####

async def bot(query: str, user_id: str) -> Optional[str]:
    print("Sending request to server!")
    headers = {"Authorization": f"Bearer {BACKEND_API_KEY}"}
    error_message = "Sorry, too many requests. Try again in a minute!"

    try:
        response = await post_request(

            'YOUR_URL', headers, {"user_input": query, "user_id": user_id}

        )
        return format_output(response)
    except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError, Exception) as e:
        print(f"Error occurred: {e}")
        return re.sub(r'<\2|\1>', error_message)

async def post_request(url: str, headers: dict, json_data: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=200) as client:
        response = await client.post(url, headers=headers, json=json_data)
        response.raise_for_status()
        return response

def format_output(response: httpx.Response, link_pattern: str) -> Optional[str]:
    response_json = response.json()
    if 'output' in response_json:
        return re.sub(link_pattern, r'<\2|\1>', response_json['output'])
    else:
        print("Output key not found in JSON response")
        return None


#### ROUTES ####

@app.get("/_health")
async def health_check():
    return {"status": "OK"}

@app.post("/")
async def slack_events(request: Request):
    print("Request received!")
    # Get the request body
    body_bytes = await request.body()
    body = json.loads(body_bytes)

    # Verify the request from Slack
    if not signature_verifier.is_valid_request(body_bytes, request.headers):
        return Response(status_code=403)
    
    # Parse the event
    event = body.get('event')
    print (event)

    # Ignore duplicate events
    event_id = event.get('event_ts')
    print(event_id)
    if event_id in processed_event_ids:
        return Response(status_code=200)
    processed_event_ids.add(event_id)
    
    # Here we're basically saying that we want all types of messages to be scanned
    if event and event.get('type'):
        # Check if the message event is from the bot itself
        if event.get('user') == bot_id:
            return Response(status_code=200)

        user_text = event.get('text')    
        user_id = event.get('user')
        
        # Event handler
        response_text = await bot(user_text, user_id)

        if response_text == "customer question":

            response_text = f'<@Dan> {response_text}'
            print(response_text)

            # Get channel ID
            channel = event.get('channel')

            # Send a response back to Slack in the thread where the bot was mentioned
            slack_client.chat_postMessage(
                channel=channel,
                text=response_text, 
                thread_ts=event.get('thread_ts') if event.get('thread_ts') else event.get('ts') 
            )
        elif response_text == "not a question":
            return Response(status_code=200)
        else:
            return Response(status_code=200)

    return Response(status_code=200)

# Local start command: uvicorn slack_bot:app --reload --port 8800

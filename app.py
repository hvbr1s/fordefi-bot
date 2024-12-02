import os
import instructor
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

class Analysis(BaseModel):
    analysis: str


# Init Anthropic client
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
model = "claude-3-5-haiku-latest"
instructor_client_anthropic = instructor.from_anthropic(AsyncAnthropic(), mode=instructor.Mode.ANTHROPIC_JSON)

#### FUNCTIONS ####

async def bot(query: str, user_id: str) -> Optional[str]:
    print("Sending request to server!")
    error_message = "Sorry, too many requests. Try again in a minute!"

    try:
        response = await ping_llm(query)
        return response 
    except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError, Exception) as e:
        print(f"Error occurred: {e}")
        return error_message

async def post_request(url: str, headers: dict, json_data: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=200) as client:
        response = await client.post(url, headers=headers, json=json_data)
        response.raise_for_status()
        return response
    
async def ping_llm(query):
        system = """
        You are a customer service triage assistant. Your role is to analyze incoming messages 
        and determine if they are customer queries related to crypto or Fordefi (a crypto wallet 
        designed for DeFi).

        Consider a message as relevant if it:
        - Asks questions about crypto transactions
        - Mentions Fordefi functionality
        - Reports issues with the wallet
        - Requests support for DeFi operations

        Your response must be a JSON file with the following structure:
            {
            "analysis": "[ANSWER 'YES' OR 'NO']",
            }
        """
        response = await instructor_client_anthropic.chat.completions.create(
                model=model,
                response_model=Analysis,
                temperature=0.0,
                max_tokens=1024,
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": query,
                    }
                ],
            )
        analysis = (response.analysis).lower().strip()
        print(f"Analysis result: {analysis}") 
        return analysis


async def format_output(response: httpx.Response, link_pattern: str) -> Optional[str]:
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
    
    # Check if this is a URL verification challenge
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}
    
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

        if response_text == "yes":

            ping_cs = f'<@U082GSCDFG9> please take a look 😊'
            print(response_text)

            # Get channel ID
            channel = event.get('channel')

            # Send a response back to Slack in the thread where the bot was mentioned
            slack_client.chat_postMessage(
                channel=channel,
                text=ping_cs, 
                thread_ts=event.get('thread_ts') if event.get('thread_ts') else event.get('ts') 
            )
        elif response_text == "no":
            return Response(status_code=200)
        else:
            return Response(status_code=200)

    return Response(status_code=200)

# Local start command: uvicorn app:app --reload --port 8800
# ngrok http http://localhost:8800

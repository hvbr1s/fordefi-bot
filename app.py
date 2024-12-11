import json
import os
import re
import asyncio
from typing import Optional
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
import httpx
import instructor
from collections import defaultdict
from datetime import datetime
from pydantic import BaseModel
import requests
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

# Load environment variables
load_dotenv()

# Initialize the FastAPI app
app = FastAPI()

# Set up message buffer and timers
message_buffer = defaultdict(list)
timers = {}
BUFFER_TIMEOUT = 25 

# Secret Management
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
THENA_AUTH_TOKEN = os.getenv("THENA_AUTH_TOKEN")

# Initialize the Slack client
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# Initialize the Slack Signature Verifier
signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET)

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
    customer_query: str
    query_summary: str

# Init Anthropic client
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
model = "claude-3-5-sonnet-latest" #(smarter)
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
    - Is a question or request for information
    - Asks questions about crypto transactions
    - Mentions Fordefi functionality
    - Reports issues with the Fordefi wallet or extension or web app on mobile or desktop
    - Requests support for DeFi operations
    - Request for help without other specifications

    Ignore the message if it:
    - Contains no question or support request
    - Is just a greeting (like "hi", "hello")
    - Is just an acknowledgment (like "thanks", "okay")
    - Is small talk or casual conversation
    - Is a response to another message without a new question

    Your response must be a JSON file with the following structure:
        {
        "customer_query": "[ANSWER 'YES' OR 'NO']",
        "query_summary": "[A VERY SHORT SUMMARY OF THE QUERY IN 7 WORDS MAX]"
        }
    """
    response = await instructor_client_anthropic.chat.completions.create(
            model=model,
            response_model=Analysis,
            temperature=0.0,
            max_tokens=512,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": query,
                }
            ],
        )
    print(f"Analysis result: {response.customer_query.capitalize()}")
    return response

async def format_output(response: httpx.Response, link_pattern: str) -> Optional[str]:
    response_json = response.json()
    if 'output' in response_json:
        return re.sub(link_pattern, r'<\2|\1>', response_json['output'])
    else:
        print("Output key not found in JSON response")
        return None
    
async def thena(username, query, summary):
    url = "https://bolt.thena.ai/rest/v2/requests"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "Authorization": f"Bearer {os.getenv('THENA_AUTH_TOKEN')}"
    }

    payload = {
        "request": {
            "status": "OPEN",
            "properties": {
                "system": {
                    "title": summary,
                    "description": f"**{username}**: '{query}'",
                    "sentiment": "Neutral", 
                    "urgency":"Medium"
                }},
            "assignment": {
                "to_user_id": "6740cc1209c61cc23e36595f"
            },
            "created_for": {
                "user_email": "customer@fordefi.com"
            },
            "private": False
        }
    }

    response = requests.post(url, headers=headers, json=payload)
    print(f"Thena API response status: {response.status_code}")
    print(f"Thena API response content: {response.text}") 
    return response

async def should_process_messages(message_key) -> bool:
    """Determine if we should process the buffered messages."""
    if not message_buffer[message_key]:
        return False
    
    earliest_msg_time = message_buffer[message_key][0]['timestamp']
    current_time = datetime.now().timestamp()
    print(f"Earlier time: {earliest_msg_time}")
    print(f"Current time: {current_time}")
    
    # Only process if BUFFER_TIMEOUT seconds have passed since the first message or if we have a large batch of messages.
    should_process = (current_time - earliest_msg_time) >= BUFFER_TIMEOUT or \
                     len(message_buffer[message_key]) >= 5
    return should_process

async def process_if_ready(message_key: str):
    """Check if the buffered messages for this key are ready to be processed and, if so, process them."""
    if message_key not in message_buffer or not message_buffer[message_key]:
        return

    earliest_msg_time = message_buffer[message_key][0]['timestamp']
    current_time = datetime.now().timestamp()

    # Check time
    if (current_time - earliest_msg_time) >= BUFFER_TIMEOUT or len(message_buffer[message_key]) >= 5:
        # Time to process
        combined_text = " ".join(m['text'] for m in message_buffer[message_key])
        print(f"Processing buffered messages for {message_key}: {combined_text}")

        event = message_buffer[message_key][0]['event']
        user_id = event.get('username')

        bot_response = await bot(combined_text, user_id)
        if isinstance(bot_response, str):
            # If an error string is returned, just clear the buffer
            print("Bot response was an error string, clearing buffer.")
            del message_buffer[message_key]
            return
        analysis = (bot_response.customer_query).lower().strip()
        summary = (bot_response.query_summary).capitalize().strip()

        if analysis == "yes":
            channel = event.get('channel')
            ping_cs = f'<@U082GSCDFG9> <@U04LKS6KL7R> please take a look 😊'
            slack_client.chat_postMessage(
                channel=channel,
                text=ping_cs, 
                thread_ts=event.get('thread_ts') if event.get('thread_ts') else event.get('ts') 
            )

            try:
                await thena(username=user_id, query=combined_text, summary=summary)
            except Exception as e:
                print(f"Error creating Thena request: {str(e)}")

        # Clear the buffer after processing
        del message_buffer[message_key]
        print('Cleared buffer after processing!')

async def schedule_processing(message_key: str):
    """Schedule a delayed check of the message buffer after BUFFER_TIMEOUT seconds."""
    # If we already have a timer for this key, do nothing. 
    # The existing timer will handle processing after BUFFER_TIMEOUT.
    if message_key in timers:
        return
    
    async def delayed_check():
        print(f"Starting {BUFFER_TIMEOUT}-second delay for key: {message_key}")
        await asyncio.sleep(BUFFER_TIMEOUT)
        print(f"{BUFFER_TIMEOUT}-second delay finished for key: {message_key}, now processing.")
        # After the sleep, re-check if we can process
        await process_if_ready(message_key)
        # Remove the timer reference
        timers.pop(message_key, None)
        print(f"Timer for key {message_key} removed from timers dict.")
    
    # Store the task so we know this key is scheduled
    timers[message_key] = asyncio.create_task(delayed_check())

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

    # Verify the request is from Slack
    if not signature_verifier.is_valid_request(body_bytes, request.headers):
        return Response(status_code=403)
    
    # Check if this is a URL verification challenge
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}
    
    event = body.get('event', {})
    print(event)

    # Ignore duplicate events
    event_id = event.get('event_ts')
    print(event_id)
    if event_id in processed_event_ids:
        print(f"Duplicate event {event_id}, not responding.")
        return Response(status_code=200)
    processed_event_ids.add(event_id)
    
    # Check event conditions and ignore certain message types
    if event and event.get('type'):
        if event.get('user') == bot_id:
            print('Ignoring, SamBot talking.')
            return Response(status_code=200)
        elif event.get('subtype') == 'channel_join':
            print('Ignoring, just someone joining the channel.')
            return Response(status_code=200)
        elif event.get('subtype') == 'message_changed':
            print('Ignoring, just someone editing a message.')
            return Response(status_code=200)
        elif event.get('subtype') == 'message_deleted':
            print('Ignoring, just someone deleting a message.')
            return Response(status_code=200)
        
        # Check username condition
        user_name = event.get('username', '')
        if re.search(r'dean|fordefi|poluy|@Ancientfish', user_name, re.IGNORECASE):
            print('Ignoring, just someone from Fordefi replying.')
            return Response(status_code=200)

        if not event.get('text'): 
            print('Ignoring, empty message text or image.')
            return Response(status_code=200)

        user_text = event.get('text')    
        user_id = event.get('username')
        timestamp = event.get('ts')
        channel = event.get('channel')

        # Buffer the message
        message_key = f"{channel}:{user_id}"
        arrival_time = datetime.now().timestamp()
        message_buffer[message_key].append({
            'text': user_text,
            'timestamp': arrival_time,
            'event': event
        })

        print(f"Current buffer state: {dict(message_buffer)}")

        # Schedule processing after BUFFER_TIMEOUT
        await schedule_processing(message_key)

        # If you want immediate processing if conditions are already met (e.g. multiple messages quickly):
        # await process_if_ready(message_key)

        return Response(status_code=200)

    return Response(status_code=200)

import json
import os
import re
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from collections import defaultdict
from datetime import datetime
from llm.ping_bot import ping_llm
from thena.create_ticket import thena
from pydantic import BaseModel
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

# Load environment variables
load_dotenv()

# Initialize the FastAPI app
app = FastAPI()

# Set up message buffer, timers and channel cooldown
message_buffer = defaultdict(list)
timers = {}
channel_last_processed = {}
BUFFER_TIMEOUT = 25
CHANNEL_COOLDOWN = 3600 # 1 hour

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

#### FUNCTIONS ####
        
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
    channel = message_buffer[message_key][0]['event'].get('channel')

    # Check if channel is in cooldown
    if channel in channel_last_processed:
        time_since_last_process = current_time - channel_last_processed[channel]
        if time_since_last_process < CHANNEL_COOLDOWN:
            print(f"Channel {channel} is in cooldown. Clearing buffer without processing.")
            del message_buffer[message_key]
            return

    # Check time
    if (current_time - earliest_msg_time) >= BUFFER_TIMEOUT or len(message_buffer[message_key]) >= 5:
        # Time to process
        combined_text = " ".join(m['text'] for m in message_buffer[message_key])
        print(f"Processing buffered messages for {message_key}: {combined_text}")

        event = message_buffer[message_key][0]['event']
        username = event.get('username')

        # Send the text to LLM for analysis
        bot_response = await ping_llm(combined_text)

        if isinstance(bot_response, str):
            # If an error string is returned, just clear the buffer
            print("Bot response was an error string, clearing buffer.")
            del message_buffer[message_key]
            return
        analysis = (bot_response.customer_query).lower().strip()
        summary = (bot_response.query_summary).capitalize().strip()
        urgency = (bot_response.urgency).capitalize().strip()

        if analysis == "yes":

            # Update the channel's last processed time
            channel_last_processed[channel] = current_time
            channel = event.get('channel')
            thread_ts = event.get('thread_ts') if event.get('thread_ts') else event.get('ts')

            # Check if it's Saturday
            current_day = datetime.now().weekday()
            if current_day in [5, 6]: # weekend
                print("It's the weekend, pinging Dima and Dan!")
                ping_cs = f'<@U02PP7JRTFS><@U082GSCDFG9> please take a look 😊.' # pings Dima and Dan
                # ping_cs = f'<@U04LKS6KL7R>, ticket please 🎫' # pings Thena
            else:
                print("It's not the weekend, pinging Dan!")
                ping_cs = f'<@U082GSCDFG9> please take a look 😊.' # pings Dan

            slack_client.chat_postMessage(
                channel=channel,
                text=ping_cs, 
                thread_ts=thread_ts
            )

            try:
                await thena(
                    username=username, 
                    query=combined_text, 
                    summary=summary, 
                    urgency=urgency,
                    channel=channel,
                    ts=thread_ts,
                    slack_client=slack_client,
                    current_day=current_day
                )
                print("*Beep Boop* Thena ticket created!")
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
        if re.search(r'@DeanKuchel|fordefi|@hvbris|@dimakogan1|@michaelpoluy|@Ancientfish|@joshschwartz|dima|poluy|dean|telebot|ron|@jacobgzx|@aprilXluo', user_name, re.IGNORECASE):
            print('Ignoring, just someone from Fordefi replying.')
            return Response(status_code=200)

        if not event.get('text'): 
            print('Ignoring, empty message text or image.')
            return Response(status_code=200)

        user_text = event.get('text')    
        user_id = event.get('username')
        channel = event.get('channel') # channel ID

        response = slack_client.conversations_info(channel=channel)
        channel_info = response["channel"]
        channel_name = channel_info["name"]
        print("Channel name is:", channel_name)

        # response = slack_client.conversations_members(channel="C06FPKLS76V")
        # member_ids = response["members"]
        # print("Channel members are:", member_ids) 

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

        return Response(status_code=200)

    return Response(status_code=200)

# Local start command: uvicorn app:app --reload --port 8800

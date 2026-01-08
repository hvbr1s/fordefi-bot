import json
import os
import re
import asyncio
import logging
from dotenv import load_dotenv
from datetime import datetime
from llm.ping_bot import ping_llm
from pydantic import BaseModel
from slack_sdk import WebClient
from collections import defaultdict
from thena.create_ticket import thena
from fastapi import FastAPI, Request, Response
from slack_sdk.signature import SignatureVerifier
from slack_post.enrich_post import enrich_bot_post

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = FastAPI()

message_buffer = defaultdict(list)
timers = {}
channel_last_processed = {}
BUFFER_TIMEOUT = 25
CHANNEL_COOLDOWN = 3600

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
THENA_AUTH_TOKEN = os.getenv("THENA_AUTH_TOKEN")

slack_client = WebClient(token=SLACK_BOT_TOKEN)
signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET)
bot_id = slack_client.auth_test()['user_id']
processed_event_ids = set()

class SlackEvent(BaseModel):
    type: str
    user: str
    text: str
    channel: str
        
async def should_process_buffer(message_key) -> bool:
    """Check if buffer is ready for processing based on timeout or size."""
    if not message_buffer[message_key]:
        return False

    earliest_msg_time = message_buffer[message_key][0]['timestamp']
    current_time = datetime.now().timestamp()

    should_process = (current_time - earliest_msg_time) >= BUFFER_TIMEOUT or \
                     len(message_buffer[message_key]) >= 5
    return should_process

async def process_buffered_messages(message_key: str):
    """Process buffered messages: analyze with LLM and post response if customer query."""
    if message_key not in message_buffer or not message_buffer[message_key]:
        return

    earliest_msg_time = message_buffer[message_key][0]['timestamp']
    current_time = datetime.now().timestamp()
    channel = message_buffer[message_key][0]['event'].get('channel')

    if channel in channel_last_processed:
        time_since_last_process = current_time - channel_last_processed[channel]
        if time_since_last_process < CHANNEL_COOLDOWN:
            remaining_cooldown = CHANNEL_COOLDOWN - time_since_last_process
            logger.info(f"Channel {channel} in cooldown: {remaining_cooldown:.0f}s remaining | Buffer cleared")
            del message_buffer[message_key]
            return

    if (current_time - earliest_msg_time) >= BUFFER_TIMEOUT or len(message_buffer[message_key]) >= 5:
        combined_text = " ".join(m['text'] for m in message_buffer[message_key])
        combined_text = redact_emails(combined_text)
        msg_count = len(message_buffer[message_key])
        logger.info(f"Processing {msg_count} messages for {message_key}")

        event = message_buffer[message_key][0]['event']
        username = event.get('username')

        bot_response = await ping_llm(combined_text)

        if isinstance(bot_response, str):
            logger.error(f"LLM error for {message_key}: {bot_response}")
            del message_buffer[message_key]
            return

        analysis = (bot_response.customer_query).lower().strip()
        summary = (bot_response.query_summary).capitalize().strip()
        urgency = (bot_response.urgency).capitalize().strip()

        if analysis == "yes":
            log_request(urgency, summary)
            channel_last_processed[channel] = current_time
            thread_ts = event.get('thread_ts') if event.get('thread_ts') else event.get('ts')
            current_day = datetime.now().weekday()

            slack_post = await enrich_bot_post(username, combined_text, channel, thread_ts, slack_client, current_day)
            logger.info(f"Customer query detected | Urgency: {urgency} | Channel: {channel}")

            try:
                post = slack_client.chat_postMessage(
                    channel=channel,
                    text=slack_post,
                    thread_ts=thread_ts
                )
                logger.info(f"Slack message posted | Channel: {channel} | Thread: {thread_ts}")

                try:
                    slack_client.reactions_add(
                        channel=channel,
                        timestamp=post['ts'],
                        name='ticket'
                    )
                except Exception as e:
                    logger.error(f"Failed to add reaction | Channel: {channel} | Error: {str(e)}")

            except Exception as e:
                logger.error(f"Failed to post message | Channel: {channel} | Error: {str(e)}")
        else:
            logger.info(f"Not a customer query | Channel: {channel} | Summary: {summary}")

        del message_buffer[message_key]

async def schedule_processing(message_key: str):
    """Schedule delayed processing of buffered messages."""
    if message_key in timers:
        return

    async def delayed_check():
        await asyncio.sleep(BUFFER_TIMEOUT)
        await process_buffered_messages(message_key)
        timers.pop(message_key, None)

    timers[message_key] = asyncio.create_task(delayed_check())

def redact_emails(text: str) -> str:
    """Replace email addresses with redacted@email.com."""
    email_pattern = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
    return re.sub(email_pattern, "redacted@email.com", text)

def log_request(urgency: str, summary: str, log_file: str = "/disk/data/request_logs.json"):
    """Persist customer query details to JSON log file."""
    timestamp = datetime.now().isoformat()
    log_entry = {
        "timestamp": timestamp,
        "urgency": urgency,
        "summary": summary
    }

    logs = []
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r') as f:
                logs = json.load(f)
        except json.JSONDecodeError:
            logger.error(f"Corrupted log file at {log_file}, starting fresh")
            logs = []

    logs.append(log_entry)

    try:
        with open(log_file, 'w') as f:
            json.dump(logs, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to write to log file | Error: {str(e)}")
@app.get("/_health")
async def health_check():
    return {"status": "OK"}

@app.post("/")
async def slack_events(request: Request):
    body_bytes = await request.body()
    body = json.loads(body_bytes)

    if not signature_verifier.is_valid_request(body_bytes, request.headers):
        logger.warning("Invalid Slack signature")
        return Response(status_code=403)

    if body.get("type") == "url_verification":
        logger.info("Slack URL verification challenge received")
        return {"challenge": body.get("challenge")}

    event = body.get('event', {})
    event_id = event.get('event_ts')

    if event_id in processed_event_ids:
        return Response(status_code=200)
    processed_event_ids.add(event_id)

    if event and event.get('type'):
        if event.get('user') == bot_id:
            return Response(status_code=200)

        ignored_subtypes = ['channel_join', 'message_changed', 'message_deleted']
        if event.get('subtype') in ignored_subtypes:
            return Response(status_code=200)

        user_name = event.get('username', '')
        if re.search(r'@DeanKuchel|fordefi|@hvbris|@dimakogan1|@michaelpoluy|@Ancientfish|@joshschwartz|poluy|dean|telebot|ron|@jacobgzx|@aprilXluo|@mlfigueroa89|@BenFordefi|@fmonte2|dor', user_name, re.IGNORECASE):
            return Response(status_code=200)

        if not event.get('text'):
            return Response(status_code=200)

        user_text = event.get('text')
        user_id = event.get('username')
        channel = event.get('channel')

        response = slack_client.conversations_info(channel=channel)
        channel_info = response["channel"]
        channel_name = channel_info["name"]

        message_key = f"{channel}:{user_id}"
        arrival_time = datetime.now().timestamp()
        message_buffer[message_key].append({
            'text': user_text,
            'timestamp': arrival_time,
            'event': event
        })

        buffer_size = len(message_buffer[message_key])
        logger.info(f"Message buffered | Channel: {channel_name} | User: {user_id} | Buffer size: {buffer_size}")

        await schedule_processing(message_key)

        return Response(status_code=200)

    return Response(status_code=200)

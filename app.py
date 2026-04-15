import os
import re
import io
import json
import httpx
import base64
import asyncio
import logging
from PIL import Image
from datetime import datetime
from dotenv import load_dotenv
from pydantic import BaseModel
from slack_sdk import WebClient
from llm.ping_bot import ping_llm
from collections import defaultdict
#from thena.create_ticket import thena
from typing import Any, Optional, List
from datadog.identify_id import identify_uuid
from slack_sdk.signature import SignatureVerifier
from slack_post.enrich_post import enrich_bot_post
from fastapi import FastAPI, Request, Response

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
ADMIN_AUTH_KEY = os.getenv("ADMIN_AUTH_KEY")

slack_client = WebClient(token=SLACK_BOT_TOKEN)
signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET)
bot_id = slack_client.auth_test()['user_id']
processed_event_ids = set()

ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
MAX_IMAGE_DIMENSION = 8000
MAX_INPUT_DIMENSION = 16000  # hard cap on input before decompression (DoS guard)


class _ImageTooLargeError(Exception):
    """Raised when an input image's declared dimensions exceed the safety cap."""


def _normalize_image_bytes(raw: bytes) -> bytes:
    """Re-encode an image as PNG via Pillow to strip Telegram compression
    metadata/encoding quirks that Anthropic rejects with 'Could not process image'.
    Also downsizes anything over Anthropic's 8000px limit, and rejects inputs
    larger than MAX_INPUT_DIMENSION before decompression to prevent bomb DoS."""
    with Image.open(io.BytesIO(raw)) as img:
        # Header-only dimension check — Image.open() does NOT decompress pixels yet.
        w, h = img.size
        if w > MAX_INPUT_DIMENSION or h > MAX_INPUT_DIMENSION:
            raise _ImageTooLargeError(
                f"image dimensions {w}x{h} exceed safety cap {MAX_INPUT_DIMENSION}px"
            )
        img.load()
        if getattr(img, "is_animated", False):
            img.seek(0)
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGBA" if "A" in img.mode else "RGB")
        if max(img.size) > MAX_IMAGE_DIMENSION:
            img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


async def download_slack_image(url: str) -> tuple[str, str]:
    """Download image from Slack url_private, normalize via Pillow, return (base64_png, 'image/png')."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            follow_redirects=True
        )
        response.raise_for_status()
        try:
            normalized = await asyncio.to_thread(_normalize_image_bytes, response.content)
        except _ImageTooLargeError as e:
            # Don't fall back to raw — the raw bytes ARE the bomb. Let caller skip this image.
            logger.error(f"Image rejected as too large | url={url} | {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Image normalization failed, falling back to raw bytes | url={url} | error={str(e)}")
            raw_type = response.headers.get('content-type', 'image/png')
            media_type = raw_type.split(';')[0].strip().lower()
            if media_type not in ALLOWED_IMAGE_TYPES:
                media_type = 'image/png'
            data = base64.b64encode(response.content).decode('utf-8')
            return data, media_type
        data = base64.b64encode(normalized).decode('utf-8')
        return data, 'image/png'


class SlackEvent(BaseModel):
    type: str
    user: str
    text: str
    channel: str
        
async def should_process_buffer(message_key) -> bool:
    if not message_buffer[message_key]:
        return False

    earliest_msg_time = message_buffer[message_key][0]['timestamp']
    current_time = datetime.now().timestamp()

    should_process = (current_time - earliest_msg_time) >= BUFFER_TIMEOUT or \
                     len(message_buffer[message_key]) >= 5
    return should_process

async def process_buffered_messages(message_key: str):
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
        combined_text = " ".join(m['text'] for m in message_buffer[message_key] if m['text'])
        combined_text = redact_emails(combined_text) if combined_text else ''
        msg_count = len(message_buffer[message_key])
        logger.info(f"Processing {msg_count} messages for {message_key}")

        event = message_buffer[message_key][0]['event']
        username = event.get('username')

        # Download images from buffered messages
        all_image_urls = []
        for m in message_buffer[message_key]:
            all_image_urls.extend(m.get('image_urls', []))

        downloaded_images = []
        for img_url in all_image_urls:
            try:
                b64_data, media_type = await download_slack_image(img_url)
                downloaded_images.append((b64_data, media_type))
            except Exception as e:
                logger.error(f"Failed to download image: {img_url} | Error: {str(e)}")

        bot_response = await ping_llm(combined_text, image_data=downloaded_images if downloaded_images else None)

        if isinstance(bot_response, str):
            logger.error(f"LLM error for {message_key}: {bot_response}")
            del message_buffer[message_key]
            return

        analysis = (bot_response.customer_query).lower().strip()
        summary = (bot_response.query_summary).capitalize().strip()
        urgency = (bot_response.urgency).capitalize().strip()
        uuids = [u.strip() for u in bot_response.uuids if u.strip()]

        # Classify UUIDs via Datadog
        transaction_ids = []
        request_ids = []
        organization_id = None
        organization_name = None
        for uuid in uuids:
            try:
                result = await asyncio.to_thread(identify_uuid, uuid)
                id_type = result.get("id_type", "unknown")
                display_uuid = result.get("resolved_uuid") or uuid
                if id_type in ("transaction_id", "both"):
                    transaction_ids.append(display_uuid)
                if id_type in ("request_id", "both"):
                    request_ids.append(display_uuid)
                if organization_id is None and result.get("organization_id"):
                    organization_id = result["organization_id"]
                    organization_name = result.get("organization_name")
                logger.info(f"UUID classified | uuid={uuid} | resolved={display_uuid} | type={id_type} | org_id={result.get('organization_id')} | org_name={result.get('organization_name')}")
            except Exception as e:
                logger.error(f"Datadog lookup failed for {uuid}: {str(e)}")

        if analysis == "yes":
            channel_name = message_buffer[message_key][0].get('channel_name', '')
            display_channel = channel_name.removeprefix('fordefi-') if channel_name else ''
            log_request(urgency, summary, display_channel, transaction_ids, request_ids, organization_id)
            channel_last_processed[channel] = current_time
            thread_ts = event.get('thread_ts') if event.get('thread_ts') else event.get('ts')

            slack_post = await enrich_bot_post(username, combined_text, channel, thread_ts, slack_client, transaction_ids, request_ids, organization_id, organization_name)
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
    if message_key in timers:
        return

    async def delayed_check():
        await asyncio.sleep(BUFFER_TIMEOUT)
        await process_buffered_messages(message_key)
        timers.pop(message_key, None)

    timers[message_key] = asyncio.create_task(delayed_check())

def redact_emails(text: str) -> str:
    email_pattern = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
    return re.sub(email_pattern, "redacted@email.com", text)

def log_request(urgency: str, summary: str, channel_name: str, transaction_ids: Optional[List[str]] = None, request_ids: Optional[List[str]] = None, organization_id: Optional[str] = None, log_file: str = "/disk/data/request_logs.json"):
    timestamp = datetime.now().isoformat(timespec="seconds")
    log_entry: dict[str, Any] = {
        "timestamp": timestamp,
        "urgency": urgency,
        "summary": summary,
        "client": channel_name,
        "platform": "telegram"
    }
    if transaction_ids:
        log_entry["transaction_ids"] = transaction_ids
    if request_ids:
        log_entry["request_ids"] = request_ids
    if organization_id:
        log_entry["organization_id"] = organization_id

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
        if re.search(r'@DeanKuchel|fordefi|@hvbris|@dimakogan1|@michaelpoluy|@Ancientfish|@joshschwartz|telebot|@aprilXluo|@mlfigueroa89|@BenFordefi|@fmonte2|@ThetcdDC|@Or0104|@itsamemario1988|@ShanySheves', user_name, re.IGNORECASE):
            return Response(status_code=200)

        has_text = bool(event.get('text'))
        has_images = any(
            f.get('mimetype', '').startswith('image/')
            for f in event.get('files', [])
        )
        if not has_text and not has_images:
            return Response(status_code=200)

        user_text = event.get('text', '')
        user_id = event.get('username')
        channel = event.get('channel')

        response = slack_client.conversations_info(channel=channel)
        channel_info = response["channel"]
        channel_name = channel_info["name"]

        image_urls = [
            f['url_private'] for f in event.get('files', [])
            if f.get('mimetype', '').startswith('image/')
        ]

        message_key = f"{channel}:{user_id}"
        arrival_time = datetime.now().timestamp()
        message_buffer[message_key].append({
            'text': user_text,
            'image_urls': image_urls,
            'timestamp': arrival_time,
            'event': event,
            'channel_name': channel_name
        })

        buffer_size = len(message_buffer[message_key])
        logger.info(f"Message buffered | Channel: {channel_name} | User: {user_id} | Buffer size: {buffer_size}")

        await schedule_processing(message_key)

        return Response(status_code=200)

    return Response(status_code=200)

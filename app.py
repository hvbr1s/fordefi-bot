import os
import re
import io
import json
import httpx
import base64
import asyncio
import logging
from PIL import Image
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from slack_sdk import WebClient
from llm.ping_bot import ping_llm
from collections import defaultdict
from typing import Any, Optional, List
from datadog.identify_id import identify_uuid
from slack_sdk.signature import SignatureVerifier
from slack_post.enrich_post import enrich_bot_post
from slack_post.channel_cache import get_channel_name
from fastapi import FastAPI, Request, Response
from classes import ImageTooLargeError, BoundedOrderedSet

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

EVENT_ID_FLUSH_TZ = ZoneInfo("Europe/Berlin")
EVENT_ID_FLUSH_INTERVAL_DAYS = 14
PROCESSED_EVENT_IDS_MAX = 20_000

_internal_users_regex = os.getenv("INTERNAL_USERS_REGEX")
if not _internal_users_regex:
    raise RuntimeError(
        "INTERNAL_USERS_REGEX is required but not set. "
        "Add it to your .env (pipe-separated handles, e.g. '@alice|@bob|teambot')."
    )
internal_users_pattern = re.compile(_internal_users_regex, re.IGNORECASE)


async def _flush_processed_event_ids_loop():
    """Clear processed_event_ids every other Saturday at 04:00 Europe/Berlin."""
    last_flush: Optional[datetime] = None
    while True:
        now = datetime.now(EVENT_ID_FLUSH_TZ)
        days_until_sat = (5 - now.weekday()) % 7
        next_run = now.replace(hour=4, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sat)
        if next_run <= now:
            next_run += timedelta(days=7)
        await asyncio.sleep((next_run - now).total_seconds())
        if last_flush is None or (datetime.now(EVENT_ID_FLUSH_TZ) - last_flush) >= timedelta(days=EVENT_ID_FLUSH_INTERVAL_DAYS - 1):
            size = len(processed_event_ids)
            processed_event_ids.clear()
            last_flush = datetime.now(EVENT_ID_FLUSH_TZ)
            logger.info(f"Flushed processed_event_ids | cleared={size}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global bot_id
    # Fill in (or refresh) bot_id at startup. Import-time attempt already ran
    # below; this covers the case where Slack was unreachable at import.
    if bot_id is None:
        try:
            bot_id = slack_client.auth_test()['user_id']
            logger.info(f"Slack auth_test OK at startup | bot_id={bot_id}")
        except Exception:
            logger.exception("Slack auth_test failed at startup")
    flush_task = asyncio.create_task(_flush_processed_event_ids_loop())
    try:
        yield
    finally:
        flush_task.cancel()


app = FastAPI(lifespan=lifespan)

message_buffer = defaultdict(list)
timers = {}
# Keyed by message_key (channel:thread:ts / channel:top:user) so a reply in
# one thread does not silence unrelated threads or users in the same channel.
key_last_processed: dict[str, float] = {}
BUFFER_TIMEOUT = 25
PROCESSING_COOLDOWN = 3600

# Telegram→Slack bridge (Telebot) posts text messages with the initiator
# handle in `event.username`, but file_share events from the same bridge
# drop that override and are authored as the bridge app itself. Cache the
# last real username seen per (channel, bot_id) for a short window so a
# handle-less file_share from the same sender can recover it.
# Value: (username, timestamp).
_recent_username_by_sender: dict[tuple[str, str], tuple[str, float]] = {}
RECENT_USERNAME_TTL = 60

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
THENA_AUTH_TOKEN = os.getenv("THENA_AUTH_TOKEN")
ADMIN_AUTH_KEY = os.getenv("ADMIN_AUTH_KEY")

slack_client = WebClient(token=SLACK_BOT_TOKEN)
signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET)
# Resolve bot_id eagerly but tolerate a Slack outage at import so the app
# can still boot; lifespan retries if this leg failed.
bot_id: Optional[str] = None
try:
    bot_id = slack_client.auth_test()['user_id']
except Exception:
    logger.exception("Slack auth_test failed at import; will retry in lifespan")
processed_event_ids = BoundedOrderedSet(max_size=PROCESSED_EVENT_IDS_MAX)

ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
MAX_API_IMAGE_DIMENSION = 1568   # Anthropic recommended max long-edge (avoids server-side resize)
MAX_API_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB hard API limit
MAX_INPUT_DIMENSION = 16000  # hard cap on input before decompression (DoS guard)

def _normalize_image_bytes(raw: bytes) -> tuple[bytes, str]:
    """Re-encode an image via Pillow to strip metadata/encoding quirks that
    Anthropic rejects with 'Could not process image'.
    Returns (image_bytes, media_type).
    - Downsizes to 1568px long-edge (API recommended max — avoids server-side resize).
    - Uses JPEG for opaque images (much smaller); PNG only when transparency is needed.
    - Progressively shrinks if the encoded output still exceeds the 5 MB API limit.
    - Rejects inputs larger than MAX_INPUT_DIMENSION before decompression (DoS guard)."""
    with Image.open(io.BytesIO(raw)) as img:
        w, h = img.size
        if w > MAX_INPUT_DIMENSION or h > MAX_INPUT_DIMENSION:
            raise ImageTooLargeError(
                f"image dimensions {w}x{h} exceed safety cap {MAX_INPUT_DIMENSION}px"
            )
        img.load()
        if getattr(img, "is_animated", False):
            img.seek(0)

        has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
        if has_alpha:
            img = img.convert("RGBA")
            fmt, media_type = "PNG", "image/png"
        else:
            img = img.convert("RGB")
            fmt, media_type = "JPEG", "image/jpeg"

        if max(img.size) > MAX_API_IMAGE_DIMENSION:
            img.thumbnail((MAX_API_IMAGE_DIMENSION, MAX_API_IMAGE_DIMENSION), Image.LANCZOS)

        logger.info(f"Normalizing image | raw_size={len(raw)} | mode={img.mode} | dimensions={img.size[0]}x{img.size[1]} | format={fmt} | has_alpha={has_alpha}")
        # Encode, then progressively shrink if output exceeds API limit
        for attempt in range(4):
            buf = io.BytesIO()
            if fmt == "JPEG":
                img.save(buf, format="JPEG", quality=85, optimize=True)
            else:
                img.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
            if len(data) <= MAX_API_IMAGE_BYTES:
                return data, media_type
            # Shrink by 50% and retry
            img = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
            logger.warning(f"Image too large ({len(data)} bytes), resizing to {img.width}x{img.height}")

        return data, media_type


async def download_slack_image(url: str) -> tuple[str, str]:
    """Download image from Slack url_private, normalize via Pillow, return (base64_data, media_type)."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            follow_redirects=True
        )
        response.raise_for_status()
        resp_ct = response.headers.get('content-type', 'unknown')
        resp_len = len(response.content)
        logger.info(f"Slack image downloaded | url={url} | status={response.status_code} | content_type={resp_ct} | content_length={resp_len}")
        if b'<!DOCTYPE' in response.content[:50] or b'<html' in response.content[:50] or 'text/html' in resp_ct:
            logger.error(f"Slack returned HTML instead of image | url={url} | content_type={resp_ct} | content_length={resp_len} | first_200_bytes={response.content[:200]!r}")
            raise ValueError(f"Slack returned HTML instead of image for {url}")
        if resp_len < 100:
            logger.error(f"Slack returned suspiciously small response | url={url} | content_type={resp_ct} | content_length={resp_len}")
        try:
            normalized, media_type = await asyncio.to_thread(_normalize_image_bytes, response.content)
        except ImageTooLargeError as e:
            logger.error(f"Image rejected as too large | url={url} | {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Image normalization failed, falling back to raw bytes | url={url} | error={str(e)}")
            if b'<!DOCTYPE' in response.content[:50] or b'<html' in response.content[:50]:
                raise ValueError(f"Downloaded content is HTML, not an image: {url}")
            raw_type = response.headers.get('content-type', 'image/png')
            media_type = raw_type.split(';')[0].strip().lower()
            if media_type not in ALLOWED_IMAGE_TYPES:
                media_type = 'image/png'
            data = base64.b64encode(response.content).decode('utf-8')
            logger.warning(f"Using raw (unnormalized) image | url={url} | media_type={media_type} | raw_size={len(response.content)} | b64_len={len(data)}")
            return data, media_type
        data = base64.b64encode(normalized).decode('utf-8')
        logger.info(f"Image normalized OK | url={url} | media_type={media_type} | normalized_size={len(normalized)} | b64_len={len(data)}")
        return data, media_type

BATCH_SIZE_TRIGGER = 10

async def process_buffered_messages(message_key: str):
    pending = message_buffer.get(message_key)
    if not pending:
        message_buffer.pop(message_key, None)
        return

    current_time = datetime.now().timestamp()
    channel = pending[0]['event'].get('channel')

    last = key_last_processed.get(message_key)
    if last is not None and (current_time - last) < PROCESSING_COOLDOWN:
        remaining = PROCESSING_COOLDOWN - (current_time - last)
        dropped = message_buffer.pop(message_key, [])
        dropped_users = sorted({m['event'].get('username', '?') for m in dropped})
        preview = " | ".join(
            redact_emails((m['text'] or '').strip().replace('\n', ' '))[:80]
            for m in dropped if m.get('text')
        )[:400]
        logger.warning(
            "Key %s in cooldown: %.0fs remaining | Dropping %d buffered message(s) | users=%s | preview=%r",
            message_key, remaining, len(dropped), dropped_users, preview,
        )
        return

    earliest_msg_time = pending[0]['timestamp']
    if not ((current_time - earliest_msg_time) >= BUFFER_TIMEOUT or len(pending) >= BATCH_SIZE_TRIGGER):
        # Neither the age threshold nor the batch threshold is met yet. Leave
        # messages buffered; schedule_processing will run us again.
        logger.info(
            "Buffer not ready for %s | age=%.1fs | size=%d — deferring",
            message_key, current_time - earliest_msg_time, len(pending),
        )
        return

    # Ready to process: take ownership of the current snapshot.
    pending = message_buffer.pop(message_key, [])
    if not pending:
        return

    combined_text = " ".join(m['text'] for m in pending if m['text'])
    combined_text = redact_emails(combined_text) if combined_text else ''
    msg_count = len(pending)
    logger.info(f"Processing {msg_count} messages for {message_key}")

    event = pending[0]['event']
    username = next(
        (m['event'].get('username') for m in pending if m['event'].get('username')),
        event.get('username') or event.get('user') or "Unknown"
    )

    # Download images from buffered messages
    all_image_urls = []
    for m in pending:
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
        return

    analysis = (bot_response.customer_query).lower().strip()
    summary = (bot_response.query_summary).capitalize().strip()
    urgency = (bot_response.urgency).capitalize().strip()
    uuids = [u.strip() for u in bot_response.uuids if u.strip()]

    # Classify UUIDs via Datadog
    transaction_ids = []
    request_ids = []
    payload_log_ids = {}
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
            if result.get("payload_log_id"):
                payload_log_ids[display_uuid] = result["payload_log_id"]
            if organization_id is None and result.get("organization_id"):
                organization_id = result["organization_id"]
                organization_name = result.get("organization_name")
            logger.info(f"UUID classified | uuid={uuid} | resolved={display_uuid} | type={id_type} | org_id={result.get('organization_id')} | org_name={result.get('organization_name')} | payload_log_id={result.get('payload_log_id')}")
        except Exception as e:
            logger.error(f"Datadog lookup failed for {uuid}: {str(e)}")

    # Accept "yes", "yes.", "yes, customer query", etc.
    if analysis.startswith("yes"):
        channel_name = pending[0].get('channel_name', '')
        display_channel = channel_name.removeprefix('fordefi-') if channel_name else ''
        log_request(urgency, summary, display_channel, transaction_ids, request_ids, organization_id)
        key_last_processed[message_key] = current_time
        thread_ts = event.get('thread_ts') if event.get('thread_ts') else event.get('ts')

        slack_post = await enrich_bot_post(
            username, combined_text, channel, thread_ts, slack_client,
            transaction_ids, request_ids, organization_id, organization_name,
            channel_name=channel_name,
            payload_log_ids=payload_log_ids,
        )
        logger.info(f"Customer query detected | Urgency: {urgency} | Channel: {channel}")

        post = None
        for attempt in range(2):
            try:
                response = slack_client.chat_postMessage(
                    channel=channel,
                    text=slack_post,
                    thread_ts=thread_ts
                )
                if response.get("ok"):
                    post = response
                    logger.info(f"Slack message posted | Channel: {channel} | Thread: {thread_ts}")
                    break
                logger.warning(f"Slack post returned not-ok | attempt={attempt + 1} | Channel: {channel} | Error: {response.get('error')}")
            except Exception as e:
                logger.warning(f"Slack post raised | attempt={attempt + 1} | Channel: {channel} | Error: {str(e)}")

        if post is None:
            logger.error(f"Failed to post message after retry | Channel: {channel} | Thread: {thread_ts}")
        else:
            try:
                slack_client.reactions_add(
                    channel=channel,
                    timestamp=post['ts'],
                    name='ticket'
                )
            except Exception as e:
                logger.error(f"Failed to add reaction | Channel: {channel} | Error: {str(e)}")
    else:
        logger.info(f"Not a customer query | Channel: {channel} | Summary: {summary}")

flush_events: dict[str, asyncio.Event] = {}

async def _run_and_cleanup(message_key: str, delay: float, flush_event: asyncio.Event):
    try:
        if delay > 0:
            try:
                await asyncio.wait_for(flush_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        await process_buffered_messages(message_key)
    except asyncio.CancelledError:
        raise
    except Exception:
        # The in-flight batch was already popped inside process_buffered_messages,
        # so it's lost to this crash. Any messages still under message_buffer[key]
        # arrived during processing and must be preserved for the finally block
        # below to reschedule.
        logger.exception(f"Unhandled error processing buffer | key={message_key}")
    finally:
        timers.pop(message_key, None)
        flush_events.pop(message_key, None)
        # Messages may have arrived during processing (after we popped the
        # buffer snapshot). If so, schedule another pass so they aren't
        # orphaned waiting for the next unrelated event.
        if message_buffer.get(message_key):
            await schedule_processing(message_key)

async def schedule_processing(message_key: str):
    if message_key in timers:
        return
    flush_event = asyncio.Event()
    flush_events[message_key] = flush_event
    timers[message_key] = asyncio.create_task(
        _run_and_cleanup(message_key, BUFFER_TIMEOUT, flush_event)
    )

def request_early_flush(message_key: str) -> None:
    """Wake the pending sleep so the processor runs now. No-op if the
    processor has already started or the timer has completed."""
    ev = flush_events.get(message_key)
    if ev is not None:
        ev.set()

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

    if not signature_verifier.is_valid_request(body_bytes, request.headers):
        logger.warning("Invalid Slack signature")
        return Response(status_code=403)

    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        logger.warning("Slack request with invalid JSON body")
        return Response(status_code=400)

    if body.get("type") == "url_verification":
        logger.info("Slack URL verification challenge received")
        return {"challenge": body.get("challenge")}

    event = body.get('event', {})
    event_id = body.get('event_id') or event.get('event_ts')

    if event_id and event_id in processed_event_ids:
        return Response(status_code=200)
    if event_id:
        processed_event_ids.add(event_id)

    try:
        await _handle_event(event)
    except Exception:
        if event_id:
            processed_event_ids.discard(event_id)
        logger.exception("Event handling failed; returning 500 so Slack retries")
        return Response(status_code=500)

    return Response(status_code=200)


async def _handle_event(event: dict):
    if event and event.get('type'):
        if event.get('user') == bot_id:
            return

        ignored_subtypes = ['channel_join', 'message_changed', 'message_deleted']
        if event.get('subtype') in ignored_subtypes:
            return

        channel = event.get('channel')
        raw_username = event.get('username', '')
        sender_id = event.get('bot_id') or event.get('user')
        now_ts = datetime.now().timestamp()

        # Cache real usernames as early as possible — even if this event would
        # otherwise be dropped — so the paired file_share that follows can
        # recover the handle and be filtered or logged correctly.
        if raw_username and channel and sender_id:
            _recent_username_by_sender[(channel, sender_id)] = (raw_username, now_ts)

        has_text = bool(event.get('text'))
        has_images = any(
            f.get('mimetype', '').startswith('image/')
            for f in event.get('files', [])
        )
        if not has_text and not has_images:
            return

        # Reuse a recent handle for bridge-posted file_shares that dropped
        # their username override. Only same (channel, bot_id) within TTL.
        effective_username = raw_username
        if not raw_username and channel and sender_id:
            cached = _recent_username_by_sender.get((channel, sender_id))
            if cached and (now_ts - cached[1]) <= RECENT_USERNAME_TTL:
                effective_username = cached[0]

        if internal_users_pattern.search(effective_username):
            return

        user_text = event.get('text', '')
        user_id = effective_username or None

        channel_name = get_channel_name(slack_client, channel)

        image_urls = [
            f['url_private'] for f in event.get('files', [])
            if f.get('mimetype', '').startswith('image/')
        ]

        # Threaded events key on thread_ts so separate threads never merge.
        # Top-level events key on (channel, user) so concurrent conversations
        # from different users stay isolated.
        thread_ts_key = event.get('thread_ts')
        if thread_ts_key:
            message_key = f"{channel}:thread:{thread_ts_key}"
        else:
            message_key = f"{channel}:top:{user_id}"
        arrival_time = datetime.now().timestamp()
        message_buffer[message_key].append({
            'text': user_text,
            'image_urls': image_urls,
            'timestamp': arrival_time,
            'event': event,
            'channel_name': channel_name
        })

        buffer_size = len(message_buffer[message_key])
        logger.info(f"Message buffered | Channel: {channel_name} | User: {user_id} | Key: {message_key} | Buffer size: {buffer_size}")

        await schedule_processing(message_key)
        if buffer_size >= BATCH_SIZE_TRIGGER:
            logger.info(f"Batch size trigger hit ({buffer_size}) | flushing {message_key} early")
            request_early_flush(message_key)

        return

    return

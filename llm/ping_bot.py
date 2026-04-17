import logging
import instructor
from classes import Analysis
from anthropic import AsyncAnthropic
from llm.system import prepare_prompt

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-7"
FALLBACK_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024

# Shared client with SDK-level retries for transient 429/5xx before we
# escalate to the fallback model.
instructor_client_anthropic = instructor.from_anthropic(
    AsyncAnthropic(max_retries=2),
    mode=instructor.Mode.ANTHROPIC_JSON,
)


def _build_content(query, image_data=None):
    """Build multimodal content blocks for the Anthropic API."""
    content = []
    if query and query.strip():
        content.append({"type": "text", "text": query.strip()})
    for b64, media_type in (image_data or []):
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64,
            },
        })
    if not content:
        content.append({"type": "text", "text": "(empty message)"})
    for i, blk in enumerate(b for b in content if b.get("type") == "image"):
        src = blk["source"]
        b64 = src["data"]
        logger.info(
            f"Image block {i} | media_type={src['media_type']} | "
            f"b64_length={len(b64)} | b64_prefix={b64[:20]}..."
        )
    return content


def _system_blocks(prompt: str):
    # cache_control keeps the system prompt in Anthropic's prompt cache
    # across calls, cutting per-request cost dramatically.
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


async def _call_model(model: str, system_blocks, content):
    return await instructor_client_anthropic.chat.completions.create(
        model=model,
        response_model=Analysis,
        max_tokens=MAX_TOKENS,
        system=system_blocks,
        messages=[{"role": "user", "content": content}],
    )


def _log_api_error(label: str, model: str, e: Exception) -> None:
    logger.error(
        f"{label} | model={model} | error_type={type(e).__name__} | error={str(e)}"
    )
    # Anthropic / httpx errors expose `response` and `body` at runtime; the
    # base Exception class doesn't, so use getattr to keep the type checker
    # happy without changing the logged output.
    resp = getattr(e, "response", None)
    if resp is not None:
        body_text = getattr(resp, "text", "N/A")
        logger.error(
            f"{label} API response | "
            f"status={getattr(resp, 'status_code', 'N/A')} | "
            f"body={body_text[:500] if isinstance(body_text, str) else body_text}"
        )
    body = getattr(e, "body", None)
    if body is not None:
        logger.error(f"{label} API error body | {body}")


async def ping_llm(query, image_data=None):
    logger.info(f"Starting LLM analysis | model={MODEL} | has_images={bool(image_data)}")
    prompt = prepare_prompt()
    system_blocks = _system_blocks(prompt)
    content = _build_content(query, image_data)

    try:
        response = await _call_model(MODEL, system_blocks, content)
        logger.info(
            f"LLM analysis complete | model={MODEL} | "
            f"customer_query={response.customer_query} | urgency={response.urgency}"
        )
        return response
    except Exception as primary_err:
        _log_api_error("LLM analysis failed", MODEL, primary_err)
        try:
            logger.info(f"Attempting fallback | model={FALLBACK_MODEL}")
            response = await _call_model(FALLBACK_MODEL, system_blocks, content)
            logger.info(
                f"Fallback analysis complete | model={FALLBACK_MODEL} | "
                f"customer_query={response.customer_query} | urgency={response.urgency}"
            )
            return response
        except Exception as fallback_err:
            _log_api_error("Fallback analysis failed", FALLBACK_MODEL, fallback_err)
            logger.warning("Returning default error response")
            return Analysis(
                customer_query="NO",
                query_summary="ERROR",
                urgency="MEDIUM",
                uuids=[],
            )

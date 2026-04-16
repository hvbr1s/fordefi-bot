import os
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

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
model = "claude-opus-4-7" # smart, slow-ish
fallback_model = "claude-sonnet-4-6" # fast, capable?
instructor_client_anthropic = instructor.from_anthropic(AsyncAnthropic(), mode=instructor.Mode.ANTHROPIC_JSON)

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
                "data": b64
            }
        })
    if not content:
        content.append({"type": "text", "text": "(empty message)"})
    return content


async def ping_llm(query, image_data=None):
    logger.info(f"Starting LLM analysis | model={model} | has_images={bool(image_data)}")
    prompt = await prepare_prompt()
    content = _build_content(query, image_data)
    try:
        response = await instructor_client_anthropic.chat.completions.create(
                model=model,
                response_model=Analysis,
                max_tokens=1024,
                system=prompt ,
                messages=[
                    {
                        "role": "user",
                        "content": content,
                    }
                ],
            )
        logger.info(f"LLM analysis complete | model={model} | customer_query={response.customer_query} | urgency={response.urgency}")
        return response
    except Exception as e:
        logger.error(f"LLM analysis failed | model={model} | error={str(e)}")
        try:
            logger.info(f"Attempting fallback | model={fallback_model}")
            response = await instructor_client_anthropic.chat.completions.create(
                    model=fallback_model,
                    response_model=Analysis,
                    max_tokens=1024,
                    system=prompt ,
                    messages=[
                        {
                            "role": "user",
                            "content": content,
                        }
                    ],
                )
            logger.info(f"Fallback analysis complete | model={fallback_model} | customer_query={response.customer_query} | urgency={response.urgency}")
            return response
        except Exception as e:
            logger.error(f"Fallback analysis failed | model={fallback_model} | error={str(e)}")
            logger.warning("Returning default error response")
            return Analysis(
                customer_query="NO",
                query_summary="ERROR",
                urgency="MEDIUM",
                uuids=[]
            )
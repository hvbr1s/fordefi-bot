import os
import logging
import instructor
from llm.system import prepare_prompt
from pydantic import BaseModel
from anthropic import AsyncAnthropic

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class Analysis(BaseModel):
    customer_query: str
    query_summary: str
    urgency: str

# Init Anthropic client
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
model = "claude-opus-4-5" # smart, slow
fallback_model = "claude-haiku-4-5" # fastest, dumb
instructor_client_anthropic = instructor.from_anthropic(AsyncAnthropic(), mode=instructor.Mode.ANTHROPIC_JSON)

async def ping_llm(query):
    logger.info(f"Starting LLM analysis | model={model}")
    prompt = await prepare_prompt()
    try:
        response = await instructor_client_anthropic.chat.completions.create(
                model=model,
                betas=["effort-2025-11-24"],
                response_model=Analysis,
                temperature=0.0,
                max_tokens=1024,
                system=prompt ,
                messages=[
                    {
                        "role": "user",
                        "content": query.strip(),
                    }
                ],
                output_config={
                    "effort": "medium"
                }
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
                    temperature=0.0,
                    max_tokens=1024,
                    system=prompt ,
                    messages=[
                        {
                            "role": "user",
                            "content": query.strip(),
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
                urgency="MEDIUM"
            )
import os
import instructor
from llm.system import prepare_prompt
from pydantic import BaseModel
from anthropic import AsyncAnthropic

class Analysis(BaseModel):
    customer_query: str
    query_summary: str
    urgency: str

# Init Anthropic client
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
model = "claude-3-5-sonnet-latest" #(smarter)
fallback_model = "claude-3-5-haiku-latest"
instructor_client_anthropic = instructor.from_anthropic(AsyncAnthropic(), mode=instructor.Mode.ANTHROPIC_JSON)

async def ping_llm(query):

    print(f"Pinging {model}!")

    prompt = await prepare_prompt()
    try:
        response = await instructor_client_anthropic.chat.completions.create(
                model=model,
                response_model=Analysis,
                temperature=0.0,
                max_tokens=512,
                system=prompt ,
                messages=[
                    {
                        "role": "user",
                        "content": query.strip(),
                    }
                ],
            )
        print(f"Analysis result: {response.customer_query.capitalize()}")
        return response
    except Exception as e:
        print(f"Error pinging {model}:  {e}")
        try:
            print(f"Pinging {fallback_model}")
            response = await instructor_client_anthropic.chat.completions.create(
                    model=fallback_model,
                    response_model=Analysis,
                    temperature=0.0,
                    max_tokens=512,
                    system=prompt ,
                    messages=[
                        {
                            "role": "user",
                            "content": query.strip(),
                        }
                    ],
                )
            print(f"Analysis result: {response.customer_query.capitalize()}")
            return response
        except Exception as e:
            print(f"Error pinging {fallback_model} {e}")
            return Analysis(
                customer_query="NO",
                query_summary="ERROR",
                urgency="MEDIUM"
            )
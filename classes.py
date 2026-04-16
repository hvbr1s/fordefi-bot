from typing import List
from pydantic import BaseModel

class ImageTooLargeError(Exception):
    """Raised when an input image's declared dimensions exceed the safety cap."""

class SlackEvent(BaseModel):
    type: str
    user: str
    text: str
    channel: str

class Analysis(BaseModel):
    customer_query: str
    query_summary: str
    urgency: str
    uuids: List[str]

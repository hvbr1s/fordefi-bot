from typing import List
from collections import OrderedDict
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


class BoundedOrderedSet:
    """Insertion-ordered set with a hard size cap.

    Used to dedupe Slack event IDs without unbounded memory growth — when
    the cap is hit, the oldest entry is evicted. Supports the same operations
    as ``set`` that app.py relies on: ``add``, ``discard``, ``clear``, ``in``,
    ``len``."""

    def __init__(self, max_size: int = 10_000) -> None:
        self._items: "OrderedDict[str, None]" = OrderedDict()
        self._max = max_size

    def add(self, item) -> None:
        if item in self._items:
            self._items.move_to_end(item)
            return
        self._items[item] = None
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def discard(self, item) -> None:
        self._items.pop(item, None)

    def clear(self) -> None:
        self._items.clear()

    def __contains__(self, item) -> bool:
        return item in self._items

    def __len__(self) -> int:
        return len(self._items)

from typing import Awaitable, Callable

from ..models import NewsEvent

Emit = Callable[[NewsEvent], Awaitable[None]]

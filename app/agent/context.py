"""Per-request context used by agent tools.

LangChain tools are invoked by the LLM without any knowledge of *which*
lead we're talking to. We inject that context using a ``ContextVar`` set
by the agent runner immediately before invoking the graph.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional

from sqlalchemy.orm import Session

from app.db.models import Lead


@dataclass
class AgentContext:
    session: Session
    lead: Lead
    # Callback to actually send outbound messages / files on WhatsApp.
    # (chatId is implicit -- the sender is bound to the current lead's phone.)
    send_video: Optional[callable] = None  # type: ignore[type-arg]


_current_ctx: ContextVar[Optional[AgentContext]] = ContextVar(
    "agent_context", default=None
)


@contextmanager
def use_context(ctx: AgentContext) -> Iterator[AgentContext]:
    token = _current_ctx.set(ctx)
    try:
        yield ctx
    finally:
        _current_ctx.reset(token)


def current_context() -> AgentContext:
    ctx = _current_ctx.get()
    if ctx is None:
        raise RuntimeError(
            "No AgentContext set. Tools must be invoked from within `use_context`."
        )
    return ctx

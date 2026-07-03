"""CRM integration placeholder.

Propeller Drones uses a CRM to manage leads. When their API is available,
implement ``mark_ready_for_call`` (and any other calls we need) inside this
module. The agent already calls ``mark_ready_for_call`` at hand-off, so no
changes to the agent layer will be required.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from app.db.models import Lead


def mark_ready_for_call(lead: Lead, note: Optional[str] = None) -> bool:
    """Push the ``ready_for_call`` status to the external CRM.

    Currently a stub -- logs and returns True. Replace the body with a
    real HTTP call (requests / httpx) once the CRM API is available.
    """
    logger.info(
        "[CRM STUB] Would mark lead {} (phone={}) as ready_for_call. Note: {}",
        lead.id, lead.phone, note or "-",
    )
    return True

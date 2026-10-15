"""One bounded way to build a GreenAPI client.

The SDK's own defaults are far longer than anything here can wait for.
``GreenApi.__init__`` uses ``host_timeout=180`` seconds *per attempt* and
mounts ``Retry(total=3, backoff_factor=1.0, allowed_methods=None)`` on both
adapters, so a single ``sendMessage`` against a GreenAPI that accepts the
connection and then never answers takes four attempts of 180s plus backoff --
twelve minutes -- before it raises. The first-contact opener makes two such
calls, the text and then the intro video, and that is where the daily 18- to
24-minute outages of 11-15 Oct 2026 came from: the webhook worker held its
database transaction and the lead's row lock for the whole time, and
``/health`` stopped answering until the second call returned.

``allowed_methods=None`` is the other half of it. It replays POSTs, so a send
GreenAPI did receive but answered slowly reaches the lead twice. Only a
failure to connect is retried here, because that one proves the request never
arrived; everything else is reported to the caller, which is the layer that
knows whether a repeat is safe. The polling loop retries its own receive after
five seconds, and the opener and the nudges log and move on.
"""

from __future__ import annotations

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from whatsapp_api_client_python.API import GreenAPI, GreenApi

from app.config import get_settings

_RETRY = Retry(total=2, connect=2, read=0, status=0, backoff_factor=0.5)


def bound_request_time(api: GreenApi) -> None:
    """Cap one GreenAPI call at ``GREEN_API_REQUEST_TIMEOUT_SECONDS``."""
    timeout = get_settings().green_api_request_timeout_seconds
    api.host_timeout = timeout
    api.media_timeout = timeout

    adapter = HTTPAdapter(max_retries=_RETRY)
    api.session.mount("http://", adapter)
    api.session.mount("https://", adapter)


def build_greenapi_client() -> GreenAPI:
    """Build a bounded client for code with no inbound notification to borrow."""
    settings = get_settings()
    api = GreenAPI(
        settings.green_api_instance_id,
        settings.green_api_token,
        host=settings.green_api_host,
        media=settings.green_api_media_host,
    )
    bound_request_time(api)
    return api

"""Automated LeadMe login + cookie refresh.

Why this module exists
----------------------
LeadMe has no proper admin API (as of 2026-07). To change status /
tags on existing leads we impersonate the logged-in browser via
saved session cookies -- see :mod:`app.crm.leadme_delete`. Those
cookies expire:

- ``csrf_cookie_name`` has a hard ``Max-Age=86400`` -- dies EVERY 24h
  regardless of activity.
- ``PHPSESSID`` typically lasts as long as we keep making requests
  but has an absolute cap (~2h idle by default in CodeIgniter).

Historically this meant a human had to log into LeadMe every day,
solve the reCAPTCHA, export cookies from DevTools, and paste them
into ``/admin/leadme-cookies``. This module removes that manual step:

1. ``GET /login`` -> capture initial PHPSESSID + csrf_cookie_name and
   extract the ``csrf_lmcms`` hidden-input value.
2. Ask 2Captcha to solve the reCAPTCHA v2 checkbox challenge for
   sitekey ``6Lc59bodAAAAAF1ew3fJoHUohz5WLd4NH05-EbkI`` on
   ``https://www.leadmecms.co.il/login``. Costs ~$0.003, usually
   solved in 15-45 seconds.
3. ``POST /login/verifyLogin`` with email/password/csrf_lmcms/
   g-recaptcha-response, ``checkbox-inline=on`` (remember me for the
   longest possible session).
4. Verify by hitting ``/app/leads`` -- 200 + LeadMe admin marker in
   the HTML means we're in. Anything else = login failed.
5. Dump the cookies to ``LEADME_COOKIES_PATH`` in the shape
   :mod:`app.crm.leadme_delete._load_cookies_file` already expects.

Fallbacks / kill switches
-------------------------
- ``LEADME_AUTO_REFRESH_ENABLED=false``: whole module becomes a no-op
  (module is imported for the health check anyway).
- Missing ``LEADME_LOGIN_EMAIL`` / ``LEADME_LOGIN_PASSWORD`` /
  ``LEADME_CAPTCHA_API_KEY``: refresh function short-circuits with a
  clear log line, leaves existing cookies alone.
- 2Captcha times out (>3 min) or returns error: refresh aborts, does
  NOT overwrite the existing cookies file. Old cookies still work
  until they expire; operator can retry via admin UI.

The manual ``/admin/leadme-cookies`` paste endpoint is intentionally
kept as belt-and-suspenders.

Not a real API
--------------
This is still a workaround for LeadMe not offering token-based admin
API access. If they ever add one, delete this module -- the queue,
health check, and admin UI don't depend on it.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from app.config import get_settings


# Sitekey observed on the LeadMe login page as of 2026-07-26. Baked in
# so we don't need to re-fetch it every refresh, but we ALSO extract it
# fresh from the page in _get_login_intel below -- if LeadMe rotates
# the sitekey we'll pick up the new one automatically and only fall
# back to this if the page structure changes and we can't parse it.
_KNOWN_SITEKEY = "6Lc59bodAAAAAF1ew3fJoHUohz5WLd4NH05-EbkI"
_LOGIN_PAGE_URL = "https://www.leadmecms.co.il/login"
_LOGIN_POST_URL = "https://www.leadmecms.co.il/login/verifyLogin"
_VERIFY_URL = "https://www.leadmecms.co.il/app/leads"

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 2Captcha polling: they say typical solve time is 15-45s. We poll
# every 5s and cap at 3min total.
_CAPTCHA_POLL_INTERVAL_SEC = 5
_CAPTCHA_MAX_WAIT_SEC = 180


# --- Refresh result -----------------------------------------------------


class RefreshResult(dict):
    """Result envelope for :func:`refresh_leadme_cookies`.

    Shape::

        {
            "ok": True | False,
            "reason": "<short human-readable>",
            "cookies_written": True | False,
            "cookie_count": <int>,
            "checked_at": "<ISO UTC>",
            "captcha_wait_seconds": <int or None>,
        }
    """


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fail(reason: str, **extra) -> RefreshResult:
    r = RefreshResult(
        ok=False,
        reason=reason,
        cookies_written=False,
        cookie_count=0,
        checked_at=_now_iso(),
        captcha_wait_seconds=None,
    )
    r.update(extra)
    return r


# --- 2Captcha client (inline, no dep) -----------------------------------


def _twocaptcha_solve_recaptcha_v2(
    api_key: str,
    sitekey: str,
    page_url: str,
    *,
    timeout_sec: int = _CAPTCHA_MAX_WAIT_SEC,
) -> Optional[str]:
    """Solve a reCAPTCHA v2 checkbox via 2Captcha. Return token or None.

    Docs: https://2captcha.com/2captcha-api#solving_recaptchav2_new
    Cost: ~$0.003 per solve (varies with load). Typical wall time:
    20-60s.
    """
    submit = "http://2captcha.com/in.php"
    result = "http://2captcha.com/res.php"
    started = time.time()

    try:
        with httpx.Client(timeout=20.0) as cli:
            r = cli.post(submit, data={
                "key": api_key,
                "method": "userrecaptcha",
                "googlekey": sitekey,
                "pageurl": page_url,
                "json": "1",
            })
            if r.status_code != 200:
                logger.error(
                    "[leadme-login] 2Captcha submit HTTP {}: {!r}",
                    r.status_code, r.text[:200],
                )
                return None
            body = r.json()
            if body.get("status") != 1:
                logger.error(
                    "[leadme-login] 2Captcha refused submit: {!r}", body,
                )
                return None
            request_id = str(body.get("request"))
            logger.info(
                "[leadme-login] 2Captcha request submitted id={} "
                "sitekey={} page={}", request_id, sitekey, page_url,
            )

            while time.time() - started < timeout_sec:
                time.sleep(_CAPTCHA_POLL_INTERVAL_SEC)
                pr = cli.get(result, params={
                    "key": api_key,
                    "action": "get",
                    "id": request_id,
                    "json": "1",
                })
                if pr.status_code != 200:
                    continue
                pb = pr.json()
                if pb.get("status") == 1:
                    tok = pb.get("request")
                    if isinstance(tok, str) and len(tok) > 20:
                        wait = int(time.time() - started)
                        logger.info(
                            "[leadme-login] 2Captcha solved in {}s "
                            "(token_len={})", wait, len(tok),
                        )
                        return tok
                # status == 0 usually means "CAPCHA_NOT_READY", keep polling
                if pb.get("request") and pb.get("request") != "CAPCHA_NOT_READY":
                    logger.error(
                        "[leadme-login] 2Captcha error while polling: {!r}",
                        pb,
                    )
                    return None
    except httpx.HTTPError as e:
        logger.error("[leadme-login] 2Captcha HTTP error: {}", e)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("[leadme-login] 2Captcha unexpected error")
        return None

    logger.error(
        "[leadme-login] 2Captcha timed out after {}s -- LeadMe login "
        "refresh aborted (existing cookies untouched)", timeout_sec,
    )
    return None


# --- Login intel scraping ----------------------------------------------


def _get_login_intel(client: httpx.Client) -> Optional[dict]:
    """GET /login. Extract csrf_lmcms hidden input value + sitekey."""
    try:
        r = client.get(_LOGIN_PAGE_URL)
    except httpx.HTTPError as e:
        logger.error("[leadme-login] GET /login failed: {}", e)
        return None
    if r.status_code != 200:
        logger.error(
            "[leadme-login] GET /login returned HTTP {}: {!r}",
            r.status_code, r.text[:200],
        )
        return None

    html = r.text
    csrf_match = re.search(
        r'<input[^>]*name=[\"\']csrf_lmcms[\"\'][^>]*value=[\"\']([^\"\']+)[\"\']',
        html,
    )
    csrf_val = csrf_match.group(1) if csrf_match else None
    sitekey_match = re.search(r'data-sitekey=[\"\']([^\"\']+)[\"\']', html)
    sitekey = sitekey_match.group(1) if sitekey_match else _KNOWN_SITEKEY

    if not csrf_val:
        logger.error(
            "[leadme-login] couldn't extract csrf_lmcms from /login "
            "(page structure may have changed). First 300 chars: {!r}",
            html[:300],
        )
        return None

    return {"csrf": csrf_val, "sitekey": sitekey}


# --- Verification -------------------------------------------------------


def _verify_logged_in(client: httpx.Client) -> tuple[bool, str]:
    """Hit /app/leads and check for the LeadMe admin marker."""
    try:
        r = client.get(_VERIFY_URL)
    except httpx.HTTPError as e:
        return False, f"HTTP error verifying: {e}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code} on /app/leads"
    body_head = r.text[:2000].lower()
    if "leadme cms |" in body_head:
        return True, "verified via LeadMe CMS marker"
    if "recaptcha" in body_head or 'name="password"' in body_head:
        return False, "got login/reCAPTCHA page back -- login likely failed"
    return False, "unexpected page content -- neither login nor admin"


# --- Cookie serialization -----------------------------------------------


def _serialize_cookies_for_disk(client: httpx.Client) -> list[dict]:
    """Serialize httpx.Cookies into the JSON shape our loader expects.

    :mod:`app.crm.leadme_delete._load_cookies_file` reads a list of
    ``{name, value, domain, path, expirationDate?}`` dicts (matches
    Chrome DevTools "Copy as JSON" format). Match that so the same
    file works both when auto-refreshed and when manually pasted.
    """
    out: list[dict] = []
    for cookie in client.cookies.jar:
        # jar is an http.cookiejar.CookieJar
        if "leadmecms.co.il" not in (cookie.domain or ""):
            continue
        entry = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
        }
        if cookie.expires:
            entry["expirationDate"] = cookie.expires
        out.append(entry)
    return out


# --- Public entrypoint --------------------------------------------------


def refresh_leadme_cookies() -> RefreshResult:
    """Perform a full LeadMe login and persist fresh cookies. Idempotent.

    Callable from a scheduler tick, admin UI button, or shell. Never
    raises. On any failure the existing cookies file is left untouched
    so the current session keeps working until it dies naturally.
    """
    settings = get_settings()

    if not settings.leadme_auto_refresh_enabled:
        return _fail("LEADME_AUTO_REFRESH_ENABLED=false")

    email = (settings.leadme_login_email or "").strip()
    password = (settings.leadme_login_password or "").strip()
    captcha_key = (settings.leadme_captcha_api_key or "").strip()
    if not email or not password:
        return _fail("LEADME_LOGIN_EMAIL / LEADME_LOGIN_PASSWORD not set")
    if not captcha_key:
        return _fail("LEADME_CAPTCHA_API_KEY not set")

    cookies_path = Path(settings.leadme_cookies_path or "data/leadme_cookies.json")

    logger.info(
        "[leadme-login] starting refresh for email={} (captcha_provider="
        "2captcha, cookies_path={})", email, cookies_path,
    )

    with httpx.Client(
        follow_redirects=False,
        timeout=20.0,
        headers={
            "User-Agent": _UA,
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        },
    ) as client:
        intel = _get_login_intel(client)
        if intel is None:
            return _fail("could not extract csrf/sitekey from /login")

        captcha_started = time.time()
        token = _twocaptcha_solve_recaptcha_v2(
            captcha_key, intel["sitekey"], _LOGIN_PAGE_URL,
        )
        captcha_wait = int(time.time() - captcha_started)
        if not token:
            return _fail(
                "2Captcha failed to solve reCAPTCHA (see previous logs)",
                captcha_wait_seconds=captcha_wait,
            )

        # LeadMe's /login/verifyLogin expects a form-encoded POST with
        # the reCAPTCHA response echoed as g-recaptcha-response. It
        # also carries csrf_lmcms both in the form AND as a cookie
        # (double-submit); httpx will carry the cookie automatically
        # since we're using the same client.
        try:
            resp = client.post(
                _LOGIN_POST_URL,
                data={
                    "csrf_lmcms": intel["csrf"],
                    "email": email,
                    "password": password,
                    "checkbox-inline": "on",  # remember me
                    "g-recaptcha-response": token,
                },
                headers={
                    "Referer": _LOGIN_PAGE_URL,
                    "Origin": "https://www.leadmecms.co.il",
                },
            )
        except httpx.HTTPError as e:
            return _fail(
                f"POST /login/verifyLogin HTTP error: {e}",
                captcha_wait_seconds=captcha_wait,
            )

        # LeadMe replies with a 302 redirect on success (or a 200 with
        # the login page repainted on failure). Either way we then
        # need to hit /app/leads to confirm we're authenticated.
        logger.info(
            "[leadme-login] verifyLogin returned HTTP {} location={!r}",
            resp.status_code, resp.headers.get("location"),
        )

        # Follow the redirect (if any) once so cookies settle.
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location") or "/"
            if loc.startswith("/"):
                loc = "https://www.leadmecms.co.il" + loc
            try:
                client.get(loc)
            except httpx.HTTPError:
                pass  # verification below is the real check

        ok, detail = _verify_logged_in(client)
        if not ok:
            return _fail(
                f"post-login verification failed: {detail}",
                captcha_wait_seconds=captcha_wait,
            )

        cookies = _serialize_cookies_for_disk(client)
        if not cookies:
            return _fail(
                "login succeeded but no LeadMe cookies to persist -- bug?",
                captcha_wait_seconds=captcha_wait,
            )
        has_phpsessid = any(c["name"] == "PHPSESSID" for c in cookies)
        has_csrf = any(c["name"].startswith("csrf") for c in cookies)
        if not (has_phpsessid and has_csrf):
            return _fail(
                f"cookies missing PHPSESSID or csrf_cookie_name: {cookies!r}",
                captcha_wait_seconds=captcha_wait,
            )

        # Atomic write: write to a sibling temp file then rename, so a
        # crash mid-write never leaves a truncated cookies file behind.
        try:
            cookies_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cookies_path.with_suffix(cookies_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(cookies_path)
        except OSError as e:
            return _fail(
                f"could not write cookies file at {cookies_path}: {e}",
                captcha_wait_seconds=captcha_wait,
            )

        # Bust the health-check cache so admin panel + scheduler pick
        # up the fresh session immediately instead of after 30s.
        try:
            from app.crm.leadme_client import _HEALTH_CACHE
            _HEALTH_CACHE["result"] = None
            _HEALTH_CACHE["checked_at"] = 0.0
        except Exception:  # noqa: BLE001
            pass

        logger.info(
            "[leadme-login] REFRESH SUCCESS -- wrote {} cookies to {} "
            "(captcha_wait={}s)",
            len(cookies), cookies_path, captcha_wait,
        )
        return RefreshResult(
            ok=True,
            reason=f"refreshed {len(cookies)} cookies",
            cookies_written=True,
            cookie_count=len(cookies),
            checked_at=_now_iso(),
            captcha_wait_seconds=captcha_wait,
        )

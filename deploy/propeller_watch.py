#!/usr/bin/env python3
"""Tell Omer on WhatsApp when the Propeller bot stops serving leads.

Twice in one week the bot failed for hours. Both times it detected the
failure correctly and wrote it to Docker logs nobody reads: OpenAI credits
ran out on 6 Oct, and on 11 Oct GreenAPI retired the hostname the poller
dialled and /health sat at 503 for three hours. The detection was never the
problem. The alerting was.

So this runs on the HOST, under a systemd timer, outside the bot container
and outside the bot process. It has to survive the thing it watches being
dead, unhealthy, crash-looping or mid-rebuild, which rules out living in the
FastAPI app or the APScheduler threads. It also reads its GreenAPI
credentials from its own file, so a bad value in the bot's .env cannot take
the alerter down alongside the bot.

Standard library only, on purpose. A monitor that can be broken by a
dependency upgrade is not a monitor.

The honest limitation: this alerts over the same GreenAPI instance the bot
uses, because that is the only one the customer has. A total GreenAPI outage
therefore cannot be announced over WhatsApp. See deploy/propeller-watch.env.example.

Usage:
    propeller-watch                 one check; alert or recover as needed
    propeller-watch --dry-run       check and print, send nothing
    propeller-watch --test-message  prove delivery end to end
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

BOT_CONTAINER = "propeller_bot"
POSTGRES_CONTAINER = "propeller_postgres"
CHROMA_CONTAINER = "propeller_chroma"

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# Two consecutive failing checks before alerting. The timer runs every
# minute, so a confirmed outage reaches the phone in about two minutes and a
# single flapped check stays silent.
CHECKS_BEFORE_ALERT = 2

HEALTH_TIMEOUT_SECONDS = 10
DOCKER_TIMEOUT_SECONDS = 10
SEND_TIMEOUT_SECONDS = 20


# --- premade messages ---------------------------------------------------
#
# Hebrew, to match the operational tooling the customer already reads: the
# admin panel is Hebrew ("LeadMe · תקין"), only the logs and identifiers are
# English. Plain text, because WhatsApp renders no markdown. Each one says
# what broke, since when, and whether leads are affected right now -- that
# last line is the whole point, it decides whether he stops what he is doing.
#
# Ordered by how much they hurt a lead. Exactly one reason is reported per
# outage, the first that matches, so the message names a cause instead of
# listing symptoms.
MESSAGES = {
    "bot_down": (
        "נפילה בפרופלור: הבוט לא רץ\n"
        "הקונטיינר {detail} מאז {since}.\n"
        "לידים שכותבים עכשיו לא מקבלים שום מענה."
    ),
    "bot_restarting": (
        "נפילה בפרופלור: הבוט בלופ של ריסטארטים\n"
        "הקונטיינר עולה ונופל שוב מאז {since}.\n"
        "לידים שכותבים עכשיו כנראה לא מקבלים מענה. לבדוק docker compose logs bot."
    ),
    "health_unreachable": (
        "נפילה בפרופלור: אין תשובה מהבוט\n"
        "בדיקת /health לא נגישה מאז {since} ({detail}).\n"
        "הבוט כנראה תקוע. לידים לא מקבלים מענה."
    ),
    "polling_stale": (
        "נפילה בפרופלור: הקשר ל-WhatsApp נפל\n"
        "קבלת ההודעות מ-GreenAPI תקועה מאז {since}.\n"
        "לידים שכותבים עכשיו לא מקבלים מענה. לבדוק את GREEN_API_HOST ואת מצב "
        "האינסטנס בקונסולה של GreenAPI."
    ),
    "postgres_down": (
        "נפילה בפרופלור: מסד הנתונים לא רץ\n"
        "הקונטיינר {detail} מאז {since}.\n"
        "הודעות של לידים לא נשמרות. זה המצב הדחוף ביותר."
    ),
    "database_unreachable": (
        "נפילה בפרופלור: הבוט לא מצליח להתחבר למסד הנתונים\n"
        "מאז {since}.\n"
        "הודעות של לידים לא נשמרות. זה המצב הדחוף ביותר."
    ),
    "openai_no_credit": (
        "נפילה בפרופלור: נגמר הקרדיט ב-OpenAI\n"
        "OpenAI מסרב לבקשות מסיבת חיוב מאז {since}.\n"
        "כל ליד מקבל עכשיו את הודעת ההתנצלות במקום תשובה אמיתית. צריך לטעון יתרה."
    ),
    "chroma_down": (
        "תקלה בפרופלור: Chroma לא רץ\n"
        "הקונטיינר {detail} מאז {since}.\n"
        "הבוט ממשיך לענות ללידים, אבל בלי בסיס הידע - התשובות יוצאות חלשות."
    ),
    "openai_error": (
        "תקלה בפרופלור: OpenAI מחזיר שגיאה\n"
        "הבדיקה התקופתית נכשלת מאז {since}. זאת לא בעיית חיוב ולא הגבלת קצב.\n"
        "ייתכן שלידים מקבלים את הודעת ההתנצלות במקום תשובה."
    ),
    "leadme_session": (
        "תקלה בפרופלור: החיבור ל-LeadMe לא תקין\n"
        "סטטוס {detail} מאז {since}.\n"
        "לידים ממשיכים לקבל מענה כרגיל, ועדכוני הרמה והתגיות ל-CRM ממשיכים "
        "לעבוד דרך ה-API. חסומות רק הפעולות שדורשות עוגיות."
    ),
}

RECOVERY_MESSAGE = (
    "פרופלור חזר לעבוד\n"
    "התקלה ({reason}) נפתרה. היא נמשכה {duration}.\n"
    "כל הבדיקות עוברות."
)


# --- configuration ------------------------------------------------------


def _config() -> dict:
    """Read the watcher's own settings. Separate from the bot's .env by
    design: a typo there must not disarm the alerter."""
    return {
        "health_url": os.environ.get("WATCH_HEALTH_URL", "http://127.0.0.1:8084/health"),
        "alert_phone": os.environ.get("WATCH_ALERT_PHONE", ""),
        "green_api_host": os.environ.get("WATCH_GREEN_API_HOST", ""),
        "green_api_instance_id": os.environ.get("WATCH_GREEN_API_INSTANCE_ID", ""),
        "green_api_token": os.environ.get("WATCH_GREEN_API_TOKEN", ""),
        "state_file": os.environ.get(
            "WATCH_STATE_FILE", "/var/lib/propeller-watch/state.json"
        ),
    }


# --- probes -------------------------------------------------------------


def _container_state(name: str) -> dict:
    """Docker's view of a container, or an empty dict if docker cannot say.

    An empty dict means "unknown", never "down": refusing to guess keeps a
    broken docker CLI from paging the customer about a bot that is fine.
    """
    try:
        completed = subprocess.run(
            [
                "docker", "inspect", "-f",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}"
                "{{else}}none{{end}}|{{.RestartCount}}",
                name,
            ],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001
        return {}
    if completed.returncode != 0:
        return {}
    parts = completed.stdout.strip().split("|")
    if len(parts) != 3:
        return {}
    try:
        restart_count = int(parts[2])
    except ValueError:
        restart_count = 0
    return {"status": parts[0], "health": parts[1], "restart_count": restart_count}


def _fetch_health(url: str) -> Tuple[Optional[dict], str]:
    """``(payload, error)``. A 503 body still parses -- that is the degraded
    case the endpoint is designed to report, not a failure to reach it."""
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8")), ""
        except Exception:  # noqa: BLE001
            return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, type(e.reason).__name__ if e.reason else "URLError"
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__


def classify(config: dict, previous_restart_count: Optional[int]) -> Tuple[Optional[str], str, int]:
    """Return ``(reason, detail, bot_restart_count)``.

    ``reason`` is ``None`` when everything that can be checked looks fine.
    Checks run in order of how badly each failure hurts a lead, and the first
    match wins, so the alert names one cause.
    """
    bot = _container_state(BOT_CONTAINER)
    restart_count = bot.get("restart_count", previous_restart_count or 0)

    if bot:
        if bot["status"] != "running":
            return "bot_down", f"{BOT_CONTAINER} במצב {bot['status']}", restart_count
        # A crash loop keeps the container "running" between restarts, so the
        # only reliable signal is the counter going up between two checks.
        if previous_restart_count is not None and restart_count > previous_restart_count:
            return "bot_restarting", "", restart_count

    payload, error = _fetch_health(config["health_url"])
    if payload is None:
        return "health_unreachable", error, restart_count

    if payload.get("polling") != "healthy":
        return "polling_stale", "", restart_count

    postgres = _container_state(POSTGRES_CONTAINER)
    if postgres and postgres["status"] != "running":
        return "postgres_down", f"{POSTGRES_CONTAINER} במצב {postgres['status']}", restart_count

    if payload.get("database") == "unreachable":
        return "database_unreachable", "", restart_count

    # Only billing failures are a billing alarm. An ordinary 429 is transient
    # back-pressure that OpenAI retries through, and waking the customer for
    # it would train him to ignore the next real alert. The classification
    # itself lives in app/followup/scheduler.py::_classify_openai_error.
    openai = payload.get("openai", "unknown")
    if openai == "no_credit":
        return "openai_no_credit", "", restart_count

    chroma = _container_state(CHROMA_CONTAINER)
    if chroma and chroma["status"] != "running":
        return "chroma_down", f"{CHROMA_CONTAINER} במצב {chroma['status']}", restart_count

    if openai == "error":
        return "openai_error", "", restart_count

    # The bot reports this key; the LeadMe cookie work owns the value.
    # "no_cookies" is deliberately absent: this deployment runs without a
    # cookie session on purpose, so that value is the steady state rather
    # than an outage, and alerting on it would page on every boot forever.
    # A session that was configured and then broke still reports "expired"
    # or "unreachable", so the alert arms itself if cookies are ever set up.
    leadme = payload.get("leadme_session", "unknown")
    if leadme in ("expired", "unreachable"):
        return "leadme_session", leadme, restart_count

    return None, "", restart_count


# --- state --------------------------------------------------------------


def _read_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except Exception:  # noqa: BLE001
        # Unreadable or corrupt state must not wedge the watcher. Starting
        # from scratch costs at most one duplicate alert.
        return {}


def _write_state(path: str, state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temp_path, path)
    except Exception as e:  # noqa: BLE001
        print(f"propeller-watch: could not write state: {type(e).__name__}", file=sys.stderr)


# --- sending ------------------------------------------------------------


def _chat_id(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("0"):
        digits = "972" + digits[1:]
    return f"{digits}@c.us"


def send_whatsapp(config: dict, message: str) -> bool:
    """Send one plain-text WhatsApp message. Never raises.

    The token sits in the URL path, so nothing here logs the URL.
    """
    for key in ("green_api_host", "green_api_instance_id", "green_api_token", "alert_phone"):
        if not config[key]:
            print(f"propeller-watch: {key} is not configured, cannot send", file=sys.stderr)
            return False

    url = (
        f"{config['green_api_host'].rstrip('/')}"
        f"/waInstance{config['green_api_instance_id']}"
        f"/sendMessage/{config['green_api_token']}"
    )
    body = json.dumps(
        {"chatId": _chat_id(config["alert_phone"]), "message": message}
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as e:
        print(f"propeller-watch: send failed, HTTP {e.code}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"propeller-watch: send failed, {type(e).__name__}", file=sys.stderr)
    return False


# --- formatting ---------------------------------------------------------


def _clock(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, ISRAEL_TZ).strftime("%H:%M")


def _minutes_in_hebrew(minutes: int) -> str:
    return "דקה" if minutes == 1 else f"{minutes} דקות"


def _duration(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    if minutes < 60:
        return _minutes_in_hebrew(minutes)
    hours, minutes = divmod(minutes, 60)
    hours_text = "שעה" if hours == 1 else f"{hours} שעות"
    if minutes == 0:
        return hours_text
    return f"{hours_text} ו-{_minutes_in_hebrew(minutes)}"


# --- one pass -----------------------------------------------------------


def run_once(config: dict, dry_run: bool) -> int:
    state = _read_state(config["state_file"])
    previous_restart_count = state.get("bot_restart_count")

    reason, detail, restart_count = classify(config, previous_restart_count)
    now = time.time()
    new_state = {"bot_restart_count": restart_count}

    if reason is None:
        if state.get("alerted"):
            message = RECOVERY_MESSAGE.format(
                reason=state.get("reason", "לא ידוע"),
                duration=_duration(now - state.get("since", now)),
            )
            print(f"propeller-watch: recovered from {state.get('reason')}")
            if dry_run:
                print(message)
            elif not send_whatsapp(config, message):
                # Keep the alert on the books so the all-clear is retried
                # next minute rather than lost.
                _write_state(config["state_file"], {**state, **new_state})
                return 0
        else:
            print("propeller-watch: ok")
        _write_state(config["state_file"], new_state)
        return 0

    # A different reason is a different outage: confirm it on its own before
    # alerting, and give it its own start time.
    if reason == state.get("reason"):
        checks = int(state.get("checks", 0)) + 1
        since = state.get("since", now)
        alerted = bool(state.get("alerted"))
    else:
        checks, since, alerted = 1, now, False

    new_state.update({"reason": reason, "since": since, "checks": checks, "alerted": alerted})

    if alerted:
        # Already told him. Staying quiet while the condition persists is the
        # point -- re-alerting every minute for three hours is worse than
        # not alerting at all.
        print(f"propeller-watch: {reason} persists (already alerted)")
        _write_state(config["state_file"], new_state)
        return 0

    if checks < CHECKS_BEFORE_ALERT:
        print(f"propeller-watch: {reason} unconfirmed ({checks}/{CHECKS_BEFORE_ALERT})")
        _write_state(config["state_file"], new_state)
        return 0

    message = MESSAGES[reason].format(since=_clock(since), detail=detail)
    print(f"propeller-watch: alerting on {reason}")
    if dry_run:
        print(message)
        new_state["alerted"] = True
    elif send_whatsapp(config, message):
        new_state["alerted"] = True
    # A failed send leaves alerted False so the next tick tries again.
    _write_state(config["state_file"], new_state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Alert on Propeller bot outages.")
    parser.add_argument("--dry-run", action="store_true", help="check and print, send nothing")
    parser.add_argument("--test-message", action="store_true", help="send one delivery test")
    parser.add_argument("--health-url", help="override WATCH_HEALTH_URL")
    parser.add_argument("--state-file", help="override WATCH_STATE_FILE")
    args = parser.parse_args()

    config = _config()
    if args.health_url:
        config["health_url"] = args.health_url
    if args.state_file:
        config["state_file"] = args.state_file

    if args.test_message:
        sent = send_whatsapp(
            config,
            "בדיקת התראות פרופלור\n"
            "זאת הודעת בדיקה. מהרגע הזה, אם הבוט מפסיק לענות ללידים, "
            "תקבל כאן הודעה עם הסיבה - ועוד אחת כשזה נפתר.",
        )
        print("propeller-watch: test message sent" if sent else "propeller-watch: test message FAILED")
        return 0 if sent else 1

    return run_once(config, args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        # A monitor must never become the outage. Anything unforeseen is
        # logged to the journal and exits clean, so systemd does not start
        # reporting the watcher as the failing unit.
        print(f"propeller-watch: unexpected {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)

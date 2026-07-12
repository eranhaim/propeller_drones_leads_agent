"""Read-only admin UI for browsing lead conversations.

Mounted under /admin on the same FastAPI app that serves the LeadMe
webhook. Two pages:

- GET /admin/             -- table of all leads, sortable by "most recent
                             activity". Click a row -> conversation view.
- GET /admin/leads/{id}   -- full message thread for one lead, formatted
                             like a WhatsApp chat (user bubbles on the
                             right, bot bubbles on the left, RTL).

Auth: HTTP Basic against ADMIN_USER / ADMIN_PASSWORD from the environment.
If either is unset the admin routes refuse to serve anything (fail-closed).

Design goals:
- Pure server-rendered HTML (no build step, no JS framework).
- RTL-first (Hebrew content).
- Zero external dependencies -- one self-contained page of CSS inlined.
- Safe: everything is HTML-escaped, no template injection.
"""

from __future__ import annotations

import html
import secrets
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Lead, Message, MessageRole
from app.db.session import session_scope

router = APIRouter(prefix="/admin", tags=["admin"])
_basic_auth = HTTPBasic()
IL = ZoneInfo("Asia/Jerusalem")


# --- auth ---------------------------------------------------------------


def _require_admin(
    credentials: HTTPBasicCredentials = Depends(_basic_auth),
) -> str:
    settings = get_settings()
    admin_user = settings.admin_user
    admin_pass = settings.admin_password

    if not admin_user or not admin_pass:
        # Fail closed if admin creds aren't configured -- much safer than
        # accidentally serving conversations to the internet.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin UI not configured (ADMIN_USER/ADMIN_PASSWORD unset).",
        )

    # constant-time comparison to avoid timing attacks
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf8"), admin_user.encode("utf8")
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf8"), admin_pass.encode("utf8")
    )
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bad credentials",
            headers={"WWW-Authenticate": 'Basic realm="Propeller Admin"'},
        )
    return credentials.username


# --- formatting helpers -------------------------------------------------


def _fmt_ts(dt: Optional[datetime]) -> str:
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IL).strftime("%Y-%m-%d %H:%M")


def _time_ago(dt: Optional[datetime]) -> str:
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"לפני {secs} שנ'"
    mins = secs // 60
    if mins < 60:
        return f"לפני {mins} דק'"
    hours = mins // 60
    if hours < 24:
        return f"לפני {hours} שע'"
    days = hours // 24
    return f"לפני {days} ימים"


STAGE_BADGE_COLOR = {
    "new": "#94a3b8",
    "engaged": "#3b82f6",
    "warm": "#f59e0b",
    "ready_for_call": "#10b981",
    "handed_off": "#8b5cf6",
}

STAGE_LABEL_HE = {
    "new": "חדש",
    "engaged": "מעורב",
    "warm": "חם",
    "ready_for_call": "בשל לשיחה",
    "handed_off": "הועבר",
}

FAMILIARITY_LABEL_HE = {
    "unknown": "לא ידוע",
    "beginner": "מתחיל",
    "aware": "מודע",
    "experienced": "מנוסה",
}


def _escape(s: Optional[str]) -> str:
    return html.escape(s or "")


# --- shared CSS ---------------------------------------------------------

_CSS = """
:root {
    --bg: #0f172a;
    --panel: #1e293b;
    --panel-2: #273548;
    --border: #334155;
    --text: #f1f5f9;
    --text-dim: #94a3b8;
    --accent: #38bdf8;
    --user-bubble: #075e54;
    --bot-bubble: #262d31;
    --user-bubble-text: #e9edef;
    --bot-bubble-text: #e9edef;
}
* { box-sizing: border-box; }
html, body {
    margin: 0; padding: 0;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
                 Arial, "Noto Sans Hebrew", sans-serif;
    font-size: 14px;
    line-height: 1.5;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header {
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
    display: flex; align-items: center; justify-content: space-between;
    position: sticky; top: 0; z-index: 10;
}
header h1 {
    margin: 0; font-size: 18px; font-weight: 600;
    display: flex; align-items: center; gap: 10px;
}
header h1 .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #10b981; box-shadow: 0 0 8px #10b98180;
}
header .meta { color: var(--text-dim); font-size: 12px; }
header .hdr-actions {
    display: flex; align-items: center; gap: 12px;
}
.hdr-btn {
    display: inline-block; padding: 6px 12px; border-radius: 6px;
    background: var(--panel-2); color: var(--text); font-size: 12px;
    font-weight: 600; border: 1px solid var(--border);
    text-decoration: none;
}
.hdr-btn:hover { background: var(--border); text-decoration: none; }
.hdr-btn.danger { background: transparent; color: #f87171; border-color: #7f1d1d; }
.hdr-btn.danger:hover { background: #7f1d1d; color: white; }

/* Search bar */
.searchbar {
    display: flex; gap: 10px; margin-bottom: 16px; align-items: center;
}
.searchbar input {
    flex: 1; padding: 10px 14px; border-radius: 8px;
    background: var(--panel); color: var(--text);
    border: 1px solid var(--border); font-size: 14px;
    direction: ltr; /* phones and English names read better LTR */
}
.searchbar input:focus { outline: none; border-color: var(--accent); }
.searchbar .count { color: var(--text-dim); font-size: 12px; min-width: 90px; text-align: left; }
tr.hidden-row { display: none; }
main { padding: 24px; max-width: 1400px; margin: 0 auto; }

/* Leads table */
table.leads {
    width: 100%; border-collapse: collapse;
    background: var(--panel); border-radius: 10px; overflow: hidden;
}
.leads thead th {
    background: var(--panel-2);
    text-align: left; padding: 12px 14px;
    font-weight: 600; color: var(--text-dim);
    font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px;
    border-bottom: 1px solid var(--border);
}
.leads tbody tr { border-bottom: 1px solid var(--border); cursor: pointer; }
.leads tbody tr:hover { background: var(--panel-2); }
.leads tbody tr:last-child { border-bottom: none; }
.leads td {
    padding: 12px 14px; vertical-align: middle;
}
.leads td.phone { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--text-dim); }
.leads td.name { font-weight: 500; }
.leads td.last-msg { color: var(--text-dim); font-size: 13px; max-width: 400px;
                     overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.leads td.count { text-align: right; font-variant-numeric: tabular-nums; color: var(--text-dim); }
.leads td.time { color: var(--text-dim); font-size: 12px; white-space: nowrap; }

.badge {
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: 11px; font-weight: 600; color: white;
    text-transform: uppercase; letter-spacing: 0.5px;
}
.summary {
    display: flex; gap: 24px; margin-bottom: 20px;
    background: var(--panel); padding: 16px 20px; border-radius: 10px;
    border: 1px solid var(--border);
}
.summary .stat { display: flex; flex-direction: column; }
.summary .stat .num { font-size: 22px; font-weight: 700; color: var(--accent); }
.summary .stat .lbl { font-size: 11px; color: var(--text-dim); text-transform: uppercase; }

/* Conversation view */
.conv-wrap { display: grid; grid-template-columns: 1fr 320px; gap: 20px; }
@media (max-width: 900px) { .conv-wrap { grid-template-columns: 1fr; } }

.chat {
    background: var(--panel); border-radius: 10px;
    padding: 20px; min-height: 400px;
    direction: rtl;
}
.chat .bubble {
    max-width: 78%; padding: 8px 12px 6px 12px;
    border-radius: 10px; margin-bottom: 8px;
    position: relative; word-wrap: break-word; white-space: pre-wrap;
    font-size: 14px; line-height: 1.45;
}
.chat .bubble.user {
    background: var(--user-bubble); color: var(--user-bubble-text);
    margin-left: auto;
    border-bottom-right-radius: 2px;
}
.chat .bubble.assistant {
    background: var(--bot-bubble); color: var(--bot-bubble-text);
    margin-right: auto;
    border-bottom-left-radius: 2px;
}
.chat .bubble.nudge { border-left: 3px solid #f59e0b; }
.chat .meta {
    display: block; font-size: 10px; color: var(--text-dim);
    margin-top: 4px; direction: ltr; text-align: left;
}
.chat .day-divider {
    text-align: center; font-size: 11px; color: var(--text-dim);
    background: var(--panel-2); padding: 4px 12px; border-radius: 12px;
    margin: 12px auto; display: block; width: fit-content;
    direction: ltr;
}

.sidepanel {
    background: var(--panel); border-radius: 10px; padding: 20px;
    border: 1px solid var(--border); height: fit-content;
    position: sticky; top: 80px;
}
.sidepanel h3 { margin: 0 0 12px 0; font-size: 13px; color: var(--text-dim);
                text-transform: uppercase; letter-spacing: 0.5px; }
.sidepanel dl { margin: 0; }
.sidepanel dt { color: var(--text-dim); font-size: 11px;
                text-transform: uppercase; letter-spacing: 0.4px;
                margin-top: 12px; }
.sidepanel dd { margin: 4px 0 0 0; font-size: 13px; word-break: break-word; }
.sidepanel .back {
    display: inline-block; margin-bottom: 16px; color: var(--text-dim); font-size: 12px;
}
.sidepanel pre {
    background: var(--bg); padding: 8px; border-radius: 6px;
    font-size: 11px; overflow-x: auto; margin: 4px 0 0 0;
    max-height: 200px; overflow-y: auto;
}
"""


def _page(title: str, body: str, *, back_href: Optional[str] = None) -> str:
    back_btn = (
        f'<a class="hdr-btn" href="{_escape(back_href)}">→ חזרה לרשימת הלידים</a>'
        if back_href else ""
    )
    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1><span class="dot"></span>פרופלור דרונס · ניהול</h1>
  <div class="hdr-actions">
    <span class="meta">{_escape(title)}</span>
    {back_btn}
    <a class="hdr-btn danger" href="/admin/logout">יציאה</a>
  </div>
</header>
<main>{body}</main>
</body>
</html>
"""


# --- routes -------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def leads_list(_: str = Depends(_require_admin)) -> str:
    # Snapshot all data into plain dicts inside the session; ORM objects are
    # detached after session_scope() exits so we can't touch attributes later.
    snapshot: list[dict] = []
    with session_scope() as s:
        rows = s.execute(
            select(
                Lead,
                func.count(Message.id).label("msg_count"),
                func.max(Message.created_at).label("last_at"),
            )
            .outerjoin(Message, Message.lead_id == Lead.id)
            .group_by(Lead.id)
            .order_by(func.max(Message.created_at).desc().nullslast())
        ).all()

        for lead, msg_count, last_at in rows:
            last = s.execute(
                select(Message)
                .where(Message.lead_id == lead.id)
                .order_by(Message.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last:
                prefix = "👤 " if last.role == MessageRole.user else "🤖 "
                text = (last.content or "").strip().replace("\n", " ")
                last_msg = prefix + text[:140]
            else:
                last_msg = ""

            snapshot.append({
                "id": lead.id,
                "name": lead.name or "",
                "phone": lead.phone or "",
                "stage": lead.funnel_stage.value,
                "muted": bool(lead.bot_muted),
                "msg_count": int(msg_count or 0),
                "last_at": last_at,
                "last_msg": last_msg,
            })

    total_leads = len(snapshot)
    handed_off = sum(1 for r in snapshot if r["stage"] == "handed_off")
    warm = sum(1 for r in snapshot if r["stage"] in ("warm", "ready_for_call"))
    active_24h_cutoff = datetime.now(timezone.utc).timestamp() - 24 * 3600

    def _is_recent(t: Optional[datetime]) -> bool:
        if not t:
            return False
        tt = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        return tt.timestamp() > active_24h_cutoff

    active_24h = sum(1 for r in snapshot if _is_recent(r["last_at"]))

    summary_html = f"""
    <div class="summary">
      <div class="stat"><div class="num">{total_leads}</div><div class="lbl">סה&quot;כ לידים</div></div>
      <div class="stat"><div class="num">{active_24h}</div><div class="lbl">פעילים ב-24ש'</div></div>
      <div class="stat"><div class="num">{warm}</div><div class="lbl">חמים · בשלים</div></div>
      <div class="stat"><div class="num">{handed_off}</div><div class="lbl">הועברו למכירות</div></div>
    </div>
    """

    trs = []
    for r in snapshot:
        badge_color = STAGE_BADGE_COLOR.get(r["stage"], "#64748b")
        stage_label = STAGE_LABEL_HE.get(r["stage"], r["stage"])
        name_html = _escape(r["name"]) if r["name"] else '<span style="color:#64748b">?</span>'
        mute_pill = (
            '<span class="badge" style="background:#dc2626;margin-inline-start:6px">בוט מושתק</span>'
            if r["muted"] else ""
        )
        haystack = " ".join([
            r["name"] or "",
            r["phone"] or "",
            r["stage"] or "",
            stage_label,
            r["last_msg"] or "",
        ]).lower()
        trs.append(f"""
        <tr data-search="{_escape(haystack)}" onclick="location.href='/admin/leads/{r['id']}'">
          <td class="name">{name_html}{mute_pill}</td>
          <td class="phone">{_escape(r['phone'])}</td>
          <td><span class="badge" style="background:{badge_color}">{_escape(stage_label)}</span></td>
          <td class="count">{r['msg_count']}</td>
          <td class="last-msg">{_escape(r['last_msg'])}</td>
          <td class="time">{_escape(_time_ago(r['last_at']))}<br><span style="opacity:0.6">{_escape(_fmt_ts(r['last_at']))}</span></td>
        </tr>
        """)

    search_html = f"""
    <div class="searchbar">
      <input id="lead-search" type="search" autofocus
             placeholder="חפש לפי שם, טלפון, שלב, או הודעה אחרונה..."
             oninput="filterLeads(this.value)">
      <span class="count" id="lead-count">{total_leads} מתוך {total_leads}</span>
    </div>
    <script>
    function filterLeads(q) {{
      q = (q || '').trim().toLowerCase();
      const rows = document.querySelectorAll('table.leads tbody tr[data-search]');
      let shown = 0;
      rows.forEach(r => {{
        const hay = r.getAttribute('data-search');
        const match = !q || hay.indexOf(q) !== -1;
        r.classList.toggle('hidden-row', !match);
        if (match) shown++;
      }});
      document.getElementById('lead-count').textContent = shown + ' מתוך ' + rows.length;
    }}
    document.addEventListener('keydown', e => {{
      if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {{
        e.preventDefault();
        document.getElementById('lead-search').focus();
      }}
    }});
    </script>
    """

    body = summary_html + search_html + f"""
    <table class="leads">
      <thead>
        <tr>
          <th>שם</th><th>טלפון</th><th>שלב</th><th style="text-align:right">הודעות</th>
          <th>הודעה אחרונה</th><th>פעילות אחרונה (שעון ישראל)</th>
        </tr>
      </thead>
      <tbody>{''.join(trs) or '<tr><td colspan="6" style="padding:40px;text-align:center;color:#64748b">אין לידים עדיין.</td></tr>'}</tbody>
    </table>
    """

    return _page(f"לידים · {total_leads}", body)


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_conversation(lead_id: int, _: str = Depends(_require_admin)) -> str:
    import json as _json

    lead_snapshot: dict = {}
    msg_snapshots: list[dict] = []

    with session_scope() as s:
        lead = s.get(Lead, lead_id)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        badge_color = STAGE_BADGE_COLOR.get(lead.funnel_stage.value, "#64748b")
        metadata_str = _json.dumps(lead.lead_metadata or {}, ensure_ascii=False, indent=2)
        videos_sent_str = ", ".join(lead.videos_sent or []) or "-"

        lead_snapshot = {
            "id": lead.id,
            "name": lead.name,
            "phone": lead.phone,
            "stage": lead.funnel_stage.value,
            "badge_color": badge_color,
            "familiarity": lead.familiarity_level.value,
            "muted": bool(lead.bot_muted),
            "created_at": _fmt_ts(lead.created_at),
            "last_message_at": _fmt_ts(lead.last_message_at),
            "videos_sent": videos_sent_str,
            "metadata_str": metadata_str,
        }

        msgs = list(s.execute(
            select(Message)
            .where(Message.lead_id == lead_id)
            .order_by(Message.created_at.asc())
        ).scalars().all())

        for m in msgs:
            nudge = m.msg_metadata.get("nudge") if isinstance(m.msg_metadata, dict) else None
            msg_snapshots.append({
                "id": m.id,
                "role": "user" if m.role == MessageRole.user else "assistant",
                "content": m.content or "",
                "created_at": m.created_at,
                "nudge": nudge,
            })

    bubbles = []
    last_day: Optional[str] = None
    for m in msg_snapshots:
        created = m["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        il = created.astimezone(IL)
        day_key = il.strftime("%Y-%m-%d")
        if day_key != last_day:
            bubbles.append(
                f'<div class="day-divider">{il.strftime("%A, %d %B %Y")}</div>'
            )
            last_day = day_key

        role_class = m["role"]
        nudge = m["nudge"]
        extra = " nudge" if nudge else ""
        time_str = il.strftime("%H:%M")
        role_label = "לקוח" if role_class == "user" else (
            "בוט · תזכורת #" + str(nudge) if nudge else "בוט"
        )
        content_html = _escape(m["content"])
        bubbles.append(f"""
        <div class="bubble {role_class}{extra}">{content_html}<span class="meta">{_escape(role_label)} · {time_str} · #{m['id']}</span></div>
        """)

    ls = lead_snapshot
    stage_label = STAGE_LABEL_HE.get(ls["stage"], ls["stage"])
    familiarity_label = FAMILIARITY_LABEL_HE.get(ls["familiarity"], ls["familiarity"])

    if ls["muted"]:
        bot_pill = (
            '<span class="badge" style="background:#dc2626">בוט מושתק</span>'
        )
        toggle_action = f"/admin/leads/{ls['id']}/unmute"
        toggle_label = "הפעל בוט מחדש"
        toggle_bg = "#10b981"
    else:
        bot_pill = (
            '<span class="badge" style="background:#10b981">בוט פעיל</span>'
        )
        toggle_action = f"/admin/leads/{ls['id']}/mute"
        toggle_label = "השתק בוט (אני משתלט)"
        toggle_bg = "#dc2626"

    # JS confirm: single-quotes in JS + escaped quote for the alert message
    confirm_msg = (
        f"למחוק את הליד {ls['phone']} ואת כל {len(msg_snapshots)} ההודעות? "
        "פעולה זו אינה הפיכה."
    ).replace("'", "\\'")

    body = f"""
    <div class="conv-wrap">
      <div class="chat">
        {''.join(bubbles) or '<p style="color:#64748b">אין הודעות עדיין.</p>'}
      </div>
      <aside class="sidepanel">
        <a class="back" href="/admin">→ כל הלידים</a>
        <h3>{_escape(ls['name']) or '(ללא שם)'}</h3>
        <dl>
          <dt>בוט</dt><dd>{bot_pill}
            <form method="post" action="{toggle_action}" style="display:inline;margin-inline-start:8px">
              <button type="submit" style="background:{toggle_bg};color:white;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">{_escape(toggle_label)}</button>
            </form>
          </dd>
          <dt>טלפון</dt><dd style="font-family:ui-monospace">{_escape(ls['phone'])}</dd>
          <dt>שלב</dt><dd><span class="badge" style="background:{ls['badge_color']}">{_escape(stage_label)}</span></dd>
          <dt>רמת היכרות</dt><dd>{_escape(familiarity_label)}</dd>
          <dt>נוצר</dt><dd>{_escape(ls['created_at'])}</dd>
          <dt>הודעה אחרונה</dt><dd>{_escape(ls['last_message_at'])}</dd>
          <dt>סרטונים שנשלחו</dt><dd>{_escape(ls['videos_sent'])}</dd>
          <dt>מטא-דאטה</dt><dd><pre>{_escape(ls['metadata_str'])}</pre></dd>
        </dl>
        <hr style="border:none;border-top:1px solid var(--border);margin:20px 0">
        <h3 style="color:#dc2626">אזור מסוכן</h3>
        <p style="color:var(--text-dim);font-size:12px;margin:0 0 10px 0">
          מחיקת הליד וכל ההודעות שלו כדי להתחיל שיחה חדשה מאפס עם אותו
          מספר וואטסאפ (שימושי לבדיקה ידנית). ההודעה הנכנסת הבאה תיצור
          ליד חדש לגמרי.
        </p>
        <form method="post" action="/admin/leads/{ls['id']}/delete"
              onsubmit="return confirm('{confirm_msg}');">
          <button type="submit"
                  style="background:#dc2626;color:white;border:none;padding:8px 14px;
                         border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">
            מחק ליד ואפס שיחה
          </button>
        </form>
      </aside>
    </div>
    """
    return _page(
        f"ליד {ls['id']} · {ls['name'] or ls['phone']}",
        body,
        back_href="/admin",
    )


def _set_muted(lead_id: int, muted: bool) -> None:
    from loguru import logger
    with session_scope() as s:
        lead = s.get(Lead, lead_id)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        lead.bot_muted = muted
        logger.info(
            "[admin] lead {} bot_muted -> {} (via /admin UI)",
            lead_id, muted,
        )


@router.post("/leads/{lead_id}/mute")
def mute_lead(lead_id: int, _: str = Depends(_require_admin)) -> RedirectResponse:
    _set_muted(lead_id, True)
    return RedirectResponse(url=f"/admin/leads/{lead_id}", status_code=303)


@router.post("/leads/{lead_id}/unmute")
def unmute_lead(lead_id: int, _: str = Depends(_require_admin)) -> RedirectResponse:
    _set_muted(lead_id, False)
    return RedirectResponse(url=f"/admin/leads/{lead_id}", status_code=303)


@router.get("/logout", response_class=HTMLResponse)
def logout() -> HTMLResponse:
    """Force the browser to forget its HTTP Basic credentials.

    HTTP Basic has no formal 'logout' — browsers cache the Authorization
    header until they see a 401 for the same realm. The trick used here:
    return a 401 with the same realm without validating creds. Most
    browsers respond by clearing the cached credentials for that realm,
    and the user gets a fresh login prompt on the next request. We wrap
    it in a small HTML page so they see a friendly 'Logged out' screen
    instead of a raw browser error.
    """
    html_body = """
    <div dir="rtl" style="max-width:420px;margin:80px auto;text-align:center;
                background:#1e293b;padding:32px;border-radius:12px;
                border:1px solid #334155;color:#f1f5f9;
                font-family:'Heebo','Assistant',sans-serif">
      <h2 style="margin:0 0 12px 0">התנתקת מהמערכת</h2>
      <p style="color:#94a3b8;margin:0 0 20px 0">
        הדפדפן התבקש לשכוח את פרטי הכניסה של הניהול.
      </p>
      <a href="/admin/"
         style="display:inline-block;padding:10px 18px;background:#38bdf8;
                color:#0f172a;border-radius:6px;text-decoration:none;
                font-weight:600">התחבר שוב</a>
    </div>
    """
    return HTMLResponse(
        content=html_body,
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Propeller Admin - logged out"'},
    )


@router.post("/leads/{lead_id}/delete")
def delete_lead(lead_id: int, _: str = Depends(_require_admin)) -> RedirectResponse:
    """Hard-delete a lead and all its messages so the next inbound WhatsApp
    from that number starts a completely fresh conversation. Intended for
    manual QA of the opener/warm-up flow. Messages cascade via the
    ondelete=CASCADE on Message.lead_id."""
    from loguru import logger
    with session_scope() as s:
        lead = s.get(Lead, lead_id)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        phone = lead.phone
        s.delete(lead)
        logger.warning(
            "[admin] hard-deleted lead {} (phone={}) via /admin UI",
            lead_id, phone,
        )
    return RedirectResponse(url="/admin", status_code=303)

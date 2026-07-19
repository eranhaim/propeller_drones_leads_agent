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

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
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
    <a class="hdr-btn" href="/admin/simulator">סימולטור</a>
    <a class="hdr-btn" href="/admin/leadme-cookies">עוגיות LeadMe</a>
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
    manual QA of the opener/warm-up flow.

    Two deletes happen, in this order:

    1. LeadMe delete via saved session cookies (best-effort; if LeadMe
       cookies are missing/expired the local delete still proceeds).
    2. Local DB delete. Messages cascade via ondelete=CASCADE on
       Message.lead_id.

    Reason for LeadMe-first: if the local DB delete succeeds but the
    LeadMe delete fails, the admin needs to know so they can clean up
    manually -- if LeadMe first, the admin sees the outcome BEFORE
    committing to the irreversible local wipe. If LeadMe fails, we
    still delete locally (we don't want a broken LeadMe integration to
    block QA) but surface the outcome in a query-string flash.
    """
    from loguru import logger
    from app.crm.leadme_delete import delete_from_leadme

    with session_scope() as s:
        lead = s.get(Lead, lead_id)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        phone = lead.phone or ""

    leadme_msg = ""
    if phone:
        try:
            ok, detail = delete_from_leadme(phone)
        except Exception:  # noqa: BLE001 -- must never block local delete
            logger.exception("[admin] LeadMe delete raised for {}", phone)
            ok, detail = False, "leadme delete raised (see logs)"
        leadme_msg = f"leadme={'ok' if ok else 'fail'}: {detail}"
        logger.info("[admin] pre-delete LeadMe attempt for {}: {}",
                    phone, leadme_msg)

    with session_scope() as s:
        lead = s.get(Lead, lead_id)
        if lead is not None:
            s.delete(lead)
    logger.warning(
        "[admin] hard-deleted lead {} (phone={}) via /admin UI ({})",
        lead_id, phone, leadme_msg or "leadme=skipped",
    )
    return RedirectResponse(url="/admin", status_code=303)


# --- LeadMe cookies self-service ---------------------------------------
#
# LeadMe does not offer a real API for deleting leads, so we drive their
# internal admin XHRs with a saved PHPSESSID cookie. That cookie expires
# on the order of hours-to-days. This page lets the admin refresh those
# cookies in ~30 seconds without SSH-ing into the box:
#
#   1. In Chrome, log into leadmecms.co.il, open DevTools.
#   2. Application tab -> Cookies -> https://www.leadmecms.co.il -> copy
#      all the cookies (or use a JSON-export extension).
#   3. Paste the JSON here and hit save.
#
# Format accepted: the same JSON list Playwright / Chrome's cookie
# export extensions produce -- [{name, value, domain, path, ...}, ...].


def _cookies_file_status() -> tuple[str, str]:
    """Return (badge_text, extra_line) describing the current cookies file."""
    settings = get_settings()
    raw_path = (settings.leadme_cookies_path or "").strip()
    if not raw_path:
        return ("לא מוגדר", "משתנה הסביבה LEADME_COOKIES_PATH ריק.")
    p = Path(raw_path)
    if not p.exists():
        return ("חסר", f"הקובץ {raw_path} לא קיים על הדיסק.")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return ("שבור", f"שגיאה בקריאת JSON: {exc}")
    leadme = [c for c in data if "leadmecms.co.il" in (c.get("domain") or "")]
    has_session = any(c.get("name") == "PHPSESSID" for c in leadme)
    has_csrf = any(c.get("name") == "csrf_cookie_name" for c in leadme)
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    age = _time_ago(mtime)
    if not has_session:
        return ("שבור", f"אין עוגיית PHPSESSID (עודכן לאחרונה {age}).")
    return (
        "פעיל",
        f"נמצאו {len(leadme)} עוגיות של leadmecms.co.il "
        f"(PHPSESSID={'✓' if has_session else '✗'}, "
        f"CSRF={'✓' if has_csrf else '✗'}). עודכן לאחרונה {age}.",
    )


@router.get("/leadme-cookies", response_class=HTMLResponse)
def leadme_cookies_form(
    _: str = Depends(_require_admin),
    saved: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    badge, detail = _cookies_file_status()
    flash = ""
    if saved:
        flash = (
            '<div style="background:#065f46;color:#d1fae5;padding:12px 16px;'
            'border-radius:8px;margin-bottom:16px">✓ העוגיות נשמרו בהצלחה. '
            'עכשיו נסה למחוק ליד מהאדמין ובדוק שהמחיקה עוברת גם ב-LeadMe.</div>'
        )
    if error:
        flash = (
            '<div style="background:#7f1d1d;color:#fecaca;padding:12px 16px;'
            f'border-radius:8px;margin-bottom:16px">✗ {_escape(error)}</div>'
        )

    body = f"""
{flash}
<div class="panel">
  <h2>עוגיות LeadMe לצורך מחיקה</h2>
  <p class="muted">
    כדי שכפתור "מחק ליד" באדמין ימחק את הליד גם ב-LeadMe (ולא רק ב-DB
    המקומי), אנחנו צריכים עוגיות התחברות של חשבון LeadMe. הן פוגות אחרי
    כמה שעות/ימים -- אז מפעם לפעם צריך לרענן אותן דרך העמוד הזה.
  </p>

  <div style="margin:16px 0;padding:12px 16px;background:#0f172a;
              border-radius:8px;border:1px solid #334155">
    <b>מצב נוכחי:</b> <span style="color:#38bdf8">{_escape(badge)}</span><br>
    <span class="muted">{_escape(detail)}</span>
  </div>

  <h3>איך מרעננים עוגיות (30 שניות):</h3>
  <ol style="line-height:1.9">
    <li>פתח Chrome וודא שאתה מחובר ל-
      <a href="https://www.leadmecms.co.il/app/leads" target="_blank"
         style="color:#38bdf8">leadmecms.co.il/app/leads</a>.</li>
    <li>לחץ F12 → Application → Cookies → www.leadmecms.co.il.</li>
    <li>הפעל את התוסף
      <a href="https://chromewebstore.google.com/search/cookie%20editor"
         target="_blank" style="color:#38bdf8">Cookie-Editor</a>
      (או דומה) → Export → JSON.</li>
    <li>הדבק את ה-JSON בשדה למטה ולחץ "שמור".</li>
  </ol>

  <form method="post" action="/admin/leadme-cookies" style="margin-top:20px">
    <label style="display:block;margin-bottom:8px;font-weight:600">
      JSON של עוגיות (רשימת אובייקטים):
    </label>
    <textarea name="cookies_json" required
              placeholder='[{{"name":"PHPSESSID","value":"...","domain":".leadmecms.co.il",...}}, ...]'
              style="width:100%;min-height:240px;background:#0f172a;
                     color:#e2e8f0;border:1px solid #334155;border-radius:8px;
                     padding:12px;font-family:monospace;font-size:12px;
                     direction:ltr" dir="ltr"></textarea>
    <div style="margin-top:12px;display:flex;gap:12px">
      <button type="submit" style="padding:10px 20px;background:#38bdf8;
              color:#0f172a;border:none;border-radius:6px;font-weight:600;
              cursor:pointer">שמור עוגיות</button>
      <a href="/admin" style="padding:10px 20px;color:#94a3b8;
         text-decoration:none">חזור</a>
    </div>
  </form>
</div>
"""
    return _page("עוגיות LeadMe", body, back_href="/admin/")


@router.post("/leadme-cookies")
def leadme_cookies_save(
    cookies_json: str = Form(...),
    _: str = Depends(_require_admin),
) -> RedirectResponse:
    from loguru import logger
    settings = get_settings()
    raw_path = (settings.leadme_cookies_path or "").strip()
    if not raw_path:
        return RedirectResponse(
            url="/admin/leadme-cookies?error="
                "LEADME_COOKIES_PATH+not+configured",
            status_code=303,
        )

    try:
        parsed = json.loads(cookies_json)
    except json.JSONDecodeError as exc:
        return RedirectResponse(
            url=f"/admin/leadme-cookies?error=Invalid+JSON:+{exc}",
            status_code=303,
        )
    if not isinstance(parsed, list):
        return RedirectResponse(
            url="/admin/leadme-cookies?error="
                "Expected+a+JSON+array+of+cookie+objects",
            status_code=303,
        )

    # Minimum viability check: must contain PHPSESSID for leadmecms.co.il.
    has_session = any(
        c.get("name") == "PHPSESSID"
        and "leadmecms.co.il" in (c.get("domain") or "")
        for c in parsed
        if isinstance(c, dict)
    )
    if not has_session:
        return RedirectResponse(
            url="/admin/leadme-cookies?error="
                "Missing+PHPSESSID+cookie+for+leadmecms.co.il",
            status_code=303,
        )

    p = Path(raw_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    logger.warning(
        "[admin] LeadMe cookies refreshed via UI ({} cookies, "
        "PHPSESSID present)", len(parsed),
    )
    return RedirectResponse(url="/admin/leadme-cookies?saved=1", status_code=303)


# --- Simulator -------------------------------------------------------------

def _build_simulator_page() -> str:
    sim_css = """
.sim-layout {
    display: flex;
    gap: 20px;
    max-width: 900px;
    margin: 30px auto;
    height: calc(100vh - 110px);
    padding: 0 16px;
}
.sim-chat {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
}
.sim-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
}
.sim-header h2 { margin: 0; font-size: 16px; }
.sim-messages {
    flex: 1;
    overflow-y: auto;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
}
.bubble {
    max-width: 80%;
    padding: 8px 12px;
    border-radius: 10px;
    font-size: 14px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
}
.bubble.user {
    background: var(--user-bubble);
    color: var(--user-bubble-text);
    align-self: flex-end;
    border-bottom-left-radius: 3px;
}
.bubble.bot {
    background: var(--bot-bubble);
    color: var(--bot-bubble-text);
    align-self: flex-start;
    border-bottom-right-radius: 3px;
}
.bubble-time {
    font-size: 10px;
    color: var(--text-dim);
    margin-top: 3px;
    text-align: left;
    direction: ltr;
}
.sim-input-row {
    display: flex;
    gap: 8px;
    margin-top: 10px;
}
.sim-input-row textarea {
    flex: 1;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    padding: 10px 12px;
    font-size: 14px;
    font-family: inherit;
    resize: none;
    height: 48px;
    line-height: 1.4;
}
.sim-input-row textarea:focus { outline: none; border-color: var(--accent); }
.sim-send-btn {
    background: var(--accent);
    color: #0f172a;
    border: none;
    border-radius: 8px;
    padding: 0 18px;
    font-size: 16px;
    cursor: pointer;
    font-weight: 700;
}
.sim-send-btn:disabled { opacity: 0.4; cursor: default; }
.sim-reset-btn {
    background: transparent;
    color: #f87171;
    border: 1px solid #7f1d1d;
    border-radius: 6px;
    padding: 4px 12px;
    font-size: 12px;
    cursor: pointer;
}
.sim-reset-btn:hover { background: #7f1d1d; color: white; }
.typing-indicator {
    display: none;
    align-self: flex-start;
    background: var(--bot-bubble);
    border-radius: 10px;
    border-bottom-right-radius: 3px;
    padding: 10px 16px;
    color: var(--text-dim);
    font-size: 13px;
}
.typing-indicator.visible { display: block; }

/* State panel */
.sim-panel {
    width: 240px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.sim-panel h3 {
    margin: 0 0 8px 0;
    font-size: 13px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.05em;
}
.state-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
}
.state-card h4 {
    margin: 0 0 10px 0;
    font-size: 12px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.05em;
}
.state-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 5px 0;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
}
.state-row:last-child { border-bottom: none; }
.state-label { color: var(--text-dim); }
.state-val {
    font-weight: 600;
    color: var(--text);
    text-align: left;
    direction: ltr;
}
.state-val.empty { color: var(--text-dim); font-weight: 400; font-style: italic; }
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 99px;
    font-size: 11px;
    font-weight: 700;
}
.badge-new       { background: #334155; color: #94a3b8; }
.badge-engaged   { background: #1d4ed8; color: #bfdbfe; }
.badge-warm      { background: #92400e; color: #fde68a; }
.badge-ready_for_call { background: #065f46; color: #6ee7b7; }
.badge-handed_off { background: #4c1d95; color: #c4b5fd; }
.badge-unknown   { background: #1e293b; color: #64748b; }
.badge-beginner  { background: #1e3a5f; color: #93c5fd; }
.badge-aware     { background: #1c3d2e; color: #6ee7b7; }
.badge-experienced { background: #3b1f5e; color: #d8b4fe; }
.videos-list {
    font-size: 12px;
    color: var(--accent);
    margin-top: 4px;
    line-height: 1.6;
}
"""
    js = r"""
const SESSION_KEY = 'sim_session_id';

function getOrCreateSession() {
  let sid = sessionStorage.getItem(SESSION_KEY);
  if (!sid) { sid = crypto.randomUUID(); sessionStorage.setItem(SESSION_KEY, sid); }
  return sid;
}

function nowTime() {
  return new Date().toLocaleTimeString('he-IL', {hour: '2-digit', minute: '2-digit'});
}

function addBubble(role, text) {
  const wrap = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'bubble ' + role;
  div.textContent = text;
  const ts = document.createElement('div');
  ts.className = 'bubble-time';
  ts.textContent = nowTime();
  div.appendChild(ts);
  wrap.appendChild(div);
  wrap.scrollTop = wrap.scrollHeight;
}

const STAGE_HE = {new:'חדש', engaged:'מעורב', warm:'חם', ready_for_call:'בשל לשיחה', handed_off:'הועבר'};
const FAM_HE   = {unknown:'לא ידוע', beginner:'מתחיל', aware:'מודע', experienced:'מנוסה'};
const INTENT_HE = {course:'קורס', shop:'חנות', service:'שירות', hobby:'תחביב', job:'עבודה', unknown:'לא ידוע'};

function val(v, map) {
  if (v === null || v === undefined) return null;
  return map ? (map[v] || v) : v;
}

function renderState(state) {
  function row(label, v, map, badgeClass) {
    const display = val(v, map);
    const empty = display === null || display === undefined || display === '';
    const cls = empty ? 'state-val empty' : 'state-val';
    const text = empty ? '—' : display;
    const inner = badgeClass && !empty
      ? `<span class="badge ${badgeClass}">${text}</span>`
      : `<span class="${cls}">${text}</span>`;
    return `<div class="state-row"><span class="state-label">${label}</span>${inner}</div>`;
  }

  const stage = state.stage || 'new';
  const fam = state.familiarity || 'unknown';
  const videos = (state.videos_sent || []);
  const callIcon = state.call_scheduled ? '✅ תואם' : (state.stage === 'handed_off' ? '✅ תואם' : '—');

  document.getElementById('panel-funnel').innerHTML =
    row('שלב', stage, STAGE_HE, 'badge badge-' + stage) +
    row('היכרות', fam, FAM_HE, 'badge badge-' + fam);

  document.getElementById('panel-intent').innerHTML =
    row('כוונה', state.intent, INTENT_HE) +
    row('תעשייה', state.industry) +
    row('ניסיון', state.has_experience === null || state.has_experience === undefined ? null : (state.has_experience ? 'כן' : 'לא'));

  document.getElementById('panel-call').innerHTML =
    row('חלון', state.preferred_call_slot) +
    row('שיחה', state.call_scheduled ? 'תואמה' : null);

  const vEl = document.getElementById('panel-videos');
  vEl.innerHTML = videos.length
    ? videos.map(v => `<div class="videos-list">▶ ${v}</div>`).join('')
    : '<span class="state-val empty">—</span>';
}

async function sendMsg() {
  const inp = document.getElementById('inp');
  const btn = document.getElementById('sendBtn');
  const typing = document.getElementById('typing');
  const text = inp.value.trim();
  if (!text) return;

  inp.value = '';
  btn.disabled = true;
  addBubble('user', text);
  typing.classList.add('visible');
  document.getElementById('messages').scrollTop = 9999;

  try {
    const res = await fetch('/admin/simulator/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: getOrCreateSession(), message: text})
    });
    const data = await res.json();
    typing.classList.remove('visible');
    addBubble('bot', data.reply || '(אין תשובה)');
    if (data.state) renderState(data.state);
  } catch(e) {
    typing.classList.remove('visible');
    addBubble('bot', 'שגיאת רשת — נסה שוב.');
  }
  btn.disabled = false;
  inp.focus();
}

function resetChat() {
  const sid = getOrCreateSession();
  fetch('/admin/simulator/reset', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: sid})
  });
  sessionStorage.removeItem(SESSION_KEY);
  document.getElementById('messages').innerHTML =
    '<div class="bubble bot">שיחה חדשה התחילה \u{1F44B} איך אוכל לעזור?</div>';
  renderState({familiarity:'unknown',stage:'new',intent:null,industry:null,
    preferred_call_slot:null,has_experience:null,videos_sent:[],call_scheduled:false});
}

document.getElementById('inp').addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
});
"""
    html = (
        "<!DOCTYPE html>\n"
        '<html lang="he" dir="rtl">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>סימולטור בוט | Propeller Admin</title>\n"
        "<style>\n" + _CSS + sim_css + "</style>\n"
        "</head>\n<body>\n"
        "<header>\n"
        '  <h1><span class="dot"></span>Propeller Admin</h1>\n'
        '  <div class="hdr-actions">\n'
        '    <a href="/admin" class="hdr-btn">רשימת לידים</a>\n'
        "  </div>\n</header>\n"
        '<div class="sim-layout">\n'
        # Chat column
        '  <div class="sim-chat">\n'
        '    <div class="sim-header">\n'
        '      <h2>&#x1F916; סימולטור בוט</h2>\n'
        '      <button class="sim-reset-btn" onclick="resetChat()">אתחול שיחה</button>\n'
        "    </div>\n"
        '    <div class="sim-messages" id="messages">\n'
        '      <div class="bubble bot">שלום! אני אלעד מאקדמיית פרופלור דרונס &#x1F44B; איך אוכל לעזור?</div>\n'
        "    </div>\n"
        '    <div class="typing-indicator" id="typing">הבוט מקליד...</div>\n'
        '    <div class="sim-input-row">\n'
        '      <textarea id="inp" placeholder="כתוב הודעה..." rows="1"></textarea>\n'
        '      <button class="sim-send-btn" id="sendBtn" onclick="sendMsg()">&#x27A4;</button>\n'
        "    </div>\n"
        "  </div>\n"
        # State panel column
        '  <div class="sim-panel">\n'
        '    <h3>מצב הליד</h3>\n'
        '    <div class="state-card">\n'
        '      <h4>משפך</h4>\n'
        '      <div id="panel-funnel">'
        '<div class="state-row"><span class="state-label">שלב</span>'
        '<span class="badge badge-new">חדש</span></div>'
        '<div class="state-row"><span class="state-label">היכרות</span>'
        '<span class="badge badge-unknown">לא ידוע</span></div>'
        "</div>\n"
        "    </div>\n"
        '    <div class="state-card">\n'
        '      <h4>פרופיל</h4>\n'
        '      <div id="panel-intent">'
        '<div class="state-row"><span class="state-label">כוונה</span><span class="state-val empty">—</span></div>'
        '<div class="state-row"><span class="state-label">תעשייה</span><span class="state-val empty">—</span></div>'
        '<div class="state-row"><span class="state-label">ניסיון</span><span class="state-val empty">—</span></div>'
        "</div>\n"
        "    </div>\n"
        '    <div class="state-card">\n'
        '      <h4>תיאום שיחה</h4>\n'
        '      <div id="panel-call">'
        '<div class="state-row"><span class="state-label">חלון</span><span class="state-val empty">—</span></div>'
        '<div class="state-row"><span class="state-label">שיחה</span><span class="state-val empty">—</span></div>'
        "</div>\n"
        "    </div>\n"
        '    <div class="state-card">\n'
        '      <h4>סרטונים שנשלחו</h4>\n'
        '      <div id="panel-videos"><span class="state-val empty">—</span></div>\n'
        "    </div>\n"
        "  </div>\n"
        "</div>\n"
        "<script>\n" + js + "\n</script>\n"
        "</body>\n</html>\n"
    )
    return html


@router.get("/simulator", response_class=HTMLResponse)
async def simulator_page(
    _: str = Depends(_require_admin),
) -> HTMLResponse:
    return HTMLResponse(_build_simulator_page())


@router.post("/simulator/chat")
async def simulator_chat(
    req: Request,
    _: str = Depends(_require_admin),
) -> dict:
    from app.agent.graph import simulate_message

    body = await req.json()
    session_id = str(body.get("session_id", "default"))
    message = str(body.get("message", "")).strip()
    if not message:
        return {"reply": ""}

    result = simulate_message(session_id, message)
    return {"reply": result["reply"], "state": result["state"]}


@router.post("/simulator/reset")
async def simulator_reset(
    req: Request,
    _: str = Depends(_require_admin),
) -> dict:
    from app.agent.graph import clear_simulation

    body = await req.json()
    session_id = str(body.get("session_id", "default"))
    clear_simulation(session_id)
    return {"ok": True}

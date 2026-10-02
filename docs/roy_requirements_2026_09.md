# Roy's Confirmed Bot Requirements

Source: WhatsApp evidence with Roy, superseded by the project-chat screenshots
and messages from 2026-09-27 through 2026-09-29.
This document intentionally excludes credentials, personal contacts, and
unavailable voice/image content.

## LeadMe priority mapping

| Level | Meaning | When to assign |
|---|---|---|
| L1 | Highest priority | Organic source/campaign, or content delivered by the bot followed by meaningful engagement. |
| L2 | Engaged | The lead meaningfully talks with the bot but is not L1; includes price questions, shallow booking, paid-source engagement, and re-entry. |
| L3 | No bot engagement | A non-priority new lead has not meaningfully engaged with the bot. |

Lower number is higher priority. Live updates only upgrade. The audited
reconciliation may correct bot-managed L1/L2/L3 rows in either direction.

Website forms, landing pages, paid source, and booking alone are never L1.
The webhook may assign source-based L1 only when LeadMe sends a source label
or campaign that exactly matches `LEADME_ORGANIC_SOURCES` /
`LEADME_ORGANIC_CAMPAIGNS`. Missing or unknown source data must never be
guessed as L1.

## Conversation and handoff

- Keep the conversation free-flowing Hebrew, short, and natural.
- Ask at most one question per message.
- Classify familiarity and adapt the explanation to it.
- Warm course leads by explaining why commercial drones are attractive, then
  explain Propeller when relevant; hand off when the lead is ready.
- Say `יועץ לימודים` or `מומחה בתחום`, not `נציג`.
- Do not recommend a specific course to a beginner. A human advisor chooses
  the fit.
- Give course information briefly and generally. Do not overload leads with
  technical course detail, weights, or exact course pricing.
- Price is only the range 1,200–11,000 NIS; exact pricing goes to an advisor.
- When the question is specifically about the four-professions course, answer
  about that course rather than generic drone professions.
- Send the shop link only when the lead discusses buying a drone or opening a
  drone business.
- Job seekers are routed to the HR email, not into the course funnel.

## Outreach and safety

- No automatic WhatsApp remarketing, content follow-ups, or 24/48-hour nudges.
- A LeadMe webhook is not WhatsApp consent. Its opener is disabled by default.
- Never use LeadMe public supplier insert/update endpoints. They can create
  unwanted leads in campaign 12277 (`הוסרו מ-Whatsapp`).
- Never fabricate LeadMe credentials, status IDs, or source attribution.
- Preserve lead, financial, and customer data.

## Required external configuration

LeadMe must send an accurate source value in one of `source`, `lead_source`,
or `origin` on the webhook. If its labels differ, set
`LEADME_ORGANIC_SOURCES` to the exact labels supplied by LeadMe.

For CRM writes, configure either a valid `LEADME_API_KEY` or a fresh
`LEADME_COOKIES_PATH` session. The queue retains pending status changes until
the CRM becomes reachable; it records `leadme_last_level` only after LeadMe
confirms the status update.

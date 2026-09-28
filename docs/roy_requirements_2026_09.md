# Roy's Confirmed Bot Requirements

Source: the supplied WhatsApp chat export with Roy, reviewed on 2026-09-28.
This document intentionally excludes credentials, personal contacts, and
unavailable voice/image content.

## LeadMe priority mapping

| Level | Meaning | When to assign |
|---|---|---|
| L1 | Highest priority | A call is booked by the bot, or LeadMe explicitly identifies the source as `אתר הבית`, `שיחה נכנסת`, or `דף נחיתה`. |
| L2 | Engaged | The lead meaningfully talks with the bot but does not book a call. |
| L3 | No bot engagement | A non-priority new lead has not meaningfully engaged with the bot. |

Lower number is higher priority. Levels only upgrade: L3 → L2 → L1.

The webhook may assign source-based L1 only when LeadMe sends a source label
that exactly matches `LEADME_LEVEL_1_SOURCES`. Missing or unknown source data
must never be guessed as L1.

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
`LEADME_LEVEL_1_SOURCES` to the exact labels supplied by LeadMe.

For CRM writes, configure either a valid `LEADME_API_KEY` or a fresh
`LEADME_COOKIES_PATH` session. The queue retains pending status changes until
the CRM becomes reachable; it records `leadme_last_level` only after LeadMe
confirms the status update.

# Roy's Corrections, October 2026

Source: Roy's messages of 2026-10-14 (six numbered corrections plus a
follow-up at 17:36), his knowledge-base corrections of 2026-08-17,
2026-09-02, 2026-09-27 and 2026-10-07, and two defects found in Eran's test
conversation of 2026-10-06. Supersedes nothing in
`docs/roy_requirements_2026_09.md`; it adds to it.

Each item names where the rule now lives, so a future change can find the
complaint it answers.

| Correction | Where it lives now |
|---|---|
| Never send the same video twice | `חוק הסרטון` + the two dedup layers in `app/agent/tools.py::send_video` |
| We DO help graduates find work | `חוק ההשמה`, `knowledge/faq/faq_from_leads.md` |
| No obligation to buy equipment | `חוק הציוד`, `knowledge/faq/faq_from_leads.md` |
| Record the delivered video in LeadMe | `סרטון · <title>` tag, `docs/leadme_integration.md` §7.1 |
| Over 80 active pilots, not 70 | prompt facts, `knowledge/company/hr_contact.md`, `knowledge/faq/faq_from_leads.md` |
| Triage a course question before answering | `חוק הניתוב` + `חוק הודעת פתיחה` |
| A campaign-entry reply must end in a question | `חוק הודעת פתיחה`, `graph._ensure_opening_question` |
| Vouchers, Dovrim, Employment Bureau, Ministry of Labour supervision | `חוק המימון`, `knowledge/funding/course_funding.md` |
| No military deposit (`פיקדון צבאי`) | same |
| Agriculture is flown with drones over 25 kg | `knowledge/licensing/flight_rules_and_licenses.md` |
| Duplicate opener in consecutive messages | `memory._looks_like_opener` + the greeting-only rule in `remove_repeated_outbound_content` |
| Raw `done` token leaked to a lead | `graph._drop_stray_token` |

## Open questions for Roy

- **The full list of funding bodies.** He named vouchers, Dovrim and the
  Employment Bureau, and ruled out the military deposit.
  `knowledge/funding/course_funding.md` lists exactly those and tells the bot
  to send anything else to an advisor rather than guess. Any additional body
  has to be added there, followed by a re-ingest.
- **What job-placement help may be promised.** The knowledge base claims
  connections to large companies, a graduates' WhatsApp group with job
  offers, and that selected graduates join Propeller itself. The bot is
  forbidden from promising a named job, a salary, or a named employer.

## Operational note

Knowledge changes do not reach the bot until Chroma is rebuilt. Deploy with
`./deploy/deploy.sh --ingest`; a deploy without it ships a stale index that
still answers with the old figures.

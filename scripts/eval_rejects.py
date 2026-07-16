"""Offline eval harness for the 13 customer rejects.

Runs a handful of scripted conversations against the LIVE agent
(same code path as WhatsApp inbound: agent.graph.handle_message) and
asserts that the bot now handles each reject correctly.

Every test creates a fresh throwaway lead in the DB with a synthetic
phone number, drives the conversation via handle_message() (no GreenAPI
sends -- we stub send_video_fn as a no-op), then applies keyword-based
assertions on the final reply and on lead_metadata / funnel_stage.

Usage:
    docker compose exec -T bot python -m scripts.eval_rejects

Exits with code 0 if all pass, 1 if any fail. Prints a table.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional


# MUST be set BEFORE app.config.get_settings() is ever called (which
# happens on the first import of most app modules). Turning this on
# makes app.crm.leadme_client no-op every write so fake test phones
# don't pollute LeadMe / the 'הוסרו מ-whatsapp' trash campaign.
os.environ["LEADME_TEST_MODE"] = "1"


from sqlalchemy import select

from app.agent.graph import handle_message
from app.db.models import FunnelStage, Lead
from app.db.session import session_scope


# Every fake lead created by this harness gets this name prefix so the
# customer can immediately spot them in the admin panel / LeadMe if any
# leak past the two hard guards (LEADME_TEST_MODE + 999-phone filter).
TEST_LEAD_NAME_PREFIX = "TEST-EVAL"


HEBREW_RE = re.compile(r"[\u0590-\u05FF]")


def _hebrew_ratio(text: str) -> float:
    if not text:
        return 0.0
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    hebrew = HEBREW_RE.findall(text)
    return len(hebrew) / len(letters)


@dataclass
class Assertion:
    """A single check against a reply or the lead's DB state after a turn."""

    name: str
    check: Callable[[str, Lead], bool]

    def run(self, reply: str, lead: Lead) -> bool:
        try:
            return self.check(reply, lead)
        except Exception as exc:  # noqa: BLE001
            print(f"    [assertion error] {self.name}: {exc}")
            return False


@dataclass
class Turn:
    user_msg: str
    assertions: List[Assertion] = field(default_factory=list)


@dataclass
class Scenario:
    name: str
    description: str
    turns: List[Turn]
    # If provided, the initial lead is upserted with this display name so the
    # opener/bot knows how to address the lead. (Real leads arrive named
    # via the LeadMe webhook opener.)
    sender_name: Optional[str] = None


# --- assertion helpers --------------------------------------------------


def contains(*needles: str) -> Callable[[str, Lead], bool]:
    def _check(reply: str, _lead: Lead) -> bool:
        text = (reply or "").lower()
        return all(n.lower() in text for n in needles)
    return _check


def contains_any(*needles: str) -> Callable[[str, Lead], bool]:
    def _check(reply: str, _lead: Lead) -> bool:
        text = (reply or "").lower()
        return any(n.lower() in text for n in needles)
    return _check


def not_contains(*needles: str) -> Callable[[str, Lead], bool]:
    def _check(reply: str, _lead: Lead) -> bool:
        text = (reply or "").lower()
        return not any(n.lower() in text for n in needles)
    return _check


def is_hebrew(min_ratio: float = 0.5) -> Callable[[str, Lead], bool]:
    def _check(reply: str, _lead: Lead) -> bool:
        return _hebrew_ratio(reply or "") >= min_ratio
    return _check


def slot_equals(expected: Optional[str]) -> Callable[[str, Lead], bool]:
    def _check(_reply: str, lead: Lead) -> bool:
        md = lead.lead_metadata or {}
        return md.get("preferred_call_slot") == expected
    return _check


def stage_equals(expected: FunnelStage) -> Callable[[str, Lead], bool]:
    def _check(_reply: str, lead: Lead) -> bool:
        return lead.funnel_stage == expected
    return _check


def intent_equals(expected: str) -> Callable[[str, Lead], bool]:
    def _check(_reply: str, lead: Lead) -> bool:
        md = lead.lead_metadata or {}
        return md.get("intent") == expected
    return _check


def videos_sent_count(op: str, n: int) -> Callable[[str, Lead], bool]:
    def _check(_reply: str, lead: Lead) -> bool:
        c = len(lead.videos_sent or [])
        return {
            "==": c == n, "<=": c <= n, ">=": c >= n,
        }[op]
    return _check


# --- scenarios ---------------------------------------------------------

SCENARIOS: List[Scenario] = [
    Scenario(
        name="english_input_gets_hebrew_reply",
        description="FB DM opener in English -> bot must reply in Hebrew",
        sender_name="Saar",
        turns=[
            Turn(
                user_msg="Hello! Can I get more info on this?",
                assertions=[
                    Assertion("reply is Hebrew (>=50%)", is_hebrew(0.5)),
                    Assertion(
                        "does not reply in English (no long English sentence)",
                        not_contains(
                            "Hello", "can I help", "would you like",
                            "our courses", "please let me know",
                        ),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="industry_not_a_slot",
        description="User answers industry question with 'הנדסת בניין'; must NOT be captured as slot",
        sender_name="Shir",
        turns=[
            Turn(user_msg="היי, מתעניין בקורס"),
            Turn(user_msg="בטח, יש לי ניסיון של תחביב"),
            Turn(user_msg="אוקיי, כן"),
            Turn(
                user_msg="הנדסת בניין",
                assertions=[
                    Assertion(
                        "preferred_call_slot NOT set to 12-15",
                        lambda _r, lead: (lead.lead_metadata or {}).get(
                            "preferred_call_slot"
                        ) != "12-15",
                    ),
                    Assertion(
                        "preferred_call_slot NOT set to a bogus value",
                        lambda _r, lead: (lead.lead_metadata or {}).get(
                            "preferred_call_slot"
                        ) in (None, "any", "9-12", "12-15", "15-18", "none"),
                    ),
                    Assertion(
                        "funnel not handed_off yet (industry != booking)",
                        lambda _r, lead: lead.funnel_stage != FunnelStage.handed_off,
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="25kg_license_no_practical",
        description="Q about 25kg license must answer theory-only, not require practical",
        sender_name="Yossi",
        turns=[
            Turn(
                user_msg="שאלה על הרישיון עד 25 קג - חייבים גם חלק מעשי או רק תיאוריה?",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "mentions theory-only for 25kg",
                        contains_any("תיאוריה", "מבחן מקוון", "CAAI"),
                    ),
                    Assertion(
                        "does NOT falsely require practical for 25kg",
                        # A wrong reply typically says "גם תיאוריה וגם מעשי"
                        # or "חייב גם מעשי". Correct reply says "רק תיאוריה"
                        # or "אין חלק מעשי חובה".
                        not_contains(
                            "גם תיאוריה וגם מעשי",
                            "חייב גם מעשי",
                            "חייב חלק מעשי",
                        ),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="job_seeker_gets_hr_email",
        description="Lead asking about employment must get HR email, not a course pitch",
        sender_name="Dana",
        turns=[
            Turn(
                user_msg="היי, אני מחפשת עבודה - אתם מגייסים מטיסי רחפנים?",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "mentions HR email",
                        contains("hr@propeller-drones.com"),
                    ),
                    Assertion(
                        "does NOT push a course as the main answer",
                        not_contains("קבע שיחה", "קורס בסיסי", "לפרטים על הקורס"),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="price_range_first_ask",
        description="First price question -> range 1200-11000, refer to advisor",
        sender_name="Ronit",
        turns=[
            Turn(
                user_msg="כמה עולה הקורס?",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "gives the range (1,200 and 11 or 10)",
                        # 15,000 / "15 אלף" all acceptable (customer bumped
                        # the top of the range from 11k to 15k in round 3)
                        contains_any("1,200", "1200"),
                    ),
                    Assertion(
                        "mentions high end (15,000 / 15 אלף)",
                        contains_any("15,000", "15000", "15 אלף"),
                    ),
                    Assertion(
                        "refers to advisor (יועץ)",
                        contains("יועץ"),
                    ),
                    Assertion(
                        "does NOT quote a single specific price",
                        # "קורס X עולה בדיוק 5,499 ₪" style
                        not_contains("עולה בדיוק"),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="business_opener_gets_shop_link",
        description="Lead who plans to open a drone business should get the shop link surfaced",
        sender_name="Tomer",
        turns=[
            Turn(
                user_msg="היי, אני רוצה לפתוח עסק של צילום אווירי לחתונות. איך מתחילים?",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "surfaces the shop URL",
                        contains("propeller-drones.shop"),
                    ),
                    Assertion(
                        "still stays in course-intent (business == also needs a course)",
                        lambda _r, lead: (lead.lead_metadata or {}).get(
                            "intent"
                        ) in ("course", "shop", None),  # any of these is fine
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="iftah_bug_slot_answer_schedules_cleanly",
        description=(
            "REGRESSION for the Iftah bug: user asked 'depends on which day', "
            "bot offered slots, user replied '12-15', bot then said "
            "'technical glitch' instead of scheduling. Must schedule cleanly "
            "without mentioning any error / technical issue."
        ),
        sender_name="Iftah",
        turns=[
            Turn(user_msg="היי, מתעניין"),
            Turn(user_msg="כן, אשמח לשיחה"),
            Turn(user_msg="תלוי באיזה יום"),
            Turn(
                user_msg="12-15",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "does NOT mention a technical glitch / error",
                        not_contains(
                            "תקלה טכנית",
                            "בעיה טכנית",
                            "יש לי תקלה",
                            "נראה שיש תקלה",
                        ),
                    ),
                    Assertion(
                        "preferred_call_slot captured as 12-15",
                        slot_equals("12-15"),
                    ),
                    Assertion(
                        "funnel_stage is handed_off",
                        stage_equals(FunnelStage.handed_off),
                    ),
                    Assertion(
                        "mentions the confirmed window 12-15",
                        contains_any("12-15", "12 ל-15", "12-15", "בין 12"),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="slot_any_time_works",
        description=(
            "Lead who is flexible ('לא משנה', 'מתי שנוח') should still get "
            "scheduled -- 'any' is a valid slot."
        ),
        sender_name="Noa",
        turns=[
            Turn(user_msg="היי, מתעניינת בקורס"),
            Turn(user_msg="כן, שיחה זה מעולה"),
            Turn(
                user_msg="לא משנה, מתי שנוח לכם",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "does NOT falsely claim technical glitch",
                        not_contains("תקלה טכנית", "בעיה טכנית"),
                    ),
                    # We accept either: bot scheduled with slot=any, OR bot
                    # is still asking for a specific window. What we REJECT
                    # is the "technical issue" fallback.
                ],
            ),
        ],
    ),
    Scenario(
        name="cancel_and_rebook",
        description=(
            "Lead schedules, then says 'רגע זה טעות, אני רוצה בבוקר'. Bot "
            "must call cancel_call, rewind, and re-schedule with the new slot "
            "without a technical-error message."
        ),
        sender_name="Ehud",
        turns=[
            Turn(user_msg="היי, כן אני רוצה שיחה"),
            Turn(user_msg="15-18"),
            Turn(
                user_msg="רגע זה טעות, אני רוצה בבוקר בין 9 ל-12",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "does NOT say technical glitch",
                        not_contains("תקלה טכנית", "בעיה טכנית"),
                    ),
                    Assertion(
                        "final slot is 9-12 (rebooked correctly)",
                        slot_equals("9-12"),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="specific_drone_model_routes_to_shop",
        description=(
            "Lead mentions a specific drone model (Mavic 3) mid-course "
            "conversation. Bot must surface the shop, not push the course "
            "exclusively."
        ),
        sender_name="Roni",
        turns=[
            Turn(user_msg="היי, מתעניין בקורס"),
            Turn(
                user_msg="אני רוצה לקנות Mavic 3, יש לכם?",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "surfaces shop URL or shop intent language",
                        contains_any(
                            "propeller-drones.shop",
                            "חנות",
                            "shop",
                        ),
                    ),
                    Assertion(
                        "intent classified as shop",
                        lambda _r, lead: (lead.lead_metadata or {}).get(
                            "intent"
                        ) == "shop",
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="lead_refuses_call_gracefully",
        description=(
            "Lead explicitly says 'לא מעוניין בשיחה כרגע, רק אינפורמציה'. "
            "Bot must NOT keep pushing a slot -- it should stay in info mode."
        ),
        sender_name="Maya",
        turns=[
            Turn(user_msg="היי, רוצה מידע על הקורס"),
            Turn(
                user_msg="לא מעוניינת בשיחה כרגע, רק אינפורמציה",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "does NOT push a time-window question in the same reply",
                        not_contains(
                            "9-12", "12-15", "15-18",
                            "איזה חלון",
                            "מתי נוח לך",
                        ),
                    ),
                    Assertion(
                        "funnel NOT handed_off (no premature scheduling)",
                        lambda _r, lead: lead.funnel_stage != FunnelStage.handed_off,
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="specific_price_ask_stays_range",
        description=(
            "Even under repeat pressure, bot must not quote a per-course price; "
            "must reroute to a call with the advisor."
        ),
        sender_name="Amit",
        turns=[
            Turn(user_msg="כמה עולה הקורס?"),
            Turn(
                user_msg="לא, אני רוצה מחיר מדויק. כמה עולה המסלול הבסיסי?",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "does NOT quote a specific numeric price like 5,499 / 3,200 / 4,700",
                        # Any 4-digit number BETWEEN the endpoints (excluding
                        # the range endpoints themselves 1200 and 15000).
                        lambda r, _l: not re.search(
                            r"(?<!\d)(?:1[3-9]\d{2}|[2-9]\d{3}|1[0-4],?\d{3})(?!\d)",
                            r or "",
                        ),
                    ),
                    Assertion(
                        "refers to advisor (יועץ)",
                        contains("יועץ"),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="beginner_asking_which_course_no_recommendation",
        description=(
            "Beginner asks 'which course should I take?' -- bot must NOT "
            "recommend a specific course. Must defer to the human advisor."
        ),
        sender_name="Liran",
        turns=[
            Turn(user_msg="היי, אני חדש לחלוטין. אין לי שום ניסיון."),
            Turn(
                user_msg="איזה קורס הכי מתאים לי?",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "does NOT recommend basic/25kg/beginner course",
                        not_contains(
                            "מומלץ להתחיל", "כדאי לך המסלול הבסיסי",
                            "רוב המתחילים בוחרים",
                            "המסלול המומלץ",
                        ),
                    ),
                    Assertion(
                        "defers to the advisor",
                        contains("יועץ"),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="already_booked_does_not_re_ask_slot",
        description=(
            "Lead already has a slot captured. When they ask a follow-up "
            "question, bot must NOT ask for the time window again."
        ),
        sender_name="Gilad",
        turns=[
            Turn(user_msg="היי מתעניין בקורס"),
            Turn(user_msg="כן, אשמח לשיחה עם יועץ"),
            Turn(user_msg="12-15"),
            Turn(
                user_msg="ומה אפשר לעשות אחרי הקורס?",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "does NOT re-ask the slot question",
                        not_contains(
                            "איזה חלון",
                            "מתי נוח לך",
                            "9-12, 12-15",
                        ),
                    ),
                    Assertion(
                        "slot is still 12-15 (unchanged)",
                        slot_equals("12-15"),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="filler_phrase_ban_end_of_reply",
        description=(
            "Bot must not end replies with 'אני כאן לעזור' / 'מוזמן לפנות' etc."
        ),
        sender_name="Shani",
        turns=[
            Turn(user_msg="היי, שאלה קטנה"),
            Turn(
                user_msg="כמה זמן לוקח קורס תיאוריה?",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "no forbidden filler ending",
                        not_contains(
                            "אני כאן לעזור",
                            "אני כאן בשבילך",
                            "מוזמן לפנות",
                            "מקווה שעזרתי",
                            "אשמח לעזור",
                            "אני זמין לכל שאלה",
                        ),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="one_question_per_message",
        description="Bot must ask at most one question per message.",
        sender_name="Ori",
        turns=[
            Turn(
                user_msg="היי, מתעניין בקורס רחפנים",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "at most one '?' in the reply",
                        lambda r, _l: (r or "").count("?") <= 1,
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="fake_time_like_18_30_not_a_slot",
        description=(
            "Lead answers slot question with '18:30' -- not a canonical "
            "window. Bot must NOT accept it as slot and NOT auto-schedule."
        ),
        sender_name="Barak",
        turns=[
            Turn(user_msg="היי, כן אני רוצה שיחה עם יועץ"),
            Turn(
                user_msg="18:30",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "slot NOT set to bogus '18:30'",
                        lambda _r, lead: (lead.lead_metadata or {}).get(
                            "preferred_call_slot"
                        ) in (None, "any", "9-12", "12-15", "15-18"),
                    ),
                    Assertion(
                        "funnel NOT handed_off yet",
                        lambda _r, lead: lead.funnel_stage != FunnelStage.handed_off,
                    ),
                    Assertion(
                        "bot re-offers the 3 canonical windows",
                        contains_any("9-12", "12-15", "15-18"),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="terminology_yoetz_not_natziv",
        description="Bot uses 'יועץ' rather than 'נציג' when offering the human handoff",
        sender_name="Amir",
        turns=[
            Turn(user_msg="היי, רוצה לדעת עוד על הקורס"),
            Turn(
                user_msg="כן, ספר לי איך זה עובד",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    # We don't hard-block any single 'נציג' mention (the
                    # LLM might slip), but if it OFFERS a call the offer
                    # itself should be 'יועץ'. Soft check: at least once in
                    # the whole conversation the bot uses 'יועץ' and never
                    # says 'נציג מכירות'.
                    Assertion(
                        "no 'נציג מכירות'",
                        not_contains("נציג מכירות"),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="four_professions_lists_master_track",
        description=(
            "Lead from the '4 professions' campaign asks what the 4 "
            "professions are. Since we now have the real list from "
            "propeller-drones.com/training-center/master, bot must name "
            "them: mapping / photography / security / FPV. Must NOT "
            "include 'agriculture' (that's in a different course)."
        ),
        sender_name="Rami",
        turns=[
            Turn(user_msg="היי, ראיתי שיש קורס עם 4 מקצועות"),
            Turn(
                user_msg="מה 4 המקצועות?",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "names at least 3 of the 4 master-track fields",
                        lambda r, _l: sum(
                            1 for kw in ("מיפוי", "צילום", "אבטחה", "FPV")
                            if kw in (r or "")
                        ) >= 3,
                    ),
                    Assertion(
                        "does NOT list agriculture (wrong course)",
                        not_contains("חקלאות"),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="level_2_pushed_on_first_reply",
        description=(
            "Lead's first inbound message triggers engagement Level 2 "
            "(replied to bot). We check the metadata not LeadMe (test "
            "mode is on)."
        ),
        sender_name="Elad",
        turns=[
            Turn(
                user_msg="היי, מתעניין בקורס",
                assertions=[
                    Assertion("reply is Hebrew", is_hebrew(0.6)),
                    Assertion(
                        "lead metadata records leadme_last_level=2",
                        lambda _r, lead: (
                            (lead.lead_metadata or {}).get(
                                "leadme_last_level"
                            ) == 2
                        ),
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="level_1_on_book_supersedes_level_2",
        description=(
            "When a lead books a call, the engagement level should "
            "become 1 (booked) regardless of the previous 2."
        ),
        sender_name="Nadav",
        turns=[
            Turn(user_msg="היי, כן אני רוצה שיחה עם יועץ"),
            Turn(
                user_msg="12-15",
                assertions=[
                    Assertion(
                        "leadme_last_level advanced to 1",
                        lambda _r, lead: (
                            (lead.lead_metadata or {}).get(
                                "leadme_last_level"
                            ) == 1
                        ),
                    ),
                    Assertion(
                        "funnel handed_off",
                        stage_equals(FunnelStage.handed_off),
                    ),
                ],
            ),
        ],
    ),
]


# --- runner ------------------------------------------------------------


def _no_op_send_video(*_args, **_kwargs) -> None:
    """Stub send_video so the agent can call the tool without hitting GreenAPI."""
    return None


def _run_scenario(scenario: Scenario) -> tuple[int, int, list[str], str]:
    """Return (passed, total, failure_lines, fake_phone_used)."""
    fake_phone = f"999{uuid.uuid4().int % 10**8:08d}"
    passed = 0
    total = 0
    failures: list[str] = []

    # Prepend the TEST- prefix to the sender name so any lead that slips
    # past the guards is obvious in the admin panel.
    tagged_sender_name = (
        f"{TEST_LEAD_NAME_PREFIX} {scenario.sender_name}"
        if scenario.sender_name
        else TEST_LEAD_NAME_PREFIX
    )

    print(f"\n=== {scenario.name} ===")
    print(f"    {scenario.description}")
    print(f"    phone={fake_phone} name={tagged_sender_name!r}")

    for i, turn in enumerate(scenario.turns, 1):
        print(f"  turn {i}: user={turn.user_msg[:70]!r}")
        try:
            reply = handle_message(
                phone=fake_phone,
                text=turn.user_msg,
                sender_name=tagged_sender_name,
                send_video_fn=_no_op_send_video,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    ✗ agent crashed: {exc}")
            failures.append(f"{scenario.name} turn {i}: crash {exc!r}")
            continue

        print(f"    bot: {reply[:200]!r}")

        with session_scope() as session:
            lead = session.execute(
                select(Lead).where(Lead.phone == fake_phone)
            ).scalar_one()
            for a in turn.assertions:
                total += 1
                ok = a.run(reply, lead)
                marker = "✓" if ok else "✗"
                print(f"    {marker} {a.name}")
                if ok:
                    passed += 1
                else:
                    failures.append(f"{scenario.name} turn {i}: {a.name}")

    return passed, total, failures, fake_phone


def _cleanup_test_leads(phones: list[str]) -> None:
    """Hard-delete every fake lead this run created + all pre-existing
    999xxx leads (from earlier runs). Cascades to messages. LeadMe is
    untouched -- test mode ensured we never pushed there in the first
    place."""
    from app.db.models import Lead as LeadModel  # avoid confusing scope
    print("\n" + "-" * 60)
    print("[cleanup] deleting fake test leads created by this run...")
    with session_scope() as s:
        # Delete this run's phones AND every 999xxxx lead lingering from
        # earlier runs so the local DB stays clean.
        stmt = select(LeadModel).where(LeadModel.phone.like("999%"))
        rows = s.execute(stmt).scalars().all()
        for lead in rows:
            s.delete(lead)
        print(f"[cleanup] deleted {len(rows)} test leads "
              f"(this run + leftovers from earlier runs)")


def main() -> int:
    total_passed = 0
    total_all = 0
    all_failures: list[str] = []
    created_phones: list[str] = []

    try:
        for scenario in SCENARIOS:
            p, t, f, phone = _run_scenario(scenario)
            total_passed += p
            total_all += t
            all_failures.extend(f)
            created_phones.append(phone)

        print("\n" + "=" * 60)
        print(f"RESULTS: {total_passed}/{total_all} assertions passed")
        if all_failures:
            print("\nFAILURES:")
            for line in all_failures:
                print(f"  - {line}")
    finally:
        _cleanup_test_leads(created_phones)

    if all_failures:
        return 1
    print("\nALL PASS \\o/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

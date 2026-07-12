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

import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from sqlalchemy import select

from app.agent.graph import handle_message
from app.db.models import FunnelStage, Lead
from app.db.session import session_scope


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
                        # 11,000 or "11 אלף" or "10,800" all acceptable
                        contains_any("1,200", "1200"),
                    ),
                    Assertion(
                        "mentions high end (11,000 / 10,800 / 11 אלף)",
                        contains_any("11,000", "11000", "11 אלף", "10,800", "10800"),
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
]


# --- runner ------------------------------------------------------------


def _no_op_send_video(*_args, **_kwargs) -> None:
    """Stub send_video so the agent can call the tool without hitting GreenAPI."""
    return None


def _run_scenario(scenario: Scenario) -> tuple[int, int, list[str]]:
    """Return (passed, total, failure_lines)."""
    fake_phone = f"999{uuid.uuid4().int % 10**8:08d}"
    passed = 0
    total = 0
    failures: list[str] = []

    print(f"\n=== {scenario.name} ===")
    print(f"    {scenario.description}")
    print(f"    phone={fake_phone}")

    for i, turn in enumerate(scenario.turns, 1):
        print(f"  turn {i}: user={turn.user_msg[:70]!r}")
        try:
            reply = handle_message(
                phone=fake_phone,
                text=turn.user_msg,
                sender_name=scenario.sender_name,
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

    return passed, total, failures


def main() -> int:
    total_passed = 0
    total_all = 0
    all_failures: list[str] = []

    for scenario in SCENARIOS:
        p, t, f = _run_scenario(scenario)
        total_passed += p
        total_all += t
        all_failures.extend(f)

    print("\n" + "=" * 60)
    print(f"RESULTS: {total_passed}/{total_all} assertions passed")
    if all_failures:
        print("\nFAILURES:")
        for line in all_failures:
            print(f"  - {line}")
        return 1

    print("\nALL PASS \\o/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

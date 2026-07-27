"""Seed the local DB with fake leads for development/testing.

Run from the repo root:
    DATABASE_URL=postgresql+psycopg2://propeller:propeller@localhost:5432/propeller_bot \
    python -m scripts.seed_dev

Or if postgres port is exposed in docker-compose (uncomment the ports line), the
DATABASE_URL above will work. Otherwise run inside the container:
    docker compose exec bot python -m scripts.seed_dev
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from app.db.models import Base, FamiliarityLevel, FunnelStage, Lead, Message, MessageRole
from app.db.session import engine, session_scope

LEADS = [
    {
        "name": "יוסי כהן",
        "phone": "972501111111",
        "stage": FunnelStage.new,
        "familiarity": FamiliarityLevel.beginner,
        "leadme_level": 1,
        "call_slot": None,
        "msgs": [
            ("user", "היי, ראיתי מודעה על דרון לצילום, מה המחיר?"),
            ("assistant", "שלום יוסי! שמח שפנית 😊 יש לנו כמה דגמים, תלוי בשימוש שמתכנן. לצילום אירועים או נוף?"),
            ("user", "אירועים בעיקר"),
        ],
    },
    {
        "name": "מיכל לוי",
        "phone": "972502222222",
        "stage": FunnelStage.engaged,
        "familiarity": FamiliarityLevel.aware,
        "leadme_level": 2,
        "call_slot": "9-12",
        "msgs": [
            ("user", "שלום, אני מעוניינת בדרון למשק חקלאי"),
            ("assistant", "היי מיכל! בשמחה. לצרכי חקלאות יש לנו דגמים עם ריסוס ומיפוי. כמה דונמים את מנהלת בערך?"),
            ("user", "בערך 80 דונם"),
            ("assistant", "מצוין! עבור 80 דונם הייתי ממליץ על הדגם X4 Pro עם סוללה מורחבת. רוצה שנשלח לך סרטון הדגמה?"),
            ("user", "כן בטח"),
            ("assistant", "שלחתי! 🎬 אם יש שאלות אחרי הצפייה, כאן אני."),
        ],
    },
    {
        "name": "דני אברהם",
        "phone": "972503333333",
        "stage": FunnelStage.warm,
        "familiarity": FamiliarityLevel.experienced,
        "leadme_level": 2,
        "call_slot": "15-18",
        "msgs": [
            ("user", "יש לי כבר DJI Mavic 3, מחפש משהו יותר מקצועי לצילום תעשייתי"),
            ("assistant", "אחלה בסיס! ל-Mavic 3 הוא מצוין לצרכן, אבל לתעשייתי אנחנו עובדים עם פלטפורמות שאפשר להחליף מצלמות ולהוסיף חיישנים תרמיים. מה סוג הפרויקט?"),
            ("user", "בדיקות תשתית, קווי חשמל"),
            ("assistant", "פרפקט לזה. יש לנו פתרון עם מצלמה תרמית + RGB בו-זמנית. רוצה שנדבר בשיחה קצרה כדי שאציג לך את המערכת?"),
            ("user", "סבבה, אני זמין בין 3 ל-6 אחה\"צ"),
        ],
    },
    {
        "name": "רחל גולן",
        "phone": "972504444444",
        "stage": FunnelStage.ready_for_call,
        "familiarity": FamiliarityLevel.aware,
        "leadme_level": 3,
        "call_slot": "12-15",
        "msgs": [
            ("user", "אני רוצה לרכוש דרון לצילום נדל\"ן, יש לכם מה שמתאים?"),
            ("assistant", "כן בהחלט! לנדל\"ן הדגם הכי פופולרי אצלנו הוא ה-Sky4K עם ייצוב 4 צירים. מחיר כולל הדרכה. רוצה לשמוע פרטים?"),
            ("user", "כן, מה הצעד הבא?"),
            ("assistant", "נשמח לתאם שיחה קצרה של 15 דק' עם אחד מהיועצים שלנו. מתי נוח לך?"),
            ("user", "מחר בצהריים בסדר"),
            ("assistant", "מעולה! רשמתי. נחזור אלייך בבוקר לאישור. 🤝"),
        ],
    },
    {
        "name": "אבי שפירא",
        "phone": "972505555555",
        "stage": FunnelStage.handed_off,
        "familiarity": FamiliarityLevel.experienced,
        "leadme_level": 3,
        "call_slot": "9-12",
        "msgs": [
            ("user", "קיבלתי המלצה עליכם מחבר, אני צריך 3 דרונים לחברת הבנייה שלי"),
            ("assistant", "שמחים לשמוע! הפנייה של חבר היא הכבוד הגדול ביותר. 3 יחידות — בהחלט עסקת ציי. נעביר אותך לנציג מכירות שיתן הצעת מחיר מותאמת."),
            ("user", "תודה"),
        ],
        "muted": True,
    },
    {
        "name": "נועה ברק",
        "phone": "972506666666",
        "stage": FunnelStage.new,
        "familiarity": FamiliarityLevel.unknown,
        "leadme_level": 1,
        "call_slot": None,
        "msgs": [
            ("user", "שלום"),
            ("assistant", "שלום! אני הבוט של פרופלור דרונס 🚁 במה אוכל לעזור?"),
        ],
    },
]


def seed() -> None:
    Base.metadata.create_all(engine)

    now = datetime.now(timezone.utc)

    with session_scope() as s:
        existing_phones = {r[0] for r in s.execute(
            __import__("sqlalchemy").text("SELECT phone FROM leads")
        ).fetchall()}

        created = 0
        skipped = 0

        for i, data in enumerate(LEADS):
            if data["phone"] in existing_phones:
                print(f"  skip  {data['phone']} ({data['name']}) — already exists")
                skipped += 1
                continue

            lead = Lead(
                phone=data["phone"],
                name=data["name"],
                funnel_stage=data["stage"],
                familiarity_level=data["familiarity"],
                bot_muted=data.get("muted", False),
                lead_metadata={
                    **({"leadme_last_level": data["leadme_level"]} if data.get("leadme_level") else {}),
                    **({"preferred_call_slot": data["call_slot"]} if data.get("call_slot") else {}),
                },
                created_at=now - timedelta(days=len(LEADS) - i),
            )
            s.add(lead)
            s.flush()  # get lead.id

            for j, (role_str, content) in enumerate(data.get("msgs", [])):
                role = MessageRole.user if role_str == "user" else MessageRole.assistant
                msg = Message(
                    lead_id=lead.id,
                    role=role,
                    content=content,
                    created_at=now - timedelta(days=len(LEADS) - i, minutes=len(data["msgs"]) - j),
                )
                s.add(msg)

            lead.last_message_at = now - timedelta(days=len(LEADS) - i, minutes=0)
            print(f"  add   {data['phone']} ({data['name']}) — {data['stage'].value}")
            created += 1

    print(f"\nסיים: {created} נוצרו, {skipped} דולגו.")


if __name__ == "__main__":
    seed()

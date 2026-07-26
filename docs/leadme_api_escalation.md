# בקשה ל-API רשמי מ-LeadMe

מסמך זה כולל טיוטת אימייל בעברית, גרסה באנגלית, וסקריפט שיחת טלפון בעברית לפנייה מסודרת ל-LeadMe עם בקשה לגישת API רשמית לניהול לידים (שינוי סטטוסים, הוספת תגיות). המטרה: להפסיק את התלות בעוגיות של דפדפן.

## למי לפנות

1. **תמיכה טכנית של LeadMe** — הפורטל הרגיל. סביר שתתחיל שם.
2. **מנהל חשבון / איש קשר עסקי** — אם יש. עדיף להצליב עם השיחה הטכנית.
3. **החברה המשלמת** — אם הם משתמשי-על ומשלמים סכום משמעותי, יש להם יותר משקל לבקשות מותאמות.

## הודעה בעברית (אימייל / וואטסאפ / טופס)

**נושא:** בקשה לגישת API רשמית לשינוי סטטוס והוספת תגיות ללידים

שלום,

אני עובד/ת עם [שם החברה] שהם לקוחות של LeadMe. אנחנו מפתחים אינטגרציה שמעדכנת סטטוסים ומוסיפה תגיות ללידים קיימים ב-LeadMe באופן אוטומטי (חלק ממערכת בוט שיווקי בוואטסאפ).

בדקנו את ה-API הציבורי של LeadMe וזיהינו שהוא מכסה בעיקר שני שימושים:

1. `POST /supplier/insert/{link_id}/{slug}` — הכנסת לידים חדשים ממקורות חיצוניים.
2. `POST /supplier/update/p/{slug}` — עדכון של לידים קיימים.

הבעיה: שני ה-endpoints הללו מתנהגים כ-UPSERT. כאשר טלפון של ליד לא נמצא בקמפיין שמוגדר לספק שלנו, המערכת יוצרת רשומה כפולה בקמפיין `הוסרו מ-Whatsapp` (מזהה 12277) שהוא קמפיין ה"אשפה" של החשבון. כתוצאה מכך צברנו עשרות רשומות כפולות שם, שלא ניתן היה למנוע בצד הלקוח בלי לעצור לגמרי את השימוש ב-API הציבורי.

כדי לעקוף את זה, אנחנו נאלצים כרגע להשתמש ב-endpoints הפנימיים של ממשק הניהול שלכם עם עוגיית `PHPSESSID` ו-`csrf_lmcms` שאנחנו שומרים מדפדפן מחובר:

- `POST /app/leads/changeLeadsStatus` — שינוי סטטוס פנימי.
- `POST /app/ajax/addLeadTag` — הוספת תגית.
- `POST /app/ajax/deleteLeads` — מחיקת ליד.
- `POST /app/ajax4/getPieData` — קריאת נתוני קמפיין.

זו פתרון עוקף (workaround) שאנחנו לא שמחים איתו, מסיבות שאני בטוח שאתם מכירים:

- עוגיית ה-CSRF פוגה כל 24 שעות (Max-Age=86400) — חייבים לרענן אותה יומית.
- כל שינוי במבנה של דפי הניהול ישבור אותנו בלי התראה.
- אימות מבוסס-דפדפן (reCAPTCHA + סשן) לא מיועד לשימוש אוטומטי.
- זה מייצר עומס מיותר על שרתי הניהול שלכם.

**מה נשמח לקבל:**

גישה רשמית ב-API עם token/API key בכותרת (Header) לפעולות הבאות על לידים **קיימים** (לא יצירת חדשים):

1. **שינוי סטטוס** של ליד לפי מזהה LeadMe שלו + מזהה סטטוס מספרי (rel).
2. **הוספת תגית** טקסטואלית לליד.
3. **קריאה** של סטטוס נוכחי + תגיות של ליד לפי מספר טלפון או מזהה.

אלה שלוש פעולות שהמערכת הפנימית שלכם כבר מבצעת בכל דף ניהול — אנחנו לא מבקשים תכונות חדשות, רק דרך נקייה להשתמש בהן בלי לחקות דפדפן.

אם יש תוכנית תשלום שכוללת גישה כזו, נשמח לשמוע. אם זה משהו שאתם יכולים לפתוח לחשבון ספציפי, גם זה מצוין. אשמח לקבוע שיחה טכנית קצרה כדי להסביר את הצורך.

תודה,
[שם]
[טלפון]
[חברה]

## English version (in case they escalate to a technical/dev-team contact)

**Subject:** Request for official API access to update lead status and tags

Hi,

I'm working with [company name], a LeadMe customer. We've built a WhatsApp lead-warming bot that needs to update status and add tags on existing LeadMe leads programmatically.

We reviewed the public API and found two supplier endpoints:

1. `POST /supplier/insert/{link_id}/{slug}` — insert new leads from external sources.
2. `POST /supplier/update/p/{slug}` — update existing leads.

Both behave as UPSERTs: when a phone isn't in our supplier's linked campaign, they silently create a duplicate lead in the account's default campaign (ID 12277 = "הוסרו מ-Whatsapp", the "trash" bucket). This produced dozens of duplicates in a campaign the customer explicitly doesn't want any activity in.

As a workaround, we're currently driving your internal admin endpoints using a saved browser `PHPSESSID` + CSRF cookie:

- `POST /app/leads/changeLeadsStatus`
- `POST /app/ajax/addLeadTag`
- `POST /app/ajax/deleteLeads`
- `POST /app/ajax4/getPieData`

We're not happy with this for the obvious reasons:

- The CSRF cookie has `Max-Age=86400` and expires every 24 hours.
- Any UI change breaks us without warning.
- Browser-session auth isn't intended for automation.
- It generates unnecessary load on your admin backend.

**What we'd love to have:**

Token/API-key based access (header auth) to three read/write actions on **existing** leads (not creating new ones):

1. **Change status** by LeadMe lead ID + numeric status rel ID.
2. **Add a tag** (free-text string) to a lead.
3. **Read** the current status + tags for a lead by phone or ID.

These are things your own UI already does on every admin page — we're not asking for new features, just a clean way to use them without impersonating a browser.

If this is behind a specific pricing tier, we're happy to hear the options. If it can be enabled per-account, that works too. I'm happy to schedule a short technical call.

Thanks,
[Name] · [Company] · [Phone]

## סקריפט שיחת טלפון (אם עונים)

1. "היי, אני מ-[חברה] ואנחנו לקוחות של LeadMe. יש לנו בעיה טכנית עם ה-API הציבורי — אפשר להעביר אותי לצוות הפיתוח או למישהו שמכיר אותו לעומק?"
2. אחרי שמעבירים: "אנחנו משתמשים ב-`/supplier/update` בשביל לעדכן סטטוסים על לידים קיימים, אבל הוא מייצר רשומות כפולות בקמפיין 12277 אצל הלקוח שלנו כי הוא מתנהג כ-upsert. בגלל זה נאלצנו לעבור לעבודה עם endpoints של ממשק הניהול הפנימי שלכם דרך עוגיות של דפדפן מחובר. אני רוצה לדעת אם יש דרך רשמית לשינוי סטטוס והוספת תגיות דרך API עם token."
3. שאלות עזר:
   - "האם יש תכנית תשלום שכוללת API-key ל-endpoints הפנימיים?"
   - "האם יש טוקן שמזוהה עם חשבון ספק (supplier) שאפשר להעביר בכותרת Authorization?"
   - "האם `/app/leads/changeLeadsStatus` ו-`/app/ajax/addLeadTag` יקבלו token במקום cookie session?"
   - "מי הטכני שיוכל לדבר איתי על זה?" (חשוב לדעת את השם ואת מייל / טלפון ישיר)

## מה לעשות אחרי הפנייה

1. תעד ב-`docs/leadme_api_escalation.md` את התאריך + עם מי דיברו + מה נאמר.
2. אם ענו "לא / אין דבר כזה" — נעדכן את המסמך הזה כדי לא לנסות שוב באותו רבעון.
3. אם ענו "אולי / נחזור אליך" — לקבוע מועד למעקב (7-14 יום).
4. אם ענו "כן" — תעד את הפרטים הטכניים (endpoint, header format, איך מקבלים token). ואז נסגור את `app/crm/leadme_login.py` ונחליף ב-`app/crm/leadme_api_client.py` נקי.

## מה קיים היום כעוקף (workaround)

בזמן שמחכים לתגובה מ-LeadMe, יש לנו:

- **מנגנון תור פנימי** (`app/crm/leadme_queue.py`) — כל פוש שלא הצליח נכנס לתור ומנוסה שוב עד שעה. שורד ריסטארט של הקונטיינר.
- **בדיקת בריאות סשן** (`check_leadme_session_health`) — יודעת כשעוגיות מתות ומעלה התראה בלוגים.
- **ריענון עוגיות אוטומטי** (`app/crm/leadme_login.py`) — התחברות מלאה דרך 2Captcha כאשר הסשן מת. חוסך את הצורך להתחבר ידנית כל 24 שעות. עולה סנטים בחודש.
- **פאנל אדמין** ב-`/admin/leadme-status` — כפתור "רענן עכשיו" ל-refresh יזום.
- **חסימת קמפיין 12277** — הקוד מסרב לכתוב שם, וטריפוויר מזהה כל גדילה חשודה.

הכל יימחק אם/כאשר LeadMe יתנו API אמיתי.

"""The standalone FPV course must stay distinct from the מטיס מאסטר module.

The customer rejected a reply that called "קורס FPV" part of קורס מטיס מאסטר.
The fix lives in the knowledge base, the ingest topic and the prompt; nothing
else in the suite covers those, so this locks all three ends of it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from app.agent.prompts import SYSTEM_PROMPT_TEMPLATE
from app.rag.ingest import KNOWLEDGE_DIR, load_local_documents


SYLLABI = Path("knowledge/courses/course_syllabi.md")
FOUR_PROFESSIONS = Path("knowledge/courses/course_4_professions.md")


class FpvCourseKnowledgeTests(unittest.TestCase):
    def test_syllabus_describes_the_standalone_six_session_course(self) -> None:
        text = SYLLABI.read_text(encoding="utf-8")

        self.assertIn("# קורס: FPV אזרחי", text)
        self.assertIn("שישה מפגשים", text)
        # The syllabus requires an existing license. The first draft of this
        # document claimed the opposite.
        self.assertIn("רישיון פנאי וספורט", text)
        self.assertNotIn("הסילבוס לא מציב רישיון כתנאי סף", text)

    def test_both_fpv_documents_name_which_product_they_describe(self) -> None:
        for path in (SYLLABI, FOUR_PROFESSIONS):
            with self.subTest(path=str(path)):
                text = path.read_text(encoding="utf-8")
                self.assertIn("קורס FPV אזרחי", text)
                self.assertIn("מתכוון לקורס העצמאי", text)

    def test_the_fpv_syllabus_is_ingested_under_the_course_syllabus_topic(self) -> None:
        topics = {
            doc.metadata["topic"]
            for doc in load_local_documents(KNOWLEDGE_DIR)
            if "# קורס: FPV אזרחי" in doc.page_content
        }

        self.assertEqual(topics, {"course_syllabus"})

    def test_the_prompt_carries_the_fpv_law_in_both_places(self) -> None:
        summary, marker, detail = SYSTEM_PROMPT_TEMPLATE.partition(
            "## עקרונות שיחה קריטיים"
        )

        self.assertTrue(marker, "the rules-summary block moved")
        # Rule 13 since 2026-10-15: the summary block carried two rules
        # numbered 12, and the duplicate was resolved by letting the
        # conversation-memory rule keep 12.
        self.assertIn('13. **"קורס FPV" הוא הקורס העצמאי.**', summary)
        self.assertIn("### חוק קורס ה-FPV", detail)


if __name__ == "__main__":
    unittest.main()

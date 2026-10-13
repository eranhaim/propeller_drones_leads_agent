"""The webhook must acknowledge every delivery LeadMe makes.

LeadMe's external interface fires at record intake, before the source, the
campaign and the tags exist, and it calls a delivery failed after five
seconds. Every rejection below used to be a 422 and a lost lead.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.webhook.server import app


class WebhookAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        secret = patch(
            "app.webhook.server.get_settings",
            return_value=type("S", (), {"webhook_secret": "s3cret"})(),
        )
        secret.start()
        self.addCleanup(secret.stop)

        self.enqueued = []
        enqueue = patch(
            "app.webhook.server._enqueue_lead",
            side_effect=self.enqueued.append,
        )
        enqueue.start()
        self.addCleanup(enqueue.stop)

    def post(self, body=None, **kwargs):
        return self.client.post("/webhook/leadme/s3cret", json=body, **kwargs)

    def test_a_bare_phone_is_accepted(self) -> None:
        # The normal case: nothing but a phone, because LeadMe has not
        # written the rest of the record yet.
        response = self.post({"phone": "052-123-4567"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "accepted")
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual(self.enqueued[0].phone, "972521234567")

    def test_hebrew_field_names_are_read(self) -> None:
        response = self.post(
            {
                "phone": "0521234567",
                "מקור הגעה": "lp",
                "סטטוס": "עמוד נחיתה אקדמיה מובייל",
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.enqueued[0].source_type, "lp")
        self.assertEqual(self.enqueued[0].tags, ("עמוד נחיתה אקדמיה מובייל",))

    def test_an_empty_payload_is_acknowledged_not_rejected(self) -> None:
        response = self.post({})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")
        self.assertEqual(self.enqueued, [])

    def test_an_unparseable_body_is_acknowledged(self) -> None:
        response = self.client.post(
            "/webhook/leadme/s3cret",
            content=b"\xff\xfe not json",
            headers={"content-type": "application/json"},
        )

        self.assertEqual(response.status_code, 200)

    def test_a_form_encoded_delivery_is_accepted(self) -> None:
        response = self.client.post(
            "/webhook/leadme/s3cret",
            data={"phone": "0521234567", "campaignId": "12284"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.enqueued[0].campaign, "12284")

    def test_a_bad_secret_is_still_refused(self) -> None:
        response = self.client.post(
            "/webhook/leadme/wrong", json={"phone": "0521234567"}
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.enqueued, [])


if __name__ == "__main__":
    unittest.main()

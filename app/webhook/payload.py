"""Canonical, safe subset of a LeadMe external-webhook payload."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


def _key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                return _values(json.loads(text))
            except json.JSONDecodeError:
                pass
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        return [
            text
            for item in value
            for text in _values(item)
        ]
    text = str(value).strip()
    return [text] if text else []


def _first(payload: Mapping[str, Any], names: Iterable[str]) -> str:
    wanted = {_key(name) for name in names}
    for field_name, value in payload.items():
        if _key(field_name) not in wanted:
            continue
        values = _values(value)
        if values:
            return values[0]
    return ""


def _all(payload: Mapping[str, Any], names: Iterable[str]) -> list[str]:
    wanted = {_key(name) for name in names}
    values: list[str] = []
    for field_name, value in payload.items():
        if _key(field_name) in wanted:
            values.extend(_values(value))
    return list(dict.fromkeys(values))


def flatten_payload(raw: Any) -> dict[str, Any]:
    """Accept a lead object or LeadMe's common single-object wrappers."""
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return dict(raw[0])
    if not isinstance(raw, dict):
        return {}

    for wrapper in ("data", "lead", "leadData"):
        inner = raw.get(wrapper)
        if isinstance(inner, dict):
            merged = dict(raw)
            merged.pop(wrapper, None)
            merged.update(inner)
            return merged
    return dict(raw)


def normalize_phone(raw: str) -> str:
    """Return Israeli phones as E.164 digits without ``+``."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("00972"):
        digits = digits[2:]
    if digits.startswith("972"):
        return "972" + digits[3:].lstrip("0")
    if digits.startswith("0"):
        return "972" + digits.lstrip("0")
    if 8 <= len(digits) <= 9:
        return "972" + digits.lstrip("0")
    return digits


def is_valid_whatsapp_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"9725\d{8}", phone))


@dataclass(frozen=True)
class LeadMeWebhookPayload:
    phone: str
    name: str
    campaign: str
    source_type: str
    tags: tuple[str, ...]
    facebook_lead_id: str

    def metadata(self) -> dict[str, Any]:
        """Return only non-sensitive source metadata for repository merging."""
        values: dict[str, Any] = {
            "leadme_campaign": self.campaign,
            "leadme_campaign_id": self.campaign,
            "leadme_source_type": self.source_type,
            "leadme_source": self.source_type,
            "leadme_tags": list(self.tags),
            "leadme_facebook_lead_id": self.facebook_lead_id,
        }
        return {key: value for key, value in values.items() if value not in ("", [])}


def normalize_leadme_payload(raw: Any) -> LeadMeWebhookPayload:
    """Normalize the configured LeadMe field names and their array variants."""
    payload = flatten_payload(raw)
    first_name = _first(payload, ("firstname", "first_name", "givenname"))
    last_name = _first(payload, ("lastname", "last_name", "surname", "familyname"))
    name = _first(payload, ("fullname", "full_name", "name"))
    if not name:
        name = " ".join(part for part in (first_name, last_name) if part)

    return LeadMeWebhookPayload(
        phone=normalize_phone(
            _first(payload, ("phone", "phoneNumber", "phone_number", "tel", "mobile"))
        ),
        name=name,
        campaign=_first(payload, ("campaign", "campaignId", "campaign_id")),
        source_type=_first(
            payload,
            (
                "sourceType",
                "source_type",
                "source",
                "arrival source",
                "arrivalSource",
            ),
        ),
        tags=tuple(_all(payload, ("tags", "tags[]", "tag"))),
        facebook_lead_id=_first(
            payload,
            (
                "Facebook Lead id",
                "Facebook Lead ID",
                "facebookLeadId",
                "facebook_lead_id",
            ),
        ),
    )

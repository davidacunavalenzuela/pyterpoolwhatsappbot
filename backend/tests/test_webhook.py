"""Pruebas de la normalización del webhook (dedupe y filtrado)."""

from __future__ import annotations

from app.api.webhook import _extract_events
from app.config import settings


def _payload(phone_number_id: str = "1387086514492751") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [{"profile": {"name": "Ana"}, "wa_id": "56940437942"}],
                            "messages": [
                                {
                                    "from": "56940437942",
                                    "id": "wamid.TEST1",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": "hola"},
                                }
                            ],
                            "statuses": [
                                {"id": "wamid.TEST1", "status": "delivered", "recipient_id": "569"}
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_extract_events_creates_one_row_per_message_and_status(monkeypatch):
    monkeypatch.setattr(settings, "wa_phone_number_id", "1387086514492751")
    events = _extract_events(_payload())

    kinds = sorted(event["kind"] for event in events)
    assert kinds == ["message", "status"]
    message_event = next(e for e in events if e["kind"] == "message")
    assert message_event["wa_message_id"] == "wamid.TEST1"
    assert message_event["payload"]["message"]["text"]["body"] == "hola"


def test_extract_events_status_key_includes_state(monkeypatch):
    """Un mismo wamid tiene estados distintos; no deben deduplicarse entre sí."""
    monkeypatch.setattr(settings, "wa_phone_number_id", "1387086514492751")
    events = _extract_events(_payload())
    status_event = next(e for e in events if e["kind"] == "status")
    assert status_event["wa_message_id"] == "wamid.TEST1:delivered"


def test_extract_events_ignores_other_phone_numbers(monkeypatch):
    monkeypatch.setattr(settings, "wa_phone_number_id", "1387086514492751")
    assert _extract_events(_payload(phone_number_id="999999")) == []


def test_extract_events_handles_empty_payload():
    assert _extract_events({}) == []
    assert _extract_events({"entry": [{"changes": [{"value": {}}]}]}) == []
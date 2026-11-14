"""Prueba del prompt de sistema: los datos del negocio deben llegar al LLM."""

from __future__ import annotations

from app.models import Customer
from app.services.agent import _system_prompt


def test_system_prompt_includes_business_and_customer():
    customer = Customer(wa_id="56940437942", name="Ana")
    app_settings = {
        "business": {
            "name": "Café Aurora",
            "currency": "CLP",
            "hours": "Lun a Vie 9-18",
            "description": "Café de especialidad",
            "delivery": "Solo dentro de la ciudad",
        },
        "agent": {
            "tone": "cercano",
            "language": "español de Chile",
            "handoff_message": "Te derivo con una persona.",
        },
        "sales": {"orders_enabled": True, "require_confirmation": True},
    }

    prompt = _system_prompt(customer, app_settings, "56940437942")

    assert "Café Aurora" in prompt
    assert "Ana" in prompt
    assert "español de Chile" in prompt
    assert "56940437942" in prompt
    # El número de tools está acotado por Groq; el prompt debe declararlo.
    assert "herramienta" in prompt.lower()


def test_system_prompt_handles_missing_settings():
    customer = Customer(wa_id="56900000000", name=None)
    prompt = _system_prompt(customer, {}, "56900000000")
    assert isinstance(prompt, str)
    assert len(prompt) > 100
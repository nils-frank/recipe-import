"""Bildweg der LLM-Stufe (A12, DESIGN.md §6), ohne Netzzugriff: der HTTP-Aufruf wird
ersetzt, geprüft wird die Nachricht, die hinausgehen würde, und die Behandlung einer
Antwort ohne Rezept.

Echte Bildkosten fallen erst im Integrationstest A10 an (recipe-import-plan.md,
Auftragsdetails A12, Punkt 7).
"""
from __future__ import annotations

import json

import pytest

import app as app_module
import llm
from conftest import FIXTURES_DIR

JPEG_BYTES = (FIXTURES_DIR / "document_rezeptfoto.jpg").read_bytes()

VALID_ANSWER = json.dumps({
    "name": "Kartoffelsuppe mit Majoran",
    "recipeIngredient": ["800 g Kartoffeln", "1 l Gemüsebrühe"],
    "recipeInstructions": ["Kartoffeln schälen.", "In der Brühe garen."],
    "recipeYield": "4 Portionen",
    "totalTime": "PT45M",
    "description": None,
    "recipeCategory": [],
    "url": None,
})

EMPTY_ANSWER = json.dumps({
    "name": "",
    "recipeIngredient": [],
    "recipeInstructions": [],
    "recipeYield": None,
    "totalTime": None,
    "description": None,
    "recipeCategory": [],
    "url": None,
})


def test_images_go_out_as_data_uris(monkeypatch):
    sent = []

    def fake_post(messages, timeout=llm.TIMEOUT_SECONDS):
        sent.append((messages, timeout))
        return VALID_ANSWER

    monkeypatch.setattr(llm, "_post", fake_post)

    recipe = llm.extract_recipe_from_images([JPEG_BYTES], "datei:foto.jpg")

    assert recipe.name == "Kartoffelsuppe mit Majoran"
    assert recipe.url == "datei:foto.jpg"

    messages, timeout = sent[0]
    assert timeout == llm.IMAGE_TIMEOUT_SECONDS
    parts = messages[1]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_unknown_image_format_is_an_llm_error(monkeypatch):
    monkeypatch.setattr(llm, "_post", lambda messages, timeout=None: VALID_ANSWER)

    with pytest.raises(llm.LlmError):
        llm.extract_recipe_from_images([b"nicht wirklich ein Bild"], "datei:x.jpg")


def test_no_recipe_on_the_image_yields_the_spec_message(monkeypatch):
    monkeypatch.setattr(llm, "_post", lambda messages, timeout=None: EMPTY_ANSWER)

    with pytest.raises(llm.NoRecipeFoundError) as exc_info:
        llm.extract_recipe_from_images([JPEG_BYTES], "datei:foto.jpg")

    assert str(exc_info.value) == "Auf dem Bild war kein Rezept zu erkennen."
    # Und app.py gibt genau diesen Wortlaut weiter, statt ihn in "Die Rezepterkennung
    # ist gescheitert: ..." zu verpacken (DESIGN.md §7).
    assert app_module._describe_source_error(exc_info.value) == (
        "Auf dem Bild war kein Rezept zu erkennen."
    )

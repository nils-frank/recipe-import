"""Tests für sources/site.py: die drei Extraktionsstufen an aufgezeichneten Fixtures
(DESIGN.md §12, Punkt 3). `requests.get` wird durch die Fixture ersetzt, kein Netzzugriff.

Fixtures, einmalig live aufgezeichnet (siehe recipe-import/tests/fixtures/):

- `site_scrapers_emmikochteinfach.html`: von `recipe-scrapers` unterstützte Domain.
- `site_jsonld_backenmachtgluecklich.html`: Domain ohne `recipe-scrapers`-Unterstützung,
  aber mit eingebettetem JSON-LD vom Typ Recipe (im `@graph`-Block).
- `site_text_wikipedia.html`: weder von `recipe-scrapers` unterstützt noch mit
  JSON-LD vom Typ Recipe - Fliesstext-Fallback über `trafilatura`.
"""
from __future__ import annotations

from pathlib import Path

from sources import site

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass


def _serve_fixture(monkeypatch, fixture_name: str) -> None:
    html = (FIXTURES / fixture_name).read_text(encoding="utf-8")

    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(html)

    monkeypatch.setattr(site.requests, "get", fake_get)


def test_scrapers_stage_hits_at_its_fixture(monkeypatch):
    _serve_fixture(monkeypatch, "site_scrapers_emmikochteinfach.html")
    result = site.fetch("https://emmikochteinfach.de/basilikum-pesto/")

    assert result.stage == "scrapers"
    assert result.text is None
    assert result.recipe is not None
    assert result.recipe.name
    assert result.recipe.recipeIngredient
    assert result.recipe.recipeInstructions


def test_jsonld_stage_hits_at_its_fixture(monkeypatch):
    _serve_fixture(monkeypatch, "site_jsonld_backenmachtgluecklich.html")
    result = site.fetch("https://www.backenmachtgluecklich.de/rezepte/apfelkuchen-mit-quark.html")

    assert result.stage == "jsonld"
    assert result.text is None
    assert result.recipe is not None
    assert result.recipe.recipeIngredient
    assert result.recipe.recipeInstructions


def test_text_stage_hits_at_its_fixture(monkeypatch):
    _serve_fixture(monkeypatch, "site_text_wikipedia.html")
    result = site.fetch("https://de.wikipedia.org/wiki/Spaghetti_Carbonara")

    assert result.stage == "text"
    assert result.recipe is None
    assert result.text is not None
    assert "Carbonara" in result.text

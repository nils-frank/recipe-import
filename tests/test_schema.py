"""Tests für schema.py (DESIGN.md §12, Punkt 4)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from schema import Recipe, to_jsonld


def _valid_kwargs(**overrides):
    kwargs = dict(name="Testrezept", recipeIngredient=["1 Ei"], recipeInstructions=["Verquirlen."])
    kwargs.update(overrides)
    return kwargs


def test_recipe_accepts_minimal_valid_data():
    recipe = Recipe(**_valid_kwargs())
    assert recipe.name == "Testrezept"
    assert recipe.recipeCategory == []
    assert recipe.url is None


def test_recipe_rejects_blank_name():
    with pytest.raises(ValidationError):
        Recipe(**_valid_kwargs(name="   "))


def test_recipe_rejects_empty_ingredient_list():
    with pytest.raises(ValidationError):
        Recipe(**_valid_kwargs(recipeIngredient=[]))


def test_recipe_rejects_ingredient_list_of_only_blanks():
    with pytest.raises(ValidationError):
        Recipe(**_valid_kwargs(recipeIngredient=["   ", ""]))


def test_recipe_rejects_empty_instructions_list():
    with pytest.raises(ValidationError):
        Recipe(**_valid_kwargs(recipeInstructions=[]))


def test_to_jsonld_adds_schema_org_envelope():
    recipe = Recipe(**_valid_kwargs())
    data = to_jsonld(recipe)
    assert data["@context"] == "https://schema.org"
    assert data["@type"] == "Recipe"
    assert data["name"] == "Testrezept"
    assert data["recipeIngredient"] == ["1 Ei"]

"""Rezept-Datenmodell, siehe DESIGN.md Abschnitt 4.

Bewusst nur die Felder, die Mealie beim JSON-Import auch verwertet. Die
Feldnamen folgen absichtlich schema.org/Recipe, damit `to_jsonld` ohne
Umbenennen auskommt. `Recipe.model_json_schema()` ist die einzige
Schema-Definition im ganzen Projekt - die LLM-Stufe (llm.py) speist ihr
`response_format` direkt daraus statt das Schema ein zweites Mal im Prompt
zu wiederholen.
"""
from __future__ import annotations

from pydantic import BaseModel, field_validator


class Recipe(BaseModel):
    name: str
    recipeIngredient: list[str]
    recipeInstructions: list[str]
    recipeYield: str | None = None
    totalTime: str | None = None               # ISO 8601, z.B. "PT45M"
    description: str | None = None
    recipeCategory: list[str] = []
    url: str | None = None                      # Quell-URL

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name darf nicht leer sein")
        return stripped

    @field_validator("recipeIngredient")
    @classmethod
    def _ingredients_not_empty(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip() for v in value if v and v.strip()]
        if not cleaned:
            raise ValueError("recipeIngredient braucht mindestens einen Eintrag")
        return cleaned

    @field_validator("recipeInstructions")
    @classmethod
    def _instructions_not_empty(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip() for v in value if v and v.strip()]
        if not cleaned:
            raise ValueError("recipeInstructions braucht mindestens einen Eintrag")
        return cleaned


def to_jsonld(recipe: Recipe) -> dict:
    """Recipe als schema.org/Recipe-JSON-LD, wie Mealies Import-Endpunkt es erwartet."""
    data = recipe.model_dump()
    data["@context"] = "https://schema.org"
    data["@type"] = "Recipe"
    return data

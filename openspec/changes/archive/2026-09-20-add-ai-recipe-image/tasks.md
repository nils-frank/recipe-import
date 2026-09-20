# Tasks

> Stand 2026-09-20: 1.1 und 1.2 sind an der laufenden Anlage gemessen, die Ergebnisse
> stehen mit Datum im Moduldocstring von `mealie_client.py`. 1.2 hat die Annahme hinter
> 2.2 widerlegt - Mealies Feld `image` trägt auch ohne Bild eine Kennung -, deshalb fragt
> `has_image()` jetzt die Mediendatei ab; DESIGN.md und design.md sind nachgezogen.
>
> 1.3 war mit dem Gemini-Schlüssel blockiert (alle Bildmodelle 429 "free_tier ... limit:
> 0", auch mit einem Schlüssel aus einem frisch angelegten Projekt - das Kontingent ist
> auf Kontoebene null). Die Stufe erzeugt ihre Bilder deshalb bei **Pollinations.ai**:
> ein GET ohne Schlüssel und ohne Konto, Antwortkörper ist das Bild. Gemessen am
> 2026-09-20: 200 image/jpeg, 768x768, 46-73 KB, 35-46 s, nur das Modell `sana`, zwei
> gleichzeitige Anfragen 429, `nologo`/`width`/`height` ohne Token wirkungslos - jedes
> Bild trägt ein Wasserzeichen. Der lange Prompt aus 4.1 (761 Zeichen) lieferte dabei
> einen leeren Teller mit Pseudo-Schrift, ein kompakter Satz ein brauchbares Bild.
>
> Abschnitt 9 ist damit erledigt: Code, Tests und Dokumentation kennen beide
> Anbieterformen, die Testreihe im Container `recipe-import:local` zählt 147 grüne Tests.
>
> Ausgerollt am 2026-09-20 auf dem Zielhost: `src/` kopiert (Sicherung unter
> `src.bak-20260920-imagestage`), `docker compose up -d --build`, Container healthy.
> Die `.env` dort trägt seither ein Token von auth.pollinations.ai in `IMAGE_API_KEY`
> (Sicherung `.env.bak-20260920-image`); zweite Probe desselben Tages: das Token wird
> erkannt und macht `width`/`height` wirksam, hebt das Wasserzeichen aber **nicht** auf.
> Alle Texte, die "beides" behaupteten, sind nachgezogen.
>
> 8.2 ist damit erledigt. 8.1 war am selben Tag zunächst **angehalten**: der
> Textmodell-Weg antwortete abwechselnd 503 ("This model is currently experiencing high
> demand") und 429 ("generate_content_free_tier_requests, limit: 20" je Minute), sieben
> Anläufe über 15 Minuten scheiterten schon vor der Bildstufe. Das liegt an Gemini, nicht
> an dieser Änderung - eine direkte Probe mit demselben Schlüssel und demselben Schema kam
> zwischendurch mit 200 zurück.
>
> 8.1 ist am 2026-09-20 spätabends nachgeholt und **erledigt**. Zwei Befunde dazu:
>
> 1. Das fest verdrahtete `gemini-3.6-flash` beantwortete jede Anfrage mit 429 ("You
>    exceeded your current quota"), `gemini-3.8-flash` in derselben Minute mit 200 -
>    derselbe Befund, der `add-llm-model-fallback` ausgelöst hat. Weil dieser Zweig noch
>    nicht ausgerollt ist, trägt die `.env` auf dem Zielhost seither
>    `LLM_MODEL=gemini-3.8-flash` (Sicherung `.env.bak-20260920-task81`); sobald
>    `LLM_MODEL_CHAIN` dort läuft, kann das Pin wieder weg.
> 2. Das erzeugte Bild zeigt nicht zuverlässig das Gericht: für "Schnelle Nougatplunder"
>    (Plunderteig, Nougatcreme, Haselnüsse) lieferte `sana` eine Tarte mit Spiegelei. Der
>    Prompt nennt Namen und Zutaten wie vorgesehen, die Stufe hat also getan, was das SPEC
>    verlangt - die Trefferquote des freien Modells ist eine eigene Frage, kein Befund
>    dieser Änderung.

## 1. Live verification before any code depends on it

- [x] 1.1 Probe Mealie's image endpoint against the running instance: `curl -s
  $MEALIE_URL/openapi.json | jq -r '.paths | keys[]' | grep -i image` plus the schema of
  the matching path, and confirm method, multipart field names, required extension field
  and success status. Verified when the exact request shape and expected status are
  written down for task 2.1 with the probe date.
- [x] 1.2 Probe what Mealie reports for a recipe without a picture: `GET
  /api/recipes/{slug}` for one recipe created via `create/html-or-json` and one that the
  scraper gave an image, and compare the image field. Verified when the "no image" value
  (null, empty string or sentinel) is written down for task 2.2 with the probe date.
- [x] 1.3 Probe the image provider: confirm the exact endpoint, the model id to pin and
  the response shape. Verified when one real generated image has been produced and both
  are written down for tasks 3.1 and 4.2. **Done 2026-09-20 in two steps:** the
  OpenAI-compatible route on the configured Gemini key is impossible (no image quota on
  the account), and Pollinations.ai was measured instead - see the note above and the
  provider decision in design.md.

## 2. Mealie client

- [x] 2.1 Add `mealie_client.set_image(slug, data)`: multipart PUT with the
  `Authorization` header only (not the JSON `_HEADERS`), extension derived from the image
  bytes, raising `MealieError` on a non-success status. Record the task 1.1 probe with its
  date in the module docstring, in the style of the existing entries. Verified by a unit
  test that monkeypatches `requests.put` and asserts the URL, the multipart fields and
  that a 4xx raises `MealieError`.
- [x] 2.2 Add `mealie_client.has_image(recipe: dict) -> bool` next to `is_placeholder`,
  using the task 1.2 finding, with the probe recorded in the docstring. Verified by unit
  tests over both response shapes from the probe.

## 3. Configuration

- [x] 3.1 Add `IMAGE_ENABLED` (default true, same truthiness parsing as
  `NAMING_ENABLED`), `IMAGE_MODEL` (pinned id from task 1.3), `IMAGE_BASE_URL` (defaults
  to `LLM_BASE_URL`) and `IMAGE_API_KEY` (defaults to `LLM_API_KEY`) to `src/config.py`,
  none via `require()`. Verified by importing `config` with none of the four set and
  seeing the text-model values come through, and by the suite still starting.
- [x] 3.2 Document the four values in `.env.example` under the optional block, including
  the cost note and that the stage can be switched off. Verified by reading the file: an
  operator can switch the stage off and point it at another provider without reading the
  code.

## 4. Generation stage

- [x] 4.1 Add the image prompt to `src/prompts.py`: English, finished dish on a plate,
  no text, no watermark, no logo, no people, with placeholders for name, ingredients and
  steps. Verified by a unit test asserting the rendered prompt contains the recipe name
  and neither a stray format placeholder nor an empty ingredient block.
- [x] 4.2 Create `src/image.py` with `generate(name, ingredients, instructions) -> bytes
  | None`: returns `None` when `IMAGE_ENABLED` is false, single POST to
  `{IMAGE_BASE_URL}/images/generations` with no retry, reads `b64_json` and falls back to
  fetching `url`, validates the bytes with `llm._image_mime`, bounds the request via
  `naming._shorten`, and never raises - every failure logs a warning and returns `None`.
  Verified by unit tests for: success, stage off, provider error status, unreachable
  provider, empty/unexpected response body, and bytes that are not a known image type.

## 5. Pipeline wiring

- [x] 5.1 Wire the stage into `app._publish()` after `store.finish()` and before
  `set_tags`/`notify`: decide "needs a picture" from `slug is None` on entry, else from
  `has_image(mealie_data)`, and skip with a log line when `mealie_data is None`. On a
  successful upload extend the tag list to `["auto-import", "ki-bild"]`. Wrap the whole
  stage so no exception can escape it. Verified by the tests in 6.2.
- [x] 5.2 Confirm no image call happens on the duplicate path (`_notify_if_done`) or on
  any failure path before `store.finish()`. Verified by a test that runs a repeated
  import and asserts the generation seam was never called.

## 6. Tests

- [x] 6.1 Add an autouse `image_off` fixture to `tests/conftest.py` setting
  `config.IMAGE_ENABLED = False`, mirroring `naming_off`, with the same reasoning in its
  docstring. Verified by `pytest` staying green and making no network call.
- [x] 6.2 Add `tests/test_image.py` covering the spec scenarios end to end through
  `_publish` with HTTP monkeypatched: picture generated and uploaded plus both tags set;
  recipe that already has an image producing no generation call and keeping its image;
  provider failure, unusable answer and rejected upload each leaving the entry `done`,
  the notification unchanged and only the `auto-import` tag. Verified by `pytest
  tests/test_image.py` passing.
- [x] 6.3 Run the whole suite and the linter and fix anything that breaks, including
  pre-existing failures found along the way. Verified by `pytest` green.

## 7. Documentation

- [x] 7.1 Extend `DESIGN.md`: a `src/image.py` module section in §6 next to the naming
  stage, the four new values in §3, the new step in the §5 flow, and the tag in the
  Mealie write-up. Verified by reading §5 top to bottom: the flow matches what the code
  does.
- [x] 7.2 Update `README.md`: the numbered "How it decides" list gains the picture step,
  and the stack line mentions image generation. Verified by reading the list against the
  implemented flow.

## 8. Real-world check

- [x] 8.1 Run one real import of a source with no picture (a photographed recipe page is
  the cheapest) against the live Mealie, and confirm in the Mealie UI: the recipe shows a
  generated picture, carries both `auto-import` and `ki-bild`, the push notification is
  unchanged in wording, and the tile in the recipe grid no longer looks broken or
  half-loaded at the sizes Mealie renders.
  **Done 2026-09-20** with a photographed recipe page (full-page capture of
  `gutekueche.at/schnelle-nougatplunder-rezept-6013`, 325 KB JPEG) via `POST
  /import/file`. Log: `LLM-Bilderkennung ... Modell gemini-3.8-flash`, one 503 retry,
  `create_from_jsonld -> slug=schnelle-nougatplunder`, `Bilderzeugung ... bei
  pollinations, Modell sana`, `Bild ... erzeugt: 66197 Bytes, image/jpeg`, `PUT
  .../image`, `PATCH ... tags=['auto-import', 'ki-bild']`. Mealie shows the recipe with
  5 ingredients, 4 steps, both tags, and the generated picture on the detail page and as
  a full-bleed grid tile at both rendered sizes - nothing broken or half-loaded. The push
  went out without a warning from `ha_notify` (it only logs failures) and its wording is
  the unchanged `"Rezept angelegt"` + title + link. The naming stage hit a 503 and kept
  the extracted name, which is its documented behaviour and not part of this change.
- [x] 8.2 Run one real import of a scrapable site that brings its own photo and confirm
  the photo is untouched, no `ki-bild` tag is set, and the log shows no image call.
  **Done 2026-09-20** with `https://www.gutekueche.de/kartoffelsuppe-rezept-2129` (the
  page serves a brownie recipe, the slug is misleading): Mealie scraped it, the log
  reads `Rezept brownies hat bereits ein Bild, keine Bilderzeugung`, then
  `PATCH ... tags=['auto-import']`. No call to the image provider, the scraped photo
  untouched.

## 9. Second image provider (Pollinations.ai)

Follows the 1.3 probe. Everything here is behind `IMAGE_PROVIDER`; the OpenAI-compatible
path stays as it is and keeps its tests.

- [x] 9.1 Add `IMAGE_PROVIDER` to `src/config.py` (`pollinations` by default, `openai`
  the only other accepted value, unknown values fall back to the default with a warning),
  and make `IMAGE_MODEL`, `IMAGE_BASE_URL` and `IMAGE_API_KEY` default per provider:
  `sana` / `https://image.pollinations.ai` / empty for pollinations, and the previous
  `LLM_*` fallbacks for openai. Record the 1.3 probe with its date in the comments, in the
  style of the existing entries. Verified by importing `config` with none of the values
  set and seeing the pollinations defaults with an empty image key, and with
  `IMAGE_PROVIDER=openai` seeing the `LLM_*` values come through.
- [x] 9.2 Replace the long image prompt in `src/prompts.py` with the compact one the
  probe validated: one sentence naming the dish and its main ingredients, plating,
  daylight, slightly from above, closing with no text and no logo. Drop the preparation
  steps from the prompt. Verified by a unit test asserting the rendered prompt contains
  the recipe name, stays under 400 characters for a normal recipe, and carries no stray
  format placeholder.
- [x] 9.3 Split `image.generate()` into the shared part (switch, prompt, size and MIME
  checks, never raises) and two transports: `_fetch_pollinations()` doing
  `GET {IMAGE_BASE_URL}/prompt/{quoted prompt}` and returning the response body as image
  bytes, sending `Authorization: Bearer` only when an image key is set; and the existing
  `_fetch_openai_compatible()`. Verified by unit tests for both transports: success, the
  prompt arriving urlencoded in the path, no Authorization header without a key, a 429
  and an unreachable provider each returning `None`.
- [x] 9.4 Update the prose: `.env.example` (the new value, the watermark, the 768x768,
  that no key is needed and what a token changes), `DESIGN.md` §3 and the `src/image.py`
  section in §6, and the module docstring of `src/image.py` with the dated probe.
  Verified by reading `.env.example` alone: an operator can switch provider and switch the
  stage off without reading the code.
- [x] 9.5 Run the whole suite and the linter inside the container image on the Pi
  (`recipe-import:local`, the Mac has Python 3.9). Verified by `pytest` green, including
  the tests that existed before this section.

# recipe-import — SPEC

Verbindliche Schnittstellenbeschreibung. Einzige Kontextquelle für die Agenten A1 bis A14
aus `../recipe-import-plan.md`.

Regel: Was hier steht, wird umgesetzt. Was hier nicht steht, wird nicht erfunden, sondern
im Bericht als offene Frage gemeldet. Zwei Stellen sind ausdrücklich als
**"live verifizieren"** markiert, dort ist Nachschlagen an der laufenden Anlage Pflicht.

Stand: 2026-08-24.

---

## 1. Zweck

Was aus dem Teilen-Menü von iPhone oder Mac kommt, erzeugt ohne weitere Interaktion ein
Rezept in Mealie. Rückmeldung per Push und in der Home-Assistant-Oberfläche.

Drei Eingangsarten, ein Kurzbefehl (§9):

| Eingang | Weg | Quelle |
|---|---|---|
| Rezeptseite im Web | HA-Webhook, `POST /import` | `sources/site.py` |
| YouTube-Video | HA-Webhook, `POST /import` | `sources/youtube.py` |
| Bild (Foto einer Rezeptseite, Screenshot) | direkt, `POST /import/file` | `sources/document.py` plus Bildmodell |
| PDF | direkt, `POST /import/file` | `sources/document.py` |

**Nicht-Ziele in Phase 1:** kein Whisper, keine Audio- oder Videoanalyse, kein
Instagram (gestrichen am 2026-08-23, siehe Plan §2.10), kein Freitext-Eingang, kein Reverse Proxy, kein HTTPS, keine Weboberfläche, kein
Mehrbenutzerbetrieb.

"Keine Queue" ist am 2026-09-20 aus dieser Liste gestrichen worden (Änderung A20,
`openspec/changes/add-transient-retry-queue`). Es bleibt bei einem Prozess ohne Broker
und ohne zweiten Container: neu ist allein, dass ein Fehlschlag, bei dem die Gegenstelle
selbst "später nochmal" sagt, mit einer Fälligkeit in derselben SQLite-Tabelle geparkt
und von einer Hintergrundschleife erneut versucht wird, statt den Menschen zu bitten,
dieselbe Quelle noch einmal zu teilen. Siehe §5, §6 `src/store.py` / `src/app.py` und
§7.

---

## 2. Laufzeit

- Python 3.12, Basis-Image `python:3.12-slim-bookworm`, Ziel `linux/arm64`.
- Gebaut wird lokal als `recipe-import:local`. Kein Registry-Tag.
- Kein Compiler im Image. Alle Abhängigkeiten müssen als arm64-Wheel verfügbar sein.
  Ist das für ein Paket nicht der Fall, im Bericht melden statt einen Build-Stage
  hinzuzufügen.

`requirements.txt`, exakt gepinnt (Versionen von A4 zur Bauzeit auf die dann aktuellen
Stände festzulegen, danach unverändert):

```
fastapi
uvicorn[standard]
pydantic
requests
recipe-scrapers
extruct
trafilatura
yt-dlp
pypdf
python-multipart
```

`pypdf` liest den Text aus PDF-Dateien (reines Python, kein Compiler),
`python-multipart` ist die Voraussetzung dafür, dass FastAPI Datei-Uploads annimmt.

`pytest` gehört in eine separate `requirements-dev.txt` und nicht ins Image.

**Ein Container, sonst nichts.** Der gesamte Dienst läuft in genau einem Container: kein
Python auf dem Host, kein zweiter Worker, kein Sidecar, kein Host-Cron, keine auf dem
Host installierte Fremdsoftware. Zustand gibt es nur unter `/data` (Bind-Mount) und
`/tmp` (tmpfs). Braucht ein Schritt ein Systemwerkzeug, kommt es ins Image oder der
Schritt entfällt. Deshalb ruft `sources/youtube.py` kein `--convert-subs srt` mehr auf:
diese Umwandlung braucht `ffmpeg`, das im Basis-Image fehlt und für reine Textumwandlung
rund 400 MB kosten würde. Der Untertitelparser liest WebVTT (das Format, das YouTube
liefert) und SRT.

---

## 3. Konfiguration

Alles über Umgebungsvariablen, gelesen in `config.py`, sonst nirgends. Stil wie
`deal-scraper/src/llm_escalate.py`: `os.environ.get` mit Vorgabewert, plus eine
`require(name)`-Hilfe, die beim Start hart abbricht, wenn ein Pflichtwert fehlt.

| Variable | Pflicht | Vorgabe | Zweck |
|---|---|---|---|
| `MEALIE_URL` | nein | `http://localhost:30081` | |
| `MEALIE_TOKEN` | ja | – | API-Token `recipe-import` |
| `HA_URL` | nein | `http://localhost:8123` | Rückmeldung, LAN-Weg |
| `HA_TOKEN` | ja | – | Long-Lived Access Token |
| `HA_NOTIFY_TARGET` | ja | – | Name des `notify.*`-Kanals in HA, ohne den `notify.`-Praefix |
| `LLM_BASE_URL` | nein | `https://generativelanguage.googleapis.com/v1beta/openai` | wechselt später auf den LiteLLM-Proxy |
| `LLM_MODEL` | nein | `gemini-3.6-flash` | das bevorzugte Textmodell; ausdrücklich gesetzt wird es zum Kopf der Kette |
| `LLM_MODEL_CHAIN` | nein | `gemini-3.8-flash, gemini-3.7-flash, gemini-3.6-flash, gemini-3.5-flash` | die Kette selbst, neuestes zuerst; gesetzt schlägt sie `LLM_MODEL` |
| `LLM_MODEL_COOLDOWN_SECONDS` | nein | `3600` | so lange wird ein als erschöpft erkanntes Modell übersprungen |
| `LLM_MODEL_AUTODISCOVER` | nein | `true` | übernimmt neuere Modelle des Anbieters von selbst, siehe unten |
| `LLM_MODEL_PATTERN` | nein | `^gemini-(\d+)\.(\d+)-flash$` | welche Namen der Modellliste überhaupt in Frage kommen |
| `LLM_MODEL_REFRESH_SECONDS` | nein | `86400` | Abstand zwischen zwei Durchläufen der Modellsuche |
| `LLM_API_KEY` | ja | – | |
| `DB_PATH` | nein | `/data/recipe-import.db` | |
| `RATE_LIMIT_PER_HOUR` | nein | `20` | harte Obergrenze, siehe §11 |
| `IMPORT_TOKEN` | ja | – | Bearer-Token für `POST /import/file`, siehe §6 und §11 |
| `MAX_UPLOAD_MB` | nein | `12` | Obergrenze je Anfrage über alle Dateien |
| `MAX_UPLOAD_FILES` | nein | `4` | mehr Bilder als das sind ein Fehler, kein Abschneiden |
| `MAX_PDF_PAGES` | nein | `10` | mehr Seiten werden nicht gelesen, siehe §6 |
| `NAMING_ENABLED` | nein | `true` | Namensstufe, siehe §5 und §6 `naming.make_name` |
| `IMAGE_ENABLED` | nein | `true` | Bildstufe, siehe §5 und §6 `image.generate` |
| `IMAGE_MODEL_CHAIN` | nein | `gemini:gemini-3-pro-image, gemini:gemini-3.1-flash-image, pollinations:sana` | geordnete Kette `Anbieter:Modell`, bestes zuerst (A22); unbrauchbarer Eintrag fällt mit Warnung weg |
| `IMAGE_MODEL_COOLDOWN_SECONDS` | nein | `3600` | so lange wird ein Kandidat übersprungen, der Kontingent oder Guthaben als leer gemeldet hat |
| `IMAGE_DEADLINE_SECONDS` | nein | `150` | Zeitbudget der **ganzen** Bildstufe je Import, nicht eines Aufrufs |
| `IMAGE_PROVIDER` | nein | `pollinations` | `gemini`, `openai` oder `pollinations`; unbekannter Wert fällt auf die Vorgabe zurück. Ausdrücklich gesetzt, wandert das Paar mit `IMAGE_MODEL` an die Spitze der Kette |
| `IMAGE_MODEL` | nein | `sana` (bzw. `gemini-3-pro-image` / `imagen-4.0-generate-001`) | fest verdrahtet, ein Modellwechsel ist hier eine bewusste Änderung |
| `IMAGE_BASE_URL` | nein | Vorgabe des Anbieters aus `IMAGE_PROVIDER` | überschreibt **nur** dessen Eintrag, nicht die der übrigen Anbieter |
| `IMAGE_API_KEY` | nein | leer (bzw. `LLM_API_KEY`) | bei `pollinations` bewusst leer: kein fremder Schlüssel an eine andere Firma |
| `QUEUE_POLL_SECONDS` | nein | `30` | Takt der Warteschlangenschleife, siehe §5 |
| `QUEUE_BACKOFF_BASE_MINUTES` | nein | `2` | Rückzug in Minuten, `base * factor ** (Versuche - 1)` |
| `QUEUE_BACKOFF_FACTOR` | nein | `3` | ergibt mit der Vorgabe 2, 6, 18, 54 Minuten |
| `QUEUE_OFFPEAK_THRESHOLD_MINUTES` | nein | `60` | ab hier statt weiterem Rückzug ein Platz im Nebenzeitfenster |
| `QUEUE_OFFPEAK_WINDOW` | nein | `02:00-06:00` | lokale Zeit, `HH:MM-HH:MM`, über Mitternacht erlaubt; ein vertippter Wert bricht den Start ab |
| `QUEUE_MAX_ATTEMPTS` | nein | `5` | Aufgabegrenze, siehe §5 |
| `QUEUE_MAX_AGE_HOURS` | nein | `24` | zweite Aufgabegrenze, die zuerst erreichte gewinnt |
| `QUEUE_PAYLOAD_DIR` | nein | `<Verzeichnis von DB_PATH>/queue` | aufbewahrte Uploads geparkter Datei-Importe, siehe §10 |
| `QUEUE_PAYLOAD_MAX_MB` | nein | `100` | Obergrenze über alle aufbewahrten Uploads zusammen |
| `LOG_LEVEL` | nein | `INFO` | |

Kein Secret landet je im Repository. `.env.example` enthält nur Platzhalter.

### Das Textmodell ist eine Kette, kein einzelner Name (A21)

Bis A21 stand hier die Regel "`LLM_MODEL` bewusst fest verdrahtet statt als wandernder
Alias, damit ein Modellwechsel eine bewusste Änderung ist". Sie gilt für das Textmodell
nicht mehr, und zwar aus einem gemessenen Grund: am 2026-09-20 antwortete
`gemini-3.6-flash` - der fest verdrahtete Name - auf jede Anfrage
`429 "You exceeded your current quota"`, während `gemini-3.8-flash` und
`gemini-3.5-flash` im selben Moment `200` antworteten. Ein fester Name heisst dann nicht
"bewusst gewählt", sondern "jeder Import scheitert, bis jemand die Variable ändert".

Statt eines Namens hält `LLM_MODEL_CHAIN` deshalb eine Reihenfolge. Antwortet ein Modell
zweimal `429`, gilt sein Kontingent als leer: es wird für `LLM_MODEL_COOLDOWN_SECONDS`
übersprungen und derselbe Aufruf geht unverändert an das nächste Modell. Zweimal
`502/503/504` wechselt ebenfalls, aber ohne Sperrfrist - auch das ist gemessen, denn
`gemini-3.7-flash` antwortete zweimal `503 "high demand"`, während zwei andere Modelle
antworteten; Last gehört einem Modell und vergeht in Minuten. `500` wechselt nicht, und
ein `404` nimmt den Namen für die Laufzeit des Prozesses aus der Kette. Erst wenn die
ganze Kette durch ist, kommt dieselbe `LlmOverloadedError` heraus wie vorher - der
Wortlaut aus §7 und das Parken nach §5 bleiben unverändert (`src/model_chain.py`,
`llm._post`).

Die zweite Hälfte der alten Regel wird ebenso umgekehrt: mit
`LLM_MODEL_AUTODISCOVER=true` liest der Dienst beim Start und danach täglich
`GET {LLM_BASE_URL}/models`, behält die Namen, die zu `LLM_MODEL_PATTERN` passen, und
stellt ein neueres Modell der Kette voran. Damit das keine stille Verschlechterung sein
kann, ist die Übernahme an drei Bedingungen gebunden: eine schemaerzwungene Probe
(`response_format: json_schema`, `strict`), die das Modell bestehen muss, ein
Protokolleintrag, und genau eine Push-Meldung, die alten und neuen Namen nennt. Wer die
alte Regel zurückhaben will, setzt `LLM_MODEL_AUTODISCOVER=false` und
`LLM_MODEL_CHAIN` auf einen einzigen Namen - das ist genau das Verhalten von vor A21.

Für `IMAGE_MODEL` gilt die alte Regel unverändert weiter: dessen Anbieter führt genau
ein Modell, und es gibt nichts, wohin gewechselt werden könnte.

---

## 4. Datenmodell

`src/schema.py`, Pydantic v2. Bewusst nur Felder, die Mealie auch verwertet:

```python
class Recipe(BaseModel):
    name: str                                  # nicht leer
    recipeIngredient: list[str]                # mindestens 1 Eintrag, Freitext: "500 g Mehl"
    recipeInstructions: list[str]              # mindestens 1 Eintrag, je ein Schritt
    recipeYield: str | None = None             # "4 Portionen"
    totalTime: str | None = None               # ISO 8601, "PT45M"
    description: str | None = None
    recipeCategory: list[str] = []
    url: str | None = None                     # Quell-URL
```

Validierung: `name` nach `strip()` nicht leer, `recipeIngredient` und
`recipeInstructions` je mindestens ein nicht-leerer Eintrag. Verstösse sind ein
`ValidationError`, kein stilles Auffüllen.

Dazu:

```python
def to_jsonld(recipe: Recipe) -> dict
    # ergänzt "@context": "https://schema.org" und "@type": "Recipe"
```

`Recipe.model_json_schema()` ist zugleich das Schema, das die LLM-Stufe erzwingt. Es gibt
genau diese eine Definition, keine zweite im Prompt.

---

## 5. Ablauf

`POST /import` nimmt an, antwortet sofort `202`, arbeitet im Hintergrund.

```
1. normalize_url(url)                       classify.py
2. store.find(hash)  -> vorhanden?          store.py       -> Rückmeldung mit altem Link, Ende
3. store.start(hash, url)                   store.py       -> status "pending"
4. classify(url)                            classify.py
5a. site:     mealie_client.import_url()    -> slug?       -> Erfolg, weiter bei 6b
5b. site:     sources.site.fetch()          -> Recipe oder Rohtext
5c. youtube:  sources.youtube.fetch()       -> Rohtext
6.  falls nur Rohtext: llm.extract_recipe() -> Recipe
6b. naming.make_name()                      naming.py      -> neuer Name oder None
7.  mealie_client.create_from_jsonld()      -> slug
    bzw. bei 5a: mealie_client.rename(slug, name) -> neuer slug
8.  store.finish(hash, slug, name)
9.  image.generate() + mealie_client.set_image()   -> nur wenn das Rezept kein Bild hat
10. mealie_client.set_tags(slug, ["auto-import"] (+ "ki-bild", falls Schritt 9 lief))
11. ha_notify.notify(...)
```

Schritt 8 steht bewusst vor 9 und 10: ab dem Slug existiert das Rezept in Mealie, und
weder ein Fehlschlag der Bildstufe noch einer von `set_tags` darf danach noch eine
Doppelanlage auslösen (Review-A8-Befund 1). Schritt 9 läuft vor der Meldung, damit das
angetippte Rezept vollständig ist und beide Tags in ein einziges PATCH passen.

`POST /import/file` nimmt Dateien an, antwortet ebenso sofort `202`:

```
1. content_hash(dateien)                    document.py    -> derselbe Hash-Raum wie URLs
2. store.find(hash) -> vorhanden?           store.py       -> Rückmeldung mit altem Link, Ende
3. store.start(hash, "datei:<name>")        store.py
4. document.read(dateien)                   document.py    -> Rohtext (PDF) oder Bilder
5a. PDF:   llm.extract_recipe(text, quelle)
5b. Bild:  llm.extract_recipe_from_images(bilder, quelle)
6. weiter wie oben ab Schritt 6b (Namensstufe, Mealie, store, Rückmeldung)
```

Bei jedem Fehlschlag: `store.fail(hash, fehlertext)` und eine Rückmeldung, die einen
Menschen in die Lage versetzt zu verstehen, woran es lag. Nie ein stiller Abbruch.

**Ausnahme: vorübergehende Fehlschläge** (A20, 2026-09-20). Sagt die Gegenstelle selbst
"später nochmal", wird der Import nicht verworfen, sondern geparkt:

```
F1. app.is_transient(fehler)                app.py     -> LlmOverloadedError,
                                                          ThrottledError,
                                                          MealieUnavailableError
F2. schedule.should_give_up(...)            schedule.py -> Grenze erreicht? endgültig
                                                           scheitern, eine letzte Meldung
F3. payload.save(hash, dateien)             payload.py  -> nur beim Datei-Weg, innerhalb
                                                           von QUEUE_PAYLOAD_MAX_MB
F4. schedule.next_due(versuche, jetzt)      schedule.py -> Rückzug, sonst Nebenzeit
F5. store.queue(hash, faellig, versuche)    store.py    -> status "queued"
F6. ha_notify.notify("Import später", ...)              -> nur beim ersten Parken
```

Eine Hintergrundschleife in `app.py` (`_poll_queue`, Takt `QUEUE_POLL_SECONDS`) holt
alle fälligen Zeilen (`store.due`), übernimmt jede atomar (`store.claim_due`) und lässt
sie denselben Weg laufen wie einen ersten Anlauf - ohne `store.start()`, damit
`created_at` und damit die Ratenbegrenzung (§11) unberührt bleiben. Gelingt der Versuch,
geht die normale Erfolgsmeldung heraus; scheitert er wieder vorübergehend, wird erneut
geparkt, ohne zweite Meldung. Ist eine der beiden Grenzen erreicht
(`QUEUE_MAX_ATTEMPTS`, `QUEUE_MAX_AGE_HOURS`), scheitert der Import endgültig mit genau
einer letzten Meldung.

Alles andere - kein Rezept gefunden, keine Untertitel, gescanntes PDF, fremder Dateityp,
Schemafehler des Modells, jede Antwort von Mealie mit HTTP-Status - scheitert unverändert
sofort mit dem Wortlaut aus §7.

---

## 6. Modulschnittstellen

Diese Signaturen sind verbindlich. A1 bis A4 arbeiten gleichzeitig und dürfen sich
ausschliesslich auf sie verlassen.

### `src/classify.py` (A1)

```python
SourceKind = Literal["site", "youtube", "unsupported"]

def classify(url: str) -> SourceKind
def normalize_url(url: str) -> str
def url_hash(url: str) -> str          # sha256 der normalisierten URL, hex, 16 Zeichen
```

`normalize_url` entfernt `utm_*`, `fbclid`, `gclid`, `si`, Fragmente und einen
abschliessenden Schrägstrich. YouTube-URLs werden auf `https://www.youtube.com/watch?v=<ID>`
vereinheitlicht, inklusive `youtu.be`, `/shorts/` und `m.youtube.com`.

`classify` erkennt YouTube an der Domain, alles andere mit `http`- oder `https`-Schema
als `site`, alles übrige als `unsupported`. Eine Instagram-Adresse ist damit `site` und
scheitert dort mit der gewöhnlichen Meldung "kein Rezept gefunden" - gewollt, siehe Plan
§2.10.

### `src/sources/site.py` und `src/sources/youtube.py` (A1)

```python
@dataclass(frozen=True)
class SourceResult:
    recipe: Recipe | None    # gefüllt, wenn ohne LLM lösbar
    text: str | None         # Rohtext für die LLM-Stufe, wenn recipe None ist
    title: str | None
    stage: str               # "scrapers" | "jsonld" | "text" | "transcript" | "caption" | "pdf" | "images"
    images: tuple[bytes, ...] = ()   # nur document.py, JPEG/PNG für das Bildmodell

def fetch(url: str) -> SourceResult
```

Genau eines von `recipe`, `text` und `images` ist gesetzt. Alle drei leer ist ein
`SourceError`. Das Feld `images` ist additiv und ausschliesslich von `document.py`
gefüllt; `site.py` und `youtube.py` lassen es leer.

Diese Module rufen **niemals** Mealie oder das LLM auf. Sie lesen nur.

**`site.py`, Reihenfolge:**

1. `recipe-scrapers` für die URL. Trifft die Bibliothek die Domain, wird das Ergebnis
   nach `Recipe` abgebildet, `stage="scrapers"`.
2. Sonst HTML holen und mit `extruct` nach eingebettetem JSON-LD vom Typ `Recipe`
   suchen, `stage="jsonld"`.
3. Sonst HTML mit `trafilatura` zu Fliesstext, `stage="text"`, `recipe=None`.

Zeitlimit 30 Sekunden je HTTP-Aufruf, ein realistischer Browser-`User-Agent`.

**`youtube.py`:**

```
yt-dlp --skip-download --write-auto-sub --write-sub --sub-lang de --print-json
```

Kein `--convert-subs`: das ruft `ffmpeg` auf, siehe §2. Gelesen wird die Datei so, wie
yt-dlp sie ablegt (`<id>.<lang>.vtt`, ersatzweise `.srt`), am 2026-08-23 live bestätigt.

**Eine Sprache je Aufruf** (geändert am 2026-08-23 nach dem Befund aus A9): Deutsch
zuerst, und nur wenn dabei keine Datei entsteht, ein zweiter Aufruf mit `--sub-lang en`.
`--sub-lang de,en` in einem Aufruf ist verboten - `yt-dlp` bricht dort beim ersten
`HTTP Error 429` einer Sprache die ganze Extraktion ab und verwirft die bereits
geladene Datei der anderen. Vor jedem Fehlerwurf wird geprüft, ob im Arbeitsverzeichnis
schon eine brauchbare Datei liegt. Eine Drosselung wird als Drosselung gemeldet, nicht
als "kein Rezept gefunden" (§7).

Untertitel plus Titel plus Videobeschreibung ergeben den Rohtext, `stage="transcript"`.
`recipe` ist immer `None`.

Alles Temporäre landet unter `/tmp`, das im Container ein tmpfs ist. **Niemals** das
Video oder die Tonspur laden. Fehlen Untertitel, wird `NoTranscriptError` geworfen, kein
Ausweichen auf einen Download.

### `src/sources/document.py` (A12)

```python
@dataclass(frozen=True)
class Upload:
    filename: str
    content_type: str
    data: bytes

def content_hash(uploads: list[Upload]) -> str    # sha256 über alle Bytes, hex, 16 Zeichen
def read(uploads: list[Upload]) -> SourceResult
```

- **PDF** (`application/pdf`): Text über `pypdf`, höchstens `MAX_PDF_PAGES` Seiten,
  `stage="pdf"`. Ergibt das weniger als 200 Zeichen, ist es ein gescanntes PDF ohne
  Textebene: `SourceError` mit dem Hinweis aus §7. Kein OCR, keine Seitenrasterung.
- **Bild** (`image/jpeg`, `image/png`, `image/webp`): die Bytes gehen unverändert als
  `images` weiter, `stage="images"`, `text=None`. Keine Bildbearbeitung im Dienst.
- **HEIC wird nicht angenommen.** Der Kurzbefehl wandelt vorher nach JPEG (§9). Grund:
  eine HEIC-Umwandlung im Dienst hiesse eine weitere Systembibliothek im Image, und das
  Gerät, das das Foto gemacht hat, kann es ohnehin verlustfrei selbst.
- Andere Inhaltstypen: `SourceError`.
- Mehrere Bilder derselben Anfrage gehören zu einem Rezept (Vorder- und Rückseite einer
  Seite) und gehen gemeinsam in einen einzigen LLM-Aufruf.

Dieses Modul ruft weder Mealie noch das LLM auf. Es liest nur.

### `src/llm.py` und `src/prompts.py` (A2)

```python
def extract_recipe(text: str, source_url: str) -> Recipe
def extract_recipe_from_images(images: list[bytes], source: str) -> Recipe   # A12
```

- OpenAI-kompatibler Aufruf `POST {LLM_BASE_URL}/chat/completions`, `Authorization: Bearer`.
- Erzwungenes Schema über `response_format` vom Typ `json_schema`, gespeist aus
  `Recipe.model_json_schema()`.
- Schlägt die Validierung fehl, genau **ein** erneuter Versuch, bei dem die
  Pydantic-Fehlermeldung als zusätzliche Nutzernachricht angehängt wird. Danach
  `LlmError`. Keine weitere Schleife.
- Zeitlimit 120 Sekunden.
- Der Prompt erzwingt deutschsprachige Ausgabe: Zutaten und Zubereitungsschritte bleiben
  oder werden Deutsch, unabhängig von der Sprache der Quelle.
- Der Prompt verbietet Erfindungen. Fehlt eine Angabe in der Quelle, bleibt das Feld leer,
  statt geraten zu werden. Das gilt besonders für Mengen und Zeiten.
- Der Prompt darf das Schema nicht ein zweites Mal in Prosa wiederholen. Eine Definition,
  §4.

`extract_recipe_from_images` (A12) nutzt denselben Endpunkt, dasselbe erzwungene Schema,
dieselbe Retry-Regel und denselben Prompt-Kern. Unterschied ist allein der Inhalt der
Nutzernachricht: statt Text eine Liste von `image_url`-Teilen mit `data:`-URIs
(`data:image/jpeg;base64,...`). Ein zusätzlicher Satz im Prompt verlangt, nur zu lesen,
was auf dem Bild wirklich steht, und unleserliche Stellen wegzulassen statt sie zu
ergänzen. Zeitlimit 180 Sekunden, weil Bilder länger brauchen.

Kann das Modell auf den Bildern kein Rezept erkennen, ist das ein `LlmError` mit dem Text
aus §7, keine leere Hülle in Mealie.

### `src/naming.py` und die Namens-Prompts in `src/prompts.py` (A18)

```python
def make_name(name: str | None, source: str,
              ingredients: list[str], instructions: list[str]) -> str | None
```

Eine eigene, kleine LLM-Stufe zwischen Extraktion und Mealie, an genau einer Stelle im
Ablauf und damit für alle vier Wege. Sie schreibt nur den Namen und fasst Zutaten,
Schritte, Zeiten oder Kategorien nicht an. Erzwungenes Schema mit dem einen Feld `name`,
über denselben `llm._post` wie die Extraktion, `LLM_MODEL` und `LLM_BASE_URL` unverändert.

Rückgabe ist der neue Name **nur dann**, wenn er brauchbar ist und sich vom vorhandenen
unterscheidet; `None` heisst in jedem anderen Fall "alten Namen behalten, nichts
schreiben". Verworfen wird eine Antwort, die leer, länger als 80 Zeichen oder mehrzeilig
ist.

`make_name` wirft **niemals** und hat **keinen** zweiten Versuch: ein Fehlschlag kostet
hier nur einen unschönen Namen, nicht den Import. Zeitlimit 30 Sekunden. Anfragegrösse
begrenzt auf 30 Zutaten, 10 Schritte, 200 Zeichen je Schritt.

`NAMING_ENABLED=false` schaltet die Stufe vollständig ab; der `import_url`-Weg kommt dann
wie zuvor ganz ohne LLM aus.

### `src/image.py` und `IMAGE_GENERATION_PROMPT_TEMPLATE` in `src/prompts.py` (A19)

```python
def generate(name: str, ingredients: list[str]) -> bytes | None
```

Aufbau wie die Namensstufe, eine Schicht weiter aussen: ein Schalter, ein Aufruf, jeder
Fehlschlag endet in `None` und einer Warnung. Sie läuft erst, wenn das Rezept in Mealie
existiert und `store.finish()` gelaufen ist - ein Fehlschlag kann deshalb keinen zweiten
Import auslösen, sondern kostet nur das Bild.

Seit A22 spricht die Stufe nicht **einen** Anbieter an, sondern geht eine geordnete Kette
von Kandidaten `Anbieter:Modell` durch (`IMAGE_MODEL_CHAIN`, Zustand in
`src/image_chain.py`). Drei Anbieterformen, weil sie sich nicht ineinander übersetzen
lassen:

* `gemini`: `POST {Basis}/models/{Modell}:generateContent`, wobei die Basis die Wurzel der
  nativen Fläche samt Versionsteil ist (`LLM_BASE_URL` ohne `/openai`, also
  `.../v1beta`). Schlüssel im Kopf
  `x-goog-api-key`, das Bild als base64 in `inlineData`. **Nicht** über
  `/images/generations` - dort bildet dieser Anbieter auf `predict` ab, was keines seiner
  Modelle führt (404, gemessen 2026-09-20). Die Anfrage verlangt `aspectRatio: "1:1"`:
  1024x1024 zum selben Preis wie die Vorgabe 1408x768, und Mealies Kachel beschneidet das
  breitere Bild ohnehin.
* `pollinations`: `GET {Basis}/prompt/{Prompt}`, der Prompt urlkodiert im Pfad, der
  Antwortkörper **ist** das Bild. Ohne Konto und ohne Schlüssel.
* `openai`: `POST {Basis}/images/generations`, gelesen werden `data[0].b64_json` und
  ersatzweise `data[0].url`. `llm._post` passt dafür nicht: der ist fest auf
  `/chat/completions` samt erzwungenem JSON-Schema.

Warum die Reihenfolge so: der Gemini-Schlüssel konnte am 2026-09-20 zunächst **keine**
Bilder erzeugen (alle Bildmodelle 429 "free_tier ... limit: 0", auch aus einem frisch
angelegten Projekt). Mit $5 Guthaben auf demselben Konto antworten alle fünf Bildmodelle
200 mit einem echten Bild, ohne Wasserzeichen: `gemini-3-pro-image` in 15,3 s für $0,134,
`gemini-3.1-flash-image` in 8,6 s für $0,067. Pollinations kostet nichts, braucht 35-46 s
und trägt unten rechts ein `pollinations.ai`-Wasserzeichen, das auch ein kostenloses Token
nicht entfernt. Deshalb steht das bezahlte Modell oben und das kostenlose als Boden
darunter: ein aufgebrauchtes Guthaben kostet Bildqualität, nicht das Bild.

Ein abgewiesener Kandidat reicht dieselbe Anfrage nach unten weiter. Was sich die Kette
dabei merkt, hängt am Grund: leeres Kontingent oder Guthaben (429, oder 402/403 mit
entsprechendem Text) sperrt ihn für `IMAGE_MODEL_COOLDOWN_SECONDS`, ein unbekannter
Modellname (404, oder 400 mit den Markern aus `llm`) nimmt ihn für die Lebensdauer des
Prozesses aus der Kette, alles andere gilt nur für diesen Import. Der Zustand liegt im
Prozess; ein Neustart fängt oben an.

**Höchstens ein Aufruf je Kandidat**, kein zweiter Versuch beim selben - ein Bildaufruf
ist der teuerste Aufruf dieses Dienstes, und der nächste Kandidat ist der bessere zweite
Versuch. Zeitlimit je Aufruf 60 s (Gemini) beziehungsweise 90 s, und darüber ein Budget
für die ganze Stufe (`IMAGE_DEADLINE_SECONDS`, 150 s): reicht die Restzeit nicht mehr für
das volle Zeitlimit des nächsten Kandidaten, endet der Gang ohne Bild. Ein gekürztes
Zeitlimit gäbe es nicht - ein bezahlter Aufruf, der nicht zu Ende laufen darf, ist Geld
für nichts. Anfragegrösse weiter begrenzt auf 10 Zutaten; die Bytes müssen JPEG, PNG oder
WebP sein (`llm._image_mime`) und höchstens 12 MB gross.

Adresse und Schlüssel stehen je Anbieter in `config.IMAGE_ENDPOINTS`. `IMAGE_BASE_URL` und
`IMAGE_API_KEY` überschreiben genau den Eintrag des Anbieters aus `IMAGE_PROVIDER` - eine
einzelne Überschreibung für alle Anbieter wäre der Weg, auf dem der Schlüssel des
Textmodells bei Pollinations landet. Die Gemini-Basis wird aus `LLM_BASE_URL` abgeleitet
(Suffix `/openai` entfällt), damit die beiden Adressen nicht auseinanderlaufen.

Ob überhaupt erzeugt wird, entscheidet `app._attach_image`: auf dem JSON-LD-Weg immer
(`to_jsonld` kennt kein Bildfeld), auf dem `import_url`-Weg nur, wenn
`mealie_client.has_image()` kein Bild findet. Ein von der Quellseite geholtes Foto wird
nie ersetzt. Fehlt die Mealie-Antwort, weil Mealie bei der Platzhalter-Prüfung nicht
erreichbar war, unterbleibt die Stufe, ebenso bei `IMAGE_ENABLED=false` - dann noch vor
der Bildstandsabfrage.

`has_image()` fragt dafür Mealies Mediendatei ab (`GET
/api/media/recipes/{id}/images/original.webp`, 200 heisst Bild, 404 heisst keines) statt
das Feld `image` der bereits vorliegenden Antwort zu lesen. Gemessen am 2026-09-20:
Mealie füllt dieses Feld mit einer Kennung auch dann, wenn gar kein Bild hinterlegt ist -
bei 28 von 29 bildlosen Rezepten dieser Anlage. Das kostet auf dem `import_url`-Weg eine
zusätzliche Anfrage, ist aber die einzige Auskunft, die stimmt. Bei Netzfehler,
unerwartetem Status oder fehlender id gilt "hat ein Bild": lieber kein erzeugtes Bild als
ein überschriebenes Foto.

Der Prompt ist als einziger in `prompts.py` englisch: seine Ausgabe ist ein Bild ohne
Text, und Bildmodelle sind auf englische Bildunterschriften trainiert. Er ist ein Satz
aus Gerichtname, Hauptzutaten, Anrichten und Licht, mit "no text, no logo" am Ende. Die
erste Fassung zählte über 761 Zeichen lang alle Verbote auf und bekam in der Probe vom
2026-09-20 genau das zurück, was sie ausschloss: einen leeren Teller mit gekritzelter
Pseudo-Schrift. Die Zubereitungsschritte stehen deshalb nicht mehr im Prompt.

Ein erzeugtes Bild ist kein Foto des gekochten Gerichts, deshalb trägt das Rezept danach
zusätzlich das Tag `ki-bild` (§5, Schritt 10) - in Mealie filterbar, statt nur im
Protokoll zu stehen. `IMAGE_ENABLED=false` schaltet die Stufe vollständig ab; bereits
hochgeladene Bilder bleiben.

### `src/mealie_client.py` (A3)

```python
def import_url(url: str) -> str | None            # None, wenn Mealie die Seite nicht scrapen kann
def create_from_jsonld(data: dict) -> str         # gibt den slug zurück
def set_tags(slug: str, tags: list[str]) -> None
def recipe_link(slug: str) -> str                 # klickbare URL für die Rückmeldung
def get_recipe_name(slug: str) -> str             # GET /api/recipes/{slug}, Feld "name"
def get_recipe(slug: str) -> dict                 # volles Recipe-Output, A16 und A18
def is_placeholder(recipe: dict) -> bool          # A16
def delete_recipe(slug: str) -> None              # A16
def rename(slug: str, name: str) -> None          # PATCH /api/recipes/{slug}, nur "name"
def recipe_texts(recipe: dict) -> tuple[list[str], list[str]]   # Zutaten, Schritte als Freitext
def has_image(recipe: dict) -> bool               # A19, GET /api/media/recipes/{id}/images/...
def set_image(slug: str, data: bytes) -> None     # A19, PUT /api/recipes/{slug}/image, multipart
```

`set_image` und `has_image` sind am 2026-09-20 an der laufenden Anlage gemessen worden
(Mealie v3.22.0), Einzelheiten im Moduldocstring: der Pfad samt beider Pflichtfelder aus
`/openapi.json`, und für `has_image` der Befund, dass nur die Mediendatei eine belastbare
Auskunft gibt. `set_image` schickt als einziger Aufruf multipart statt JSON und kommt
deshalb ohne `_HEADERS` aus.

`rename` gibt den danach gültigen Slug zurück, weil Mealie ihn aus dem neuen Namen **neu
ableitet** - live verifiziert am 2026-08-24 gegen Mealie v3.22.0: nach der Umbenennung
antwortet der alte Slug mit 404, und ein im Rumpf mitgeschickter `slug` wird ignoriert.
`recipe-naming-plan.md` §2.1 nimmt das Gegenteil an. Ab der Umbenennung gilt deshalb nur
noch der zurückgegebene Slug - für `set_tags`, für `store.finish` und für den Link in der
Rückmeldung. Unschädlich ist der Wechsel, weil die Umbenennung vor `store.finish` und vor
der Push-Meldung läuft: den alten Slug kennt zu diesem Zeitpunkt noch niemand ausserhalb
des Dienstes.

**Live verifizieren, nicht raten.** Erster Schritt von A3:

```
curl -s http://mealie.local:30081/openapi.json | jq -r '.paths | keys[]' | grep -i recipe
```

Daraus die tatsächlichen Pfade für URL-Import, JSON-Import und Tags belegen. Der Bericht
nennt die verwendeten Pfade wörtlich. Ebenso live zu belegen ist die Form des
Oberflächen-Links zu einem Rezept für `recipe_link`, indem ein vorhandenes Rezept
abgefragt und der Link nachvollzogen wird.

Authentifizierung: `Authorization: Bearer {MEALIE_TOKEN}`. Zeitlimit 60 Sekunden.
`import_url` gibt bei einem Fehlschlag `None` zurück und wirft nicht, weil Stufe 1 des
Ablaufs regulär danebengehen darf.

### `src/store.py` (A3)

SQLite unter `DB_PATH`, eine Tabelle:

```sql
CREATE TABLE IF NOT EXISTS imports (
  url_hash   TEXT PRIMARY KEY,
  url        TEXT NOT NULL,
  status     TEXT NOT NULL,      -- pending | queued | done | failed
  slug       TEXT,
  title      TEXT,
  error      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,   -- A20
  due_at     TEXT,                         -- A20, UTC ISO-8601
  payload    TEXT                          -- A20, JSON zu aufbewahrten Uploads
);
```

```python
def init() -> None
def find(url_hash: str) -> dict | None
def start(url_hash: str, url: str) -> bool   # False = ein anderer Lauf hat den Hash schon
def finish(url_hash: str, slug: str, title: str) -> None
def fail(url_hash: str, error: str) -> None
def pending() -> list[dict]
def recent_count(seconds: int) -> int
# A20, Warteschlange:
def queue(url_hash: str, due_at: str, attempts: int, payload: str | None = None) -> None
def due(now: str) -> list[dict]              # fällige queued-Zeilen, älteste zuerst
def claim_due(url_hash: str) -> bool         # atomar queued -> pending
def queued() -> list[dict]
```

Die drei Spalten kommen additiv dazu (`ALTER TABLE`, abgesichert über
`PRAGMA table_info`, ausgeführt in `init()`): eine bestehende Datenbank wandert ohne
Handgriff, und eine ältere Fassung des Dienstes ignoriert sie wieder. `queued` heisst
"wartet auf einen späteren Versuch", `pending` unverändert "wird gerade bearbeitet".
`finish` und `fail` räumen `due_at` und `attempts` mit ab - ein Endzustand trägt keine
Fälligkeit mehr. `claim_due` benutzt dasselbe atomare Muster wie `start` und aus
demselben Grund. Bewusst keine zweite Tabelle: sonst müsste jede bestehende Zusicherung
zwei Tabellen befragen, um den Stand einer Quelle zu kennen (A20, `design.md`).

`start` ist der Anspruch auf einen Hash und muss atomar sein: ein `INSERT ... ON CONFLICT`,
das nur auf einem `failed`-Eintrag erneut greift, nicht Lesen und danach Schreiben.
Liefert es `False`, läuft der Hash bereits woanders und der Aufrufer bricht ab, ohne zu
melden. Zwei fast gleichzeitige Anfragen zur selben URL erzeugen so genau ein Rezept
(ergänzt am 2026-08-23 nach Befund 3 des Reviews A8).

`get_recipe_name` schliesst die Lücke auf dem `import_url`-Weg: dort schabt Mealie selbst,
der Dienst hält also kein `Recipe`-Objekt und kennt den Namen nicht. Ohne diesen Aufruf
stünde der Slug in der Rückmeldung, was §7 widerspricht (ergänzt am 2026-08-23 nach
Befund 2).

`start` auf einem vorhandenen `failed`-Eintrag setzt ihn zurück auf `pending`, ein
erneuter Versuch derselben URL muss also möglich sein. Auf einem `done`-Eintrag wird gar
nicht erst gestartet, siehe Ablauf Schritt 2.

### `src/ha_notify.py` (A4)

```python
def notify(title: str, message: str, link: str | None = None) -> None
```

Sendet **genau einen** Kanal:

- `POST {HA_URL}/api/services/notify/{HA_NOTIFY_TARGET}`

`persistent_notification.create` ist am 2026-08-23 auf Wunsch des Nutzers entfernt worden
(Plan §2.8). Der Mac bekommt damit keine Rückmeldung mehr.

Der Kanal wird ueber `HA_NOTIFY_TARGET` konfiguriert (Name des `notify.*`-Dienstes
ohne Praefix). Ein reiner `notify.send_message`-Kanal eignet sich nicht: er meldet nur
`supported_features: 1` (Titel) und nimmt kein `data`-Feld entgegen, kann also kein
`clickAction` auf den Mealie-Link tragen.



Diese Funktion wirft **niemals**. Eine fehlgeschlagene Rückmeldung wird protokolliert,
darf aber einen erfolgreichen Import nicht nachträglich zu einem Fehler machen.

### `src/app.py` (A4)

```
POST /import        {"url": "..."}          -> 202 {"status": "accepted"}
                                               400 bei fehlender oder unbrauchbarer URL
                                               429 bei überschrittener Ratenbegrenzung
POST /import/file   multipart/form-data     -> 202 {"status": "accepted"}
                    Feld "files", 1..MAX_UPLOAD_FILES
                    Header: Authorization: Bearer <IMPORT_TOKEN>
                                               400 bei fehlender oder ungeeigneter Datei
                                               401 ohne oder mit falschem Token
                                               413 über MAX_UPLOAD_MB
                                               429 bei überschrittener Ratenbegrenzung
GET  /healthz                               -> 200 {"status": "ok"}
```

`POST /import/file` wird vom Kurzbefehl direkt aufgerufen, nicht über Home Assistant:
ein Webhook-Auslöser nimmt JSON und Formularfelder entgegen, aber keine Binärdatei
sinnvoll entgegen und reicht sie nicht weiter. Deshalb trägt dieser Endpunkt seine eigene
Authentifizierung (`IMPORT_TOKEN`, Vergleich mit `hmac.compare_digest`, niemals im
Protokoll). Die Rückmeldung läuft weiterhin über Home Assistant, §6 `ha_notify`.

Der Vergleich in Zahlen: `MAX_UPLOAD_MB` begrenzt die Summe aller Dateien einer Anfrage,
`MAX_UPLOAD_FILES` ihre Anzahl. Überschreitungen werden abgewiesen, nicht beschnitten.

Beim Start: `store.init()`, danach die verwaisten Upload-Verzeichnisse abräumen
(`payload.sweep`), danach jeden `pending`-Eintrag erneut in die Verarbeitung geben.
Damit übersteht ein Auftrag einen Neustart des Containers. Seit A20 gilt das auch für
einen Datei-Import, sofern seine Bytes aufbewahrt sind; ohne sie bleibt es bei
"bitte die Datei noch einmal teilen". Zuletzt startet die Warteschlangenschleife
(`_poll_queue`), die beim Herunterfahren abgebrochen und abgewartet wird.

Die Verarbeitung selbst läuft über `BackgroundTasks`; die Warteschlange (§5) ist eine
einzige `asyncio`-Aufgabe im selben Prozess, kein Broker und kein zweiter Container.

```python
def is_transient(exc: Exception) -> bool     # A20, Weiche "später nochmal"
async def _run_due_once(now: datetime | None = None) -> int   # ein Durchgang, testbar
```

Dazu zwei neue Module:

### `src/schedule.py` (A20)

```python
def next_due(attempts: int, now: datetime, jitter: Callable[[], float] = random.random) -> datetime
def should_give_up(attempts: int, created_at: str | datetime, now: datetime) -> bool
```

Reine Rechnung, kein Ein- und Ausgabeverkehr: `now` und die Streuquelle sind Argumente,
damit kein Test schläft (§12). Rückzug mit ±25% Streuung; überschreitet die Verzögerung
`QUEUE_OFFPEAK_THRESHOLD_MINUTES`, wird stattdessen ein zufälliger Punkt im nächsten
Nebenzeitfenster gewählt - läuft das Fenster gerade und passt der Versuch noch hinein,
das laufende. Das Fenster ist lokale Zeit, die gespeicherte Fälligkeit absolute UTC-Zeit.

### `src/payload.py` (A20)

```python
def save(url_hash: str, uploads: list[Upload]) -> str      # JSON für die Spalte payload
def load(url_hash: str, payload: str | None) -> list[Upload] | None
def delete(url_hash: str) -> None                          # idempotent
def total_bytes() -> int
def fits_in_budget(uploads: list[Upload]) -> bool
def sweep(keep: set[str]) -> list[str]
```

Ein Verzeichnis je `url_hash` unter `QUEUE_PAYLOAD_DIR`, darin eine durchnummerierte
Datei je Upload. Der ursprüngliche Dateiname steht in der JSON-Beschreibung, nie im
Pfad: er ist fremde Eingabe. Gelöscht wird an jedem Endzustand, und was ein Absturz
dazwischen übrig lässt, räumt `sweep()` beim Start ab.

---

## 7. Rückmeldungstexte

Deutsch, knapp, immer mit Grund. Beispiele, wörtlich zu verwenden:

| Fall | Titel | Nachricht |
|---|---|---|
| Erfolg | `Rezept angelegt` | `<Name>` plus Link |
| Schon vorhanden | `Rezept schon vorhanden` | `<Name> wurde bereits importiert` plus Link |
| Kein Untertitel | `Import fehlgeschlagen` | `Das Video hat keine Untertitel, daraus lässt sich kein Rezept lesen.` |
| Kein Rezept erkennbar | `Import fehlgeschlagen` | `Auf der Seite war kein Rezept zu finden.` |
| LLM-Fehler (Schema auch im zweiten Anlauf verfehlt) | `Import fehlgeschlagen` | `Die Rezepterkennung ist gescheitert: <Grund>` |
| Kein eingestelltes Modell existiert beim Anbieter (A21, HTTP 404 auf jedem Namen der Kette) | `Import fehlgeschlagen` | `Die Rezepterkennung ist gescheitert: <Grund>` - der Grund nennt jeden abgewiesenen Namen |
| Mealie lehnt ab (Antwort mit HTTP-Status) | `Import fehlgeschlagen` | `Mealie hat den Import abgelehnt: <Grund>` |
| Gescanntes PDF | `Import fehlgeschlagen` | `Dieses PDF enthält keinen lesbaren Text. Ein Foto der Seite funktioniert besser.` |
| Datei zu gross oder falscher Typ | `Import fehlgeschlagen` | `Diese Datei kann ich nicht lesen: <Grund>` |
| Kein Rezept auf dem Bild | `Import fehlgeschlagen` | `Auf dem Bild war kein Rezept zu erkennen.` |

Geparkt statt gescheitert (A20, 2026-09-20). Diese drei Fälle endeten bis dahin mit
"Bitte später erneut teilen"; heute wird der Import automatisch wiederholt, und genau
eine Meldung sagt das zu - nicht eine je Versuch. Seit A21 wird die erste Zeile
seltener erreicht: sie gilt erst, wenn **jedes** Modell der Kette abgelehnt hat, nicht
schon beim ersten (§3, `src/model_chain.py`). Der Wortlaut ist derselbe geblieben, denn
für den Menschen ist es derselbe Fall.

`app._describe_source_error` trägt für `LlmOverloadedError` weiterhin wörtlich
`Das Sprachmodell ist gerade überlastet. Bitte später erneut teilen.` - das ist der
Rückfall für den Fall, dass das Parken selbst nicht möglich war.

| Fall | Titel | Nachricht |
|---|---|---|
| **Jedes** Modell der Kette ist erschöpft oder ausgelastet (A21: HTTP 429/502/503/504 auf jedem Namen, oder HTTP 500 auf dem gerade genutzten - jeweils auch nach dem Wiederholungsversuch) | `Import später` | `Das Sprachmodell ist gerade ausgelastet. Ich versuche es automatisch später noch einmal.` |
| YouTube drosselt (HTTP 429) | `Import später` | `YouTube drosselt gerade die Untertitel. Ich versuche es automatisch später noch einmal.` |
| Mealie nicht erreichbar (Verbindungsfehler, kein HTTP-Status) | `Import später` | `Mealie ist gerade nicht erreichbar. Ich versuche es automatisch später noch einmal.` |
| Wiederholungen aufgebraucht oder Altersgrenze erreicht | `Import fehlgeschlagen` | `Auch nach mehreren Versuchen hat es nicht geklappt. Bitte noch einmal teilen.` |
| Kein Platz mehr für aufbewahrte Uploads (`QUEUE_PAYLOAD_MAX_MB`) | `Import fehlgeschlagen` | `Für einen späteren Versuch ist kein Speicher mehr frei. Bitte die Datei später noch einmal teilen.` |

---

## 8. Home Assistant (A6)

Drei Teile. `home-assistant-best-practices`-Skill **vor** dem ersten Eingriff lesen.

**a) `rest_command` in `configuration.yaml`.** Dafür gibt es keine Oberfläche, die Datei
muss angefasst werden. Vorher Kopie mit `.bak-YYYYMMDD`, danach `ha core check`, erst
dann neu laden.

```yaml
rest_command:
  recipe_import:
    url: "http://recipe-import.local:30082/import"
    method: POST
    content_type: "application/json"
    payload: '{"url": "{{ url }}"}'
```

**b) Automation "Rezept importieren".** Webhook-Auslöser mit der ID aus Plan §7, V4. Der
Wert steht nicht im Repository: er liegt in der Automation in Home Assistant und im
Kurzbefehl. Wer ihn braucht, liest ihn dort.

**Kritisch:** `local_only: false` setzen. Der Aufruf kommt von unterwegs über einen
Tailscale-Subnetz-Router herein. Ob Home Assistant dabei die LAN-Adresse des Routers
oder eine Adresse aus `100.64.0.0/10` als Quelle sieht, haengt allein daran, ob auf
diesem Router `--snat-subnet-routes` aktiv ist. Im zweiten Fall zaehlt Home Assistant die
Quelle nicht zum lokalen Netz und weist den Aufruf stumm ab. `local_only: false` macht
das Verhalten unabhaengig von dieser Einstellung. Der Schutz liegt ohnehin in der
Webhook-ID, nicht in der Netzpruefung. `allowed_methods` auf `POST` begrenzen.

Die Automation ruft `rest_command.recipe_import` mit `trigger.json.url` auf.

**c) Fallback zum Einfügen von Hand.** Helfer `input_text.rezept_url`, `max: 255`, plus
eine Karte auf dem Dashboard. Eine zweite Automation reicht Änderungen an diesem Helfer
an denselben `rest_command` weiter und leert ihn danach.

Die 255-Zeichen-Grenze ist eine harte Grenze des Helfertyps und wird dokumentiert, nicht
umgangen.

---

## 9. Kurzbefehl für iPhone und Mac (A7 dokumentiert ihn, der Nutzer legt ihn an)

**Genau ein Kurzbefehl**, über iCloud auf beiden Geräten, im Teilen-Menü aktiviert. Er
verzweigt nach der Art der Eingabe, statt in mehrere Kurzbefehle zu zerfallen - im
Teilen-Menü soll ein einziger Eintrag stehen.

1. Eingabe: URLs, Bilder, PDF-Dateien, Dateien. "Im Teilen-Menü anzeigen" ein.
2. Verzweigung ohne Typabfrage: "URLs aus Eingabe abrufen" auf die Kurzbefehl-Eingabe.
   Mindestens eine Adresse heisst URL-Zweig, sonst Datei-Zweig; dort trennt
   "Details von Dateien abrufen" mit der Auswertung "Dateiendung" das PDF vom Bild. Eine
   Aktion "Dateityp abrufen" gibt es in der Kurzbefehle-App nicht. Klickanleitung für iOS 26 und macOS 26:
   `setup-kurzbefehl.md`.
3. **URL-Zweig:** "Inhalte von URL abrufen", Ziel
   `http://homeassistant.local:8123/api/webhook/<Webhook-ID>`, Methode `POST`, Anfragetext
   JSON, Feld `url` = Eingabe. Unverändert gegenüber Phase 1.
4. **Bild-Zweig:** zuerst "Bild konvertieren" nach JPEG (iPhone-Fotos sind HEIC, das
   nimmt der Dienst nicht an, §6). Dann "Inhalte von URL abrufen", Ziel
   `http://recipe-import.local:30082/import/file`, Methode `POST`, Anfragetext `Formular`,
   Feld `files` = das konvertierte Bild, Kopfzeile
   `Authorization: Bearer <IMPORT_TOKEN>`.
5. **Datei-Zweig:** wie der Bild-Zweig, ohne Umwandlung.
6. Keine Rückmeldung im Kurzbefehl auswerten. Die kommt asynchron per Push.

Das Token steht im Kurzbefehl, also auf beiden Geräten. Das ist derselbe Rang wie die
Webhook-ID: ein geteiltes Geheimnis im LAN beziehungsweise Tailnet, kein Benutzerkonto.
Es wird wie die Webhook-ID behandelt, niemals ins Repository.

Die LAN-Adresse gilt auf beiden Wegen: zuhause direkt, unterwegs über den
Tailscale-Subnetz-Router, der das Heimnetz ins Tailnet traegt. Das gilt für
Home Assistant wie für recipe-import. Tailscale muss
auf dem Geraet aktiv sein.

Geprueft vom LAN aus: `GET http://homeassistant.local:8123/api/` ohne Token
antwortet `401`, Home Assistant ist also erreichbar. Der Weg von unterwegs ist noch
offen und wird bei der ersten Nutzung des Kurzbefehls über Mobilfunk belegt.

---

## 10. Betrieb (A4)

Vierter Stack in der Struktur aus `umzug` T1, keine abweichende Konvention:

| Repo | eigener Docker-Host |
|---|---|
| `compose/recipe-import/compose.yaml` | `/srv/recipe-import/compose.yaml` |
| `compose/recipe-import/.env.example` | `/srv/recipe-import/.env`, Modus 600 |
| `compose/systemd/recipe-import.service` | `/etc/systemd/system/recipe-import.service` |
| – | `/var/lib/homelab/recipe-import/` als Bind-Mount auf `/data` |

- Port `30082`, nur auf dem LAN veröffentlicht.
- `tmpfs` auf `/tmp`, Grösse 256 MB. Schützt die SD-Karte vor yt-dlp-Schreibzugriffen.
- `restart: unless-stopped`, Healthcheck gegen `/healthz`.
- Speicherobergrenze `mem_limit: 512m`. Der Host hat rund 2 GB frei, und Paperless soll
  nicht durch diesen Dienst verdrängt werden.
- Die systemd-Unit folgt exakt dem Muster der drei vorhandenen unter `compose/systemd/`.
- Aufbewahrte Uploads geparkter Datei-Importe liegen unter `QUEUE_PAYLOAD_DIR`, per
  Vorgabe `/data/queue` und damit im bereits eingehängten Bind-Mount - kein neues
  Volume. Der Platzbedarf ist durch `QUEUE_PAYLOAD_MAX_MB` (Vorgabe 100 MB) begrenzt,
  und jeder Eintrag verschwindet mit dem Endzustand seines Imports, spätestens mit
  `QUEUE_MAX_AGE_HOURS` (A20).

---

## 11. Ratenbegrenzung

`app.py` weist `POST /import` **und** `POST /import/file` mit `429` ab, wenn
`store.recent_count(3600)` den Wert aus `RATE_LIMIT_PER_HOUR` erreicht. Ein gemeinsamer
Zähler, weil beide Wege dieselben LLM-Kosten auslösen - Bilder sogar teurer. Grund: die Webhook-ID ist die einzige Authentifizierung
des gesamten Wegs.

Gezählt werden angenommene Importe, keine Versuche: `recent_count` liest `created_at`,
und kein Weg der Warteschlange (§5) fasst dieses Feld an. Ein fälliger Versuch läuft
deshalb auch dann, wenn die Stundengrenze gerade ausgeschöpft ist, und erhöht den Zähler
nicht (A20). Wer sie kennt, könnte sonst beliebig kostenpflichtige LLM-Aufrufe
auslösen.

---

## 12. Tests (A5)

`pytest`, **ohne jeden Netzzugriff**. Alles Äussere wird ersetzt.

Fixtures einmalig aufzeichnen und ins Repository legen:

- eine deutsche Rezeptseite, die `recipe-scrapers` kennt
- eine Seite mit JSON-LD, aber ohne Unterstützung durch `recipe-scrapers`
- eine Seite ganz ohne strukturierte Daten
- eine SRT-Untertiteldatei aus einem Kochvideo, dazu dieselbe als WebVTT
- ein kleines PDF mit Textebene und ein winziges JPEG (wenige Kilobyte, kein echtes
  Rezeptfoto nötig - der LLM-Aufruf ist ersetzt)

Mindestens abzudecken:

1. `normalize_url` für alle vier YouTube-Formen und für Trackingparameter.
2. `url_hash` ist für zwei unterschiedlich geschriebene Fassungen derselben URL gleich.
3. Jede der drei `site.py`-Stufen trifft an ihrer Fixture.
4. Schema-Validierung weist leere Zutatenliste und leeren Namen zurück.
5. Der Idempotenzweg: derselbe Hash zweimal erzeugt genau einen Mealie-Aufruf.
6. Wiederaufnahme: ein `pending`-Eintrag beim Start wird erneut verarbeitet.
7. `ha_notify.notify` wirft auch dann nicht, wenn Home Assistant mit `500` antwortet.
8. Die Ratenbegrenzung greift beim konfigurierten Wert, über beide Endpunkte hinweg
   gezählt.
9. `document.read` liest den Text aus dem PDF, reicht ein JPEG als `images` durch und
   weist HEIC sowie einen zu grossen Upload zurück.
10. `POST /import/file` antwortet `401` ohne Token, `413` über `MAX_UPLOAD_MB` und `202`
    im Normalfall.
11. `content_hash` ist für dieselbe Datei gleich, für eine veränderte verschieden - eine
    zweimal geteilte Datei erzeugt kein Duplikat.
12. Die Namensstufe (A18): unbrauchbare Antworten werden verworfen, ein unveränderter
    Name löst kein `rename` aus, ein Ausfall der Stufe lässt den Import durchlaufen, und
    `NAMING_ENABLED=false` erzeugt keinen LLM-Aufruf. Weil die Stufe im Dienst
    standardmässig an ist, schaltet `conftest.py` sie für alle übrigen Tests ab - sonst
    liefe die Testreihe gegen das Netz.
13. Die Bildstufe (A19): ein Rezept ohne Bild bekommt eines und danach beide Tags, ein
    von Mealie geschabtes Bild bleibt unangetastet und erzeugt keinen Aufruf, und jeder
    Fehlschlag der Stufe (Anbieter, unbrauchbare Antwort, abgelehnter Upload) lässt den
    Import `done` mit unveränderter Rückmeldung und nur dem Tag `auto-import`. Ein
    zweites Teilen derselben Quelle kostet kein Bild. Wie die Namensstufe ist sie in
    `conftest.py` für alle übrigen Tests abgeschaltet.

---

## 13. Stil

Wie `deal-scraper/src/`, das ist die Referenz. Modul-Docstring, der den Zweck und die
getroffene Entscheidung nennt. Kommentare erklären das Warum, besonders dort, wo ein
Wert an der laufenden Anlage gemessen wurde. Keine Kommentare, die den Code
nacherzählen. Englische Bezeichner im Code, deutsche Texte in allem, was ein Mensch zu
sehen bekommt.

Protokollierung über `logging`, nie `print`. Kein Token und kein API-Key darf je in einer
Protokollzeile landen.

Den mechanischen Teil dieser Regeln erzwingt seit A22 `ruff` (gepinnt in
`requirements-dev.txt`, konfiguriert in `pyproject.toml`, in CI derselbe Aufruf wie
lokal: `ruff check src tests`). Eingeschaltet sind `E`, `F`, `I`, `B`, `BLE`, `C4` und
`RUF` bei einer Zeilenlänge von 110 - also Importreihenfolge, Zeilenlänge und die
fehlerträchtigen Muster, nicht der Wortlaut und nicht die Kommentardichte: das bleibt
eine menschliche Entscheidung, und einen Formatierer gibt es bewusst nicht.

`BLE001` bleibt absichtlich an. Die Stufen, die nie werfen dürfen (Namensstufe,
Aufräumarbeiten und alles, was nach dem Anlegen in Mealie noch folgt), fangen breit,
jede einen Vermerk in der Form `# noqa: BLE001 - <Grund>`, den `src/sources/site.py`
schon vorher benutzt hat. So bleibt der nächste unbedachte `except Exception` ein Fund,
statt in einer globalen Ausnahme unterzugehen. Dieselbe Form gilt für jede andere
Ausnahme von einer Regel: Code, Bindestrich, ein kurzer Grund auf Deutsch. `B008` ist
für FastAPIs `File`/`Header`/`Depends` in der Konfiguration ausgenommen, weil das dort
die vorgesehene Schreibweise ist.

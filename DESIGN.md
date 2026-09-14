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
Instagram (gestrichen am 2026-08-23, siehe Plan §2.10), kein Freitext-Eingang, keine Queue, kein Reverse Proxy, kein HTTPS, keine Weboberfläche, kein
Mehrbenutzerbetrieb.

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
| `LLM_MODEL` | nein | `gemini-3.6-flash` | fest verdrahtet, kein wandernder Alias |
| `LLM_API_KEY` | ja | – | |
| `DB_PATH` | nein | `/data/recipe-import.db` | |
| `RATE_LIMIT_PER_HOUR` | nein | `20` | harte Obergrenze, siehe §11 |
| `IMPORT_TOKEN` | ja | – | Bearer-Token für `POST /import/file`, siehe §6 und §11 |
| `MAX_UPLOAD_MB` | nein | `12` | Obergrenze je Anfrage über alle Dateien |
| `MAX_UPLOAD_FILES` | nein | `4` | mehr Bilder als das sind ein Fehler, kein Abschneiden |
| `MAX_PDF_PAGES` | nein | `10` | mehr Seiten werden nicht gelesen, siehe §6 |
| `NAMING_ENABLED` | nein | `true` | Namensstufe, siehe §5 und §6 `naming.make_name` |
| `LOG_LEVEL` | nein | `INFO` | |

Kein Secret landet je im Repository. `.env.example` enthält nur Platzhalter.

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
8.  mealie_client.set_tags(slug, ["auto-import"])
9.  store.finish(hash, slug, name)
10. ha_notify.notify(...)
```

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
```

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
  status     TEXT NOT NULL,      -- pending | done | failed
  slug       TEXT,
  title      TEXT,
  error      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
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
```

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

Beim Start: `store.init()`, danach jeden `pending`-Eintrag erneut in die Verarbeitung
geben. Damit übersteht ein Auftrag einen Neustart des Containers.

Die Verarbeitung selbst läuft über `BackgroundTasks`, keine Queue.

---

## 7. Rückmeldungstexte

Deutsch, knapp, immer mit Grund. Beispiele, wörtlich zu verwenden:

| Fall | Titel | Nachricht |
|---|---|---|
| Erfolg | `Rezept angelegt` | `<Name>` plus Link |
| Schon vorhanden | `Rezept schon vorhanden` | `<Name> wurde bereits importiert` plus Link |
| Kein Untertitel | `Import fehlgeschlagen` | `Das Video hat keine Untertitel, daraus lässt sich kein Rezept lesen.` |
| YouTube drosselt (HTTP 429) | `Import fehlgeschlagen` | `YouTube drosselt gerade die Untertitel. Bitte später erneut teilen.` |
| Kein Rezept erkennbar | `Import fehlgeschlagen` | `Auf der Seite war kein Rezept zu finden.` |
| LLM überlastet (HTTP 429/500/502/503/504, auch nach einem Wiederholungsversuch) | `Import fehlgeschlagen` | `Das Sprachmodell ist gerade überlastet. Bitte später erneut teilen.` |
| LLM-Fehler (sonst) | `Import fehlgeschlagen` | `Die Rezepterkennung ist gescheitert: <Grund>` |
| Mealie nicht erreichbar | `Import fehlgeschlagen` | `Mealie hat den Import abgelehnt: <Grund>` |
| Gescanntes PDF | `Import fehlgeschlagen` | `Dieses PDF enthält keinen lesbaren Text. Ein Foto der Seite funktioniert besser.` |
| Datei zu gross oder falscher Typ | `Import fehlgeschlagen` | `Diese Datei kann ich nicht lesen: <Grund>` |
| Kein Rezept auf dem Bild | `Import fehlgeschlagen` | `Auf dem Bild war kein Rezept zu erkennen.` |

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

---

## 11. Ratenbegrenzung

`app.py` weist `POST /import` **und** `POST /import/file` mit `429` ab, wenn
`store.recent_count(3600)` den Wert aus `RATE_LIMIT_PER_HOUR` erreicht. Ein gemeinsamer
Zähler, weil beide Wege dieselben LLM-Kosten auslösen - Bilder sogar teurer. Grund: die Webhook-ID ist die einzige Authentifizierung
des gesamten Wegs. Wer sie kennt, könnte sonst beliebig kostenpflichtige LLM-Aufrufe
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

---

## 13. Stil

Wie `deal-scraper/src/`, das ist die Referenz. Modul-Docstring, der den Zweck und die
getroffene Entscheidung nennt. Kommentare erklären das Warum, besonders dort, wo ein
Wert an der laufenden Anlage gemessen wurde. Keine Kommentare, die den Code
nacherzählen. Englische Bezeichner im Code, deutsche Texte in allem, was ein Mensch zu
sehen bekommt.

Protokollierung über `logging`, nie `print`. Kein Token und kein API-Key darf je in einer
Protokollzeile landen.

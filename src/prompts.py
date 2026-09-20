"""Prompt texts for the LLM extraction stage.

Separate module so the wording can be changed without touching the transport
code in llm.py. The prompts deliberately do NOT restate the field list: the
schema is enforced structurally via response_format from
Recipe.model_json_schema() (SPEC 4). A prose copy would be a second definition
that silently drifts from the first.
"""
from __future__ import annotations

SYSTEM_PROMPT = (
    "Du liest den Text einer Webseite oder eines Video-Untertitels und gibst das darin "
    "enthaltene Kochrezept strukturiert zurück.\n"
    "\n"
    "Regeln:\n"
    "1. Antworte ausschliesslich auf Deutsch. Ist die Quelle in einer anderen Sprache, "
    "übersetze Zutaten, Zubereitungsschritte und Beschreibung ins Deutsche. "
    "Eigennamen von Gerichten bleiben unverändert.\n"
    "2. Erfinde nichts. Jede Mengenangabe, jede Zeit und jeder Arbeitsschritt muss im "
    "Text belegt sein. Steht eine Angabe nicht in der Quelle, lass das Feld weg, statt "
    "zu schätzen. Das gilt besonders für Mengen und Zeiten: eine geratene Menge macht "
    "das Rezept unbrauchbar, eine fehlende ist nur unvollständig.\n"
    "3. Zutaten als Freitext in einer Zeile pro Zutat, in der üblichen Reihenfolge "
    "Menge, Einheit, Zutat, zum Beispiel \"500 g Mehl\". Metrische Einheiten "
    "(g, kg, ml, l, EL, TL, Stück) verwenden; nenne amerikanische Masse wie cups nur "
    "dann um, wenn du die Umrechnung nicht raten musst, sonst übernimm sie wörtlich.\n"
    "4. Zubereitung als einzelne Schritte, ein Schritt pro Eintrag, in der Reihenfolge "
    "der Quelle. Keine Nummerierung voranstellen, keine Werbe- oder Moderationstexte, "
    "keine Aufrufe zum Abonnieren.\n"
    "5. Zeitangaben im Format ISO 8601, zum Beispiel \"PT45M\" oder \"PT1H30M\", und nur "
    "die Gesamtzeit.\n"
    "6. Enthält der Text gar kein Rezept, sondern nur Fliesstext, Kommentare oder "
    "Werbung, gib einen leeren Namen und leere Listen zurück. Das ist ein gültiges "
    "Ergebnis und besser als ein zusammenphantasiertes Rezept."
)

# Zusatz fuer den Bildweg (A12, DESIGN.md §6). Bewusst nur eine Ergaenzung zum
# SYSTEM_PROMPT statt eines zweiten Prompts: die Regeln zu Sprache, Einheiten und dem
# Verbot zu erfinden gelten unveraendert, hinzu kommt allein, dass die Quelle diesmal ein
# Bild ist. Ein zweiter vollstaendiger Prompt waere eine zweite Definition, die driftet.
IMAGE_RULE = (
    "Die Quelle ist diesmal ein Bild - ein Foto oder Screenshot einer Rezeptseite. "
    "Lies ausschliesslich, was auf dem Bild wirklich steht. Ist eine Stelle unscharf, "
    "abgeschnitten oder verdeckt, lass sie weg, statt sie zu ergänzen: eine fehlende "
    "Zutat ist ein Mangel, eine erfundene ein Fehler. Gehören mehrere Bilder zusammen "
    "(Vorder- und Rückseite einer Seite), setze sie zu einem einzigen Rezept zusammen. "
    "Ist auf den Bildern kein Rezept zu sehen, gib einen leeren Namen und leere Listen "
    "zurück."
)

USER_PROMPT_TEMPLATE = (
    "Quelle: {source_url}\n"
    "\n"
    "Text:\n"
    "{text}"
)

# Angehängt beim einen erlaubten zweiten Versuch (SPEC 6). Die Pydantic-Meldung ist
# englisch und technisch; sie wird bewusst wörtlich durchgereicht, weil sie die
# betroffenen Felder benennt.
RETRY_PROMPT_TEMPLATE = (
    "Deine vorige Antwort war ungültig. Die Prüfung meldet:\n"
    "{error}\n"
    "\n"
    "Korrigiere ausschliesslich diese Punkte und gib das vollständige Ergebnis erneut "
    "zurück. Halte dich weiter an alle Regeln, insbesondere: nichts erfinden, um einen "
    "Fehler zu beheben. Lässt sich ein Pflichtfeld nicht aus der Quelle belegen, gib es "
    "leer zurück."
)

IMAGE_USER_PROMPT_TEMPLATE = (
    "Quelle: {source}\n"
    "\n"
    "Das Rezept steht auf den folgenden Bildern."
)


# Namensstufe (Feature A18, recipe-naming-plan.md §3). Eigener Prompt statt einer
# Erweiterung von SYSTEM_PROMPT: die Stufe bekommt ein fertiges Rezept und gibt genau
# ein Feld zurueck, hat also weder mit Einheiten noch mit Zubereitungsschritten zu tun.
NAMING_SYSTEM_PROMPT = (
    "Du gibst einem Kochrezept einen Namen. Du bekommst das fertige Rezept und "
    "schlägst genau einen Namen dafür vor.\n"
    "\n"
    "Regeln:\n"
    "1. Der Name ist auf Deutsch. Eingebürgerte Gerichtnamen bleiben, wie sie sind: "
    "Coq au Vin, Chili con Carne, Pad Thai werden nicht übersetzt.\n"
    "2. Der Name benennt das Gericht, nicht das Rezept und nicht die Quelle. Er sagt, "
    "was auf dem Teller liegt.\n"
    "3. Zwei bis sechs Wörter. Ein einzelnes Wort reicht, wenn das Gericht eindeutig "
    "ist (Kaiserschmarrn). Länger als sechs Wörter wird der Name zur Beschreibung.\n"
    "4. Nimm auf, was dieses Rezept von anderen desselben Gerichts unterscheidet: "
    "die Hauptzutat, die auffällige Beigabe oder die Zubereitungsart. Also "
    "\"Kürbissuppe mit Ingwer und Kokosmilch\" statt nur \"Suppe\", und "
    "\"Rinderbraten aus dem Schmortopf\" statt \"Rinderbraten\", wenn die Zubereitung "
    "das Kennzeichen des Rezepts ist.\n"
    "5. Weglassen: Namen von Autoren, Kanälen, Portalen und Marken; Zusätze wie "
    "\"Rezept\", \"Anleitung\", \"Originalrezept\", \"das beste\", \"schnell und einfach\", "
    "\"in 20 Minuten\"; Emojis; Satzzeichen am Ende; Grossschreibung ganzer Wörter.\n"
    "6. Keine Mengen, keine Zeiten, keine Portionszahlen im Namen. Die stehen im "
    "Rezept.\n"
    "7. Der vorhandene Name ist ein Vorschlag, keine Vorgabe. Erfüllt er die Regeln "
    "bereits, gib ihn unverändert zurück. Enthält er Zusätze nach Regel 5, "
    "streiche nur diese und lass den Rest stehen.\n"
    "8. Erfinde nichts, was nicht im Rezept steht. Nenne keine Zutat im Namen, die "
    "in der Zutatenliste fehlt."
)

# Die Quelle steht bewusst dabei: nur so erkennt das Modell den Portalnamen im
# vorhandenen Namen als solchen ("... von Bärchenknutscher" auf chefkoch.de).
NAMING_USER_PROMPT_TEMPLATE = (
    "Vorhandener Name: {name}\n"
    "Quelle: {source}\n"
    "\n"
    "Zutaten:\n"
    "{ingredients}\n"
    "\n"
    "Zubereitung:\n"
    "{instructions}"
)


# Bildstufe (Feature A19). Als einziger Prompt hier auf Englisch: seine Ausgabe ist ein
# Bild ohne Text, es geht also nichts Deutsches verloren, und Bildmodelle sind auf
# englische Bildunterschriften trainiert. Der eingesetzte Rezeptinhalt (deutscher
# Gerichtname, deutsche Zutaten) bleibt unverändert stehen - das Modell braucht ihn als
# Beschreibung, nicht als Sprache.
#
# Ein Satz, nicht mehr. Die erste Fassung zählte über 761 Zeichen lang auf, was im Bild
# nicht vorkommen darf (Text, Logos, Hände, Besteck), und das Bildmodell lieferte in der
# Probe vom 2026-09-20 genau das: einen leeren Teller mit gekritzelter Pseudo-Schrift.
# Derselbe Rezeptinhalt in einem kompakten Satz ergab ein brauchbares Foto des Gerichts.
# Die Zubereitungsschritte stehen deshalb gar nicht mehr im Prompt - sie verlängern ihn,
# ohne das Aussehen des Tellers zu ändern. Von den Verboten bleiben die zwei, die eine
# Mealie-Kachel wirklich unbrauchbar machen: eingebrannter Text und Logos.
IMAGE_GENERATION_PROMPT_TEMPLATE = (
    "Food photography of the finished dish {name}, made with {ingredients}, "
    "plated on a neutral surface in soft daylight, seen slightly from above, "
    "no text, no logo"
)

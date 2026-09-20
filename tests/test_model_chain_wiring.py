"""Die Modellkette im laufenden Dienst (Aufgaben 6.1 und 6.2).

Zwei Zusicherungen, die keiner der übrigen Tests trägt: der Start wartet nicht auf die
Modellsuche, und die Bildstufe bleibt von der Kette unberührt.
"""
from __future__ import annotations

import asyncio
import threading

import pytest
import requests

import app as app_module
import config
import image
import llm
import model_chain


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)


@pytest.fixture
def payload_dir(tmp_path, monkeypatch):
    """Aufbewahrte Uploads in ein Wegwerfverzeichnis, wie in test_retry_queue.py -
    `lifespan` räumt beim Start darin auf."""
    directory = tmp_path / "queue"
    monkeypatch.setattr(config, "QUEUE_PAYLOAD_DIR", str(directory))
    return directory


def test_start_wartet_nicht_auf_die_modellsuche(monkeypatch, isolated_store, payload_dir):
    """6.1: hängt die Modellliste, ist `/healthz` trotzdem sofort da.

    Die Liste wird hier wirklich blockiert (ein Ereignis, das nie gesetzt wird) und die
    Anfrage läuft in einem echten Thread - genau die Lage, die `asyncio.to_thread`
    abfangen soll. Ohne sie stünde die Ereignisschleife, und `/healthz` käme erst nach
    dem Zeitablauf des Anbieters.
    """
    monkeypatch.setattr(config, "LLM_MODEL_AUTODISCOVER", True)
    monkeypatch.setattr(config, "QUEUE_POLL_SECONDS", 3600)

    haengt = threading.Event()
    angefragt = threading.Event()

    def haengende_liste(*args, **kwargs):
        angefragt.set()
        haengt.wait(timeout=10)
        raise requests.RequestException("abgebrochen")

    monkeypatch.setattr(requests, "get", haengende_liste)

    async def run():
        async with app_module.lifespan(app_module.app):
            # Der Suchlauf ist unterwegs und hängt ...
            for _ in range(100):
                if angefragt.is_set():
                    break
                await asyncio.sleep(0.01)
            assert angefragt.is_set()
            # ... die Ereignisschleife antwortet trotzdem.
            antwort = await asyncio.wait_for(app_module.healthz(), timeout=1)
            haengt.set()
            return antwort

    try:
        assert asyncio.run(run()) == {"status": "ok"}
    finally:
        haengt.set()


def test_bildstufe_nutzt_weiter_ihr_eigenes_modell(monkeypatch):
    """6.2: die Kette hat das Textmodell gewechselt - die Bildstufe merkt davon nichts."""
    for model in config.LLM_MODEL_CHAIN[:-1]:
        model_chain.mark_exhausted(model)
    assert model_chain.head() == config.LLM_MODEL_CHAIN[-1]

    gefragt: list[str] = []

    class Antwort:
        status_code = 200
        content = b"\xff\xd8\xff" + b"0" * 2000
        text = ""

        def __init__(self):
            self.headers = {"Content-Type": "image/jpeg"}

        def json(self):
            return {}

    def fake_get(url, **kwargs):
        gefragt.append(kwargs["params"]["model"])
        return Antwort()

    monkeypatch.setattr(config, "IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "IMAGE_PROVIDER", "pollinations")
    monkeypatch.setattr(image.requests, "get", fake_get)

    assert image.generate("Apfelkuchen", ["360 g Mehl"]) is not None
    assert gefragt == [config.IMAGE_MODEL]
    assert config.IMAGE_MODEL not in model_chain.configured()

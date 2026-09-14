# recipe-import: FastAPI-Dienst, der eine URL entgegennimmt und über
# recipe-scrapers/extruct/trafilatura/yt-dlp plus LLM-Fallback ein Rezept in Mealie
# anlegt, siehe README.md. Reines Wheel-Image: kein Compiler, alle Abhängigkeiten sind
# arm64-Wheels (siehe requirements.txt).
FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Module liegen flach unter /app, keine "src."-Paketpräfixe - siehe app.py, das seine
# Geschwistermodule (config, store, classify, ...) direkt importiert.
COPY src/ ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# /data nimmt die SQLite-Datenbank auf (DB_PATH-Vorgabe /data/recipe-import.db).
VOLUME ["/data"]

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

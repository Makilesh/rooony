"""Populate the local VectorAI DB with fake activity records so the MCP
server can be exercised end-to-end before the real capture pipeline exists.

Run: python -m src.seed_sample_data
"""
import random
import uuid
from datetime import datetime, timedelta

from . import config, vectorstore

SAMPLE_ACTIVITIES = [
    ("coding", "VS Code", "Debugging pandas contact matching in the import script"),
    ("coding", "VS Code", "Writing unit tests for the embeddings module"),
    ("email", "Gmail", "Replying to teammate about the Actian schema"),
    ("email", "Gmail", "Reading a thread about the screenshot capture cadence"),
    ("meeting", "Zoom", "Standup call discussing MCP tool interface"),
    ("browsing", "Chrome", "Reading Actian VectorAI documentation"),
    ("design", "Figma", "Sketching the RAG query flow diagram"),
    ("coding", "Terminal", "Running docker compose for the local vector DB"),
]


def fake_embedding(dim: int) -> list[float]:
    v = [random.gauss(0, 1) for _ in range(dim)]
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


def main():
    vectorstore.ensure_collection()

    today = datetime.now().replace(microsecond=0, second=0)
    start_of_day = today.replace(hour=9, minute=0)

    count = 0
    for minutes in range(0, 8 * 60, 15):  # every 15 min, 9am-5pm
        ts = start_of_day + timedelta(minutes=minutes)
        activity, app, desc = random.choice(SAMPLE_ACTIVITIES)
        record = {
            "timestamp": ts.isoformat(),
            "embedding": fake_embedding(config.EMBED_DIM),
            "activity": activity,
            "application": app,
            "description": desc,
            "source": "screen_image",
        }
        vectorstore.upsert_activity(str(uuid.uuid4()), record)
        count += 1

    print(f"Seeded {count} sample activity records into '{config.COLLECTION}'.")


if __name__ == "__main__":
    main()

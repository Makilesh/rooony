"""Seed the local VectorAI DB with your 5 most recent real screenshots
(from C:\\Users\\khyaa_0k6t362\\OneDrive\\Pictures\\Screenshots), embedded
for real with CLIP — so `ask`/`search_by_text` can be tested with genuine
semantic similarity, not just plumbing.

The `activity`/`application`/`description` fields below were written by
looking at each screenshot once, by hand — a stand-in for whatever
OCR/captioning your teammates' capture pipeline will do automatically.
The `embedding` itself is real: computed from each image's actual pixels
via the same CLIP model `embed_text` uses for queries.

Run: python -m src.seed_real_screenshots
Run with --recreate the first time (or after changing EMBED_MODEL/EMBED_DIM)
to drop and recreate the collection at the right vector size:
    python -m src.seed_real_screenshots --recreate
"""
import argparse
import os
import uuid
from datetime import datetime

from . import config, vectorstore
from .embeddings import embed_image

SCREENSHOTS_DIR = r"C:\Users\khyaa_0k6t362\OneDrive\Pictures\Screenshots"

# The SDK requires string point IDs to be valid UUIDs, so we derive a stable
# one from each filename (uuid5 is deterministic — re-running this script
# updates the same points instead of creating duplicates).
_ID_NAMESPACE = uuid.NAMESPACE_URL

# (filename, activity, application, description)
RECORDS = [
    (
        "Screenshot 2026-09-06-1236.png",
        "coding",
        "Claude Code",
        "Reviewing Cosmolex import pipeline rewrite: contact dedupe logic "
        "(dedupe_by_cleaned_name, completeness-score tie-break vs old "
        "'last row wins' behavior)",
    ),
    (
        "Screenshot 2026-09-06 123600.png",
        "coding",
        "Claude Code",
        "Same Cosmolex import pipeline rewrite conversation, one minute "
        "earlier (near-duplicate capture)",
    ),
    (
        "Screenshot 2026-09-05 123714ukhdc.png",
        "browsing",
        "Kibana / Elastic Agent Builder",
        "Reading a 'Potter Answers' agent response about Quidditch and "
        "Hagrid's warning about Slytherin, in an Elasticsearch project",
    ),
    (
        "Screenshot 2026-09-05 123714.png",
        "browsing",
        "Kibana / Elastic Agent Builder",
        "Same Elastic Agent Builder 'Potter Answers' conversation "
        "(near-duplicate capture of the Quidditch/Slytherin answer)",
    ),
    (
        "Screenshot 2026-09-05 123624.png",
        "browsing",
        "Chrome",
        "Just submitted the Quidditch/Hagrid question to the 'Potter "
        "Answers' agent in Kibana, response still loading",
    ),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recreate", action="store_true",
                         help="drop and recreate the collection first (needed after an EMBED_MODEL/EMBED_DIM change)")
    args = parser.parse_args()

    vectorstore.ensure_collection(recreate=args.recreate)

    count = 0
    for filename, activity, application, description in RECORDS:
        path = os.path.join(SCREENSHOTS_DIR, filename)
        if not os.path.exists(path):
            print(f"skip (not found): {path}")
            continue

        ts = datetime.fromtimestamp(os.path.getmtime(path)).replace(microsecond=0)
        record = {
            "timestamp": ts.isoformat(),
            "embedding": embed_image(path),
            "activity": activity,
            "application": application,
            "description": description,
            "source": "screen_image",
            "screenshot_path": path,
        }
        point_id = str(uuid.uuid5(_ID_NAMESPACE, filename))
        vectorstore.upsert_activity(point_id, record)
        count += 1
        print(f"seeded: {ts.isoformat()}  {application}: {description[:60]}...")

    print(f"\nSeeded {count} real screenshot records (real CLIP embeddings) into '{config.COLLECTION}'.")


if __name__ == "__main__":
    main()

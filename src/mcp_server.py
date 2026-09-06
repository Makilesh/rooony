"""MCP server over the screen-memory database.

Two families of tools:

  Actian-native (unchanged from the MCP branch) - point-level lookups straight
  out of the vector DB, no SQLite involved:
    search_by_time(start_iso, end_iso, limit)
    search_by_text(query, limit, start_iso, end_iso)
    ask(question, limit)

  Memory API (memory.py) - session-level answers, ranked
  0.55*cosine + 0.30*entity_overlap + 0.15*recency, with the winner expanded to
  its whole thread arc. These work on either vector backend and degrade to
  SQLite when the vector layer is down:
    recall(query, limit)
    what_was_i_doing(minutes_ago)
    todays_receipt()

Prefer the memory API for "what was I doing / working on" questions: a session
carries a narrative and a resume_hint, a single point does not.

Run: python -m src.mcp_server
"""
import sys
import traceback
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory  # noqa: E402  (repo root)

from . import vectorstore  # noqa: E402
from .embeddings import embed_text  # noqa: E402
from .time_parse import extract_time_window  # noqa: E402

mcp = MCPServer("actian-screen-activity")


def _safe(fn, *args, **kwargs):
    """An MCP tool that raises kills the tool call; return the error instead."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(traceback.format_exc(), file=sys.stderr)
        return {"error": f"{type(e).__name__}: {e}",
                "hint": "run `uv run doctor.py` - the vector DB or the API key "
                        "is usually what's wrong"}


def _format_answer(matches: list[dict]) -> str:
    if not matches:
        return "I couldn't find any recorded activity matching that."
    lines = []
    for m in matches[:5]:
        ts = m.get("timestamp", "unknown time")
        app = m.get("application", "unknown app")
        desc = m.get("description", "")
        lines.append(f"- [{ts}] {app}: {desc}")
    return "Here's what was recorded:\n" + "\n".join(lines)


def _format_sessions(sessions: list[dict]) -> str:
    if not sessions:
        return "Nothing recorded for that."
    out = []
    for s in sessions[:5]:
        out.append(f"- [{s['started_at']} - {s['ended_at']}, {s['duration_min']}m] "
                   f"{s['title']}: {s['narrative']}")
        if s.get("resume_hint"):
            out.append(f"    next: {s['resume_hint']}")
    return "\n".join(out)


# --- Actian-native ---------------------------------------------------------
@mcp.tool()
def search_by_time(start_iso: str, end_iso: str, limit: int = 20) -> dict:
    """Look up recorded activity within an ISO 8601 time range (metadata-only, no embeddings)."""
    def go():
        matches = vectorstore.search_by_time(start_iso, end_iso, limit=limit)
        return {"answer": _format_answer(matches), "matches": matches}
    return _safe(go)


@mcp.tool()
def search_by_text(query: str, limit: int = 5, start_iso: str = None,
                   end_iso: str = None) -> dict:
    """Semantic search over recorded screen text, optionally within a time window."""
    def go():
        vector = embed_text(query)
        matches = vectorstore.search_by_vector(vector, limit=limit,
                                               start_iso=start_iso, end_iso=end_iso)
        return {"answer": _format_answer(matches), "matches": matches}
    return _safe(go)


@mcp.tool()
def ask(question: str, limit: int = 5) -> dict:
    """Answer a free-text question like 'what was I doing at 16:00?' by combining
    time-window and semantic search over individual captured frames."""
    def go():
        start_iso, end_iso = extract_time_window(question)
        if start_iso and end_iso:
            matches = vectorstore.search_by_time(start_iso, end_iso, limit=limit)
            if not matches:
                matches = vectorstore.search_by_vector(
                    embed_text(question), limit=limit,
                    start_iso=start_iso, end_iso=end_iso)
        else:
            matches = vectorstore.search_by_vector(embed_text(question), limit=limit)
        return {"answer": _format_answer(matches), "matches": matches}
    return _safe(go)


# --- memory API (session level) -------------------------------------------
@mcp.tool()
def recall(query: str, limit: int = 5) -> dict:
    """Recall work sessions matching a question. Parses any time window out of the
    query, searches vectors and exact entity matches, ranks them, and expands the
    best hit to its full thread arc. Use this for 'what was I working on' questions."""
    def go():
        out = memory.recall_context(query, limit=limit)
        out["answer"] = _format_sessions(out.get("results", []))
        return out
    return _safe(go)


@mcp.tool()
def what_was_i_doing(minutes_ago: int = 30) -> dict:
    """What was on screen N minutes ago: the session covering that moment, its
    frames, what interrupted it, and the literal next action to resume."""
    def go():
        out = memory.what_was_i_doing(minutes_ago)
        s = out.get("session")
        out["answer"] = _format_sessions([s]) if s else "Nothing recorded then."
        return out
    return _safe(go)


@mcp.tool()
def todays_receipt() -> dict:
    """Today's work, framed as what got done: focus minutes, threads, what was
    accomplished, what to pick up next."""
    def go():
        out = memory.todays_receipt()
        out["answer"] = out.get("headline", "")
        return out
    return _safe(go)


if __name__ == "__main__":
    mcp.run()

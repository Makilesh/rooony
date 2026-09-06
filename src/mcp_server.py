"""MCP server exposing read/RAG access to the local Actian VectorAI DB.

Tools:
  - search_by_time(start_iso, end_iso, limit): pure metadata range query.
  - search_by_text(query, limit, start_iso, end_iso): semantic search, optionally
    narrowed to a time window.
  - ask(question, limit): parses a free-text question for a time expression
    (e.g. "at 16:00", "around 4pm yesterday"); if found, does a time-window
    lookup, otherwise falls back to semantic search over the whole question.
    Returns both a plain-language answer and the structured matches.

Run: python -m src.mcp_server
"""
from mcp.server.mcpserver import MCPServer

from . import vectorstore
from .embeddings import embed_text
from .time_parse import extract_time_window

mcp = MCPServer("actian-screen-activity")


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


@mcp.tool()
def search_by_time(start_iso: str, end_iso: str, limit: int = 20) -> dict:
    """Look up recorded activity within an ISO 8601 time range (metadata-only, no embeddings)."""
    matches = vectorstore.search_by_time(start_iso, end_iso, limit=limit)
    return {"answer": _format_answer(matches), "matches": matches}


@mcp.tool()
def search_by_text(query: str, limit: int = 5, start_iso: str = None, end_iso: str = None) -> dict:
    """Semantic search over recorded activity descriptions/screenshots, optionally within a time window."""
    vector = embed_text(query)
    matches = vectorstore.search_by_vector(vector, limit=limit, start_iso=start_iso, end_iso=end_iso)
    return {"answer": _format_answer(matches), "matches": matches}


@mcp.tool()
def ask(question: str, limit: int = 5) -> dict:
    """Answer a free-text question like 'what was I working on in Gmail?' or
    'what was I doing at 16:00?' by combining time-window and semantic search."""
    start_iso, end_iso = extract_time_window(question)

    if start_iso and end_iso:
        matches = vectorstore.search_by_time(start_iso, end_iso, limit=limit)
        if not matches:
            # fall back to semantic search narrowed to that window
            vector = embed_text(question)
            matches = vectorstore.search_by_vector(vector, limit=limit, start_iso=start_iso, end_iso=end_iso)
    else:
        vector = embed_text(question)
        matches = vectorstore.search_by_vector(vector, limit=limit)

    return {"answer": _format_answer(matches), "matches": matches}


if __name__ == "__main__":
    mcp.run()

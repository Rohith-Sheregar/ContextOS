"""Development CLI for ContextOS commands before packaging."""
import argparse
import os
import sys

# Ensure project root is in sys.path to allow absolute imports of backend.*
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.core.memory_store import MemoryStore
from backend.app.core.database import init_db
from backend.app.daemon.agents.query import QueryAgent


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="contextos")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ask_parser = subparsers.add_parser("ask", help="Ask a question about indexed ContextOS memory.")
    ask_parser.add_argument("question", nargs="+", help="Natural language question to ask.")
    ask_parser.add_argument("--project", help="Restrict search to one project name.")
    ask_parser.add_argument("--raw", action="store_true", help="Return retrieved summaries without LLM synthesis.")
    ask_parser.add_argument(
        "--backfill",
        action="store_true",
        help="Index existing SQLite summaries into Chroma before asking.",
    )

    backfill_parser = subparsers.add_parser("backfill", help="Index existing SQLite summaries into Chroma.")
    backfill_parser.set_defaults(command="backfill")

    args = parser.parse_args(argv)
    init_db()
    memory = MemoryStore()
    if not memory.enabled:
        print("Error: MemoryStore is disabled. Install ChromaDB and sentence-transformers first.")
        return 1

    if args.command == "backfill":
        counts = memory.backfill_from_sqlite()
        print(
            "Backfill complete: "
            f"{counts['sessions']} final summaries, "
            f"{counts['mini_summaries']} mini summaries, "
            f"{counts['queued_retried']} queued retries indexed."
        )
        return 0

    if args.command == "ask":
        if args.backfill:
            counts = memory.backfill_from_sqlite()
            print(
                "Backfill complete: "
                f"{counts['sessions']} final summaries, "
                f"{counts['mini_summaries']} mini summaries, "
                f"{counts['queued_retried']} queued retries indexed."
            )

        question = " ".join(args.question)
        print(f"Querying ContextOS memory for: '{question}'...")
        agent = QueryAgent(memory)
        if args.raw:
            agent.api_key = None
        result = agent.ask(question, project_name=args.project)

        print(f"\n{result['answer']}\n")
        if result.get("sources"):
            print("--- Sources ---")
            for source in result["sources"]:
                timestamp = source["timestamp"][:10] if source.get("timestamp") else "unknown date"
                session = source["session_id"][:8] if source.get("session_id") else "unknown"
                project = source.get("project_name") or "unknown project"
                print(f"  [{timestamp}] Session {session}... ({project})")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

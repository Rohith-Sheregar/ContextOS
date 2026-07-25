"""
contextos CLI — the single entry point for all ContextOS commands.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.markdown import Markdown
    from rich.table import Table
    from rich.text import Text
    import questionary
    import pyperclip
    console = Console()
except ImportError:
    # Fallback to standard print if rich isn't installed for some reason
    class FallbackConsole:
        def print(self, *args, **kwargs):
            print(*args)
    console = FallbackConsole()
    Panel = lambda text, **kwargs: f"=== {kwargs.get('title', '')} ===\n{text}\n==================="
    Markdown = lambda text, **kwargs: text


def _setup_encoding():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------

def cmd_help(args):
    """Shows the beautiful interactive help menu."""
    help_text = """
Welcome to **ContextOS**! 🧠

ContextOS is a near-zero-footprint background daemon that silently records dev activity and lets you query your own work history in natural language.

### Core Commands

- `contextos start`  — Starts the daemon in the background to monitor your work.
- `contextos stop`   — Stops the daemon gracefully.
- `contextos status` — Shows daemon health and currently active sessions.

### Intelligence Commands

- `contextos ask "question"` — Query your memory. Example: `contextos ask "why did I change the DB logic?"`
- `contextos diary`          — Print the Dev Diary summary for the most recently completed session.
- `contextos backfill`       — Index existing SQLite summaries into the vector store.

*(Tip: Keep working normally in your terminal while ContextOS runs silently in the background!)*
"""
    console.print(Panel(Markdown(help_text), title="[bold cyan]ContextOS CLI Guide[/bold cyan]", border_style="cyan"))
    return 0

def _prompt_api_key_if_missing():
    from contextos.core.config import settings
    if not settings.OPENROUTER_API_KEY and not settings.GEMINI_API_KEY:
        console.print("[yellow]ContextOS requires an LLM API key to generate summaries and answer questions.[/yellow]")
        try:
            key = questionary.password("Please enter your OpenRouter or Gemini API key (or press Enter to skip): ").ask()
            if key and key.strip():
                env_file = Path.home() / ".contextos" / ".env"
                env_file.parent.mkdir(parents=True, exist_ok=True)
                with open(env_file, "a", encoding="utf-8") as f:
                    # Guess API based on prefix
                    if key.startswith("AIza"):
                        f.write(f"\nGEMINI_API_KEY={key.strip()}\n")
                    else:
                        f.write(f"\nOPENROUTER_API_KEY={key.strip()}\n")
                console.print(f"[green]Saved API key to {env_file}[/green]\n")
                # Reload settings if possible, or instruct user
                console.print("[cyan]API key configured! Continuing...[/cyan]")
        except Exception:
            pass

def cmd_interactive_menu(args):
    """Interactive TUI menu for ContextOS."""
    _prompt_api_key_if_missing()

    choices = [
        questionary.Choice("🧠 Ask a question", "ask"),
        questionary.Choice("📝 View recent Dev Diary", "diary"),
        questionary.Choice("📋 Export full context for AI (Clipboard)", "export"),
        questionary.Separator(),
        questionary.Choice("🚀 Start Daemon", "start"),
        questionary.Choice("🛑 Stop Daemon", "stop"),
        questionary.Choice("📊 Status", "status"),
        questionary.Choice("❓ Help", "help"),
        questionary.Choice("❌ Exit", "exit")
    ]

    action = questionary.select(
        "What would you like to do?",
        choices=choices,
        style=questionary.Style([
            ('qmark', 'fg:#673ab7 bold'),
            ('question', 'bold'),
            ('answer', 'fg:#f44336 bold'),
            ('pointer', 'fg:#673ab7 bold'),
            ('highlighted', 'fg:#673ab7 bold'),
            ('selected', 'fg:#cc5454'),
            ('separator', 'fg:#cc5454'),
            ('instruction', ''),
            ('text', ''),
        ])
    ).ask()

    if not action or action == "exit":
        return 0

    # Mock an args object for the commands
    class MockArgs:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    if action == "ask":
        question = questionary.text("What do you want to ask your memory?").ask()
        if question:
            return cmd_ask(MockArgs(question=[question], raw=False, backfill=False, project=None))
    elif action == "diary":
        return cmd_diary(MockArgs(project=None))
    elif action == "export":
        return cmd_export_context(MockArgs(project=None))
    elif action == "start":
        return cmd_start(MockArgs())
    elif action == "stop":
        return cmd_stop(MockArgs())
    elif action == "status":
        return cmd_status(MockArgs())
    elif action == "help":
        return cmd_help(MockArgs())

    return 0


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def cmd_start(args):
    """Forks the daemon into the background."""
    # Check if already running
    pid_file = settings.PID_FILE
    if pid_file.exists():
        try:
            payload = json.loads(pid_file.read_text())
            pid = payload.get("pid")
            
            import psutil
            try:
                process = psutil.Process(int(pid))
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    console.print(f"[yellow]ContextOS daemon appears to already be running (PID {pid}).[/yellow]")
                    console.print("Run [cyan]contextos status[/cyan] to verify, or [red]contextos stop[/red] to stop it.")
                    return 1
            except (psutil.NoSuchProcess, ValueError, TypeError):
                pass # Stale lock - process is dead
        except Exception:
            pass  # Stale lock — let the daemon clean it up

    daemon_script = Path(__file__).parent / "_daemon_process.py"
    log_file = settings.LOG_FILE
    log_file.parent.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        with open(log_file, "a") as log_out:
            proc = subprocess.Popen(
                [sys.executable, str(daemon_script)],
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdout=log_out,
                stderr=log_out,
                close_fds=False,
            )
    else:
        with open(log_file, "a") as log_out:
            proc = subprocess.Popen(
                [sys.executable, str(daemon_script)],
                stdout=log_out,
                stderr=log_out,
                start_new_session=True,
                close_fds=True,
            )

    console.print(f"[green]✅ ContextOS daemon started (PID {proc.pid}).[/green]")
    console.print(f"Logs: {log_file}")
    console.print("Run [cyan]contextos status[/cyan] to verify.")
    return 0


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

def cmd_stop(args):
    """Sends SIGTERM to the running daemon."""
    from contextos.core.config import settings

    pid_file = settings.PID_FILE
    if not pid_file.exists():
        console.print("[yellow]No ContextOS daemon appears to be running (no PID file found).[/yellow]")
        return 1

    try:
        payload = json.loads(pid_file.read_text())
        pid = int(payload["pid"])
    except Exception as e:
        console.print(f"[red]Could not read PID file: {e}[/red]")
        return 1

    try:
        if sys.platform == "win32":
            subprocess.call(["taskkill", "/PID", str(pid), "/F"])
        else:
            os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Sent stop signal to daemon (PID {pid}).[/green]")
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
        return 0
    except ProcessLookupError:
        console.print(f"[yellow]No process found with PID {pid}. Removing stale PID file.[/yellow]")
        pid_file.unlink(missing_ok=True)
        return 1
    except Exception as e:
        console.print(f"[red]Failed to stop daemon: {e}[/red]")
        return 1


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args):
    """Shows daemon status and last health snapshot."""
    from contextos.core.config import settings
    from contextos.core.database import get_db_conn, init_db

    init_db()

    pid_file = settings.PID_FILE
    if pid_file.exists():
        try:
            payload = json.loads(pid_file.read_text())
            pid = payload.get("pid")
            started_at = payload.get("started_at", "unknown")
            console.print(f"[bold green]✅ ContextOS daemon is RUNNING (PID {pid}, started {started_at})[/bold green]")
        except Exception:
            console.print("[bold yellow]⚠️  PID file exists but could not be read.[/bold yellow]")
    else:
        console.print("[bold red]❌ ContextOS daemon is NOT running.[/bold red]")

    try:
        with get_db_conn() as conn:
            # Event count
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            sessions_total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            console.print(f"\n[cyan]Database:[/cyan] {total} events, {sessions_total} sessions total.")
            console.print(f"[cyan]DB path:[/cyan]  {settings.DB_PATH}")

            # Active sessions
            rows = conn.execute("SELECT project_name, start_time FROM sessions WHERE status = 'ACTIVE'").fetchall()
            if rows:
                console.print(f"\n[bold blue]Active sessions ({len(rows)}):[/bold blue]")
                for r in rows:
                    console.print(f"  • [yellow]{r['project_name']}[/yellow] (since {r['start_time'][:19]})")
            else:
                console.print("\n[dim]No active sessions.[/dim]")

            # Last health snapshot
            health_row = conn.execute(
                "SELECT timestamp, cpu_percent, memory_rss_bytes, thread_count FROM daemon_health ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            
            if health_row:
                rss_mb = (health_row["memory_rss_bytes"] or 0) / (1024 * 1024)
                
                table = Table(title="Daemon Health Snapshot", title_style="bold magenta", border_style="magenta")
                table.add_column("Metric", style="cyan")
                table.add_column("Value", style="green")
                
                table.add_row("Timestamp", str(health_row['timestamp'][:19]))
                table.add_row("CPU", f"{health_row['cpu_percent']:.1f}%")
                table.add_row("Memory", f"{rss_mb:.1f} MB")
                table.add_row("Threads", str(health_row['thread_count']))
                
                console.print()
                console.print(table)

    except Exception as e:
        console.print(f"\n[red]Could not read database: {e}[/red]")

    return 0


# ---------------------------------------------------------------------------
# diary
# ---------------------------------------------------------------------------

def cmd_diary(args):
    """Prints the last session's Dev Diary summary."""
    from contextos.core.database import get_db_conn, init_db

    init_db()
    project = getattr(args, "project", None)

    try:
        with get_db_conn() as conn:
            if project:
                row = conn.execute(
                    "SELECT project_name, start_time, end_time, summary FROM sessions "
                    "WHERE status = 'COMPLETED' AND project_name = ? AND summary IS NOT NULL "
                    "ORDER BY end_time DESC LIMIT 1",
                    (project,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT project_name, start_time, end_time, summary FROM sessions "
                    "WHERE status = 'COMPLETED' AND summary IS NOT NULL "
                    "ORDER BY end_time DESC LIMIT 1"
                ).fetchone()

        if not row or not row["summary"]:
            console.print("[yellow]No completed session with a summary found yet.[/yellow]")
            return 0

        title = f"[bold cyan]Dev Diary — {row['project_name']}[/bold cyan]"
        subtitle = f"[dim]Session: {row['start_time'][:19]} → {(row['end_time'] or '')[:19]}[/dim]"
        
        console.print()
        console.print(Panel(Markdown(row["summary"]), title=title, subtitle=subtitle, border_style="blue"))
        console.print()
        return 0
    except Exception as e:
        console.print(f"[red]Error reading diary: {e}[/red]")
        return 1


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------

def cmd_ask(args):
    """Query ContextOS memory in natural language."""
    from contextos.core.database import init_db
    from contextos.core.memory_store import MemoryStore
    from contextos.daemon.agents.query import QueryAgent

    init_db()
    memory = MemoryStore()
    if not memory.enabled:
        console.print("[red]Error: MemoryStore is disabled. Install ChromaDB and sentence-transformers first.[/red]")
        return 1

    if getattr(args, "backfill", False):
        counts = memory.backfill_from_sqlite()
        console.print(
            f"[green]Backfill complete:[/green] {counts['sessions']} final summaries, "
            f"{counts['mini_summaries']} mini summaries, "
            f"{counts['queued_retried']} queued retries indexed."
        )

    question = " ".join(args.question)
    console.print(f"[bold magenta]Querying ContextOS memory for:[/bold magenta] '{question}'...\n")
    
    agent = QueryAgent(memory)
    if getattr(args, "raw", False):
        agent.api_key = None
        
    with console.status("[cyan]Searching memories and synthesizing answer...[/cyan]"):
        result = agent.ask(question, project_name=getattr(args, "project", None))

    console.print(Panel(Markdown(result["answer"]), title="[bold green]Answer[/bold green]", border_style="green"))
    
    if result.get("sources"):
        console.print("\n[bold dim]--- Sources ---[/bold dim]")
        for source in result["sources"]:
            timestamp = source["timestamp"][:10] if source.get("timestamp") else "unknown date"
            session = source["session_id"][:8] if source.get("session_id") else "unknown"
            project = source.get("project_name") or "unknown project"
            console.print(f"  • [cyan][{timestamp}][/cyan] Session {session}... [yellow]({project})[/yellow]")
    return 0


# ---------------------------------------------------------------------------
# backfill & migrate
# ---------------------------------------------------------------------------

def cmd_backfill(args):
    from contextos.core.database import init_db
    from contextos.core.memory_store import MemoryStore

    init_db()
    memory = MemoryStore()
    if not memory.enabled:
        console.print("[red]Error: MemoryStore is disabled.[/red]")
        return 1

    with console.status("[cyan]Backfilling SQLite summaries into ChromaDB...[/cyan]"):
        counts = memory.backfill_from_sqlite()
        
    console.print(
        f"[green]Backfill complete:[/green] {counts['sessions']} final summaries, "
        f"{counts['mini_summaries']} mini summaries, "
        f"{counts['queued_retried']} queued retries indexed."
    )
    return 0

def cmd_migrate(args):
    from contextos.core.config import settings
    import shutil

    old_db = Path(args.source) if getattr(args, "source", None) else Path.cwd() / "data" / "contextos.db"
    new_db = settings.DB_PATH

    if not old_db.exists():
        console.print(f"[red]Source DB not found: {old_db}[/red]")
        return 1
    if new_db.exists():
        console.print(f"[red]Target already exists: {new_db}[/red]")
        console.print("Delete it first if you want to replace it.")
        return 1

    new_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(old_db), str(new_db))
    console.print(f"[green]Migrated:[/green] {old_db} → {new_db}")

    old_chroma = old_db.parent / "chroma"
    new_chroma = settings.CHROMA_DIR
    if old_chroma.exists() and not new_chroma.exists():
        shutil.copytree(str(old_chroma), str(new_chroma))
        console.print(f"[green]Migrated:[/green] {old_chroma} → {new_chroma}")

    console.print("[bold cyan]Migration complete. Run 'contextos status' to verify.[/bold cyan]")
    return 0


# ---------------------------------------------------------------------------
# export context
# ---------------------------------------------------------------------------

def cmd_export_context(args):
    """Exports recent ContextOS history to clipboard for use with another LLM."""
    from contextos.core.database import get_db_conn, init_db
    init_db()

    project = getattr(args, "project", None)
    context_parts = ["# ContextOS Developer Memory Export\n\nBelow is the recent context of my work sessions.\n"]

    try:
        with get_db_conn() as conn:
            # 1. Get the last 3 completed dev diaries
            if project:
                diaries = conn.execute(
                    "SELECT project_name, start_time, end_time, summary FROM sessions "
                    "WHERE status = 'COMPLETED' AND project_name = ? AND summary IS NOT NULL "
                    "ORDER BY end_time DESC LIMIT 3", (project,)
                ).fetchall()
            else:
                diaries = conn.execute(
                    "SELECT project_name, start_time, end_time, summary FROM sessions "
                    "WHERE status = 'COMPLETED' AND summary IS NOT NULL "
                    "ORDER BY end_time DESC LIMIT 3"
                ).fetchall()

            if diaries:
                context_parts.append("## Recent Completed Sessions (Dev Diaries)\n")
                for d in reversed(diaries):
                    context_parts.append(f"### Project: {d['project_name']} ({d['start_time'][:19]} -> {d['end_time'][:19]})\n")
                    context_parts.append(f"{d['summary']}\n\n")

            # 2. Get events from current active sessions
            active_sessions = conn.execute("SELECT session_id, project_name, start_time FROM sessions WHERE status = 'ACTIVE'").fetchall()
            
            if active_sessions:
                context_parts.append("## Current Active Session Events (Unsummarized)\n")
                for s in active_sessions:
                    context_parts.append(f"### Active Project: {s['project_name']} (started {s['start_time'][:19]})\n")
                    events = conn.execute(
                        "SELECT timestamp, source, event_type, file_path, payload FROM events "
                        "WHERE project_name = ? AND timestamp >= ? "
                        "ORDER BY timestamp ASC LIMIT 100", 
                        (s['project_name'], s['start_time'])
                    ).fetchall()
                    
                    if not events:
                        context_parts.append("*No events recorded yet.*\n\n")
                    else:
                        for e in events:
                            payload_str = e['payload']
                            if payload_str and len(payload_str) > 1000:
                                payload_str = payload_str[:1000] + "... (truncated)"
                            
                            context_parts.append(f"- [{e['timestamp'][:19]}] {e['source']} | {e['event_type']} | {e['file_path']}")
                            if payload_str and payload_str != "null":
                                context_parts.append(f"  - Payload: {payload_str}")
                        context_parts.append("\n")

        # Compile final string
        full_context = "\n".join(context_parts)
        
        try:
            import pyperclip
            pyperclip.copy(full_context)
            console.print("[bold green]✅ Full context copied to clipboard![/bold green]")
            console.print("[dim]You can now paste this directly into Claude, ChatGPT, or another LLM.[/dim]")
        except Exception as e:
            console.print(f"[yellow]Could not copy to clipboard (pyperclip error): {e}[/yellow]")
            console.print("[dim]Printing context to stdout instead:[/dim]\n")
            print(full_context)
            
        return 0
    except Exception as e:
        console.print(f"[red]Error exporting context: {e}[/red]")
        return 1

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    _setup_encoding()

    # If no arguments provided, show the interactive TUI menu
    if (not argv and len(sys.argv) == 1):
        return cmd_interactive_menu(None)

    parser = argparse.ArgumentParser(
        prog="contextos",
        description="ContextOS — query your own dev history in natural language.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("help", help="Show the interactive ContextOS guide.")
    subparsers.add_parser("start", help="Start the daemon in the background.")
    subparsers.add_parser("stop", help="Stop the running daemon.")
    subparsers.add_parser("status", help="Show daemon status and health metrics.")

    diary_p = subparsers.add_parser("diary", help="Print the last session's Dev Diary.")
    diary_p.add_argument("--project", help="Filter by project name.")

    ask_p = subparsers.add_parser("ask", help="Query ContextOS memory in natural language.")
    ask_p.add_argument("question", nargs="+", help="Natural language question.")
    ask_p.add_argument("--project", help="Restrict search to one project name.")
    ask_p.add_argument("--raw", action="store_true", help="Skip LLM synthesis, return raw summaries.")
    ask_p.add_argument("--backfill", action="store_true", help="Re-index SQLite summaries before asking.")

    subparsers.add_parser("backfill", help="Index existing SQLite summaries into ChromaDB.")

    migrate_p = subparsers.add_parser("migrate", help="Migrate old data/ directory to ~/.contextos/.")
    migrate_p.add_argument("--source", help="Path to old contextos.db (default: ./data/contextos.db).")

    args = parser.parse_args(argv)

    if not args.command:
        return cmd_help(args)

    dispatch = {
        "help": cmd_help,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "diary": cmd_diary,
        "ask": cmd_ask,
        "export": cmd_export_context,
        "backfill": cmd_backfill,
        "migrate": cmd_migrate,
    }

    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

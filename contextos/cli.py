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
    questionary = None
    pyperclip = None
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


def _is_interactive() -> bool:
    return bool(questionary and sys.stdin.isatty())


def _normalize_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _project_name_for_path(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve()
    return resolved.name or str(resolved)


def _load_trusted_projects() -> list[dict]:
    from contextos.core.config import settings

    path = settings.TRUSTED_PROJECTS_FILE
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    projects = data.get("projects", []) if isinstance(data, dict) else data
    normalized = []
    for item in projects:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict) or not item.get("path"):
            continue
        project_path = _normalize_path(item["path"])
        normalized.append({
            "name": item.get("name") or _project_name_for_path(project_path),
            "path": project_path,
            "trusted_at": item.get("trusted_at") or datetime.now(timezone.utc).isoformat(),
        })
    return normalized


def _save_trusted_projects(projects: list[dict]) -> None:
    from contextos.core.config import settings

    unique = {}
    for project in projects:
        path = _normalize_path(project["path"])
        unique[path.lower()] = {
            "name": project.get("name") or _project_name_for_path(path),
            "path": path,
            "trusted_at": project.get("trusted_at") or datetime.now(timezone.utc).isoformat(),
        }

    settings.TRUSTED_PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.TRUSTED_PROJECTS_FILE.write_text(
        json.dumps({"projects": list(unique.values())}, indent=2),
        encoding="utf-8",
    )


def _is_project_trusted(project_path: str | Path) -> bool:
    normalized = _normalize_path(project_path).lower()
    return any(project["path"].lower() == normalized for project in _load_trusted_projects())


def _trust_project(project_path: str | Path) -> None:
    projects = _load_trusted_projects()
    normalized = _normalize_path(project_path)
    if any(project["path"].lower() == normalized.lower() for project in projects):
        return
    projects.append({
        "name": _project_name_for_path(normalized),
        "path": normalized,
        "trusted_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_trusted_projects(projects)


def _untrust_project(project_name: str) -> None:
    projects = [
        project for project in _load_trusted_projects()
        if project.get("name") != project_name
    ]
    _save_trusted_projects(projects)


def _trusted_watch_paths(include_current: bool = True) -> list[str]:
    paths: list[str] = []
    for project in _load_trusted_projects():
        project_path = project["path"]
        if Path(project_path).exists() and project_path not in paths:
            paths.append(project_path)

    from contextos.core.config import settings
    for configured_path in settings.WATCH_PATHS:
        normalized = _normalize_path(configured_path)
        if Path(normalized).exists() and normalized not in paths:
            paths.append(normalized)

    current = _normalize_path(Path.cwd())
    if include_current and _is_project_trusted(current) and current not in paths:
        paths.append(current)

    return paths


def _ensure_current_project_trusted() -> bool:
    current = _normalize_path(Path.cwd())
    project_name = _project_name_for_path(current)

    if _is_project_trusted(current):
        return True

    if not _is_interactive():
        console.print(
            f"[red]ContextOS needs project access before watching '{project_name}'.[/red]"
        )
        console.print("Run [cyan]contextos[/cyan] in this folder and approve access.")
        return False

    console.print(f"\n[bold cyan]Project Access[/bold cyan] [dim]{current}[/dim]")
    allowed = questionary.confirm(
        f"Allow ContextOS to remember this project ({project_name})?",
        default=True,
    ).ask()

    if not allowed:
        console.print("[yellow]Project not added. ContextOS will not watch this folder.[/yellow]")
        return False

    _trust_project(current)
    console.print(f"[green]Project trusted:[/green] {project_name}")
    return True


def _env_file_upsert(key: str, value: str) -> Path:
    from contextos.core.config import settings

    env_file = settings.CONTEXTOS_HOME / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()

    prefix = f"{key}="
    replaced = False
    output = []
    for line in lines:
        if line.startswith(prefix):
            output.append(f"{key}={value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{key}={value}")

    env_file.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    return env_file


def _daemon_payload() -> dict:
    from contextos.core.config import settings

    try:
        return json.loads(settings.PID_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pid_is_running(pid) -> bool:
    try:
        import psutil
        process = psutil.Process(int(pid))
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def _stop_daemon_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.call(["taskkill", "/PID", str(pid), "/F"])
    else:
        os.kill(pid, signal.SIGTERM)


def _spawn_daemon(watch_paths: list[str]):
    from contextos.core.config import settings

    daemon_script = Path(__file__).parent / "_daemon_process.py"
    log_file = settings.LOG_FILE
    log_file.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["WATCH_PATHS"] = json.dumps(watch_paths or [_normalize_path(Path.cwd())])

    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        with open(log_file, "a", encoding="utf-8") as log_out:
            return subprocess.Popen(
                [sys.executable, str(daemon_script)],
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdout=log_out,
                stderr=log_out,
                close_fds=False,
                env=env,
            )

    with open(log_file, "a", encoding="utf-8") as log_out:
        return subprocess.Popen(
            [sys.executable, str(daemon_script)],
            stdout=log_out,
            stderr=log_out,
            start_new_session=True,
            close_fds=True,
            env=env,
        )


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------

def cmd_help(args):
    """Shows the help menu."""
    help_text = """
Welcome to **ContextOS**.

ContextOS is a local developer memory layer. It watches only projects you approve, writes everything to `~/.contextos`, and lets you ask your own work history in natural language.

### Workflow

```
cd your-project
contextos          # approve this project, pick up where you left off
contextos start    # start or refresh the background daemon
contextos ask "why did I change the auth logic?"
contextos diary    # read the last dev diary
contextos dashboard
contextos stop
contextos forget   # wipe this project from memory
```

> Advanced commands: `contextos log`, `contextos export`, `contextos status`
"""
    console.print(Panel(Markdown(help_text), title="[bold cyan]ContextOS[/bold cyan]", border_style="cyan"))
    return 0

def _prompt_api_key_if_missing():
    from contextos.core.config import settings
    if not settings.OPENROUTER_API_KEY and not settings.GEMINI_API_KEY:
        if not _is_interactive():
            return

        console.print("[yellow]Add an LLM API key for AI summaries and answers.[/yellow]")
        try:
            key = questionary.password("OpenRouter or Gemini API key (Enter to skip): ").ask()
            if key and key.strip():
                env_key = "GEMINI_API_KEY" if key.startswith("AIza") else "OPENROUTER_API_KEY"
                env_file = _env_file_upsert(env_key, key.strip())
                console.print(f"[green]Saved API key to {env_file}[/green]\n")
                setattr(settings, env_key, key.strip())
        except Exception:
            pass



def cmd_interactive_menu(args):
    """Interactive TUI menu for ContextOS."""
    
    banner = r"""
   ______            __             __  ____  _____
  / ____/___  ____  / /____  _  ___/ /_/ __ \/ ___/
 / /   / __ \/ __ \/ __/ _ \| |/_/ __/ / / /\__ \  
/ /___/ /_/ / / / / /_/  __/>  </ /_/ /_/ /___/ /  
\____/\____/_/ /_/\__/\___/_/|_|\__/\____//____/   
    """
    console.print(f"[bold cyan]{banner}[/bold cyan]")
    
    _prompt_api_key_if_missing()
    project_ready = _ensure_current_project_trusted()

    choices = [
        questionary.Choice("Ask your memory", "ask"),
        questionary.Choice("Open Dashboard", "dashboard"),
        questionary.Choice("View Dev Diary", "diary"),
        questionary.Separator(),
        questionary.Choice("Start / refresh project memory", "start", disabled=None if project_ready else "project access not approved"),
        questionary.Choice("Pause ContextOS", "stop"),
        questionary.Choice("Forget this project", "forget", disabled=None if project_ready else "project access not approved"),
        questionary.Choice("Help", "help"),
        questionary.Choice("Exit", "exit")
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
    elif action == "dashboard":
        return cmd_dashboard(MockArgs())
    elif action == "start":
        return cmd_start(MockArgs())
    elif action == "stop":
        return cmd_stop(MockArgs())
    elif action == "forget":
        return cmd_forget(MockArgs(project=_project_name_for_path(Path.cwd())))
    elif action == "help":
        return cmd_help(MockArgs())

    return 0


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def cmd_start(args):
    """Forks the daemon into the background."""
    from contextos.core.config import settings

    _prompt_api_key_if_missing()
    if not _ensure_current_project_trusted():
        return 1

    watch_paths = _trusted_watch_paths()
    settings.WATCH_PATHS = watch_paths

    pid_file = settings.PID_FILE
    if pid_file.exists():
        payload = _daemon_payload()
        pid = payload.get("pid")
        if _pid_is_running(pid):
            running_paths = {
                _normalize_path(path).lower()
                for path in payload.get("watch_paths", [])
                if path
            }
            desired_paths = {_normalize_path(path).lower() for path in watch_paths}

            if desired_paths and desired_paths.issubset(running_paths):
                console.print(f"[green]ContextOS is already running for this project (PID {pid}).[/green]")
                console.print(f"Dashboard: [cyan]http://{settings.DASHBOARD_HOST}:{settings.DASHBOARD_PORT}[/cyan]")
                return 0

            console.print("[cyan]Refreshing ContextOS so the trusted project list is active...[/cyan]")
            try:
                _stop_daemon_pid(int(pid))
                pid_file.unlink(missing_ok=True)
            except Exception as exc:
                console.print(f"[red]Could not refresh running daemon: {exc}[/red]")
                return 1
        else:
            pid_file.unlink(missing_ok=True)

    proc = _spawn_daemon(watch_paths)
    console.print(f"[green]ContextOS is watching {_project_name_for_path(Path.cwd())} (PID {proc.pid}).[/green]")
    console.print(f"Dashboard  [cyan]http://{settings.DASHBOARD_HOST}:{settings.DASHBOARD_PORT}[/cyan]")
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

    _prompt_api_key_if_missing()

    init_db()
    memory = MemoryStore()
    if not memory.enabled:
        console.print("[red]Error: MemoryStore is disabled. Install sqlite-vec and ONNX embedding dependencies first.[/red]")
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
        agent.disable_synthesis()
        
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

    with console.status("[cyan]Backfilling SQLite summaries into sqlite-vec...[/cyan]"):
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
# init (vscode auto-start)
# ---------------------------------------------------------------------------

def cmd_init(args):
    """Sets up VS Code tasks.json to automatically run 'contextos start' on folder open."""
    import json
    vscode_dir = Path(".vscode")
    vscode_dir.mkdir(exist_ok=True)
    
    tasks_file = vscode_dir / "tasks.json"
    
    task_def = {
        "label": "ContextOS: Auto-Start",
        "type": "shell",
        "command": "contextos start",
        "runOptions": {
            "runOn": "folderOpen"
        },
        "presentation": {
            "reveal": "never",
            "panel": "shared",
            "showReuseMessage": False,
            "clear": True
        },
        "isBackground": True,
        "problemMatcher": []
    }

    if tasks_file.exists():
        try:
            with open(tasks_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"version": "2.0.0", "tasks": []}
            
        if "tasks" not in data:
            data["tasks"] = []
            
        # Check if already exists
        for task in data.get("tasks", []):
            if task.get("label") == "ContextOS: Auto-Start":
                console.print("[yellow]VS Code auto-start task is already configured in .vscode/tasks.json[/yellow]")
                return 0
                
        data["tasks"].append(task_def)
    else:
        data = {
            "version": "2.0.0",
            "tasks": [task_def]
        }
        
    with open(tasks_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        
    console.print(f"[bold green]✅ Initialized ContextOS auto-start![/bold green]")
    console.print("[dim]Created or updated .vscode/tasks.json[/dim]")
    console.print("\n[yellow]Important Note:[/yellow] The next time you open this folder in VS Code, it will prompt you to [bold]'Allow Automatic Tasks in Folder'[/bold]. Click [bold]Allow[/bold] so ContextOS can start automatically!")
    return 0

# ---------------------------------------------------------------------------
# log & forget
# ---------------------------------------------------------------------------

def cmd_log(args):
    """Prints a chronological log of recent developer activity."""
    from contextos.core.database import get_db_conn, init_db
    init_db()

    project = getattr(args, "project", None)
    limit = getattr(args, "limit", 50)

    try:
        with get_db_conn() as conn:
            if project:
                rows = conn.execute(
                    "SELECT timestamp, source, event_type, file_path, payload FROM events "
                    "WHERE project_name = ? ORDER BY timestamp DESC LIMIT ?",
                    (project, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT timestamp, project_name, source, event_type, file_path, payload FROM events "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                ).fetchall()

        if not rows:
            console.print("[dim]No activity logged yet.[/dim]")
            return 0

        table = Table(title="Recent Activity Log" + (f" ({project})" if project else ""), box=None)
        table.add_column("Time", style="cyan")
        if not project:
            table.add_column("Project", style="magenta")
        table.add_column("Source", style="green")
        table.add_column("Event")
        table.add_column("File")

        for r in reversed(rows):
            time_str = r['timestamp'][:19].replace('T', ' ')
            src = r['source']
            etype = r['event_type']
            fpath = r['file_path']
            
            if not project:
                table.add_row(time_str, r['project_name'], src, etype, fpath)
            else:
                table.add_row(time_str, src, etype, fpath)
        
        console.print(table)
        return 0
    except Exception as e:
        console.print(f"[red]Error reading log: {e}[/red]")
        return 1

def cmd_forget(args):
    """Deletes all data for a specific project."""
    from contextos.core.database import delete_project_data, init_db
    init_db()
    project = getattr(args, "project", None) or _project_name_for_path(Path.cwd())

    if not _is_interactive():
        console.print("[red]For safety, run this command in an interactive terminal.[/red]")
        return 1
    
    confirm = questionary.confirm(
        f"⚠️ Are you sure you want to permanently delete all history and summaries for project '{project}'?",
        default=False
    ).ask()

    if not confirm:
        console.print("[dim]Aborted.[/dim]")
        return 0

    try:
        counts = delete_project_data(project)
        _untrust_project(project)
        console.print(f"[green]✅ Forgot project '{project}'.[/green]")
        console.print(f"[dim]Deleted {counts['events']} events, {counts['sessions']} sessions, {counts['vectors']} vector documents.[/dim]")
        return 0
    except Exception as e:
        console.print(f"[red]Error deleting project data: {e}[/red]")
        return 1

# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

def cmd_dashboard(args):
    """Opens the ContextOS local web dashboard."""
    from contextos.core.config import settings

    host = settings.DASHBOARD_HOST
    port = settings.DASHBOARD_PORT
    url = f"http://{host}:{port}"

    if not settings.DASHBOARD_ENABLED:
        console.print("[yellow]Dashboard is disabled (DASHBOARD_ENABLED=false in .env).[/yellow]")
        console.print("Set [cyan]DASHBOARD_ENABLED=true[/cyan] in ~/.contextos/.env and restart the daemon.")
        return 1

    console.print(f"[bold green]📊 ContextOS Dashboard[/bold green] → [cyan]{url}[/cyan]")
    console.print("[dim]Make sure the daemon is running ([cyan]contextos start[/cyan]) for live data.[/dim]\n")

    import webbrowser
    try:
        webbrowser.open(url)
        console.print("[green]Opened in your default browser.[/green]")
    except Exception:
        console.print(f"[yellow]Could not open browser automatically.[/yellow]")
        console.print(f"Navigate to [bold cyan]{url}[/bold cyan] manually.")

    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _run_advanced_command(command: str, rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"contextos {command}")

    if command == "log":
        parser.add_argument("--project", help="Filter by project name.")
        parser.add_argument("--limit", type=int, default=50, help="Number of events to show (default: 50).")
        return cmd_log(parser.parse_args(rest))

    if command == "migrate":
        parser.add_argument("--source", help="Path to old contextos.db (default: ./data/contextos.db).")
        return cmd_migrate(parser.parse_args(rest))

    if command == "export":
        parser.add_argument("--project", help="Filter by project name.")
        return cmd_export_context(parser.parse_args(rest))

    if command == "backfill":
        return cmd_backfill(parser.parse_args(rest))

    if command == "init":
        return cmd_init(parser.parse_args(rest))

    return 1


def main(argv: list[str] | None = None) -> int:
    _setup_encoding()

    args_list = list(sys.argv[1:] if argv is None else argv)

    # If no arguments provided, show the interactive TUI menu
    if not args_list:
        return cmd_interactive_menu(None)

    advanced_commands = {"log", "export", "backfill", "migrate", "init"}
    if args_list[0] in advanced_commands:
        return _run_advanced_command(args_list[0], args_list[1:])

    parser = argparse.ArgumentParser(
        prog="contextos",
        description="A near-zero-footprint background daemon for developer memory."
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("start", help="Start or refresh project memory.")
    subparsers.add_parser("stop", help="Pause ContextOS.")
    subparsers.add_parser("status", help="Show daemon health and active sessions.")
    subparsers.add_parser("dashboard", help="Open the local ContextOS dashboard.")
    subparsers.add_parser("help", help="Show the ContextOS workflow.")

    diary_p = subparsers.add_parser("diary", help="Print the last session's Dev Diary.")
    diary_p.add_argument("--project", help="Filter by project name.")

    ask_p = subparsers.add_parser("ask", help="Ask your ContextOS memory.")
    ask_p.add_argument("question", nargs="+", help="Natural language question.")
    ask_p.add_argument("--project", help="Restrict search to one project name.")
    ask_p.add_argument("--raw", action="store_true", help=argparse.SUPPRESS)
    ask_p.add_argument("--backfill", action="store_true", help=argparse.SUPPRESS)

    forget_p = subparsers.add_parser("forget", help="Forget this project.")
    forget_p.add_argument("--project", help=argparse.SUPPRESS)

    args = parser.parse_args(args_list)

    if not args.command:
        return cmd_help(args)

    dispatch = {
        "help": cmd_help,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "diary": cmd_diary,
        "log": cmd_log,
        "forget": cmd_forget,
        "ask": cmd_ask,
        "export": cmd_export_context,
        "init": cmd_init,
        "backfill": cmd_backfill,
        "migrate": cmd_migrate,
        "dashboard": cmd_dashboard,
    }

    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

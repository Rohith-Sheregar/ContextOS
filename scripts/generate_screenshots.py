import os
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def generate_svgs():
    os.makedirs("assets", exist_ok=True)
    
    # 1. Ask Command SVG
    console = Console(record=True, width=100)
    console.print("[bold magenta]Querying ContextOS memory for:[/bold magenta] 'why did I change the DB logic?'...\n")
    
    ans = """The database logic was updated to fix a race condition where background threads attempted to flush empty event batches during SQLite lock contention.

An **exponential backoff** and a **retry buffer** were added to prevent dropping events when the database is locked. This ensures high-concurrency writes work reliably."""

    console.print(Panel(Markdown(ans), title="[bold green]Answer[/bold green]", border_style="green"))
    console.print("\n[bold dim]--- Sources ---[/bold dim]")
    console.print("  • [cyan][2024-05-12][/cyan] Session a9b3f2e1... [yellow](MyWebApp)[/yellow]")
    console.print("  • [cyan][2024-05-12][/cyan] Session c71a29f8... [yellow](MyWebApp)[/yellow]")
    
    console.save_svg("assets/ask_screenshot.svg", title="contextos ask")

    # 2. Status Command SVG
    console2 = Console(record=True, width=100)
    console2.print("[bold green]✅ ContextOS daemon is RUNNING (PID 14932, started 2024-05-12T10:00:00)[/bold green]")
    console2.print("\n[cyan]Database:[/cyan] 15,342 events, 84 sessions total.")
    console2.print("[cyan]DB path:[/cyan]  ~/.contextos/data/contextos.db")
    console2.print("\n[bold blue]Active sessions (1):[/bold blue]")
    console2.print("  • [yellow]MyWebApp[/yellow] (since 2024-05-12T10:15:00)")
    
    table = Table(title="Daemon Health Snapshot", title_style="bold magenta", border_style="magenta")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Timestamp", "2024-05-12T11:42:15")
    table.add_row("CPU", "0.5%")
    table.add_row("Memory", "45.2 MB")
    table.add_row("Threads", "12")
    console2.print()
    console2.print(table)
    
    console2.save_svg("assets/status_screenshot.svg", title="contextos status")

    # 3. Diary Command SVG
    console3 = Console(record=True, width=100)
    summary = """- Implemented exponential backoff for the SQLite `EventQueue` flush worker.
- Added `tests/unit/test_orchestrator.py` to cover idle timeout boundaries.
- **Decision:** Used parameterized SQL queries to prevent SQL injection and improve parsing speed."""
    
    console3.print()
    console3.print(Panel(Markdown(summary), title="[bold cyan]Dev Diary — MyWebApp[/bold cyan]", subtitle="[dim]Session: 2024-05-12T09:00:00 → 2024-05-12T11:30:00[/dim]", border_style="blue"))
    console3.print()
    console3.save_svg("assets/diary_screenshot.svg", title="contextos diary")

if __name__ == "__main__":
    generate_svgs()

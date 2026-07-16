"""Quick health snapshot for README reporting."""
import sqlite3
from pathlib import Path

DB = Path("data/contextos.db")
if not DB.exists():
    print("No database found. Run the daemon first.")
    exit(1)

conn = sqlite3.connect(DB)

# Health samples
rows = conn.execute(
    "SELECT timestamp, cpu_percent, memory_rss_bytes/(1024.0*1024.0), memory_percent, thread_count, open_files "
    "FROM daemon_health ORDER BY timestamp DESC LIMIT 10"
).fetchall()

if rows:
    print("=== Last 10 Health Snapshots ===")
    print(f"{'Timestamp':<30} {'CPU%':>6} {'RSS MB':>8} {'Mem%':>6} {'Threads':>8} {'Files':>6}")
    print("-" * 75)
    for r in rows:
        print(f"{r[0]:<30} {r[1]:>6.2f} {r[2]:>8.1f} {r[3]:>6.2f} {r[4]:>8} {r[5] or 'N/A':>6}")

    # Aggregates
    agg = conn.execute(
        "SELECT MIN(cpu_percent), AVG(cpu_percent), MAX(cpu_percent), "
        "AVG(memory_rss_bytes)/(1024.0*1024.0), MAX(memory_rss_bytes)/(1024.0*1024.0), "
        "COUNT(*) FROM daemon_health"
    ).fetchone()
    print(f"\n=== Aggregates ({agg[5]} samples) ===")
    print(f"CPU   — Min: {agg[0]:.2f}%   Avg: {agg[1]:.2f}%   Max: {agg[2]:.2f}%")
    print(f"RAM   — Avg: {agg[3]:.1f} MB   Max: {agg[4]:.1f} MB")
else:
    print("No health data found. Run the daemon for at least 60 seconds.")

# Counts
sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
health = conn.execute("SELECT COUNT(*) FROM daemon_health").fetchone()[0]
print(f"\nTotal sessions: {sessions}")
print(f"Total events:   {events}")
print(f"Total health samples: {health}")

conn.close()

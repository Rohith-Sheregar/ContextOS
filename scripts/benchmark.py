import os
import time
import psutil
from pathlib import Path
from contextos.core.config import settings
from contextos.core.database import get_db_conn

def benchmark():
    print("Starting ContextOS Benchmark...")
    print("This will test the daemon idle footprint and DB size.")
    
    # Measure DB sizes
    db_path = settings.DB_PATH
    chroma_path = settings.CHROMA_DIR
    
    db_size = db_path.stat().st_size if db_path.exists() else 0
    
    chroma_size = 0
    if chroma_path.exists():
        for f in chroma_path.rglob('*'):
            if f.is_file():
                chroma_size += f.stat().st_size
                
    with get_db_conn() as conn:
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        
    print(f"\n--- Storage Footprint ---")
    print(f"Events stored: {events}")
    print(f"Sessions stored: {sessions}")
    print(f"SQLite DB Size: {db_size / 1024:.1f} KB")
    print(f"Chroma DB Size: {chroma_size / 1024 / 1024:.2f} MB")
    
    # Calculate bytes per event (approximate)
    bytes_per_event = db_size / max(events, 1)
    print(f"Estimated SQLite bytes per event: {bytes_per_event:.0f} bytes")
    print(f"Estimated disk growth per 1000 events: {(bytes_per_event * 1000) / 1024:.1f} KB")
    
    # For CPU/RAM, check daemon_health
    print(f"\n--- Runtime Footprint (daemon_health table) ---")
    with get_db_conn() as conn:
        rows = conn.execute("SELECT cpu_percent, memory_rss_bytes, thread_count FROM daemon_health ORDER BY timestamp DESC LIMIT 5").fetchall()
        
    if not rows:
        print("No health records found in DB yet. Run the daemon for a minute first.")
        return
        
    avg_cpu = sum(r["cpu_percent"] for r in rows) / len(rows)
    avg_mem = sum(r["memory_rss_bytes"] for r in rows) / len(rows)
    avg_threads = sum(r["thread_count"] for r in rows) / len(rows)
    
    print(f"Average Idle CPU: {avg_cpu:.2f}%")
    print(f"Average Idle RAM: {avg_mem / 1024 / 1024:.1f} MB")
    print(f"Average Threads: {avg_threads:.1f}")

if __name__ == "__main__":
    benchmark()

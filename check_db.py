import sqlite3
import json
from pathlib import Path
from datetime import datetime

DB_PATH = Path("data/contextos.db")

if not DB_PATH.exists():
    print(f"Database not found at {DB_PATH}")
    exit(1)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

try:
    # Print Sessions
    cursor.execute("SELECT session_id, project_name, start_time, end_time, status, summary FROM sessions ORDER BY start_time DESC;")
    session_rows = cursor.fetchall()
    
    print("\n--- SESSIONS ---")
    if not session_rows:
        print("No sessions found.")
    else:
        for row in session_rows:
            session_id, project_name, start_time, end_time, status, summary = row
            print(f"[{status}] {project_name} | {session_id}")
            print(f"  Started: {start_time}")
            if end_time:
                print(f"  Ended:   {end_time}")
            if summary:
                print(f"  Summary: {summary}")
            print("-" * 50)
            
    # Print recent events
    cursor.execute("SELECT source, event_type, file_path, payload, timestamp FROM events ORDER BY timestamp DESC LIMIT 30;")
    rows = cursor.fetchall()

    print("\n--- LAST 30 EVENTS ---")
    if not rows:
        print("No events found. (Make sure you saved your changes and the flush interval has passed!)")
    
    for row in rows:
        source, event_type, file_path, payload_str, timestamp = row
        print(f"[{timestamp}] {source.upper()} | {event_type.upper()}")
        
        if file_path and file_path != "terminal" and file_path != "summarizer":
            print(f"  Path: {file_path}")
            
        if source == "terminal" and payload_str:
            try:
                payload = json.loads(payload_str)
                content = payload.get("content", "").strip()
                if content:
                    # Truncate content for display
                    if len(content) > 200:
                        content = content[:200] + "..."
                    print(f"  Output: {content}")
            except Exception:
                pass
                
        if source == "git" and payload_str:
            try:
                payload = json.loads(payload_str)
                if "message" in payload:
                    print(f"  Commit: {payload['message']}")
            except Exception:
                pass
                
        if source == "agent" and payload_str:
            try:
                payload = json.loads(payload_str)
                if "text" in payload:
                    print(f"  Mini-Summary: {payload['text']}")
            except Exception:
                pass
        
        print("-" * 50)
        
except sqlite3.OperationalError as e:
    print(f"Error reading database: {e}")
finally:
    conn.close()

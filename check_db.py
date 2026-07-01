import sqlite3
import os

db_path = os.path.join("data", "contextos.db")

if not os.path.exists(db_path):
    print(f"Database not found at {db_path}. Has the daemon created it yet?")
    exit()

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    # Assuming the table is named 'events' based on our architecture
    cursor.execute("SELECT timestamp, source, event_type, file_path, payload FROM events ORDER BY timestamp DESC LIMIT 30;")
    rows = cursor.fetchall()
    
    print("\n--- LAST 30 EVENTS ---")
    if not rows:
        print("No events found. (Make sure you saved your changes and the flush interval has passed!)")
    else:
        for row in rows:
            timestamp, source, event_type, file_path, payload_str = row
            try:
                import json
                payload = json.loads(payload_str)
                
                print(f"[{timestamp}] {source.upper()} | {event_type.upper()}")
                
                if source == "terminal":
                    content = payload.get("content", "").strip()
                    print("-" * 50)
                    print(content)
                    print("-" * 50 + "\n")
                else:
                    # Filesystem events
                    print(f"  Path: {file_path}")
                    if "dest_path" in payload:
                        print(f"  To:   {payload['dest_path']}")
                    print("-" * 50 + "\n")
                    
            except Exception:
                print(f"[{timestamp}] {source} : {event_type} -> {payload_str[:100]}...")
            
except sqlite3.OperationalError as e:
    print(f"Error reading database: {e}")
finally:
    conn.close()
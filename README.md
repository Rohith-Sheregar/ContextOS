# ContextOS

ContextOS is a local, near-zero-footprint developer memory daemon. It runs quietly in the background while you code, records useful development activity, summarizes your sessions, and lets you query your own work history in natural language.

**ContextOS does not track you to build a productivity dashboard. The only goal is recall.** 

It exists so you can do this:

```powershell
PS C:\Projects\MyWebApp> contextos ask "why did I change the queue flushing logic yesterday?"

Querying ContextOS memory for: 'why did I change the queue flushing logic yesterday?'...

The queue flushing logic was changed to fix a race condition where background threads 
would attempt to flush empty event batches during SQLite lock contention. You added 
an exponential backoff and a retry buffer to prevent dropping events when the 
database is locked.

--- Sources ---
  [2024-05-12] Session a9b3f... (MyWebApp)
  [2024-05-12] Session c71a2... (MyWebApp)
```

## Why This Helps

Modern development leaves context scattered across file changes, commits, terminal output, notes, and half-remembered decisions. ContextOS turns that activity into a searchable memory layer so you can:

- return to a project after a break and quickly remember where you left off
- reconstruct why a file, feature, or refactor changed
- generate automatic dev diary entries from real work sessions
- ask questions about previous work instead of digging through commits manually

## Installation

ContextOS requires Python 3.11+.

```bash
pip install contextos-daemon
```

This installs the `contextos` command-line tool. All configuration and persistent data will be stored safely outside your projects in `~/.contextos/`.

## Bring Your Own Model

ContextOS uses your own LLM API keys for session summarization and query synthesis. Currently, OpenRouter and Gemini are supported.

Create `~/.contextos/.env` and add your keys:

```env
OPENROUTER_API_KEY=your_openrouter_key_here
GEMINI_API_KEY=your_gemini_key_here
```

If no key is provided, ContextOS will still record events and index them, but will fall back to raw semantic retrieval without LLM synthesis when you use `contextos ask`.

## Usage

Start the daemon in any project directory. It runs completely in the background.

```powershell
PS C:\Projects\JarvisLauncher> contextos start
ContextOS daemon started (PID 14932).
Logs: C:\Users\Username\.contextos\logs\daemon.log
```

Keep working normally. ContextOS will silently monitor your filesystem, git commits, and terminal activity (ignoring noisy directories like `node_modules` or `.git`). 

When you pause for a while, ContextOS automatically closes the session and writes a Dev Diary.

Check on the daemon:
```powershell
PS C:\Projects\JarvisLauncher> contextos status
✅ ContextOS daemon is RUNNING (PID 14932, started 2024-05-12T10:00:00)

Active sessions (1):
  • JarvisLauncher (since 2024-05-12T10:15:00)
```

Query your memory:
```powershell
PS C:\Projects\JarvisLauncher> contextos ask "what was I debugging this morning?"
```

Read the latest session summary:
```powershell
PS C:\Projects\JarvisLauncher> contextos diary
```

Stop the daemon when you are done:
```powershell
PS C:\Projects\JarvisLauncher> contextos stop
```

## Performance & Footprint

A background daemon must be invisible. We built ContextOS to stay completely out of your way.

**Actual Idle Benchmark Results:**
- **CPU:** `0.5%` (The daemon sleeps and only wakes on OS-level file events)
- **RAM:** `~470 MB` (The bulk of this is the local PyTorch `sentence-transformers` model kept in memory for instant semantic embedding)
- **Disk:** `< 15 MB` per 1000 events. Uses SQLite with WAL-mode for high-concurrency writes, and a local ChromaDB instance for embeddings.

## Architecture

ContextOS is built on a robust asynchronous pipeline:

1. **Watchers:** `watchdog` (Filesystem), `gitpython` (Git), and custom Terminal transcript parsers feed raw events.
2. **Queue:** An async `EventQueue` batches activity to minimize SQLite I/O lock contention.
3. **Orchestrator:** The `SessionOrchestrator` groups events into sessions, automatically splitting them when you go idle for 30 minutes.
4. **Agents:** 
   - `SummarizerAgent` condenses events into mini-summaries and final Dev Diaries.
   - `CrossProjectAgent` searches other projects for similar past work while you code.
   - `ReentryAgent` writes a "Welcome Back" brief if you haven't touched a project in days.
5. **Memory:** Summaries are embedded via `sentence-transformers/all-MiniLM-L6-v2` and stored in a local `ChromaDB` vector store.

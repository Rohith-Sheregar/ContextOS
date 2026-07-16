# ContextOS

ContextOS is a local developer memory daemon. It runs quietly in the background while you code, records useful development activity, summarizes your sessions, and is being built toward a natural-language recall command:

```bash
contextos ask "why did I refactor the queue module last week?"
```

The core idea is not tracking for its own sake. The goal is recall: helping a developer recover the context, reasoning, blockers, and decisions behind their own work.

## Why This Helps

Modern development leaves context scattered across file changes, commits, terminal output, notes, and half-remembered decisions. ContextOS turns that activity into a searchable memory layer so you can:

- return to a project after a break and quickly remember where you left off
- reconstruct why a file, feature, or refactor changed
- generate automatic dev diary entries from real work sessions
- ask questions about previous work instead of digging through commits manually
- eventually carry useful memory across projects, not just inside one repository

The end product will be an installable package. A user should be able to install ContextOS, start it in whatever project they are currently working on, provide their own LLM API key, and have a local project memory system running with minimal setup.

## Current Status

ContextOS is currently in early daemon development.

| Phase | Status | Summary |
|---|---:|---|
| Phase 0: Baseline daemon | Done | File, git, and terminal watchers write events to SQLite. Sessions are started and completed automatically. Summaries are generated through an LLM provider when configured. |
| Phase 1: Harden what exists | Done | Added watcher-level ignore rules, health telemetry, SQLite retry handling, daemon PID locking, watcher restart supervision, git polling backoff, and unit tests. |
| Phase 2: Memory layer | Done | Added ChromaDB semantic memory, local embeddings, backfill, query agent, cited answers, and integration tests. |
| Phase 3: Re-entry and cross-project intelligence | Next | Generate return-to-project briefs and find similar previous work across projects. |
| Phase 4: Packaging and distribution | Planned | Ship as an installable package with `contextos start`, `contextos stop`, `contextos status`, `contextos ask`, and `contextos diary`. |
| Phase 5: Launch prep | Planned | Benchmark idle CPU/RAM/disk use, record a demo, and polish docs for public use. |

## What Works Today

- Watches filesystem changes with `watchdog`
- Watches git branch and commit changes with `gitpython`
- Watches terminal transcript files
- Batches events through an async queue before writing to SQLite
- Stores data in `data/contextos.db`
- Uses SQLite WAL mode for better local concurrency
- Starts a coding session on first activity
- Ends a session after an idle timeout
- Generates mini summaries and final dev diary summaries when an LLM key is configured
- Runs in dummy-summary mode when no LLM key is configured
- Embeds summaries into local ChromaDB memory with `sentence-transformers`
- Backfills existing SQLite summaries into semantic memory
- Answers natural-language questions over stored memories with cited sources
- Supports raw retrieval mode for testing without an LLM/API call
- Logs daemon health snapshots to a `daemon_health` table
- Prevents duplicate daemon processes with a `daemon.pid` lockfile
- Restarts failed watchers instead of letting one watcher crash the whole daemon
- Applies hard ignore rules at the watcher layer for noisy folders like `node_modules`, `.git`, `venv`, `__pycache__`, `build`, and `dist`
- Includes unit and integration tests for queueing, sessions, ignore rules, memory backfill, retrieval, and citations

## LLM Provider Model

ContextOS is designed as a bring-your-own-key tool. The user supplies their own API key for whichever LLM provider the project supports.

For current development, OpenRouter is the main configured provider because it can be used with free or low-cost models during testing:

```env
OPENROUTER_API_KEY=your_openrouter_key_here
```

The codebase also has early Gemini key support for the re-entry agent:

```env
GEMINI_API_KEY=your_gemini_key_here
```

Provider support is still evolving. The intended product direction is provider flexibility: users should be able to use their own preferred LLM provider or model, as long as ContextOS has an adapter for it.

## Architecture

Current repo layout:

```text
backend/
  run_daemon.py
  requirements.txt
  app/
    core/
      config.py
      database.py
      memory_store.py
      ignore.py
      lockfile.py
    daemon/
      observer.py
      queue.py
      orchestrator.py
      health.py
      watchers/
        filesystem.py
        git.py
        terminal.py
      agents/
        summarizer.py
        query.py
        reentry.py
  tests/
    unit/
    integration/
data/
  contextos.db
```

Key pieces:

- `DaemonObserver` starts the queue, watchers, orchestrator, health monitor, and watcher supervisor.
- `EventQueue` batches activity before writing to SQLite.
- `SessionOrchestrator` turns raw events into work sessions.
- `SummarizerAgent` creates mini summaries and final session summaries.
- `MemoryStore` stores summaries in local ChromaDB and can backfill existing SQLite summaries.
- `QueryAgent` retrieves relevant memories and returns answers with cited session sources.
- `HealthMonitor` records CPU, memory, thread count, and open file telemetry.
- `DaemonLock` prevents two daemon processes from writing to the same database.

## Development Setup

From the repo root:

```bash
python -m venv venv
.\venv\Scripts\activate
pip install -r backend\requirements.txt
```

Create `backend/.env`:

```env
OPENROUTER_API_KEY=your_openrouter_key_here
```

The API key is optional for local daemon testing. Without a key, ContextOS still records events and uses dummy summaries, but useful AI-generated memory requires a configured provider.

Run the daemon:

```bash
python backend/run_daemon.py
```

View stored sessions and summaries:

```bash
python check_db.py
```

Backfill existing summaries into semantic memory:

```bash
python backend/cli.py backfill
```

Ask a question with raw retrieved summaries, without making an LLM call:

```bash
python backend/cli.py ask --raw "why did I change the database retry logic?"
```

Ask a question with LLM synthesis when `OPENROUTER_API_KEY` is configured:

```bash
python backend/cli.py ask "why did I change the database retry logic?"
```

Run the tests:

```bash
.\venv\Scripts\python.exe -m pytest backend\tests
```

## Build Roadmap

### Phase 0: Baseline daemon - completed

The first version proved the daemon loop:

- filesystem watcher
- git watcher
- terminal transcript watcher
- async event queue
- SQLite storage
- automatic session start and completion
- mini summaries and final dev diary generation

### Phase 1: Reliability and observability - completed

Phase 1 hardened the daemon before adding bigger features:

- added `psutil` health monitoring
- added `daemon_health` SQLite table
- added configurable polling and git idle backoff
- added watcher-level hard ignores
- added SQLite timeout and retry handling
- added watcher restart supervision
- added `daemon.pid` lockfile
- added unit tests for queue, orchestrator, and ignore matching

### Phase 2: Memory layer - completed

This phase turns session summaries into queryable semantic memory:

- added local embeddings with `sentence-transformers/all-MiniLM-L6-v2`
- added ChromaDB as a local persistent vector store
- added `MemoryStore` around SQLite plus Chroma
- embedded mini summaries and final summaries as they are generated
- added backfill for existing SQLite summaries
- added an embedding retry queue for degraded operation
- added `QueryAgent` for natural-language recall
- added cited answers with project/date/session sources
- added a development CLI through `python backend/cli.py ask "..."`
- added integration tests for memory retrieval, project filtering, backfill, and query citations

### Phase 3: Re-entry and cross-project intelligence

Once memory search exists:

- generate a short brief when returning to a stale project
- compare new summaries against memories from other projects
- surface similar past solutions when relevant

### Phase 4: Package and distribute

The intended end product is not a repo script. It should become an installable package:

```bash
pipx install contextos
contextos start
contextos status
contextos ask "what was I debugging yesterday?"
contextos diary
contextos stop
```

At that point, ContextOS should work from any project directory the user is currently working in. Configuration should live in the user's home directory, such as `~/.contextos/config.toml`, and data should live outside individual repos, such as `~/.contextos/data.db`.

### Phase 5: Launch prep

Before a public launch:

- benchmark idle CPU usage
- benchmark RAM footprint
- estimate disk growth per day
- record a short demo of `contextos ask`
- rewrite public docs around the main differentiator: ContextOS does not just track what you did, it helps you ask why

## Testing

Current unit test coverage:

- queue batching and flush timing
- queue retry behavior after failed writes
- session state transitions from active to completed
- mini-summary trigger timing
- watcher ignore matching for noisy paths

Run:

```bash
.\venv\Scripts\python.exe -m pytest backend\tests
```

Current integration test coverage:

- integration tests for SQLite plus vector memory
- deterministic retrieval tests for `contextos ask`
- project-filtered retrieval
- SQLite-to-Chroma backfill
- disabled-memory retry queue behavior

Future test layers:

- package-level smoke tests for fresh install, start, track, ask, and stop

## Product Direction

The finished ContextOS experience should feel like this:

1. Install the package.
2. Add your own LLM API key.
3. Start ContextOS inside any project.
4. Keep working normally.
5. Come back later and ask natural-language questions about your own development history.

ContextOS should stay low-footprint, local-first, and focused on memory recall. Dashboards, extra watchers, and visual analytics are secondary until the query-and-recall loop works well.

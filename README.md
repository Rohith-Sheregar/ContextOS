<p align="center">
  <h1 align="center">🧠 ContextOS</h1>
  <p align="center">
    <strong>A local, near-zero-footprint developer memory daemon.</strong><br>
    It runs quietly in the background while you code, records what you touch, summarizes your sessions, and lets you ask your own work history questions in plain English.
  </p>
  <p align="center">
    <a href="https://pypi.org/project/contextos-daemon/"><img src="https://img.shields.io/pypi/v/contextos-daemon.svg?style=for-the-badge&logo=pypi&color=blue" alt="PyPI"></a>
    <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.11+-blue.svg?style=for-the-badge&logo=python&logoColor=white" alt="Python version"></a>
    <a href="https://github.com/rohith-sheregar/ContextOS/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge" alt="License"></a>
  </p>
</p>

ContextOS does not build a productivity dashboard, does not score you, and does not phone home. The only goal is recall.

<p align="center">
  <img src="assets/ask_screenshot.svg" alt="ContextOS Ask Query Example" width="80%">
</p>

---

## ⚡ Why this exists

Development context is scattered across file diffs, commit messages, terminal scrollback, and half-remembered decisions. Most of it evaporates the moment you close your laptop. ContextOS turns that activity into a searchable memory layer so you can:

- **Return to a project after a break** and know exactly where you left off.
- **Reconstruct *why* a file or feature changed**, not just *that* it changed.
- **Get an automatically written Dev Diary** for every session, with zero manual effort.
- **Ask questions about past work** instead of archaeologizing through `git log`.

## ✨ How it's different

Most background dev-activity tools (time trackers, usage analytics, AI-coding-session loggers) answer *"what did I do and for how long."* ContextOS answers *"why did I do it,"* on demand, in natural language, grounded in your own history — not a leaderboard, not a report you'll never read.

---

## 🚀 Installation

Requires Python 3.11+.

```bash
pip install contextos-daemon
```

This installs the `contextos` CLI. All data and configuration live outside your projects, in `~/.contextos/` — the daemon never writes into a directory it's watching.

### Bring your own model

ContextOS uses your own LLM API key for summarization and query synthesis. OpenRouter and Gemini are currently supported.

```bash
# ~/.contextos/.env
OPENROUTER_API_KEY=your_key_here
GEMINI_API_KEY=your_key_here
```

*No key configured?* ContextOS still records and semantically indexes everything — `contextos ask` just returns the raw retrieved summaries instead of an LLM-synthesized answer.

---

## 💻 Usage

Start the daemon in any project directory:

```bash
$ contextos start
```

Keep working normally. ContextOS watches your filesystem, git activity, and terminal output in the background, ignoring noise like `node_modules`, `.git`, and build artifacts. When you go idle, it closes the session and writes a Dev Diary automatically.

<p align="center">
  <img src="assets/status_screenshot.svg" alt="ContextOS Status Overview" width="80%">
</p>

```bash
$ contextos ask "what was I debugging this morning?"
$ contextos diary                 # latest Dev Diary
$ contextos backfill              # re-index existing history into the vector store
$ contextos stop
```

<p align="center">
  <img src="assets/diary_screenshot.svg" alt="ContextOS Diary Output" width="80%">
</p>

---

## 🏗️ Architecture

```mermaid
graph TD
    subgraph Watchers
    FS[Filesystem]
    GT[Git]
    TM[Terminal]
    end

    EQ[EventQueue<br>batched, WAL-mode]
    DB[(SQLite<br>events, sessions)]
    SO[SessionOrchestrator<br>idle detection]
    
    subgraph Agents
    SA[SummarizerAgent]
    CPA[CrossProjectAgent]
    RA[ReentryAgent]
    QA[QueryAgent]
    end
    
    MS[MemoryStore<br>ONNX MiniLM]
    CH[(ChromaDB)]

    Watchers -->|events| EQ
    EQ -->|writes| DB
    DB --> SO
    SO -->|cadence| Agents
    Agents -->|embeds| MS
    MS -->|stores| CH
```

**Design principles:**
- **Local-first.** SQLite + ChromaDB on disk, no external service required to record or search.
- **Bring your own key.** LLM calls only happen for summarization and `ask` — and only if you've configured one.
- **Resilient by default.** Each watcher runs independently and is supervised; a crashed watcher restarts without taking the daemon down. SQLite writes retry through lock contention instead of dropping events.
- **Path-aware.** Every watched directory is registered against its absolute path, so generated artifacts (Dev Diaries, similarity notices, re-entry briefs) always land in the actual project, not a guessed working directory.

---

## 🏎️ Performance & Footprint

| Metric | Value |
|---|---|
| Idle CPU | `0.5%` |
| Idle RAM | `113.8 MB` |
| Disk growth | `15.0 KB / 1000 events` |

Embeddings run through ChromaDB's built-in ONNX MiniLM model — no PyTorch dependency, loaded on demand rather than held in memory permanently.

---

## 🛠️ Testing

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

The suite covers event-queue batching and retry behavior, session idle-timeout state transitions, filesystem ignore-pattern matching, cross-project similarity thresholds (including false-positive checks), re-entry stale-gate logic, and query-agent retrieval accuracy against seeded fixtures.

---

## 🗺️ Roadmap

- [x] Filesystem, git, and terminal watchers with automatic supervision and restart
- [x] Session lifecycle management with idle detection
- [x] LLM-powered mini-summaries and Dev Diaries
- [x] Semantic memory via ChromaDB (`contextos ask`)
- [x] Cross-project similarity detection
- [x] Re-entry briefs after a break
- [ ] Optional local dashboard (`localhost`) for browsing history without the CLI
- [ ] Additional watcher sources (clipboard is present but off by default; browser tab history under consideration)

---

## 🤝 Contributing

Issues and PRs welcome. Please run `pytest tests/ -v` before submitting — CI runs the full suite on Python 3.11 and 3.12.

## 📄 License

MIT

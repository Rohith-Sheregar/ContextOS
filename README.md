# ContextOS: Developer Session Memory Agent

ContextOS is a background local agent that silently watches your coding sessions—tracking file changes, git commits, terminal commands, and clipboard events. It builds a persistent memory per project, so when you return to a project after a gap, you don't waste time trying to remember what you were doing.

## Features

- **Observer Agent**: Silently monitors filesystem and git events in the background.
- **Summarizer Agent**: Uses AI (via OpenRouter/Google Gemini/Groq) to intelligently condense session events into memory entries.
- **Auto Dev Diary**: At the end of every coding session, ContextOS auto-generates a changelog-style entry summarizing the entire session. Your project history becomes beautifully documented without manual effort.

## Upcoming Phases

- **Re-entry Brief Agent**: Generates an instant onboarding brief when you return to a project (e.g. "Last session you were debugging the SAR preprocessing pipeline...").
- **Cross-Project Intelligence**: Detects when you are solving a problem you've already solved in another project and automatically surfaces your past solution.
- **Frustration Detector**: Detects when you are stuck based on behavioral signals (repeated commands, rapid file switching) and proactively surfaces relevant documentation.
- **"Why did I write this?" Explainer**: Query any file, function, or decision to get a natural language explanation of your original reasoning reconstructed from session memory.

## Setup

1. Install dependencies and activate virtual environment:
   ```bash
   python -m venv venv
   .\venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Create a `.env` file in `backend/` and configure your API key for OpenRouter or Google Gemini:
   ```env
   OPENROUTER_API_KEY=your_key_here
   ```

3. Run the background daemon:
   ```bash
   python backend/run_daemon.py
   ```

4. View your compiled Dev Diaries:
   ```bash
   python check_db.py
   ```

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from contextos.core.config import settings

logger = logging.getLogger("contextos.daemon.agents.reentry")


class ReentryAgent:
    """
    Generates a 'Welcome back' brief when a developer returns to a project
    that has been idle for at least REENTRY_STALE_AFTER_HOURS hours.
    """

    def __init__(self):
        self.api_key = settings.OPENROUTER_API_KEY or settings.GEMINI_API_KEY
        self.provider = (
            "openrouter" if settings.OPENROUTER_API_KEY
            else "gemini" if settings.GEMINI_API_KEY
            else None
        )

        if not self.provider:
            logger.warning("No LLM API Key configured. ReentryAgent will be disabled.")

    def generate_brief(self, project_name: str, last_summary: str, project_root: "Path | None" = None):
        """
        Generates a re-entry brief from the last session summary + uncommitted
        git diff, and writes it to <project_root>/<REENTRY_BRIEF_RELATIVE_PATH>.
        """
        if not self.provider:
            return

        logger.info(f"Generating re-entry brief for project: {project_name}")

        if project_root is None:
            project_root = Path.cwd()
        git_diff = self._get_git_diff(project_root)

        prompt = (
            f"You are ContextOS, a developer assistant. The developer is returning to the "
            f"project '{project_name}' after a break.\n\n"
            f"## Last Session Summary\n{last_summary}\n\n"
            f"## Current Uncommitted Changes (git diff HEAD)\n{git_diff}\n\n"
            f"Write a short, engaging 'Welcome back' brief in Markdown. Include:\n"
            f"1. A one-sentence reminder of what they were doing.\n"
            f"2. What changed since they last committed (based on the diff above).\n"
            f"3. One or two concrete next steps to pick up where they left off.\n"
            f"Keep it under 200 words. Be direct — no fluff."
        )

        brief_text = self._call_llm(prompt)

        if brief_text:
            self._write_brief_to_file(project_root, brief_text)

    def _get_git_diff(self, project_root: Path) -> str:
        """Returns the latest uncommitted diff, truncated to 2 000 chars."""
        try:
            import git
            repo = git.Repo(str(project_root), search_parent_directories=True)
            diff = repo.git.diff("HEAD")
            if not diff:
                diff = repo.git.diff("--cached")
            if diff:
                return diff[:2000] + (" ... (truncated)" if len(diff) > 2000 else "")
            return "(no uncommitted changes)"
        except Exception as exc:
            logger.debug(f"Could not read git diff: {exc}")
            return "(could not read git diff)"

    def _write_brief_to_file(self, project_root: Path, brief_text: str):
        """Writes the generated brief to <project_root>/<REENTRY_BRIEF_RELATIVE_PATH>."""
        brief_path = project_root / settings.REENTRY_BRIEF_RELATIVE_PATH
        try:
            brief_path.parent.mkdir(parents=True, exist_ok=True)
            brief_path.write_text(
                f"<!-- ContextOS Auto-Generated Re-entry Brief -->\n\n{brief_text}",
                encoding="utf-8",
            )
            logger.info(f"Re-entry brief written to {brief_path}")
        except Exception as exc:
            logger.error(f"Failed to write re-entry brief: {exc}")

    def _call_llm(self, prompt: str) -> str:
        if self.provider == "openrouter":
            return self._call_openrouter(prompt)
        if self.provider == "gemini":
            return self._call_gemini(prompt)
        return ""

    def _call_openrouter(self, prompt: str) -> str:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "ContextOS Daemon",
        }
        data = {
            "model": "openai/gpt-oss-20b:free",
            "messages": [
                {"role": "system", "content": "You are ContextOS, an invisible background developer assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.5,
            "max_tokens": 800,
        }
        try:
            req = urllib.request.Request(
                url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            logger.error(f"OpenRouter API call failed in ReentryAgent: {exc}")
            return ""

    def _call_gemini(self, prompt: str) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={self.api_key}"
        )
        data = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
                return result["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as exc:
            logger.error(f"Gemini API call failed in ReentryAgent: {exc}")
            return ""

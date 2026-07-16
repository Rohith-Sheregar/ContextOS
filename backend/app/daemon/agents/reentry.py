import json
import logging
import os
import urllib.request
import urllib.error

from backend.app.core.config import settings

logger = logging.getLogger("contextos.daemon.agents.reentry")

class ReentryAgent:
    def __init__(self):
        # We try to use OpenRouter, falling back to Gemini if configured
        self.api_key = settings.OPENROUTER_API_KEY or settings.GEMINI_API_KEY
        self.provider = "openrouter" if settings.OPENROUTER_API_KEY else "gemini" if settings.GEMINI_API_KEY else None

        if not self.provider:
            logger.warning("No LLM API Key configured. ReentryAgent will be disabled.")

    def generate_brief(self, project_name: str, last_summary: str):
        """Generates a re-entry brief and writes it to CONTEXTOS_BRIEF.md."""
        if not self.provider:
            return

        logger.info(f"Generating re-entry brief for project: {project_name}")

        prompt = (
            f"You are ContextOS, a developer assistant. The developer is returning to the project '{project_name}' "
            f"after a break.\n\nHere is the summary of what they did in their last session:\n{last_summary}\n\n"
            f"Write a short, engaging 'Welcome back' brief. Remind them of what they were working on, and suggest "
            f"1 or 2 logical next steps to pick up where they left off. Format it as beautiful Markdown."
        )

        brief_text = self._call_llm(prompt)

        if brief_text:
            self._write_brief_to_file(project_name, brief_text)

    def _write_brief_to_file(self, project_name: str, brief_text: str):
        """Writes the generated brief to the project root directory."""
        # For now, we assume the watched project is the parent of the backend directory
        project_root = settings.BASE_DIR.parent
        brief_path = project_root / "CONTEXTOS_BRIEF.md"

        try:
            with open(brief_path, "w", encoding="utf-8") as f:
                f.write(f"<!-- ContextOS Auto-Generated Re-entry Brief -->\n\n{brief_text}")
            logger.info(f"Re-entry brief written to {brief_path}")
        except Exception as e:
            logger.error(f"Failed to write re-entry brief: {e}")

    def _call_llm(self, prompt: str) -> str:
        """Call LLM API using urllib (thread-safe)."""
        if self.provider == "openrouter":
            return self._call_openrouter(prompt)
        elif self.provider == "gemini":
            return self._call_gemini(prompt)
        return ""

    def _call_openrouter(self, prompt: str) -> str:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "ContextOS Daemon"
        }

        data = {
            "model": "openai/gpt-oss-20b:free",
            "messages": [
                {"role": "system", "content": "You are ContextOS, an invisible background developer assistant."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.5,
            "max_tokens": 800
        }

        try:
            req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error(f"OpenRouter API call failed in ReentryAgent: {e}")
            return ""

    def _call_gemini(self, prompt: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={self.api_key}"
        data = json.dumps({'contents':[{'parts':[{'text':prompt}]}]}).encode('utf-8')
        headers = {'Content-Type': 'application/json'}

        try:
            req = urllib.request.Request(url, data=data, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=20) as response:
                result = json.loads(response.read().decode('utf-8'))
                return result['candidates'][0]['content']['parts'][0]['text'].strip()
        except Exception as e:
            logger.error(f"Gemini API call failed in ReentryAgent: {e}")
            return ""

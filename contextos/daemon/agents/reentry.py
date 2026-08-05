import logging
from pathlib import Path

from contextos.core.config import settings
from contextos.daemon.agents.llm import LLMClient

logger = logging.getLogger("contextos.daemon.agents.reentry")


class ReentryAgent:
    """
    Generates a 'Welcome back' brief when a developer returns to a project
    that has been idle for at least REENTRY_STALE_AFTER_HOURS hours.
    """

    def __init__(self):
        self.llm = LLMClient(
            role="reentry",
            system_prompt="You are ContextOS, an invisible background developer assistant.",
            temperature=0.5,
            max_tokens=800,
            allow_gemini=True,
        )
        self.provider = self.llm.provider
        self.api_key = self.llm.api_key

        if not self.provider:
            logger.warning("No LLM API Key configured. ReentryAgent will be disabled.")

    def generate_brief(
        self, project_name: str, last_summary: str, project_root: "Path | None" = None
    ):
        """
        Generates a re-entry brief from the last session summary + uncommitted
        git diff, and writes it to <project_root>/<REENTRY_BRIEF_RELATIVE_PATH>.
        """
        if not self.provider:
            return

        logger.info("Generating re-entry brief for project: %s", project_name)

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
            logger.debug("Could not read git diff: %s", exc)
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
            logger.info("Re-entry brief written to %s", brief_path)
        except Exception as exc:
            logger.error("Failed to write re-entry brief: %s", exc)

    def _call_llm(self, prompt: str) -> str:
        result = self.llm.complete(prompt)
        if result.text:
            return result.text

        if result.error:
            logger.error(
                "ReentryAgent LLM call via %s failed: %s",
                result.provider,
                result.error,
            )
        return ""

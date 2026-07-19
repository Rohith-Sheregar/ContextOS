import logging
from pathlib import Path

from contextos.core.config import settings

logger = logging.getLogger("contextos.daemon.agents.cross_project")


class CrossProjectAgent:
    """
    After each mini-summary is embedded, searches the full memory store (all
    projects) for semantically similar past work from *other* projects.
    """

    def __init__(self, memory_store):
        self.memory_store = memory_store

    def check_for_similar_work(
        self,
        current_project: str,
        current_summary: str,
        project_root: Path,
    ):
        if not self.memory_store or not self.memory_store.enabled:
            return

        all_matches = self.memory_store.query(
            current_summary,
            project_name=None,
            top_k=settings.CROSS_PROJECT_TOP_K,
            max_distance=settings.CROSS_PROJECT_MAX_DISTANCE,
        )

        cross_matches = [
            m for m in all_matches
            if m.get("project_name") and m["project_name"] != current_project
        ]

        if not cross_matches:
            logger.debug(
                f"No cross-project matches for '{current_project}' "
                f"(threshold={settings.CROSS_PROJECT_MAX_DISTANCE})"
            )
            return

        logger.info(
            f"Found {len(cross_matches)} cross-project match(es) for '{current_project}': "
            + ", ".join(f"{m['project_name']} (d={m.get('score', '?'):.3f})" for m in cross_matches)
        )
        self._write_similar_file(project_root, current_project, current_summary, cross_matches)

    def _write_similar_file(
        self,
        project_root: Path,
        current_project: str,
        current_summary: str,
        matches: list[dict],
    ):
        similar_path = project_root / settings.CROSS_PROJECT_MATCH_RELATIVE_PATH

        lines = [
            "<!-- ContextOS Auto-Generated Cross-Project Similarity Notice -->\n",
            "## ContextOS: Similar Past Work Found\n",
            f"While working on **{current_project}**, ContextOS found related work in other projects.\n",
        ]

        for match in matches:
            other_project = match.get("project_name", "unknown")
            timestamp = (match.get("timestamp") or "")[:10]
            summary_snippet = (match.get("text") or "")[:300].replace("\n", " ")
            score = match.get("score")
            score_str = f" (similarity score: {score:.3f})" if score is not None else ""

            lines.append(f"\n### {other_project} — {timestamp}{score_str}\n")
            lines.append(f"> {summary_snippet}\n")

        try:
            similar_path.parent.mkdir(parents=True, exist_ok=True)
            similar_path.write_text("\n".join(lines), encoding="utf-8")
            logger.info(f"Cross-project similarity file written to {similar_path}")
        except Exception as exc:
            logger.error(f"Failed to write cross-project similarity file: {exc}")

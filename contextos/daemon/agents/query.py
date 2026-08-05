import logging

from contextos.core.memory_store import MemoryStore
from contextos.daemon.agents.llm import LLMClient

logger = logging.getLogger("contextos.daemon.agents.query")


class QueryAgent:
    """Retrieves relevant session memories and synthesizes an answer via LLM."""

    def __init__(self, memory_store: MemoryStore):
        self.memory_store = memory_store
        self.llm = LLMClient(
            role="query",
            system_prompt="You are ContextOS, a developer memory assistant.",
            temperature=0.2,
            max_tokens=600,
        )
        self.provider = self.llm.provider
        self.api_key = self.llm.api_key

    def disable_synthesis(self):
        self.provider = None
        self.api_key = None

    def ask(self, question: str, project_name: str | None = None) -> dict:
        """
        Retrieves relevant memories and synthesizes an answer.
        Returns {"answer": str, "sources": list[dict]}.
        """
        matches = self.memory_store.query(question, project_name=project_name)

        if not matches:
            return {"answer": "No relevant memories found in ContextOS.", "sources": []}

        context_blocks = []
        for match in matches:
            timestamp = match.get("timestamp", "")[:10]
            project = match.get("project_name", "unknown")
            text = match.get("text", "")
            context_blocks.append(f"[{timestamp}] ({project}) - {text}")

        context = "\n\n".join(context_blocks)

        if not self.provider:
            answer = "Retrieved memories (no LLM synthesis - LLM provider not configured):\n\n" + context
            return {"answer": answer, "sources": matches}

        prompt = (
            f"You are ContextOS, a developer memory assistant. Answer the following question "
            f"using ONLY the provided memory excerpts. Be concise and cite the project and date "
            f"when relevant.\n\n"
            f"## Question\n{question}\n\n"
            f"## Memory Excerpts\n{context}\n\n"
            f"## Answer"
        )

        answer = self._call_llm(prompt)
        return {"answer": answer, "sources": matches}

    def _call_llm(self, prompt: str) -> str:
        result = self.llm.complete(prompt)
        if result.text:
            return result.text

        if result.error:
            logger.error("QueryAgent LLM call failed via %s: %s", result.provider, result.error)
            if result.error.startswith("HTTP "):
                return f"[Error contacting LLM: {result.error}]"
        return "[Error contacting LLM]"

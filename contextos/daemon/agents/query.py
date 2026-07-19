import json
import logging
import urllib.request
import urllib.error
from contextos.core.config import settings
from contextos.core.memory_store import MemoryStore

logger = logging.getLogger("contextos.daemon.agents.query")


class QueryAgent:
    """Retrieves relevant session memories and synthesizes an answer via LLM."""

    def __init__(self, memory_store: MemoryStore):
        self.memory_store = memory_store
        self.api_key = settings.OPENROUTER_API_KEY

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
            session_id = match.get("session_id", "")
            timestamp = match.get("timestamp", "")[:10]
            project = match.get("project_name", "unknown")
            text = match.get("text", "")
            context_blocks.append(f"[{timestamp}] ({project}) — {text}")

        context = "\n\n".join(context_blocks)

        if not self.api_key:
            # Raw mode: return retrieved summaries without synthesis
            answer = "Retrieved memories (no LLM synthesis — API key not configured):\n\n" + context
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
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "ContextOS",
        }
        data = {
            "model": "openai/gpt-oss-20b:free",
            "messages": [
                {"role": "system", "content": "You are ContextOS, a developer memory assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 600,
        }

        try:
            req = urllib.request.Request(
                url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error(f"QueryAgent OpenRouter call failed: HTTP {e.code} — {body[:200]}")
            return f"[Error contacting LLM: HTTP {e.code}]"
        except Exception as exc:
            logger.error(f"QueryAgent LLM call failed: {exc}")
            return "[Error contacting LLM]"

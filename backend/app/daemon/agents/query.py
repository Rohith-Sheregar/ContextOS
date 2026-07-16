import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Any, Optional

from backend.app.core.config import settings
from backend.app.core.memory_store import MemoryStore

logger = logging.getLogger("contextos.agents.query")

class QueryAgent:
    """Takes a natural-language question, retrieves relevant memories,
    and generates a cited answer."""

    def __init__(self, memory_store: MemoryStore):
        self.memory_store = memory_store
        self.api_key = settings.OPENROUTER_API_KEY

    def ask(self, question: str, project_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Returns:
            {
                "answer": str,           # LLM-synthesized answer (or raw matches if no API key)
                "sources": [             # Citations
                    {"session_id": str, "project_name": str, "timestamp": str, "summary": str}
                ]
            }
        """
        # 1. Semantic search via MemoryStore
        matches = self.memory_store.query(question, project_name=project_name, top_k=5)

        if not matches:
            return {"answer": "No relevant memories found in ContextOS.", "sources": []}

        # Format the sources list
        sources = []
        for m in matches:
            sources.append({
                "session_id": m.get("session_id", "unknown"),
                "project_name": m.get("project_name", "unknown"),
                "timestamp": m.get("timestamp", "unknown"),
                "summary": m.get("text", ""),
                "score": m.get("score"),
            })

        if not self.api_key or self.api_key == "PASTE_YOUR_OPENROUTER_KEY_HERE":
            logger.warning("No OpenRouter API key found. Returning raw matching summaries.")
            raw_answer = "Here are the top raw summaries that match your query (LLM synthesis disabled without API key):\n\n"
            for s in sources:
                raw_answer += f"- [{s['project_name']} on {s['timestamp'][:10]}] {s['summary']}\n"
            return {"answer": raw_answer.strip(), "sources": sources}

        # 2. Pull raw event context for the top-matching sessions (for grounding)
        context_blocks = []
        for match in matches[:3]:  # Limit context to top 3 to avoid blowing up context window
            session_events = self.memory_store.get_session_context(match.get("session_id", ""))

            # Format a snippet of events
            events_snippet = []
            for ev in session_events[:20]: # Only take first 20 events to save tokens
                line = f"[{ev['timestamp']}] {ev['source']} {ev['event_type']} {ev['file_path']}"
                if ev['payload']:
                    line += f" (Payload: {ev['payload']})"
                events_snippet.append(line)

            context_blocks.append({
                "summary": match.get("text", ""),
                "session_id": match.get("session_id", ""),
                "project": match.get("project_name", ""),
                "timestamp": match.get("timestamp", ""),
                "events_snippet": "\n".join(events_snippet),
            })

        # 3. Send to LLM with grounding prompt
        prompt = self._build_grounded_prompt(question, context_blocks)
        answer = self._call_llm(prompt)

        # 4. Return answer + citations
        return {"answer": answer, "sources": sources}

    def _build_grounded_prompt(self, question: str, context_blocks: list[dict]) -> str:
        prompt = (
            "You are ContextOS, a developer's personal memory assistant.\n"
            "The developer is asking a question about their own past work history.\n"
            "Below is the relevant session history retrieved from their local database.\n\n"
            "CRITICAL INSTRUCTIONS:\n"
            "1. Answer the question using ONLY the provided session history.\n"
            "2. If the answer is not in the history, say so clearly.\n"
            "3. Cite your sources by mentioning the project name and date (e.g. 'In project X on YYYY-MM-DD...').\n\n"
            "--- RETRIEVED SESSION HISTORY ---\n\n"
        )

        for block in context_blocks:
            prompt += f"SESSION ID: {block['session_id']}\n"
            prompt += f"PROJECT: {block['project']}\n"
            prompt += f"DATE: {block['timestamp']}\n"
            prompt += f"SUMMARY: {block['summary']}\n"
            prompt += f"RAW EVENTS SAMPLE:\n{block['events_snippet']}\n"
            prompt += "-" * 40 + "\n\n"

        prompt += f"USER QUESTION: {question}\n\nANSWER:\n"
        return prompt

    def _call_llm(self, prompt: str) -> str:
        """Call OpenRouter API using urllib (thread-safe, no SDK needed)."""
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "ContextOS Daemon"
        }

        data = {
            # Use a capable, fast model for query synthesis
            "model": "meta-llama/llama-3-8b-instruct:free",
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1, # Keep it factual
            "max_tokens": 1000
        }

        try:
            req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error(f"OpenRouter API call failed in QueryAgent: HTTP {e.code} - {body[:200]}")
            return "Error: Failed to synthesize answer from OpenRouter API."
        except Exception as e:
            logger.error(f"OpenRouter API call failed in QueryAgent: {e}")
            return "Error: Failed to synthesize answer due to network/API error."

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

from contextos.core.config import settings

logger = logging.getLogger("contextos.daemon.agents.llm")


def _configured_key(value: str | None) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    if not stripped or stripped == "PASTE_YOUR_OPENROUTER_KEY_HERE":
        return None
    return stripped


@dataclass
class LLMResult:
    text: str
    provider: str | None
    error: str | None = None


class LLMClient:
    """Synchronous LLM client shared by all agents. Handles Ollama, OpenRouter,
    and Gemini behind a single .complete() call. Caches responses in SQLite to
    avoid re-calling the API for identical prompts."""

    def __init__(
        self,
        *,
        role: str,
        system_prompt: str,
        temperature: float,
        max_tokens: int,
        allow_gemini: bool = False,
    ):
        self.role = role
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.allow_gemini = allow_gemini
        self.provider = self._select_provider()
        self.api_key = self._api_key_for_provider(self.provider)

    def complete(self, prompt: str) -> LLMResult:
        if not self.provider:
            return LLMResult("", None, "No LLM provider configured")

        # Check cache first
        if settings.LLM_CACHE_ENABLED:
            cached = self._check_cache(prompt)
            if cached is not None:
                return LLMResult(cached, self.provider)

        if self.provider == "ollama":
            result = self._call_ollama(prompt)
        elif self.provider == "openrouter":
            result = self._call_openrouter(prompt)
        elif self.provider == "gemini":
            result = self._call_gemini(prompt)
        else:
            return LLMResult("", None, f"Unsupported LLM provider: {self.provider}")

        # Cache successful responses
        if result.text and not result.error and settings.LLM_CACHE_ENABLED:
            self._save_cache(prompt, result.text, result.provider)

        return result

    def _check_cache(self, prompt: str) -> str | None:
        try:
            from contextos.core.database import get_cached_llm_response
            return get_cached_llm_response(prompt)
        except Exception:
            return None

    def _save_cache(self, prompt: str, response: str, provider: str | None):
        try:
            from contextos.core.database import save_llm_response_cache
            save_llm_response_cache(prompt, response, provider)
        except Exception:
            pass  # caching is best-effort

    def _select_provider(self) -> str | None:
        requested = (settings.LLM_PROVIDER or "auto").strip().lower()

        if requested in {"none", "off", "disabled"}:
            return None
        if requested == "ollama":
            return "ollama"
        if requested == "openrouter":
            return "openrouter" if _configured_key(settings.OPENROUTER_API_KEY) else None
        if requested == "gemini":
            if self.allow_gemini and _configured_key(settings.GEMINI_API_KEY):
                return "gemini"
            return None

        # auto: try openrouter first, then gemini
        if _configured_key(settings.OPENROUTER_API_KEY):
            return "openrouter"
        if self.allow_gemini and _configured_key(settings.GEMINI_API_KEY):
            return "gemini"
        return None

    def _api_key_for_provider(self, provider: str | None) -> str | None:
        if provider == "openrouter":
            return _configured_key(settings.OPENROUTER_API_KEY)
        if provider == "gemini":
            return _configured_key(settings.GEMINI_API_KEY)
        return None

    def _ollama_model(self) -> str:
        role_model = {
            "summarizer": settings.OLLAMA_SUMMARIZER_MODEL,
            "query": settings.OLLAMA_QUERY_MODEL,
            "reentry": settings.OLLAMA_REENTRY_MODEL,
        }.get(self.role, "")
        return (role_model or settings.OLLAMA_MODEL).strip()

    def _call_ollama(self, prompt: str) -> LLMResult:
        model = self._ollama_model()
        if not model:
            return LLMResult("", "ollama", "OLLAMA_MODEL is not configured")

        url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
        data = {
            "model": model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        headers = {"Content-Type": "application/json"}

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=settings.LLM_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8"))
                text = (result.get("message") or {}).get("content", "")
                return LLMResult(text.strip(), "ollama")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error("Ollama call failed: HTTP %s - %s", exc.code, body[:200])
            return LLMResult("", "ollama", f"HTTP {exc.code}")
        except Exception as exc:
            logger.error("Ollama call failed: %s", exc)
            return LLMResult("", "ollama", str(exc))

    def _call_openrouter(self, prompt: str) -> LLMResult:
        if not self.api_key:
            return LLMResult("", "openrouter", "OPENROUTER_API_KEY is not configured")

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "ContextOS Daemon",
        }
        data = {
            "model": settings.OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=settings.LLM_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8"))
                return LLMResult(result["choices"][0]["message"]["content"].strip(), "openrouter")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error("OpenRouter call failed: HTTP %s - %s", exc.code, body[:200])
            return LLMResult("", "openrouter", f"HTTP {exc.code}")
        except Exception as exc:
            logger.error("OpenRouter call failed: %s", exc)
            return LLMResult("", "openrouter", str(exc))

    def _call_gemini(self, prompt: str) -> LLMResult:
        if not self.api_key:
            return LLMResult("", "gemini", "GEMINI_API_KEY is not configured")

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{settings.GEMINI_MODEL}:generateContent?key={self.api_key}"
        )
        data = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
            },
        }
        headers = {"Content-Type": "application/json"}

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=settings.LLM_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8"))
                text = result["candidates"][0]["content"]["parts"][0]["text"]
                return LLMResult(text.strip(), "gemini")
        except Exception as exc:
            logger.error("Gemini call failed: %s", exc)
            return LLMResult("", "gemini", str(exc))

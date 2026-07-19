import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from contextos.core.config import settings
from contextos.core.database import (
    delete_memory_backfill_item,
    enqueue_memory_backfill,
    get_db_conn,
    load_memory_backfill_items,
)

logger = logging.getLogger("contextos.memory")

try:
    import chromadb
    from chromadb.utils import embedding_functions
    CHROMA_AVAILABLE = True
except ImportError:
    CHROMA_AVAILABLE = False


class MemoryStore:
    """Unified interface over SQLite structured data and Chroma semantic memory."""

    def __init__(self):
        self.enabled = CHROMA_AVAILABLE
        self.client = None
        self.embedding_fn = None
        self.collection = None

        if not self.enabled:
            logger.warning("ChromaDB or sentence-transformers not installed. MemoryStore disabled.")
            return

        logger.info("Initializing MemoryStore at %s with model %s", settings.CHROMA_DIR, settings.EMBEDDING_MODEL)
        try:
            self.client = chromadb.PersistentClient(path=str(settings.CHROMA_DIR))
            self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=settings.EMBEDDING_MODEL
            )
            self.collection = self.client.get_or_create_collection(
                name="session_memories",
                embedding_function=self.embedding_fn,
            )
            logger.info("MemoryStore initialized successfully.")
        except Exception as e:
            logger.error("Failed to initialize ChromaDB: %s", e)
            self.enabled = False

    def store_summary(
        self,
        text: str,
        metadata: Dict[str, Any],
        *,
        doc_id: str | None = None,
        enqueue_on_failure: bool = True,
    ) -> bool:
        if not text or not text.strip():
            return False

        clean_metadata = self._clean_metadata(metadata)
        doc_id = doc_id or self._make_doc_id(clean_metadata)

        if not self.enabled or self.collection is None:
            if enqueue_on_failure:
                self._queue_for_retry(doc_id, text, clean_metadata, "MemoryStore disabled")
            return False

        try:
            self.collection.upsert(
                documents=[text],
                metadatas=[clean_metadata],
                ids=[doc_id],
            )
            logger.debug("Stored summary %s in MemoryStore.", doc_id)
            return True
        except Exception as e:
            logger.error("Failed to store summary in MemoryStore: %s", e)
            if enqueue_on_failure:
                self._queue_for_retry(doc_id, text, clean_metadata, str(e))
            return False

    def query(
        self,
        question: str,
        project_name: Optional[str] = None,
        top_k: Optional[int] = None,
        max_distance: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Semantic similarity search against stored summaries."""
        if not self.enabled or self.collection is None:
            logger.warning("MemoryStore is disabled. Cannot query.")
            return []

        top_k = top_k or settings.QUERY_TOP_K
        max_distance = settings.QUERY_MAX_DISTANCE if max_distance is None else max_distance
        where_clause = {"project_name": project_name} if project_name else None

        try:
            results = self.collection.query(
                query_texts=[question],
                n_results=top_k,
                where=where_clause,
            )

            matches = []
            documents = results.get("documents") or [[]]
            metadatas = results.get("metadatas") or [[]]
            ids = results.get("ids") or [[]]
            distances = results.get("distances") or [[]]

            for i, text in enumerate(documents[0]):
                distance = distances[0][i] if distances and distances[0] else None
                if distance is not None and distance > max_distance:
                    continue
                metadata = metadatas[0][i] if metadatas and metadatas[0] else {}
                matches.append({
                    "id": ids[0][i],
                    "text": text,
                    "score": distance,
                    **metadata,
                })

            return matches
        except Exception as e:
            logger.error("Failed to query MemoryStore: %s", e)
            return []

    def get_session_context(self, session_id: str) -> List[Dict[str, Any]]:
        """Pulls raw events for one session from SQLite for answer grounding."""
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT project_name, start_time, end_time FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return []

                project_name = row["project_name"]
                start_time = row["start_time"]
                end_time = row["end_time"]

                query = """
                    SELECT timestamp, source, event_type, file_path, payload
                    FROM events
                    WHERE project_name = ? AND timestamp >= ?
                """
                params: list[Any] = [project_name, start_time]

                if end_time:
                    query += " AND timestamp <= ?"
                    params.append(end_time)

                query += " ORDER BY timestamp ASC"
                cursor.execute(query, params)

                return [
                    {
                        "timestamp": ev["timestamp"],
                        "source": ev["source"],
                        "event_type": ev["event_type"],
                        "file_path": ev["file_path"],
                        "payload": ev["payload"],
                    }
                    for ev in cursor.fetchall()
                ]
        except Exception as e:
            logger.error("Failed to get session context: %s", e)
            return []

    def backfill_from_sqlite(self) -> dict[str, int]:
        """Embeds existing mini summaries and final session summaries from SQLite."""
        counts = {"sessions": 0, "mini_summaries": 0, "queued_retried": 0}

        for summary in self._load_final_summaries():
            if self.store_summary(summary["text"], summary["metadata"]):
                counts["sessions"] += 1

        for summary in self._load_mini_summaries():
            if self.store_summary(summary["text"], summary["metadata"]):
                counts["mini_summaries"] += 1

        counts["queued_retried"] = self.retry_queued_embeddings()
        return counts

    def retry_queued_embeddings(self, limit: int = 100) -> int:
        """Attempts to embed summaries that were queued after prior failures."""
        if not self.enabled:
            return 0

        succeeded = 0
        for item in load_memory_backfill_items(limit=limit):
            stored = self.store_summary(
                item["text"],
                item["metadata"],
                doc_id=item["doc_id"],
                enqueue_on_failure=False,
            )
            if stored:
                delete_memory_backfill_item(item["doc_id"])
                succeeded += 1
        return succeeded

    def _load_final_summaries(self) -> list[dict]:
        try:
            with get_db_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT session_id, project_name, start_time, end_time, summary
                    FROM sessions
                    WHERE summary IS NOT NULL AND TRIM(summary) != ''
                    """
                ).fetchall()

                summaries = []
                for row in rows:
                    summaries.append({
                        "text": row["summary"],
                        "metadata": {
                            "project_name": row["project_name"],
                            "session_id": row["session_id"],
                            "timestamp": row["end_time"] or row["start_time"],
                            "summary_type": "final",
                            "file_paths_touched": self._file_paths_for_session(conn, row),
                        },
                    })
                return summaries
        except Exception as e:
            logger.error("Failed to load final summaries for backfill: %s", e)
            return []

    def _load_mini_summaries(self) -> list[dict]:
        try:
            with get_db_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT timestamp, project_name, payload
                    FROM events
                    WHERE source = 'agent' AND event_type = 'mini_summary'
                    """
                ).fetchall()

                summaries = []
                for row in rows:
                    try:
                        payload = json.loads(row["payload"] or "{}")
                    except Exception:
                        continue
                    text = payload.get("text")
                    session_id = payload.get("session_id")
                    if not text or not session_id:
                        continue
                    summaries.append({
                        "text": text,
                        "metadata": {
                            "project_name": row["project_name"],
                            "session_id": session_id,
                            "timestamp": row["timestamp"],
                            "summary_type": "mini",
                        },
                    })
                return summaries
        except Exception as e:
            logger.error("Failed to load mini summaries for backfill: %s", e)
            return []

    def _file_paths_for_session(self, conn, session_row) -> list[str]:
        query = """
            SELECT DISTINCT file_path
            FROM events
            WHERE project_name = ? AND timestamp >= ?
              AND file_path NOT IN ('terminal', 'summarizer')
        """
        params: list[Any] = [session_row["project_name"], session_row["start_time"]]
        if session_row["end_time"]:
            query += " AND timestamp <= ?"
            params.append(session_row["end_time"])

        rows = conn.execute(query, params).fetchall()
        return sorted({row["file_path"] for row in rows if row["file_path"]})

    def _queue_for_retry(self, doc_id: str, text: str, metadata: dict, error: str):
        try:
            enqueue_memory_backfill(doc_id, text, metadata, error=error)
        except Exception:
            logger.exception("Failed to queue summary %s for embedding retry.", doc_id)

    def _make_doc_id(self, metadata: Dict[str, Any]) -> str:
        session_id = metadata.get("session_id", "unknown")
        summary_type = metadata.get("summary_type", "mini")
        timestamp = metadata.get("timestamp", "")
        return hashlib.md5(f"{session_id}_{summary_type}_{timestamp}".encode("utf-8")).hexdigest()

    def _clean_metadata(self, metadata: Dict[str, Any]) -> dict:
        clean_metadata = {}
        for key, value in metadata.items():
            if isinstance(value, (str, int, float, bool)):
                clean_metadata[key] = value
            elif isinstance(value, list):
                clean_metadata[key] = ",".join(str(item) for item in value)
            elif value is not None:
                clean_metadata[key] = str(value)

        clean_metadata.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        return clean_metadata

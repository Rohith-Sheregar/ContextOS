from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tarfile
import threading
import urllib.request
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from contextos.core.config import settings
from contextos.core.database import (
    delete_memory_backfill_item,
    enqueue_memory_backfill,
    get_db_conn,
    load_memory_backfill_items,
    run_with_db_retry,
)

logger = logging.getLogger("contextos.memory")

try:
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    EMBEDDING_DEPS_AVAILABLE = True
    EMBEDDING_IMPORT_ERROR: Exception | None = None
except ImportError as exc:
    EMBEDDING_DEPS_AVAILABLE = False
    EMBEDDING_IMPORT_ERROR = exc

try:
    import sqlite_vec
    from sqlite_vec import serialize_float32

    SQLITE_VEC_AVAILABLE = True
    SQLITE_VEC_IMPORT_ERROR: Exception | None = None
except ImportError as exc:
    SQLITE_VEC_AVAILABLE = False
    SQLITE_VEC_IMPORT_ERROR = exc


class ONNXMiniLMEmbedder:
    """Minimal ONNX MiniLM embedding wrapper used by the sqlite-vec store."""

    MODEL_NAME = "all-MiniLM-L6-v2"
    DOWNLOAD_PATH = Path.home() / ".cache" / "chroma" / "onnx_models" / MODEL_NAME
    EXTRACTED_FOLDER_NAME = "onnx"
    ARCHIVE_FILENAME = "onnx.tar.gz"
    MODEL_DOWNLOAD_URL = (
        "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"
    )
    MODEL_SHA256 = "913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3"
    REQUIRED_FILES = (
        "config.json",
        "model.onnx",
        "special_tokens_map.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "vocab.txt",
    )

    def __init__(self, preferred_providers: Optional[list[str]] = None):
        if not EMBEDDING_DEPS_AVAILABLE:
            raise ImportError(
                "onnxruntime, tokenizers, and numpy are required for local embeddings"
            ) from EMBEDDING_IMPORT_ERROR

        if preferred_providers and (
            not all(isinstance(provider, str) for provider in preferred_providers)
            or len(preferred_providers) != len(set(preferred_providers))
        ):
            raise ValueError("preferred_providers must be a unique list of strings")

        self._preferred_providers = preferred_providers
        self._lock = threading.Lock()

    def embed(self, documents: Sequence[str]) -> list[list[float]]:
        if not documents:
            return []

        with self._lock:
            self._download_model_if_not_exists()
            embeddings = self._forward(list(documents))
            return [
                embedding.astype(np.float32).tolist()
                for embedding in embeddings
            ]

    def _forward(self, documents: list[str], batch_size: int = 32):
        all_embeddings = []

        for i in range(0, len(documents), batch_size):
            batch = documents[i:i + batch_size]
            encoded = [self.tokenizer.encode(document) for document in batch]

            input_ids = np.array([item.ids for item in encoded], dtype=np.int64)
            attention_mask = np.array(
                [item.attention_mask for item in encoded],
                dtype=np.int64,
            )
            token_type_ids = np.array(
                [np.zeros(len(item.ids), dtype=np.int64) for item in encoded],
                dtype=np.int64,
            )

            model_output = self.model.run(
                None,
                {
                    "input_ids": input_ids,
                    "attention_mask": attention_mask,
                    "token_type_ids": token_type_ids,
                },
            )
            last_hidden_state = model_output[0]
            input_mask_expanded = np.broadcast_to(
                np.expand_dims(attention_mask, -1),
                last_hidden_state.shape,
            )
            embeddings = np.sum(last_hidden_state * input_mask_expanded, 1) / np.clip(
                input_mask_expanded.sum(1),
                a_min=1e-9,
                a_max=None,
            )
            all_embeddings.append(self._normalize(embeddings).astype(np.float32))

        return np.concatenate(all_embeddings)

    def _normalize(self, vectors):
        norm = np.linalg.norm(vectors, axis=1)
        norm[norm == 0] = 1e-12
        return vectors / norm[:, np.newaxis]

    @cached_property
    def tokenizer(self):
        tokenizer = Tokenizer.from_file(str(self._model_dir / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=256)
        tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", length=256)
        return tokenizer

    @cached_property
    def model(self):
        providers = self._preferred_providers or ort.get_available_providers()
        if "CoreMLExecutionProvider" in providers:
            providers = [provider for provider in providers if provider != "CoreMLExecutionProvider"]

        session_options = ort.SessionOptions()
        session_options.log_severity_level = 3
        session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        return ort.InferenceSession(
            str(self._model_dir / "model.onnx"),
            providers=providers,
            sess_options=session_options,
        )

    @property
    def _model_dir(self) -> Path:
        return self.DOWNLOAD_PATH / self.EXTRACTED_FOLDER_NAME

    def _download_model_if_not_exists(self):
        if all((self._model_dir / file_name).exists() for file_name in self.REQUIRED_FILES):
            return

        self.DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
        archive_path = self.DOWNLOAD_PATH / self.ARCHIVE_FILENAME
        if not archive_path.exists() or not self._verify_sha256(archive_path):
            self._download_archive(archive_path)

        with tarfile.open(archive_path, mode="r:gz") as archive:
            if hasattr(tarfile, "data_filter"):
                archive.extractall(path=self.DOWNLOAD_PATH, filter="data")
            else:
                archive.extractall(path=self.DOWNLOAD_PATH)

    def _download_archive(self, archive_path: Path):
        tmp_path = archive_path.with_suffix(".tmp")
        try:
            with urllib.request.urlopen(self.MODEL_DOWNLOAD_URL, timeout=60) as response:
                with tmp_path.open("wb") as file:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        file.write(chunk)

            if not self._verify_sha256(tmp_path):
                raise ValueError("Downloaded ONNX model archive failed SHA256 verification")

            os.replace(tmp_path, archive_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _verify_sha256(self, path: Path) -> bool:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest() == self.MODEL_SHA256
        except OSError:
            return False


class MemoryStore:
    """Unified interface over SQLite structured data and sqlite-vec semantic memory."""

    def __init__(self, embedder: ONNXMiniLMEmbedder | None = None):
        self.enabled = SQLITE_VEC_AVAILABLE and EMBEDDING_DEPS_AVAILABLE
        self.client = None
        self.collection = None
        self.embedding_fn = None

        if not SQLITE_VEC_AVAILABLE:
            logger.warning("sqlite-vec is not installed. MemoryStore disabled: %s", SQLITE_VEC_IMPORT_ERROR)
            self.enabled = False
            return

        if not EMBEDDING_DEPS_AVAILABLE:
            logger.warning("ONNX embedding dependencies are unavailable. MemoryStore disabled: %s", EMBEDDING_IMPORT_ERROR)
            self.enabled = False
            return

        logger.info(
            "Initializing sqlite-vec MemoryStore at %s with %s",
            settings.DB_PATH,
            settings.EMBEDDING_MODEL,
        )
        try:
            self.embedding_fn = embedder or ONNXMiniLMEmbedder()
            self._ensure_schema()
            logger.info("MemoryStore initialized successfully.")
        except Exception as exc:
            logger.error("Failed to initialize sqlite-vec MemoryStore: %s", exc)
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

        if not self.enabled or getattr(self, "embedding_fn", None) is None:
            if enqueue_on_failure:
                self._queue_for_retry(doc_id, text, clean_metadata, "MemoryStore disabled")
            return False

        try:
            embedding = self._embed_one(text)

            def _store():
                with get_db_conn() as conn:
                    self._ensure_schema(conn)
                    self._upsert_document_and_vector(conn, doc_id, text, clean_metadata, embedding)
                    conn.commit()

            run_with_db_retry("memory_store.store_summary", _store)
            logger.debug("Stored summary %s in MemoryStore.", doc_id)
            return True
        except Exception as exc:
            logger.error("Failed to store summary in MemoryStore: %s", exc)
            if enqueue_on_failure:
                self._queue_for_retry(doc_id, text, clean_metadata, str(exc))
            return False

    def query(
        self,
        question: str,
        project_name: Optional[str] = None,
        top_k: Optional[int] = None,
        max_distance: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Semantic similarity search against stored summaries."""
        if not self.enabled or getattr(self, "embedding_fn", None) is None:
            logger.warning("MemoryStore is disabled. Cannot query.")
            return []

        if not question or not question.strip():
            return []

        top_k = max(1, int(top_k or settings.QUERY_TOP_K))
        max_distance = settings.QUERY_MAX_DISTANCE if max_distance is None else max_distance

        try:
            query_embedding = self._embed_one(question)

            def _query():
                with get_db_conn() as conn:
                    self._ensure_schema(conn)
                    params: list[Any] = [serialize_float32(query_embedding), top_k]
                    project_filter = ""
                    if project_name:
                        project_filter = " AND project_name = ?"
                        params.append(project_name)

                    rows = conn.execute(
                        f"""
                        WITH knn_matches AS (
                            SELECT rowid, distance
                            FROM memory_vectors
                            WHERE embedding MATCH ?
                              AND k = ?
                              {project_filter}
                        )
                        SELECT
                            d.doc_id,
                            d.text,
                            d.metadata,
                            knn_matches.distance
                        FROM knn_matches
                        JOIN memory_documents d ON d.id = knn_matches.rowid
                        ORDER BY knn_matches.distance ASC
                        """,
                        params,
                    ).fetchall()

                    matches = []
                    for row in rows:
                        distance = row["distance"]
                        if distance is not None and distance > max_distance:
                            continue

                        try:
                            metadata = json.loads(row["metadata"] or "{}")
                        except json.JSONDecodeError:
                            metadata = {}

                        matches.append({
                            "id": row["doc_id"],
                            "text": row["text"],
                            "score": distance,
                            **metadata,
                        })
                    return matches

            return run_with_db_retry("memory_store.query", _query)
        except Exception as exc:
            logger.error("Failed to query MemoryStore: %s", exc)
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
        except Exception as exc:
            logger.error("Failed to get session context: %s", exc)
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
        if not getattr(self, "enabled", False):
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
        except Exception as exc:
            logger.error("Failed to load final summaries for backfill: %s", exc)
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
        except Exception as exc:
            logger.error("Failed to load mini summaries for backfill: %s", exc)
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

    def _ensure_schema(self, conn: sqlite3.Connection | None = None):
        if conn is not None:
            self._load_sqlite_vec(conn)
            self._create_memory_schema(conn)
            return

        def _init():
            with get_db_conn() as db_conn:
                self._ensure_schema(db_conn)
                db_conn.commit()

        run_with_db_retry("memory_store.ensure_schema", _init)

    def _load_sqlite_vec(self, conn: sqlite3.Connection):
        if not SQLITE_VEC_AVAILABLE:
            raise ImportError("sqlite-vec is not installed") from SQLITE_VEC_IMPORT_ERROR

        load_extension_supported = hasattr(conn, "enable_load_extension")
        if load_extension_supported:
            conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            if load_extension_supported:
                conn.enable_load_extension(False)

    def _create_memory_schema(self, conn: sqlite3.Connection):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id TEXT UNIQUE NOT NULL,
                text TEXT NOT NULL,
                metadata TEXT NOT NULL,
                project_name TEXT,
                session_id TEXT,
                timestamp TEXT,
                summary_type TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_documents_project ON memory_documents(project_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_documents_session ON memory_documents(session_id)")
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
                embedding float[{settings.EMBEDDING_DIMENSION}] distance_metric=cosine,
                project_name TEXT
            )
            """
        )

    def _upsert_document_and_vector(
        self,
        conn: sqlite3.Connection,
        doc_id: str,
        text: str,
        metadata: dict,
        embedding: list[float],
    ):
        now = datetime.now(timezone.utc).isoformat()
        project_name = str(metadata.get("project_name") or "")
        row = conn.execute(
            """
            INSERT INTO memory_documents (
                doc_id, text, metadata, project_name, session_id, timestamp,
                summary_type, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                text = excluded.text,
                metadata = excluded.metadata,
                project_name = excluded.project_name,
                session_id = excluded.session_id,
                timestamp = excluded.timestamp,
                summary_type = excluded.summary_type,
                updated_at = excluded.updated_at
            RETURNING id
            """,
            (
                doc_id,
                text,
                json.dumps(metadata),
                project_name,
                metadata.get("session_id"),
                metadata.get("timestamp"),
                metadata.get("summary_type"),
                now,
                now,
            ),
        ).fetchone()

        if row is None:
            raise RuntimeError(f"Failed to upsert memory document {doc_id}")

        row_id = row["id"]
        conn.execute("DELETE FROM memory_vectors WHERE rowid = ?", (row_id,))
        conn.execute(
            "INSERT INTO memory_vectors(rowid, embedding, project_name) VALUES (?, ?, ?)",
            (row_id, serialize_float32(embedding), project_name),
        )

    def _embed_one(self, text: str) -> list[float]:
        if getattr(self, "embedding_fn", None) is None:
            raise RuntimeError("Embedding function is unavailable")

        embeddings = self.embedding_fn.embed([text])
        if not embeddings:
            raise RuntimeError("Embedding function returned no vectors")

        embedding = embeddings[0]
        if len(embedding) != settings.EMBEDDING_DIMENSION:
            raise ValueError(
                f"Expected {settings.EMBEDDING_DIMENSION}-dimensional embedding, got {len(embedding)}"
            )
        return [float(value) for value in embedding]

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

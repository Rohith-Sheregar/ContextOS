import os
import re
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Iterable

from contextos.core.config import settings


@lru_cache(maxsize=1)
def _compiled_ignore_patterns() -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in settings.IGNORE_PATTERNS)


def _normalized_parts(path: str) -> tuple[str, ...]:
    normalized = os.path.abspath(path).replace("\\", "/")
    return PurePosixPath(normalized).parts


def should_ignore_path(
    path: str,
    *,
    ignore_dir_names: Iterable[str] | None = None,
    ignore_patterns: Iterable[re.Pattern[str]] | None = None,
) -> bool:
    """Return True when a path should never be observed by daemon watchers."""
    normalized = os.path.abspath(path).replace("\\", "/")
    ignored_names = {name.casefold() for name in (ignore_dir_names or settings.IGNORE_DIR_NAMES)}

    for part in _normalized_parts(normalized):
        if part.casefold() in ignored_names:
            return True

    patterns = tuple(ignore_patterns) if ignore_patterns is not None else _compiled_ignore_patterns()
    return any(pattern.search(normalized) for pattern in patterns)

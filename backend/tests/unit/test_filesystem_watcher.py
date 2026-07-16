import pytest

from backend.app.core.ignore import should_ignore_path
from backend.app.daemon.watchers.filesystem import FilesystemEventHandler


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("project/node_modules/package/index.js", True),
        ("project/.git/objects/aa/bb", True),
        ("project/venv/Lib/site-packages/pkg.py", True),
        ("project/.venv/Scripts/python.exe", True),
        ("project/__pycache__/module.pyc", True),
        ("project/build/app.bundle.js", True),
        ("project/dist/app.whl", True),
        ("project/.next/cache/file", True),
        ("project/data/contextos.db-wal", True),
        ("project/logs/daemon.log", True),
        ("project/src/build_tools/compiler.py", False),
        ("project/src/contextos/queue.py", False),
    ],
)
def test_should_ignore_path_matches_hard_ignores(path, expected):
    assert should_ignore_path(path) is expected


def test_filesystem_handler_applies_ignore_matcher_at_watcher_level():
    handler = FilesystemEventHandler(loop=None, queue=None, watch_path="project")

    assert handler.should_ignore("project/node_modules") is True
    assert handler.should_ignore("project/src/app.py") is False

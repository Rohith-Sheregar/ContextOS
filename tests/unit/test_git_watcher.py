import os
from pathlib import Path
from unittest.mock import MagicMock
from contextos.daemon.watchers.git import GitWatcher

def test_git_watcher_uses_watch_path_for_project_name(tmp_path, monkeypatch):
    """
    Test with watch_path != repo.working_tree_dir asserting git and filesystem watchers
    agree on project_name by both using the absolute watch_path's basename.
    """
    # Create a git repo
    repo_dir = tmp_path / "my_monorepo"
    repo_dir.mkdir()
    
    import git
    repo = git.Repo.init(repo_dir)
    
    # We want to watch a subfolder inside the repo
    watch_subfolder = repo_dir / "backend_service"
    watch_subfolder.mkdir()
    
    # Fake a git commit so the repo isn't completely empty
    readme = repo_dir / "README.md"
    readme.write_text("Hello")
    repo.index.add([str(readme)])
    repo.index.commit("Initial commit")
    
    loop = MagicMock()
    queue = MagicMock()
    watcher = GitWatcher(loop, queue)
    
    # Mock asyncio methods to avoid event loop errors in synchronous tests
    import asyncio
    def fake_create_task(coro):
        coro.close()
        return MagicMock()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, loop: None)

    # Start watching the subfolder
    watcher.start([str(watch_subfolder)])
    
    # It should have registered the subfolder path in self.repos
    abs_subfolder = os.path.abspath(str(watch_subfolder))
    assert abs_subfolder in watcher.repos
    
    # Run a manual check (monkeypatch the branch/commit so it detects a 'change' if needed,
    # or just look at what it would enqueue). Let's simulate a commit change.
    watcher.state[abs_subfolder]["commit"] = "fake_old_commit"
    
    # _check_repos returns True if changed
    changed = watcher._check_repos()
    
    assert changed is True
    assert queue.put.called
    
    # The event should use the basename of the watch_path, NOT the repo root
    event_data = queue.put.call_args[0][0]
    assert event_data["project_name"] == "backend_service"
    
    watcher.stop()

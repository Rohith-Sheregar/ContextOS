import os
import asyncio
import logging
import time
from git import Repo, InvalidGitRepositoryError
from contextos.core.config import settings
from contextos.core.ignore import should_ignore_path
from contextos.daemon.queue import EventQueue

logger = logging.getLogger("contextos.watchers.git")

class GitWatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: EventQueue):
        self.loop = loop
        self.queue = queue
        self.repos = {}
        self.state = {}
        self._running = False
        self._worker_task = None
        self._current_interval = settings.GIT_POLL_INTERVAL

    def start(self, watch_paths: list[str]):
        """Starts monitoring the specified list of directory paths for git changes."""
        if self._running:
            return

        for path in watch_paths:
            path_abs = os.path.abspath(path)
            if not os.path.exists(path_abs):
                continue
            if should_ignore_path(path_abs):
                logger.info(f"Path is ignored, skipping git watch: {path_abs}")
                continue

            try:
                repo = Repo(path_abs, search_parent_directories=True)
                self.repos[path_abs] = repo

                try:
                    active_branch = repo.active_branch.name
                except TypeError:
                    active_branch = "DETACHED"

                try:
                    head_commit = repo.head.commit.hexsha
                except Exception:
                    head_commit = None

                self.state[path_abs] = {"branch": active_branch, "commit": head_commit}
                logger.info(f"Setting up GitWatcher for: {repo.working_tree_dir}")
            except InvalidGitRepositoryError:
                logger.info(f"Path is not a git repository, skipping git watch: {path_abs}")

        if not self.repos:
            logger.info("No valid git repositories found to watch.")
            return

        self._running = True
        self._current_interval = settings.GIT_POLL_INTERVAL
        self._worker_task = asyncio.create_task(self._poll_loop())
        logger.info("GitWatcher started.")

    def stop(self):
        """Stops the git monitoring."""
        if not self._running:
            return

        self._running = False
        if self._worker_task:
            self._worker_task.cancel()

        for repo in self.repos.values():
            repo.close()

        self.repos.clear()
        self.state.clear()
        logger.info("GitWatcher stopped.")

    async def _poll_loop(self):
        """Polls the git repositories periodically."""
        while self._running:
            interval = self._current_interval
            expected_wake = time.monotonic() + interval
            try:
                await asyncio.sleep(interval)
                drift = time.monotonic() - expected_wake
                if drift > settings.SLEEP_WAKE_DRIFT_SECONDS:
                    logger.info("Detected possible sleep/wake gap of %.1fs in GitWatcher.", drift)
                changed = await asyncio.to_thread(self._check_repos)
                if changed:
                    self._current_interval = settings.GIT_POLL_INTERVAL
                else:
                    self._current_interval = min(
                        settings.GIT_POLL_MAX_INTERVAL,
                        max(settings.GIT_POLL_INTERVAL, self._current_interval * settings.GIT_POLL_BACKOFF_FACTOR),
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in GitWatcher polling loop; retrying in {settings.WATCHER_RESTART_DELAY_SECONDS}s: {e}")
                await asyncio.sleep(settings.WATCHER_RESTART_DELAY_SECONDS)

    def _check_repos(self) -> bool:
        """Checks all monitored repos for state changes."""
        changed = False
        for path_abs, repo in self.repos.items():
            try:
                repo.git.clear_cache()
                project_name = os.path.basename(repo.working_tree_dir)
                current_state = self.state.get(path_abs, {})

                try:
                    active_branch = repo.active_branch.name
                except TypeError:
                    active_branch = "DETACHED"

                try:
                    head_commit = repo.head.commit
                    head_hexsha = head_commit.hexsha
                except Exception:
                    head_hexsha = None

                if current_state.get("branch") != active_branch:
                    logger.info(f"Git checkout detected: {current_state.get('branch')} -> {active_branch}")
                    event_data = {
                        "source": "git",
                        "event_type": "checkout",
                        "file_path": repo.working_tree_dir,
                        "project_name": project_name,
                        "payload": {
                            "old_branch": current_state.get("branch"),
                            "new_branch": active_branch,
                        },
                    }
                    asyncio.run_coroutine_threadsafe(self.queue.put(event_data), self.loop)
                    self.state[path_abs]["branch"] = active_branch
                    changed = True

                if head_hexsha and current_state.get("commit") != head_hexsha:
                    logger.info(f"Git new commit detected: {head_hexsha}")
                    payload = {
                        "hash": head_hexsha,
                        "author": head_commit.author.name,
                        "message": head_commit.message.strip(),
                        "branch": active_branch,
                    }
                    if head_commit.parents:
                        parent = head_commit.parents[0]
                        diffs = parent.diff(head_commit)
                        payload["files_changed"] = len(diffs)
                    else:
                        payload["files_changed"] = "initial"

                    event_data = {
                        "source": "git",
                        "event_type": "commit",
                        "file_path": repo.working_tree_dir,
                        "project_name": project_name,
                        "payload": payload,
                    }
                    asyncio.run_coroutine_threadsafe(self.queue.put(event_data), self.loop)
                    self.state[path_abs]["commit"] = head_hexsha
                    changed = True

            except Exception as e:
                logger.error(f"Error checking git repo {path_abs}: {e}")
        return changed

    def is_alive(self) -> bool:
        return self._running and self._worker_task is not None and not self._worker_task.done()

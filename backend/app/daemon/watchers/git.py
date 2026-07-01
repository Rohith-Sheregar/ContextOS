import os
import asyncio
import logging
from git import Repo, InvalidGitRepositoryError
from backend.app.core.config import settings
from backend.app.daemon.queue import EventQueue

logger = logging.getLogger("contextos.watchers.git")

class GitWatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: EventQueue):
        self.loop = loop
        self.queue = queue
        self.repos = {}  # Map of path -> Repo object
        self.state = {}  # Map of path -> {branch: str, commit: str}
        self._running = False
        self._worker_task = None

    def start(self, watch_paths: list[str]):
        """Starts monitoring the specified list of directory paths for git changes."""
        if self._running:
            return
            
        for path in watch_paths:
            path_abs = os.path.abspath(path)
            if not os.path.exists(path_abs):
                continue
            
            try:
                repo = Repo(path_abs, search_parent_directories=True)
                self.repos[path_abs] = repo
                
                # Initialize state
                try:
                    active_branch = repo.active_branch.name
                except TypeError:
                    active_branch = "DETACHED"
                
                try:
                    head_commit = repo.head.commit.hexsha
                except Exception:
                    head_commit = None
                    
                self.state[path_abs] = {
                    "branch": active_branch,
                    "commit": head_commit
                }
                logger.info(f"Setting up GitWatcher for: {repo.working_tree_dir}")
            except InvalidGitRepositoryError:
                logger.info(f"Path is not a git repository, skipping git watch: {path_abs}")
        
        if not self.repos:
            logger.info("No valid git repositories found to watch.")
            return

        self._running = True
        self._worker_task = asyncio.create_task(self._poll_loop())
        logger.info("GitWatcher started.")

    def stop(self):
        """Stops the git monitoring."""
        if not self._running:
            return
            
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
        
        # Close repo objects to free file handles
        for repo in self.repos.values():
            repo.close()
            
        self.repos.clear()
        self.state.clear()
        logger.info("GitWatcher stopped.")

    async def _poll_loop(self):
        """Polls the git repositories periodically."""
        interval = settings.GIT_POLL_INTERVAL
        
        while self._running:
            try:
                await asyncio.sleep(interval)
                await asyncio.to_thread(self._check_repos)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in GitWatcher polling loop: {e}")

    def _check_repos(self):
        """Checks all monitored repos for state changes."""
        for path_abs, repo in self.repos.items():
            try:
                # Refresh repo state
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
                
                # Check for branch change
                if current_state.get("branch") != active_branch:
                    logger.info(f"Git checkout detected: {current_state.get('branch')} -> {active_branch}")
                    event_data = {
                        "source": "git",
                        "event_type": "checkout",
                        "file_path": repo.working_tree_dir,
                        "project_name": project_name,
                        "payload": {
                            "old_branch": current_state.get("branch"),
                            "new_branch": active_branch
                        }
                    }
                    asyncio.run_coroutine_threadsafe(self.queue.put(event_data), self.loop)
                    self.state[path_abs]["branch"] = active_branch
                
                # Check for commit change
                if head_hexsha and current_state.get("commit") != head_hexsha:
                    logger.info(f"Git new commit detected: {head_hexsha}")
                    
                    # Basic commit info
                    payload = {
                        "hash": head_hexsha,
                        "author": head_commit.author.name,
                        "message": head_commit.message.strip(),
                        "branch": active_branch
                    }
                    
                    # Try to get basic stats if parent exists
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
                        "payload": payload
                    }
                    asyncio.run_coroutine_threadsafe(self.queue.put(event_data), self.loop)
                    self.state[path_abs]["commit"] = head_hexsha
                    
            except Exception as e:
                logger.error(f"Error checking git repo {path_abs}: {e}")

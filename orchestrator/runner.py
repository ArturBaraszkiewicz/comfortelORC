from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .processes import ProcessTree, process_options
from .providers import Output, command_for, prompt_for
from .store import Store

LOG_LIMIT = 8 * 1024 * 1024
DIFF_LIMIT = 2 * 1024 * 1024
logger = logging.getLogger(__name__)


def git(*args: str, cwd: Path | None = None, timeout=30) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout,
                            env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "Błąd Git.")
    return result.stdout.strip()


def validate_project(path: str) -> tuple[str, str]:
    location = Path(path).expanduser().resolve(strict=True)
    if not location.is_dir():
        raise ValueError("Wskaż katalog repozytorium Git.")
    if git("rev-parse", "--is-bare-repository", cwd=location) == "true":
        raise ValueError("Wskaż zwykłe repozytorium z plikami roboczymi.")
    root = Path(git("rev-parse", "--show-toplevel", cwd=location)).resolve()
    commit = git("rev-parse", "--verify", "HEAD", cwd=root)
    return str(root), commit


class Cancelled(Exception):
    pass


class Runner:
    def __init__(self, store: Store, data_dir: Path, command_factory=command_for):
        self.store = store
        self.data_dir = data_dir
        self.command_factory = command_factory
        self.stopping = threading.Event()
        self.wakeup = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.loop, name="local-orchestrator-worker", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                raise RuntimeError("Proces roboczy nie zakończył się w wymaganym czasie.")

    def loop(self):
        while not self.stopping.is_set():
            try:
                job = self.store.claim()
                if job:
                    self.run(job)
                    continue
            except Exception:
                logger.exception("Błąd obsługi kolejki")
            self.wakeup.wait(1)
            self.wakeup.clear()

    def check_cancelled(self, job_id):
        if self.stopping.is_set() or self.store.job(job_id)["cancel_requested"]:
            raise Cancelled()

    def run_process(self, job_id, command, cwd, timeout, on_line, stdin_text=None):
        self.check_cancelled(job_id)
        env = os.environ.copy()
        # A server launched from Claude must not make the child look like a nested session.
        env.pop("CLAUDECODE", None)
        env.update({"PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"})
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   **process_options())
        tree = None
        lines = queue.Queue(maxsize=128)
        reader_stop = threading.Event()

        def put(item):
            while not reader_stop.is_set():
                try:
                    lines.put(item, timeout=0.2)
                    return
                except queue.Full:
                    pass

        def read():
            try:
                while not reader_stop.is_set():
                    # Tool results can be large JSONL records. Keep a whole record
                    # up to the overall output budget instead of splitting at 64 KB.
                    line = process.stdout.readline(LOG_LIMIT + 1)
                    if not line:
                        break
                    put(line.decode("utf-8", errors="replace").rstrip("\r\n"))
            finally:
                put(None)

        def write():
            try:
                if stdin_text:
                    process.stdin.write(stdin_text.encode("utf-8"))
                    process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()

        reader = writer = None
        try:
            tree = ProcessTree(process)
            reader = threading.Thread(target=read, daemon=True)
            writer = threading.Thread(target=write, daemon=True)
            reader.start()
            writer.start()
            deadline = time.monotonic() + timeout
            received = 0
            eof = False
            while not eof:
                self.check_cancelled(job_id)
                if time.monotonic() > deadline:
                    raise TimeoutError("Przekroczono limit czasu zadania.")
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    # Kill leftover children that inherited stdout after their parent exits.
                    if process.poll() is not None:
                        tree.close()
                    continue
                if line is None:
                    eof = True
                else:
                    received += len(line)
                    if received > LOG_LIMIT:
                        raise RuntimeError("Agent przekroczył limit logów (8 MB). Wynik częściowy został zachowany.")
                    on_line(line)
            while process.poll() is None:
                self.check_cancelled(job_id)
                if time.monotonic() > deadline:
                    raise TimeoutError("Przekroczono limit czasu zadania.")
                self.stopping.wait(0.1)
            return process.returncode
        finally:
            reader_stop.set()
            if tree:
                tree.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for thread in (reader, writer):
                if thread:
                    thread.join(timeout=2)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()

    def capture_diff(self, job, workspace):
        # Intent-to-add makes new, non-ignored files appear in the review patch too.
        git("add", "--intent-to-add", "--", ".", cwd=workspace, timeout=8)
        patch_path = workspace.parent / "changes.patch"
        with patch_path.open("wb") as patch:
            result = subprocess.run(["git", "diff", "--no-ext-diff", "--no-textconv", "--binary",
                                     job["base_commit"], "--"], cwd=workspace, stdout=patch,
                                    stderr=subprocess.PIPE, timeout=8,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
        size = patch_path.stat().st_size
        with patch_path.open("rb") as patch:
            preview = patch.read(DIFF_LIMIT).decode("utf-8", errors="replace")
        self.store.update(job["id"], diff=preview, diff_truncated=int(size > DIFF_LIMIT))

    def run(self, job):
        job_id = job["id"]
        workspace = self.data_dir / "workspaces" / job_id / "repo"
        output = Output()
        exit_code = None
        status, error = "failed", ""
        prepared = False
        try:
            self.check_cancelled(job_id)
            if not shutil.which("git"):
                raise ValueError("Nie znaleziono Git w PATH.")
            workspace.parent.mkdir(parents=True, exist_ok=True)
            self.store.event(job_id, "status", "Przygotowuję osobną kopię repozytorium z zapisanego commita.")
            code = self.run_process(job_id, ["git", "clone", "--no-hardlinks", "--no-checkout", "--",
                                           job["project_path"], str(workspace)],
                                    workspace.parent, 180, lambda line: self.store.event(job_id, "log", line))
            if code:
                raise RuntimeError("Nie udało się skopiować repozytorium. Szczegóły są w logu.")
            self.check_cancelled(job_id)
            git("remote", "remove", "origin", cwd=workspace, timeout=8)
            branch = f"local-task/{job_id[:12]}"
            git("checkout", "-b", branch, job["base_commit"], cwd=workspace, timeout=8)
            self.store.update(job_id, workspace=str(workspace), branch=branch)
            prepared = True
            self.store.event(job_id, "status", f"Uruchamiam {job['provider']} · {branch}")

            def on_line(line):
                for kind, message in output.parse(job["provider"], line):
                    self.store.event(job_id, kind, message)

            exit_code = self.run_process(job_id, self.command_factory(job, workspace), workspace,
                                         job["timeout_minutes"] * 60, on_line, prompt_for(job))
            self.check_cancelled(job_id)
            if exit_code != 0:
                error = output.error or output.result or f"Agent zakończył się kodem {exit_code}. Sprawdź logi i logowanie CLI."
            elif output.error:
                error = output.error
            elif not output.completed:
                error = "Proces zakończył się bez potwierdzenia wyniku agenta. Sprawdź logi."
            else:
                status = "succeeded"
        except Cancelled:
            status = "interrupted" if self.stopping.is_set() else "cancelled"
            error = "Aplikacja została zatrzymana." if self.stopping.is_set() else "Zadanie anulowane."
        except Exception as exc:
            error = str(exc)
            self.store.event(job_id, "error", error)
        finally:
            if prepared:
                try:
                    self.capture_diff(job, workspace)
                except Exception as exc:
                    self.store.event(job_id, "error", f"Nie udało się przygotować różnic: {exc}")
                    if status == "succeeded":
                        status, error = "failed", "Agent zakończył pracę, ale zapis różnic nie powiódł się. Kopia robocza jest zachowana."
            self.store.update(job_id, result=output.result, exit_code=exit_code, **output.stats)
            self.store.finish(job_id, status, error)
            self.store.event(job_id, "status", "Zakończono: " + self.store.job(job_id)["status"])

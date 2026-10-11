import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from orchestrator.auth import Auth, COOKIE
from orchestrator.integrations import document_text, jira_issue, validate_remote, project_commit
from orchestrator.processes import InstanceLock
from orchestrator.providers import Output, command_for
from orchestrator.runner import Runner, git
from orchestrator.server import create_app
from orchestrator.store import Store


class Environment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orchestrator-test-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "source repo"
        self.repo.mkdir()
        git("init", str(self.repo))
        git("config", "user.name", "Test", cwd=self.repo)
        git("config", "user.email", "test@example.invalid", cwd=self.repo)
        (self.repo / "readme.txt").write_text("original\n", encoding="utf-8")
        git("add", ".", cwd=self.repo)
        git("commit", "-m", "initial", cwd=self.repo)
        self.commit = git("rev-parse", "HEAD", cwd=self.repo)
        self.store = Store(self.root / "data" / "orchestrator.db")
        self.store.initialize()
        self.project = self.store.add_project("Test project", str(self.repo), "Keep it simple.")

    def tearDown(self):
        # Windows read-only Git object files are removed by TemporaryDirectory's cleanup.
        self.temp.cleanup()

    def queued(self, **overrides):
        fields = dict(project_id=self.project["id"], title="Test", prompt="Perform task",
                      provider="claude", model="", kind="code", timeout_minutes=1,
                      base_commit=self.commit, created_by="admin")
        fields.update(overrides)
        return self.store.add_job(**fields)

    def fake_agent(self, body):
        script = self.root / "fake_agent.py"
        script.write_text("import sys, json, time\nfrom pathlib import Path\nsys.stdin.read()\n" + body, encoding="utf-8")
        return lambda job, workspace: [sys.executable, str(script)]


class WorkerTests(Environment):
    def test_real_process_isolated_workspace_patch_and_unicode(self):
        # Dirty source files are intentionally not copied or overwritten.
        (self.repo / "readme.txt").write_text("user's uncommitted work\n", encoding="utf-8")
        command = self.fake_agent(
            "Path('readme.txt').write_text('updated\\n', encoding='utf-8')\n"
            "Path('nowy.txt').write_text('Zażółć gęślą jaźń\\n', encoding='utf-8')\n"
            "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'Pracuję.'}]} }), flush=True)\n"
            "print(json.dumps({'type':'result','subtype':'success','result':'Gotowe: zażółć.', 'usage':{'output_tokens':12}}), flush=True)\n")
        job = self.queued()
        runner = Runner(self.store, self.root / "data", command)
        runner.run(self.store.claim())
        result = self.store.job(job["id"])
        self.assertEqual(result["status"], "succeeded", result["error"])
        self.assertIn("zażółć", result["result"])
        self.assertIn("nowy.txt", result["diff"])
        self.assertIn("+updated", result["diff"])
        self.assertEqual((self.repo / "readme.txt").read_text(), "user's uncommitted work\n")
        self.assertFalse((self.repo / "nowy.txt").exists())
        self.assertEqual(git("remote", cwd=Path(result["workspace"])), "")
        self.assertEqual(result["output_tokens"], 12)

    def test_agent_error_even_with_success_exit_code(self):
        command = self.fake_agent("print(json.dumps({'type':'result','is_error':True,'result':'Missing login'}))\n")
        job = self.queued()
        Runner(self.store, self.root / "data", command).run(self.store.claim())
        result = self.store.job(job["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("Missing login", result["error"])

    def test_no_result_is_not_reported_as_success(self):
        job = self.queued()
        Runner(self.store, self.root / "data", self.fake_agent("print('hello')\n")).run(self.store.claim())
        self.assertEqual(self.store.job(job["id"])["status"], "failed")

    def test_cancel_running_job(self):
        job = self.queued()
        command = self.fake_agent("print('ready', flush=True)\ntime.sleep(60)\n")
        runner = Runner(self.store, self.root / "data", command)
        runner.start()
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if any(event["text"] == "ready" for event in self.store.events(job["id"])):
                    break
                time.sleep(.1)
            else:
                self.fail("Fake agent did not start")
            self.store.cancel(job["id"])
            deadline = time.monotonic() + 10
            while self.store.job(job["id"])["status"] == "running" and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertEqual(self.store.job(job["id"])["status"], "cancelled")
        finally:
            runner.stop()

    def test_timeout_stops_process(self):
        job = self.queued()
        runner = Runner(self.store, self.root / "data")
        with self.assertRaises(TimeoutError):
            runner.run_process(job["id"], [sys.executable, "-c", "import time; time.sleep(60)"],
                               self.repo, .3, lambda line: None)

    def test_concurrent_claim_does_not_execute_job_twice(self):
        self.queued()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.store.claim())) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_recovery_does_not_auto_resume_jobs(self):
        first = self.queued()
        self.store.claim()
        second = self.queued()
        self.store.recover()
        self.assertEqual(self.store.job(first["id"])["status"], "interrupted")
        self.assertEqual(self.store.job(second["id"])["status"], "interrupted")
        self.assertIsNone(self.store.claim())

    def test_instance_lock(self):
        first = InstanceLock(self.root / "lock")
        second = InstanceLock(self.root / "lock")
        first.acquire()
        try:
            with self.assertRaises(RuntimeError):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()

    def test_managed_repository_fetches_new_commit_before_task(self):
        mirror = self.root / "managed.git"
        git("clone", "--bare", str(self.repo), str(mirror))
        branch = git("symbolic-ref", "--short", "HEAD", cwd=self.repo)
        (self.repo / "readme.txt").write_text("new committed content\n", encoding="utf-8")
        git("add", ".", cwd=self.repo)
        git("commit", "-m", "next", cwd=self.repo)
        expected = git("rev-parse", "HEAD", cwd=self.repo)
        snapshot = project_commit({"path": str(mirror), "remote_url": str(self.repo), "branch": branch})
        self.assertEqual(snapshot, expected)
        project = self.store.add_project("Remote", str(mirror), "", "ssh://example.invalid/repo", branch)
        job = self.queued(project_id=project["id"], base_commit=snapshot)
        factory = self.fake_agent("assert Path('readme.txt').read_text() == 'new committed content\\n'\nprint(json.dumps({'type':'result','subtype':'success','result':'OK'}))\n")
        Runner(self.store, self.root / "data", factory).run(self.store.claim())
        self.assertEqual(self.store.job(job["id"])["status"], "succeeded")


class ApiTests(Environment):
    def setUp(self):
        super().setUp()
        self.auth = Auth(self.store)
        self.auth.set_password("admin", "testing-password-123", role="admin")
        self.auth.set_password("member", "testing-password-456")
        self.app = create_app(self.root / "data", start_worker=False)
        self.client = TestClient(self.app, base_url="http://localhost")
        self.client.__enter__()
        self.client.headers["X-Orchestrator-Token"] = self.app.state.token

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def login(self, username="admin", password="testing-password-123"):
        response = self.client.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)

    def test_requires_authentication_and_csrf_and_rejects_foreign_origin(self):
        self.assertEqual(self.client.get("/api/projects").status_code, 401)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)
        self.login()
        self.assertEqual(self.client.get("/api/projects").status_code, 200)
        self.assertEqual(self.client.get("/api/projects", headers={"X-Orchestrator-Token": "bad"}).status_code, 403)
        self.assertEqual(self.client.get("/api/projects", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 400)
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/projects").status_code, 401)

    def test_member_cannot_add_project_or_cancel_others_jobs(self):
        self.login("member", "testing-password-456")
        self.assertEqual(self.client.post("/api/projects", json={"name": "Other", "path": str(self.repo)}).status_code, 403)
        job = self.queued(created_by="admin")
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/cancel").status_code, 403)

    def test_create_job_cancel_retry_and_owner(self):
        self.login()
        with patch("orchestrator.server.executable", return_value=[sys.executable]):
            response = self.client.post("/api/jobs", json={"project_id": self.project["id"], "title": "Inspect", "prompt": "Inspect code"})
            self.assertEqual(response.status_code, 201, response.text)
            job = response.json()
            self.assertEqual(job["created_by"], "admin")
            self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/retry").status_code, 409)
            cancelled = self.client.post(f"/api/jobs/{job['id']}/cancel").json()
            self.assertEqual(cancelled["status"], "cancelled")
            retried = self.client.post(f"/api/jobs/{job['id']}/retry")
            self.assertEqual(retried.status_code, 201)
            self.assertNotEqual(retried.json()["id"], job["id"])

    def test_password_change_invalidates_sessions_and_stores_no_plaintext(self):
        self.login()
        self.auth.set_password("admin", "a-new-password-123", role="admin")
        self.assertEqual(self.client.get("/api/projects").status_code, 401)
        with self.store.connect() as db:
            stored = db.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()[0]
        self.assertNotIn("a-new-password", stored)

    def test_login_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.client.post("/api/login", json={"username": "no-user", "password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"username": "admin", "password": "testing-password-123"}).status_code, 429)

    def test_https_public_host_sets_secure_cookie(self):
        hosted_dir = self.root / "hosted"
        hosted_store = Store(hosted_dir / "orchestrator.db")
        hosted_store.initialize()
        Auth(hosted_store).set_password("owner", "hosted-password-123", role="admin")
        app = create_app(hosted_dir, start_worker=False, public_url="https://orchestrator.example.com")
        with TestClient(app, base_url="https://orchestrator.example.com") as client:
            client.headers.update({"X-Orchestrator-Token": app.state.token, "Origin": "https://orchestrator.example.com"})
            response = client.post("/api/login", json={"username": "owner", "password": "hosted-password-123"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("Secure", response.headers["set-cookie"])
            self.assertIn("HttpOnly", response.headers["set-cookie"])
            self.assertIn("SameSite=strict", response.headers["set-cookie"])
            self.assertEqual(client.get("/api/health").json()["username"], "owner")


class IntegrationTests(unittest.TestCase):
    def test_provider_arguments_no_shell_or_permission_bypass(self):
        with patch("orchestrator.providers.executable", return_value=["agent"]):
            command = command_for({"provider": "codex", "kind": "code", "model": ""}, Path("/work"))
            self.assertIn("workspace-write", command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
            command = command_for({"provider": "claude", "kind": "analysis", "model": ""}, Path("/work"))
            self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")

    def test_codex_completed_event_and_error(self):
        output = Output()
        output.parse("codex", json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}}))
        output.parse("codex", json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12}}))
        self.assertTrue(output.completed)
        self.assertEqual(output.result, "Done")
        output.parse("codex", json.dumps({"type": "turn.failed", "error": {"message": "Denied"}}))
        self.assertEqual(output.error, "Denied")

    def test_jira_adf_text_and_invalid_key(self):
        self.assertEqual(document_text({"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Opis"}]}]}), "Opis\n")
        with self.assertRaises(ValueError):
            jira_issue("../../config")

    def test_remote_validation(self):
        with patch.dict(os.environ, {"COMFORTEL_GIT_HOSTS": "git.example.com"}):
            validate_remote("git@git.example.com:team/project.git")
            validate_remote("https://git.example.com/team/project.git")
            for bad in ["ext::sh -c command", "file:///etc/passwd", "https://token@git.example.com/project", "https://other.example/project"]:
                with self.assertRaises(ValueError, msg=bad):
                    validate_remote(bad)


if __name__ == "__main__":
    unittest.main()

"""Optional UI test. Uses a temporary account/repo and a fake CLI, never a model."""
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from playwright.sync_api import sync_playwright, expect

from orchestrator.auth import Auth
from orchestrator.runner import git
from orchestrator.server import create_app
from orchestrator.store import Store

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "data" / "qa"


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="orchestrator-browser-") as folder:
        root = Path(folder)
        source = root / "source"
        source.mkdir()
        git("init", str(source))
        git("config", "user.name", "Browser Test", cwd=source)
        git("config", "user.email", "test@example.invalid", cwd=source)
        (source / "README.md").write_text("Example project\n", encoding="utf-8")
        git("add", ".", cwd=source)
        git("commit", "-m", "initial", cwd=source)
        fake = root / "fake.py"
        fake.write_text("""import json, sys, time
from pathlib import Path
sys.stdin.read()
print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'Sprawdzam strukturę repozytorium.'}]}}), flush=True)
time.sleep(1)
Path('review.txt').write_text('Przykładowy wynik testu UI.\\n', encoding='utf-8')
print(json.dumps({'type':'result','subtype':'success','result':'Test panelu zakończony. Utworzono review.txt w osobnej kopii projektu. To kontrolowany proces testowy, bez wywołania modelu.'}), flush=True)
""", encoding="utf-8")
        store = Store(root / "data" / "orchestrator.db")
        store.initialize()
        password = secrets.token_urlsafe(20)
        Auth(store).set_password("tester", password, role="admin")
        app = create_app(root / "data", command_factory=lambda job, workspace: [sys.executable, str(fake)])
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        if not server.started:
            raise RuntimeError("Test server did not start")
        try:
            with sync_playwright() as playwright:
                executable = os.environ.get("BROWSER_EXECUTABLE")
                if not executable and os.name == "nt":
                    executable = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
                browser = playwright.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                page.set_default_timeout(12000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}")
                expect(page.get_by_role("heading", name="Dobrze Cię widzieć.")).to_be_visible()
                page.screenshot(path=str(ARTIFACTS / "login.png"), full_page=True)
                page.get_by_label("Login", exact=True).fill("tester")
                page.get_by_label("Hasło", exact=True).fill(password)
                page.get_by_role("button", name="Zaloguj się →", exact=True).click()
                expect(page.get_by_role("heading", name="Twoje centrum pracy.")).to_be_visible()
                page.locator("#empty-add-project").click()
                page.get_by_label("Nazwa projektu", exact=True).fill("Projekt testowy Comfortel")
                page.get_by_text("Alternatywnie: istniejące repozytorium na serwerze", exact=True).click()
                page.get_by_label("Ścieżka na serwerze", exact=True).fill(str(source))
                page.locator('#project-form button[type="submit"]').click()
                expect(page.locator("#project-dialog")).not_to_be_visible()
                page.locator("#new-task").click()
                page.get_by_label("Tytuł", exact=True).fill("Przegląd struktury projektu")
                page.get_by_label("Opis zadania", exact=True).fill("Przygotuj przegląd projektu i zapisz krótkie podsumowanie.")
                page.locator("#task-kind").select_option("code")
                page.locator('#task-form button[type="submit"]').click()
                expect(page.locator("#detail-status")).to_have_text("Ukończone", timeout=30000)
                expect(page.locator("#result-text")).to_contain_text("Test panelu zakończony")
                page.screenshot(path=str(ARTIFACTS / "dashboard.png"), full_page=True)
                page.get_by_role("tab", name="Przebieg", exact=True).click()
                expect(page.locator("#event-log")).to_contain_text("Sprawdzam strukturę")
                page.get_by_role("tab", name="Zmiany", exact=True).click()
                expect(page.locator("#diff-text")).to_contain_text("review.txt")
                with page.expect_download() as download:
                    page.locator("#download-patch").click()
                assert download.value.suggested_filename.endswith(".patch")
                page.get_by_role("tab", name="Wynik", exact=True).click()
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(ARTIFACTS / "mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile layout overflows"
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.locator('[data-page="system"]').click()
                expect(page.get_by_role("heading", name="Połączenia.", exact=True)).to_be_visible()
                page.locator("#logout").click()
                expect(page.get_by_role("heading", name="Dobrze Cię widzieć.")).to_be_visible()
                assert not errors, errors
                assert not (source / "review.txt").exists(), "Source repo was changed"
                browser.close()
                print("PASS: login, project, job, logs, result, diff, download, mobile, connections, logout")
                print(f"Screenshots: {ARTIFACTS}")
        finally:
            server.should_exit = True
            thread.join(timeout=25)
            sock.close()


if __name__ == "__main__":
    main()

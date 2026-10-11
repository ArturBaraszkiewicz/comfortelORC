"""Git access uses the host's SSH/credential configuration; Jira uses server env."""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

from .runner import git

_git_lock = threading.Lock()


def validate_remote(url: str):
    parsed = urlparse(url)
    if parsed.scheme in {"https", "ssh"}:
        host = parsed.hostname
        if parsed.password or (parsed.scheme == "https" and parsed.username):
            raise ValueError("Nie zapisuj tokenów w adresie Git. Skonfiguruj dostęp na serwerze.")
    elif re.fullmatch(r"[a-zA-Z0-9_.-]+@[a-zA-Z0-9.-]+:[a-zA-Z0-9_./-]+", url):
        host = url.split("@", 1)[1].split(":", 1)[0]
    else:
        raise ValueError("Podaj adres Git HTTPS lub SSH (np. git@host:grupa/projekt.git).")
    allowed = {s.strip().lower() for s in os.environ.get("COMFORTEL_GIT_HOSTS", "").split(",") if s.strip()}
    if not host:
        raise ValueError("Nieprawidłowy adres Git.")
    if allowed and host.lower() not in allowed:
        raise ValueError("Ten serwer Git nie jest na liście COMFORTEL_GIT_HOSTS.")


def clone_remote(url: str, branch: str, data_dir: Path):
    validate_remote(url)
    git("check-ref-format", "--branch", branch)
    directory = data_dir / "repositories" / uuid.uuid4().hex
    directory.parent.mkdir(parents=True, exist_ok=True)
    with _git_lock:
        git("clone", "--bare", "--single-branch", "--branch", branch, "--", url, str(directory), timeout=180)
    return str(directory)


def project_commit(project: dict):
    path = Path(project["path"])
    if project.get("remote_url"):
        branch = project["branch"]
        with _git_lock:
            git("fetch", "origin", f"+refs/heads/{branch}:refs/heads/{branch}", cwd=path, timeout=120)
            return git("rev-parse", "--verify", f"refs/heads/{branch}", cwd=path)
    return git("rev-parse", "--verify", "HEAD", cwd=path)


def integration_status():
    return {"jira": {"url": os.environ.get("COMFORTEL_JIRA_URL", ""),
                     "configured": bool(os.environ.get("COMFORTEL_JIRA_URL") and os.environ.get("COMFORTEL_JIRA_TOKEN"))},
            "git_hosts": os.environ.get("COMFORTEL_GIT_HOSTS", "")}


def document_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(document_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    text = value.get("text", "") + document_text(value.get("content", []))
    if value.get("type") in {"paragraph", "heading", "listItem", "codeBlock", "hardBreak"}:
        text += "\n"
    return text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def jira_issue(key: str):
    key = key.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key):
        raise ValueError("Podaj klucz zadania Jira, np. TI-123.")
    base = os.environ.get("COMFORTEL_JIRA_URL", "").rstrip("/")
    token = os.environ.get("COMFORTEL_JIRA_TOKEN", "")
    email = os.environ.get("COMFORTEL_JIRA_EMAIL", "")
    if not base or not token:
        raise ValueError("Administrator musi skonfigurować COMFORTEL_JIRA_URL i COMFORTEL_JIRA_TOKEN na serwerze.")
    parsed = urlparse(base)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
        raise ValueError("Nieprawidłowy adres Jira w konfiguracji serwera.")
    version = os.environ.get("COMFORTEL_JIRA_API_VERSION", "2")
    if version not in {"2", "3"}:
        raise ValueError("COMFORTEL_JIRA_API_VERSION musi mieć wartość 2 lub 3.")
    authorization = ("Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()) if email else "Bearer " + token
    request = urllib.request.Request(f"{base}/rest/api/{version}/issue/{quote(key)}?fields=summary,description,status,project",
                                     headers={"Accept": "application/json", "Authorization": authorization})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            data = json.loads(response.read(2 * 1024 * 1024))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Jira zwróciła HTTP {exc.code}. Sprawdź klucz zadania oraz uprawnienia konta integracji.") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise ValueError("Nie udało się pobrać zadania z Jira. Sprawdź adres i połączenie serwera.") from None
    fields = data.get("fields", {})
    title = f"{key}: {fields.get('summary', '')}"
    description = document_text(fields.get("description"))
    link = f"{base}/browse/{key}"
    return {"key": key, "title": title[:160], "url": link,
            "prompt": f"Zadanie Jira: {title}\n{link}\n\n{description}"[:24000]}

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def executable(provider: str) -> list[str]:
    """Resolve npm shims to Node entrypoints; never interpolate prompts into a shell."""
    if provider not in {"claude", "codex"}:
        raise ValueError("Nieznany agent.")
    override = os.environ.get(f"LOCAL_ORCHESTRATOR_{provider.upper()}_BIN")
    found = override or shutil.which(provider)
    if not found:
        raise ValueError(f"Nie znaleziono {provider} w PATH. Zainstaluj CLI i uruchom aplikację ponownie.")
    path = Path(found)
    if not path.is_file():
        raise ValueError(f"Nie istnieje plik wykonywalny: {path}")
    if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        package = ("@openai/codex/bin/codex.js" if provider == "codex"
                   else "@anthropic-ai/claude-code/cli.js")
        entrypoint = path.parent / "node_modules" / package
        node = shutil.which("node")
        if node and entrypoint.is_file():
            return [node, str(entrypoint)]
        raise ValueError(f"Nie można uruchomić skryptu {path.name}. Wskaż natywny plik .exe przez LOCAL_ORCHESTRATOR_{provider.upper()}_BIN.")
    return [str(path)]


def availability():
    result = []
    for provider, label in (("claude", "Claude Code"), ("codex", "Codex")):
        try:
            command = executable(provider)
            result.append({"id": provider, "name": label, "available": True, "path": command[0]})
        except ValueError as exc:
            result.append({"id": provider, "name": label, "available": False, "error": str(exc)})
    return result


def command_for(job: dict, workspace: Path) -> list[str]:
    provider = job["provider"]
    command = executable(provider)
    if provider == "claude":
        # No blanket permission bypass. Analysis exposes only file-reading tools.
        tools = "Read,Glob,Grep" if job["kind"] == "analysis" else "Read,Glob,Grep,Edit,Write,Bash"
        command += ["-p", "--verbose", "--output-format", "stream-json",
                    "--permission-mode", "dontAsk", "--tools", tools, "--allowedTools", tools,
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    else:
        command += ["exec", "--json", "--color", "never", "--ephemeral",
                    "--sandbox", "read-only" if job["kind"] == "analysis" else "workspace-write",
                    "-c", 'approval_policy="never"', "-C", str(workspace)]
    if job["model"]:
        command += ["--model", job["model"]]
    if provider == "codex":
        command.append("-")
    return command


def prompt_for(job: dict) -> str:
    return (
        "Pracujesz dla Local Orchestrator w osobnej lokalnej kopii repozytorium. "
        "Nie wykonuj git push, nie twórz PR i nie modyfikuj repozytorium źródłowego. "
        "Nie uruchamiaj usług w tle. Na koniec odpowiedz po polsku: wynik, zmienione pliki, "
        "wykonane sprawdzenia i ograniczenia. "
        + ("To jest analiza: nie zmieniaj plików.\n" if job["kind"] == "analysis"
           else "Wykonaj zadanie i odpowiednie testy. Zostaw zmiany do przeglądu.\n")
        + f"\nInstrukcje projektu:\n{job['instructions']}\n\nZadanie:\n{job['prompt']}\n"
    )


@dataclass
class Output:
    result: str = ""
    error: str = ""
    completed: bool = False
    stats: dict = field(default_factory=dict)

    def parse(self, provider: str, line: str) -> list[tuple[str, str]]:
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return [("log", line)] if line.strip() else []
        if not isinstance(data, dict):
            return [("log", line)]
        kind = data.get("type")
        events = []
        if provider == "claude":
            if kind == "assistant":
                for item in data.get("message", {}).get("content", []):
                    if item.get("type") == "text":
                        events.append(("assistant", item.get("text", "")))
                    elif item.get("type") == "tool_use":
                        events.append(("tool", f"{item.get('name', 'Narzędzie')}: "
                                       + json.dumps(item.get("input", {}), ensure_ascii=False)))
            elif kind == "result":
                self.completed = True
                self.result = data.get("result") or "\n".join(str(e) for e in data.get("errors", []))
                if data.get("is_error") or data.get("subtype", "success") != "success":
                    self.error = self.result or "Agent zakończył zadanie błędem."
                if data.get("permission_denials"):
                    self.error = "Agent napotkał odmowę uprawnień. Sprawdź wynik i logi."
                    events.append(("error", self.error))
                usage = data.get("usage") or {}
                self.stats = {"cost_usd": data.get("total_cost_usd"),
                              "input_tokens": usage.get("input_tokens"),
                              "output_tokens": usage.get("output_tokens")}
            elif kind == "system":
                subtype = data.get("subtype", "")
                events.append(("status", data.get("message") or f"Claude: {subtype}"))
        else:
            if kind in {"item.started", "item.completed"}:
                item = data.get("item", {})
                if item.get("type") == "agent_message" and kind == "item.completed":
                    self.result = item.get("text", "")
                    events.append(("assistant", self.result))
                elif item.get("type") == "command_execution":
                    events.append(("tool", item.get("command", "")))
                    if item.get("aggregated_output"):
                        events.append(("log", item["aggregated_output"]))
                elif item.get("type") == "file_change" and kind == "item.completed":
                    events.append(("tool", "Zmiany: " + json.dumps(item.get("changes", []), ensure_ascii=False)))
            elif kind == "turn.completed":
                self.completed = True
                usage = data.get("usage") or {}
                self.stats = {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}
            elif kind in {"turn.failed", "error"}:
                error = data.get("error") or data.get("message") or "Błąd Codexa."
                self.error = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                events.append(("error", self.error))
        return events

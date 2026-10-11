# Comfortel Orchestrator — bez kontenerów

Samodzielna aplikacja WWW inspirowana przepływem obecnego orchestratora. Działa na jednej VM z Linuxem, obsługuje konta kilku osób, wspólne projekty i kolejkę zadań AI. Nie wymaga Docker, Kubernetes, Redis, PostgreSQL ani budowania frontendu.

## Co działa

- Logowanie, sesje ważne 12 godzin, role `admin` i `member`, wylogowanie i unieważnianie sesji po zmianie hasła.
- Administrator dodaje repozytoria przez Git SSH/HTTPS albo istniejącą ścieżkę na serwerze.
- Wszyscy zalogowani mają dostęp do wspólnych projektów, zadań i wyników. Nie ma jeszcze uprawnień per projekt.
- Zadania analizy lub zmian w kodzie, wybór Claude Code / Codex, opcjonalny model i limit czasu.
- Trwała kolejka SQLite; jedno zadanie agenta naraz, niezależnie od liczby użytkowników WWW.
- Zapis autora zadania. Anulowanie własnego zadania lub dowolnego przez administratora.
- Osobna kopia Git na zadanie; źródłowe repozytorium nie jest zmieniane przez mechanizm przygotowania kopii.
- Podgląd logów, odpowiedzi, wykorzystania modelu (gdy CLI je zwraca), diff i pobranie pełnego patcha.
- Pobranie tytułu i opisu Jira po kluczu, np. `TI-123`, do formularza zadania. Użytkownik wybiera odpowiadający projekt przed uruchomieniem.
- Ponowienie zadania jako nowego wykonania; aktualny commit źródłowy pobierany jest przy zlecaniu.

To pierwsza działająca wersja przepływu zadań, nie pełna kopia starej aplikacji. Nie ma jeszcze automatycznych webhooków, katalogu wszystkich repozytoriów z API dostawcy Git, publikowania branchy/PR, komentarzy i zmian statusu w Jira, czatu wieloturowego, harmonogramów ani grafu pamięci. Wynik pracy zostaje na serwerze do przeglądu i pobrania. Integracja Jira jest tylko do odczytu.

## Architektura

```text
Przeglądarki zespołu
        │ HTTPS
        ▼
Caddy / firmowe reverse proxy
        │ 127.0.0.1:8787
        ▼
FastAPI + panel HTML/JS ─── SQLite (konta, sesje, projekty, zadania, logi)
        │
        ├── Git SSH/HTTPS ── firmowe repozytoria
        ├── Jira REST ────── opis wskazanego zgłoszenia
        └── kolejka ──────── kopia repo → Claude Code / Codex → wynik + patch
```

Proces aplikacji zarządza kolejką w tle. Nie trzeba instalować własnego demona; na VM utrzymuje go standardowy `systemd`. Zamknięcie przeglądarki nie zatrzymuje zadania. Zamknięcie serwera przerywa bieżące wykonanie. Przy restarcie wcześniejsze zadania aktywne i oczekujące są oznaczane jako przerwane — wymagają świadomego ponowienia. Nie uruchamiaj `uvicorn --workers` ani kilku instancji korzystających z jednego katalogu danych.

## Uruchomienie lokalne (Windows)

Wymagania: Python 3.10+, Git i zainstalowane CLI wybranego agenta.

```powershell
git clone https://github.com/ArturBaraszkiewicz/comfortelORC.git
cd comfortelORC
.\start.cmd
```

Pierwszy start tworzy `.venv`, instaluje zależności i prosi o hasło konta `admin`. Panel: **http://127.0.0.1:8787**. Zatrzymanie: `Ctrl+C` w oknie serwera. W razie zajętego portu: `.\start.cmd -Port 8790`.

Logowanie do modelu wykonaj osobno jako ten sam użytkownik systemowy, który uruchamia aplikację: `claude` lub `codex login`. Aplikacja nie korzysta z sesji otwartego okna Codexa; wywołuje CLI. Błędy logowania, limity lub błędy sandboxa CLI będą widoczne w logach zadań.

## Instalacja na VM Linux

Przenieś **cały ten folder** do `/opt/comfortel-orchestrator`. Nie kopiuj lokalnego `.venv`, `data` ani danych dostępowych z Windows. Poniższe polecenia wykonuje administrator VM; nie były uruchamiane na docelowym serwerze w trakcie przygotowania projektu.

1. Zainstaluj Python 3.10+ z modułem `venv`, Git oraz Caddy (albo wykorzystaj istniejące firmowe reverse proxy).
2. Utwórz konto systemowe i katalog danych:

   ```bash
   sudo useradd --system --create-home --home-dir /var/lib/comfortel-orchestrator --shell /bin/bash comfortel-orchestrator
   sudo install -d -o comfortel-orchestrator -g comfortel-orchestrator -m 700 /var/lib/comfortel-orchestrator/data
   cd /opt/comfortel-orchestrator
   sudo python3 -m venv .venv
   sudo .venv/bin/python -m pip install -r requirements.lock.txt
   ```

3. Zainstaluj wybrane CLI zgodnie z instrukcją dostawcy. Skonfiguruj je i zaloguj **jako `comfortel-orchestrator`**, nie jako root. Katalog domowy konta to `/var/lib/comfortel-orchestrator`. Node.js jest potrzebny, jeśli korzystasz z instalacji CLI przez npm. Zależności projektów (np. Java, Node, .NET) muszą być dostępne na VM. Codex potrzebuje działającego mechanizmu sandboxa Linux; nie wyłączamy go flagą omijającą zabezpieczenia.
4. Skonfiguruj dostęp tego konta do firmowego Git: klucz SSH, `known_hosts` i uprawnienia repozytoriów albo systemowy credential helper dla HTTPS. Adresy repozytoriów w panelu nie mogą zawierać tokenów i haseł. Przetestuj dostęp komendą `git ls-remote ADRES_REPOZYTORIUM` jako konto usługi.
5. Utwórz konta panelu:

   ```bash
   sudo -u comfortel-orchestrator /opt/comfortel-orchestrator/.venv/bin/python /opt/comfortel-orchestrator/manage.py add-user artur --role admin --data-dir /var/lib/comfortel-orchestrator/data
   sudo -u comfortel-orchestrator /opt/comfortel-orchestrator/.venv/bin/python /opt/comfortel-orchestrator/manage.py add-user developer --role member --data-dir /var/lib/comfortel-orchestrator/data
   ```

   Powtórzenie polecenia dla tego samego loginu ustawia nowe hasło i rolę oraz wylogowuje istniejące sesje tego konta.

6. Skopiuj `deploy/server.env.example` do `/etc/comfortel-orchestrator.env`, ustaw prawdziwy adres panelu, dozwolone hosty Git i konfigurację Jira. Plik powinien należeć do root i mieć prawa `600` — `systemd` odczyta go przed przełączeniem użytkownika. Tokenów nie wpisuj do repozytorium.
7. Skopiuj `deploy/comfortel-orchestrator.service` do `/etc/systemd/system/`:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now comfortel-orchestrator
   sudo systemctl status comfortel-orchestrator
   sudo journalctl -u comfortel-orchestrator -f
   ```

8. Skonfiguruj domenę panelu w firmowym DNS oraz reverse proxy. Przykład jest w `deploy/Caddyfile`: zastąp domenę przykładową prawdziwą, zgodną z `LOCAL_ORCHESTRATOR_PUBLIC_URL`, i dołącz konfigurację do istniejącego Caddy. Zweryfikuj: `caddy validate --config /etc/caddy/Caddyfile`, potem przeładuj usługę. Backend pozostaje na `127.0.0.1:8787`; użytkownicy wchodzą przez HTTPS. Dla domeny wewnętrznej użyj firmowego certyfikatu/CA lub firmowej bramy HTTPS zamiast zakładać dostępność publicznego ACME.

Usługa ma `ProtectSystem=strict` i zapis do `/var/lib/comfortel-orchestrator`; zewnętrzne katalogi projektów wymagają osobnego rozważenia praw systemowych. Najprościej dodawać projekty przez URL Git — aplikacja przechowuje je we własnym katalogu danych. Użytkownik usługi nie powinien mieć uprawnień root ani sudo.

## Firmowe Git i Jira

Git działa przez standardowy protokół Git i nie zależy od GitLab/Bitbucket/Gitea. Administrator dodaje URL, gałąź i instrukcje projektu w panelu. Aplikacja zapisuje bazowe repozytorium bare w `data/repositories/`, pobiera wskazaną gałąź przed utworzeniem zadania i klonuje osobną kopię do `data/workspaces/<id>/repo/`.

Jira korzysta z REST API:

- Server / Data Center: `COMFORTEL_JIRA_URL`, `COMFORTEL_JIRA_TOKEN` jako PAT, `COMFORTEL_JIRA_API_VERSION=2`, pusty `COMFORTEL_JIRA_EMAIL`.
- Cloud: adres instancji, e-mail konta, API token i `COMFORTEL_JIRA_API_VERSION=3`.

Uprawnienia konta integracji określają, jakie zgłoszenia zespół może pobrać przez panel. Wspólne konta CLI określają zużycie i rozliczanie modeli. Na tym etapie użytkownicy nie przekazują własnych kluczy AI przez przeglądarkę.

## Wyniki i dane

- `data/orchestrator.db`: SQLite w trybie WAL.
- `data/repositories/`: kopie firmowych repozytoriów.
- `data/workspaces/<id>/repo/`: zachowana kopia robocza agenta.
- `data/workspaces/<id>/changes.patch`: pełny diff, również nowe pliki nieignorowane przez Git. Pliki ignorowane pozostają tylko w kopii roboczej.

Panel pokazuje do 200 najnowszych zadań; starsza historia pozostaje w bazie. Nie ma automatycznego usuwania kopii roboczych — zaplanuj miejsce i retencję przed codziennym użyciem zespołu. Limit logów to 8 MB na proces, podgląd diff do 2 MB / 5000 linii, pełny patch jest do pobrania. Backup SQLite wykonuj przez mechanizm SQLite backup lub po zatrzymaniu usługi; nie kopiuj samego otwartego pliku `.db` z pominięciem WAL. Zachowaj również katalog kopii roboczych, jeśli potrzebujesz odtworzyć wyniki.

Osobne kopie repozytoriów nie izolują całego systemu. W trybie zmian Claude ma dostęp do narzędzi plikowych i powłoki konta usługi; Codex działa z `workspace-write`. To narzędzie dla zaufanego zespołu na dedykowanej VM, ze wspólnym dostępem do projektów. Nie ma jeszcze izolacji uprawnień wykonawczych między użytkownikami.

## Sprawdzenia

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Testy wykonują prawdziwe operacje Git i uruchamiają kontrolowane procesy testowe zamiast płatnych wywołań modelu. Sprawdzają logowanie, role, CSRF, kolejkę, anulowanie, timeout, restart, blokadę instancji i zachowanie źródłowego repozytorium. Po skonfigurowaniu VM wykonaj dodatkowo rzeczywiste zadanie analizy, zadanie zmiany pliku oraz pobranie firmowego zgłoszenia Jira.

Opcjonalny test interfejsu: zainstaluj `playwright` i uruchom `python tests/browser_smoke.py`. Na Windows korzysta z Chrome w standardowej lokalizacji; inny plik wykonywalny można wskazać przez `BROWSER_EXECUTABLE`. Na Linuxie zainstaluj również przeglądarkę testową (`python -m playwright install chromium`). Test tworzy i usuwa osobne konto, bazę i repozytorium testowe; nie wywołuje AI. Zrzuty zapisuje w `data/qa/`.

## Źródła integracji

- [Claude Code: tryb nieinteraktywny](https://code.claude.com/docs/en/headless)
- [Codex: tryb nieinteraktywny](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Caddy: reverse proxy i HTTPS](https://caddyserver.com/docs/quick-starts/reverse-proxy)

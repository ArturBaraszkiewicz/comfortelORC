"""Account management stays on the host; no public registration endpoint."""
import argparse
import getpass
import os
from pathlib import Path

from orchestrator.auth import Auth
from orchestrator.store import Store

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zarządzanie kontami Local Orchestrator")
    parser.add_argument("action", choices=["add-user"])
    parser.add_argument("username")
    parser.add_argument("--role", choices=["admin", "member"], default="member")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("LOCAL_ORCHESTRATOR_DATA", str(Path(__file__).parent / "data"))))
    args = parser.parse_args()
    password = getpass.getpass("Hasło (minimum 12 znaków): ")
    confirm = getpass.getpass("Powtórz hasło: ")
    if password != confirm:
        raise SystemExit("Hasła nie są identyczne.")
    store = Store(args.data_dir.resolve() / "orchestrator.db")
    store.initialize()
    try:
        Auth(store).set_password(args.username, password, role=args.role)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print(f"Konto {args.username} jest gotowe. Istniejące sesje tego konta zostały wylogowane.")

"""Password authentication and revocable, server-side browser sessions."""
import hashlib
import hmac
import secrets
import time

from .store import Store, now

COOKIE = "orchestrator_session"
SESSION_SECONDS = 12 * 60 * 60
ITERATIONS = 600_000


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"{ITERATIONS}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    iterations, salt, expected = encoded.split("$")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    return hmac.compare_digest(actual, expected)


class Auth:
    def __init__(self, store: Store):
        self.store = store

    def set_password(self, username: str, password: str, role: str = "member"):
        if role not in {"admin", "member"}:
            raise ValueError("Nieprawidłowa rola użytkownika.")
        if not username or len(username) > 80 or not all(c.isalnum() or c in "._-" for c in username):
            raise ValueError("Login: 1–80 znaków, litery, cyfry, kropka, podkreślenie lub myślnik.")
        if len(password) < 12:
            raise ValueError("Hasło musi mieć co najmniej 12 znaków.")
        encoded = password_hash(password)
        with self.store.connect() as db:
            db.execute("INSERT INTO users (username, password_hash, created_at, role) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash, role=excluded.role",
                       (username, encoded, now(), role))
            db.execute("DELETE FROM sessions WHERE username=?", (username,))

    def has_users(self):
        with self.store.connect() as db:
            return bool(db.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    def login(self, username: str, password: str, address: str) -> str | None:
        timestamp = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_attempts WHERE attempted_at<?", (timestamp - 900,))
            failures = db.execute("SELECT COUNT(*) FROM login_attempts WHERE address=?", (address,)).fetchone()[0]
            if failures >= 10:
                raise ValueError("Zbyt wiele prób logowania. Spróbuj ponownie za 15 minut.")
            db.execute("INSERT INTO login_attempts VALUES (?, ?)", (address, timestamp))
            row = db.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
        encoded = row[0] if row else f"{ITERATIONS}$unknown$" + "0" * 64
        if not verify_password(password, encoded):
            return None
        token = secrets.token_urlsafe(48)
        with self.store.connect() as db:
            db.execute("DELETE FROM login_attempts WHERE address=?", (address,))
            db.execute("DELETE FROM sessions WHERE expires_at<?", (timestamp,))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (hashlib.sha256(token.encode()).hexdigest(), username, timestamp + SESSION_SECONDS))
        return token

    def user(self, token: str | None):
        if not token:
            return None
        with self.store.connect() as db:
            row = db.execute("SELECT u.username, u.role FROM sessions s JOIN users u ON u.username=s.username WHERE token_hash=? AND expires_at>?",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            return dict(row) if row else None

    def logout(self, token: str | None):
        if token:
            with self.store.connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

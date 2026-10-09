"""User accounts and email one-time codes (SQLite, separate from the read-only election database).

Security choices:
* passwords hashed with scrypt (stdlib), per-user random salt, constant-time comparison;
* one-time codes: 6 digits from `secrets`, stored hashed, valid 10 minutes, 5 attempts max,
  60 s between two sends, single use;
* 5 wrong passwords lock the account for 15 minutes;
* callers get generic errors so an attacker cannot tell whether an email is registered.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from edan_chat import config

CODE_TTL_S = 10 * 60
CODE_MAX_ATTEMPTS = 5
RESEND_COOLDOWN_S = 60
MAX_FAILED_LOGINS = 5
LOCKOUT_S = 15 * 60
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name    TEXT NOT NULL,
    last_name     TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    verified      INTEGER NOT NULL DEFAULT 0,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  REAL NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_login    REAL
);
CREATE TABLE IF NOT EXISTS login_codes (
    email      TEXT PRIMARY KEY,
    code_hash  TEXT NOT NULL,
    purpose    TEXT NOT NULL,           -- 'signup' | 'login'
    expires_at REAL NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    sent_at    REAL NOT NULL
);
"""


class AuthError(ValueError):
    """Message is safe to show to the user."""


@dataclass
class User:
    id: int
    first_name: str
    last_name: str
    email: str

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


# ---- helpers ----------------------------------------------------------------------------------
def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, _ = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, bytes.fromhex(salt_hex)), stored)


def _hash_code(email: str, code: str) -> str:
    return hashlib.sha256(f"{email}:{code}".encode()).hexdigest()


def check_password_policy(password: str) -> None:
    if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise AuthError("Le mot de passe doit contenir au moins 8 caractères, dont des lettres et des chiffres.")


class UserStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or config.USERS_DB_PATH)  # read at call time (tests redirect it)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self):
        """Commit even when an AuthError is raised: failed-attempt counters must persist
        (sqlite3's own context manager would roll them back, disabling the lockout)."""
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.commit()
            db.close()

    # ---- accounts ------------------------------------------------------------------------------
    def create_user(self, first_name: str, last_name: str, email: str, password: str) -> None:
        first_name, last_name, email = first_name.strip(), last_name.strip(), normalize_email(email)
        if not first_name or not last_name:
            raise AuthError("Le nom et le prénom sont obligatoires.")
        if not EMAIL_RE.match(email):
            raise AuthError("Adresse email invalide.")
        check_password_policy(password)
        with self._db() as db:
            row = db.execute("SELECT verified FROM users WHERE email = ?", (email,)).fetchone()
            if row and row["verified"]:
                raise AuthError("Un compte existe déjà avec cet email. Connectez-vous.")
            if row:  # unverified previous attempt: overwrite it
                db.execute("UPDATE users SET first_name=?, last_name=?, password_hash=? WHERE email=?",
                           (first_name, last_name, hash_password(password), email))
            else:
                db.execute("INSERT INTO users (first_name, last_name, email, password_hash, created_at) "
                           "VALUES (?, ?, ?, ?, ?)", (first_name, last_name, email, hash_password(password), time.time()))

    def check_credentials(self, email: str, password: str) -> None:
        """Raise AuthError unless email + password are valid (generic message, lockout)."""
        email = normalize_email(email)
        generic = AuthError("Email ou mot de passe incorrect.")
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if row is None:
                hash_password(password)  # same cost as a real check: no timing oracle
                raise generic
            if row["locked_until"] > time.time():
                minutes = int((row["locked_until"] - time.time()) // 60) + 1
                raise AuthError(f"Trop de tentatives. Réessayez dans {minutes} min.")
            if not verify_password(password, row["password_hash"]):
                failed = row["failed_logins"] + 1
                locked = time.time() + LOCKOUT_S if failed >= MAX_FAILED_LOGINS else 0
                db.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE email=?",
                           (0 if locked else failed, locked, email))
                raise generic
            if not row["verified"]:
                raise AuthError("Adresse email non confirmée : terminez l'inscription avec le code reçu.")
            db.execute("UPDATE users SET failed_logins=0 WHERE email=?", (email,))

    def get_user(self, email: str) -> User | None:
        with self._db() as db:
            row = db.execute("SELECT id, first_name, last_name, email FROM users WHERE email = ?",
                             (normalize_email(email),)).fetchone()
        return User(**dict(row)) if row else None

    # ---- one-time codes ------------------------------------------------------------------------
    def issue_code(self, email: str, purpose: str) -> str:
        email = normalize_email(email)
        with self._db() as db:
            prev = db.execute("SELECT sent_at FROM login_codes WHERE email = ?", (email,)).fetchone()
            if prev and time.time() - prev["sent_at"] < RESEND_COOLDOWN_S:
                wait = int(RESEND_COOLDOWN_S - (time.time() - prev["sent_at"])) + 1
                raise AuthError(f"Un code vient d'être envoyé. Patientez {wait} s avant d'en demander un autre.")
            code = f"{secrets.randbelow(10**6):06d}"
            db.execute("INSERT OR REPLACE INTO login_codes (email, code_hash, purpose, expires_at, attempts, sent_at) "
                       "VALUES (?, ?, ?, ?, 0, ?)",
                       (email, _hash_code(email, code), purpose, time.time() + CODE_TTL_S, time.time()))
        return code

    def verify_code(self, email: str, code: str, purpose: str) -> User:
        email, code = normalize_email(email), re.sub(r"\D", "", code or "")
        invalid = AuthError("Code invalide ou expiré.")
        with self._db() as db:
            row = db.execute("SELECT * FROM login_codes WHERE email = ? AND purpose = ?", (email, purpose)).fetchone()
            if row is None or row["expires_at"] < time.time():
                raise invalid
            if row["attempts"] >= CODE_MAX_ATTEMPTS:
                db.execute("DELETE FROM login_codes WHERE email = ?", (email,))
                raise AuthError("Trop d'essais : demandez un nouveau code.")
            if not hmac.compare_digest(row["code_hash"], _hash_code(email, code)):
                db.execute("UPDATE login_codes SET attempts = attempts + 1 WHERE email = ?", (email,))
                raise invalid
            db.execute("DELETE FROM login_codes WHERE email = ?", (email,))  # single use
            db.execute("UPDATE users SET verified = 1, last_login = ? WHERE email = ?", (time.time(), email))
        user = self.get_user(email)
        if user is None:
            raise invalid
        return user

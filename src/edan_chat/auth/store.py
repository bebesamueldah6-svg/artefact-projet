"""User accounts, email one-time codes and activity log — SQLite or MySQL/MariaDB (e.g. XAMPP).

The backend is chosen by `USERS_DB_URL` (SQLAlchemy URL):
  sqlite:///data/users.db                                    (default, zero setup)
  mysql+pymysql://edan_app:***@127.0.0.1:3306/edan_chat      (XAMPP / any MySQL)
Tables are created automatically. The election data stays in its own read-only DuckDB file.

Security choices:
* passwords hashed with scrypt (stdlib), per-user random salt, constant-time comparison;
* one-time codes: 6 digits from `secrets`, stored hashed, valid 10 minutes, 5 attempts max,
  60 s between two sends, single use;
* 5 wrong passwords lock the account for 15 minutes;
* callers get generic errors so an attacker cannot tell whether an email is registered;
* failed-attempt counters are committed even when an AuthError is raised.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from edan_chat import config

CODE_TTL_S = 10 * 60
CODE_MAX_ATTEMPTS = 5
RESEND_COOLDOWN_S = 60
MAX_FAILED_LOGINS = 5
LOCKOUT_S = 15 * 60
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

metadata = MetaData()
users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("first_name", String(100), nullable=False),
    Column("last_name", String(100), nullable=False),
    Column("email", String(255), nullable=False, unique=True),
    Column("password_hash", String(200), nullable=False),
    Column("verified", Integer, nullable=False, default=0),
    Column("failed_logins", Integer, nullable=False, default=0),
    Column("locked_until", Float, nullable=False, default=0),
    Column("created_at", Float, nullable=False),
    Column("last_login", Float),
    mysql_charset="utf8mb4",
)
login_codes = Table(
    "login_codes", metadata,
    Column("email", String(255), primary_key=True),
    Column("code_hash", String(64), nullable=False),
    Column("purpose", String(10), nullable=False),        # 'signup' | 'login'
    Column("expires_at", Float, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("sent_at", Float, nullable=False),
    mysql_charset="utf8mb4",
)
login_events = Table(                                     # connection history
    "login_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", String(255), nullable=False, index=True),
    Column("event", String(30), nullable=False),          # signup | login_ok | bad_password | bad_code | locked | logout
    Column("created_at", Float, nullable=False),
    mysql_charset="utf8mb4",
)
questions = Table(                                        # questions asked in the app
    "questions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id"), index=True),
    Column("question", Text, nullable=False),
    Column("lang", String(2)),
    Column("route", String(20)),
    Column("intent", String(60)),
    Column("kind", String(20)),
    Column("latency_ms", Float),
    Column("tokens", Integer),
    Column("trace_id", String(32)),
    Column("created_at", Float, nullable=False),
    mysql_charset="utf8mb4",
)


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


def make_engine(target: str | Path | None = None) -> Engine:
    """`target` is a SQLAlchemy URL or a SQLite file path; default: config.USERS_DB_URL."""
    target = target or config.USERS_DB_URL or config.USERS_DB_PATH  # read at call time (tests redirect it)
    if isinstance(target, Path) or "://" not in str(target):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        target = f"sqlite:///{Path(target).as_posix()}"
    return create_engine(str(target), pool_pre_ping=True, pool_recycle=3600)


class UserStore:
    def __init__(self, target: str | Path | None = None):
        self.engine = make_engine(target)
        metadata.create_all(self.engine)

    @property
    def backend(self) -> str:
        return self.engine.dialect.name  # 'sqlite' | 'mysql'

    @contextmanager
    def _db(self):
        """Commit even when an AuthError is raised: failed-attempt counters must persist."""
        conn = self.engine.connect()
        try:
            yield conn
        finally:
            conn.commit()
            conn.close()

    def _event(self, conn, email: str, event: str) -> None:
        conn.execute(insert(login_events).values(email=email, event=event, created_at=time.time()))

    # ---- accounts ------------------------------------------------------------------------------
    def create_user(self, first_name: str, last_name: str, email: str, password: str) -> None:
        first_name, last_name, email = first_name.strip(), last_name.strip(), normalize_email(email)
        if not first_name or not last_name:
            raise AuthError("Le nom et le prénom sont obligatoires.")
        if not EMAIL_RE.match(email):
            raise AuthError("Adresse email invalide.")
        check_password_policy(password)
        with self._db() as db:
            row = db.execute(select(users.c.verified).where(users.c.email == email)).first()
            if row and row.verified:
                raise AuthError("Un compte existe déjà avec cet email. Connectez-vous.")
            values = {"first_name": first_name, "last_name": last_name, "password_hash": hash_password(password)}
            if row:  # unverified previous attempt: overwrite it
                db.execute(update(users).where(users.c.email == email).values(**values))
            else:
                db.execute(insert(users).values(email=email, created_at=time.time(), verified=0,
                                                failed_logins=0, locked_until=0, **values))
            self._event(db, email, "signup")

    def check_credentials(self, email: str, password: str) -> None:
        """Raise AuthError unless email + password are valid (generic message, lockout)."""
        email = normalize_email(email)
        generic = AuthError("Email ou mot de passe incorrect.")
        with self._db() as db:
            row = db.execute(select(users).where(users.c.email == email)).first()
            if row is None:
                hash_password(password)  # same cost as a real check: no timing oracle
                raise generic
            if row.locked_until > time.time():
                self._event(db, email, "locked")
                minutes = int((row.locked_until - time.time()) // 60) + 1
                raise AuthError(f"Trop de tentatives. Réessayez dans {minutes} min.")
            if not verify_password(password, row.password_hash):
                failed = row.failed_logins + 1
                locked = time.time() + LOCKOUT_S if failed >= MAX_FAILED_LOGINS else 0
                db.execute(update(users).where(users.c.email == email)
                           .values(failed_logins=0 if locked else failed, locked_until=locked))
                self._event(db, email, "bad_password")
                raise generic
            if not row.verified:
                raise AuthError("Adresse email non confirmée : terminez l'inscription avec le code reçu.")
            db.execute(update(users).where(users.c.email == email).values(failed_logins=0))

    def get_user(self, email: str) -> User | None:
        with self._db() as db:
            row = db.execute(select(users.c.id, users.c.first_name, users.c.last_name, users.c.email)
                             .where(users.c.email == normalize_email(email))).first()
        return User(**row._mapping) if row else None

    # ---- one-time codes ------------------------------------------------------------------------
    def issue_code(self, email: str, purpose: str) -> str:
        email = normalize_email(email)
        with self._db() as db:
            prev = db.execute(select(login_codes.c.sent_at).where(login_codes.c.email == email)).first()
            if prev and time.time() - prev.sent_at < RESEND_COOLDOWN_S:
                wait = int(RESEND_COOLDOWN_S - (time.time() - prev.sent_at)) + 1
                raise AuthError(f"Un code vient d'être envoyé. Patientez {wait} s avant d'en demander un autre.")
            code = f"{secrets.randbelow(10**6):06d}"
            db.execute(delete(login_codes).where(login_codes.c.email == email))
            db.execute(insert(login_codes).values(email=email, code_hash=_hash_code(email, code), purpose=purpose,
                                                  expires_at=time.time() + CODE_TTL_S, attempts=0, sent_at=time.time()))
        return code

    def verify_code(self, email: str, code: str, purpose: str) -> User:
        email, code = normalize_email(email), re.sub(r"\D", "", code or "")
        invalid = AuthError("Code invalide ou expiré.")
        with self._db() as db:
            row = db.execute(select(login_codes).where(login_codes.c.email == email,
                                                       login_codes.c.purpose == purpose)).first()
            if row is None or row.expires_at < time.time():
                raise invalid
            if row.attempts >= CODE_MAX_ATTEMPTS:
                db.execute(delete(login_codes).where(login_codes.c.email == email))
                raise AuthError("Trop d'essais : demandez un nouveau code.")
            if not hmac.compare_digest(row.code_hash, _hash_code(email, code)):
                db.execute(update(login_codes).where(login_codes.c.email == email)
                           .values(attempts=login_codes.c.attempts + 1))
                self._event(db, email, "bad_code")
                raise invalid
            db.execute(delete(login_codes).where(login_codes.c.email == email))  # single use
            db.execute(update(users).where(users.c.email == email).values(verified=1, last_login=time.time()))
            self._event(db, email, "login_ok")
        user = self.get_user(email)
        if user is None:
            raise invalid
        return user

    # ---- activity --------------------------------------------------------------------------------
    def log_logout(self, email: str) -> None:
        with self._db() as db:
            self._event(db, normalize_email(email), "logout")

    def log_question(self, user_id: int | None, turn) -> None:
        tokens = turn.usage.get("prompt_tokens", 0) + turn.usage.get("completion_tokens", 0)
        with self._db() as db:
            db.execute(insert(questions).values(
                user_id=user_id, question=turn.question[:2000], lang=turn.lang, route=turn.route,
                intent=(turn.intent or "")[:60], kind=turn.kind, latency_ms=turn.latency_ms, tokens=tokens,
                trace_id=turn.trace_id, created_at=time.time()))

    def stats(self) -> dict:
        with self._db() as db:
            return {"users": db.execute(select(func.count()).select_from(users)).scalar_one(),
                    "questions": db.execute(select(func.count()).select_from(questions)).scalar_one(),
                    "logins": db.execute(select(func.count()).select_from(login_events)
                                         .where(login_events.c.event == "login_ok")).scalar_one()}

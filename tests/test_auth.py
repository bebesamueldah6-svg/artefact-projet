"""Accounts, password hashing, email one-time codes, lockout — and the full UI flow in demo mode."""

import re
import time

import pytest

from edan_chat.auth import store as store_mod
from edan_chat.auth.store import AuthError, UserStore, hash_password, verify_password


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "users.db")


def _verified_user(store, email="awa.kone@example.com", password="Secret123"):
    store.create_user("Awa", "Koné", email, password)
    store.verify_code(email, store.issue_code(email, "signup"), "signup")
    return email, password


def test_password_hashing():
    h = hash_password("Secret123")
    assert h.startswith("scrypt$") and "Secret123" not in h
    assert verify_password("Secret123", h) and not verify_password("secret123", h)
    assert hash_password("Secret123") != h  # random salt


@pytest.mark.parametrize("first, last, email, pwd, msg", [
    ("", "Koné", "a@b.ci", "Secret123", "obligatoires"),
    ("Awa", "Koné", "not-an-email", "Secret123", "invalide"),
    ("Awa", "Koné", "a@b.ci", "short1", "8 caractères"),
    ("Awa", "Koné", "a@b.ci", "onlyletters", "chiffres"),
])
def test_signup_validation(store, first, last, email, pwd, msg):
    with pytest.raises(AuthError, match=msg):
        store.create_user(first, last, email, pwd)


def test_signup_requires_email_confirmation(store):
    store.create_user("Awa", "Koné", "Awa.Kone@Example.com ", "Secret123")
    with pytest.raises(AuthError, match="non confirmée"):
        store.check_credentials("awa.kone@example.com", "Secret123")
    user = store.verify_code("awa.kone@example.com", store.issue_code("awa.kone@example.com", "signup"), "signup")
    assert user.display_name == "Awa Koné" and user.email == "awa.kone@example.com"
    store.check_credentials("AWA.KONE@example.com", "Secret123")  # email is case-insensitive
    with pytest.raises(AuthError, match="existe déjà"):
        store.create_user("Awa", "Koné", "awa.kone@example.com", "Other1234")


def test_generic_error_does_not_reveal_accounts(store):
    _verified_user(store)
    with pytest.raises(AuthError) as unknown:
        store.check_credentials("nobody@example.com", "Secret123")
    with pytest.raises(AuthError) as wrong:
        store.check_credentials("awa.kone@example.com", "Wrong1234")
    assert str(unknown.value) == str(wrong.value) == "Email ou mot de passe incorrect."


def test_lockout_after_failed_passwords(store):
    email, password = _verified_user(store)
    for _ in range(store_mod.MAX_FAILED_LOGINS):
        with pytest.raises(AuthError):
            store.check_credentials(email, "Wrong1234")
    with pytest.raises(AuthError, match="Trop de tentatives"):
        store.check_credentials(email, password)  # even the right password is refused while locked


def test_code_is_single_use_limited_and_expires(store, monkeypatch):
    email, _ = _verified_user(store)
    monkeypatch.setattr(store_mod, "RESEND_COOLDOWN_S", 0)
    code = store.issue_code(email, "login")
    assert re.fullmatch(r"\d{6}", code)
    with pytest.raises(AuthError, match="invalide"):
        store.verify_code(email, "000000" if code != "000000" else "111111", "login")
    assert store.verify_code(email, f" {code[:3]} {code[3:]} ", "login").email == email  # spaces tolerated
    with pytest.raises(AuthError):
        store.verify_code(email, code, "login")  # single use

    code = store.issue_code(email, "login")
    for _ in range(store_mod.CODE_MAX_ATTEMPTS):
        with pytest.raises(AuthError):
            store.verify_code(email, "999999" if code != "999999" else "888888", "login")
    with pytest.raises(AuthError, match="Trop d'essais"):
        store.verify_code(email, code, "login")  # brute force stopped

    code = store.issue_code(email, "login")
    real_time = time.time
    monkeypatch.setattr(store_mod.time, "time", lambda: real_time() + store_mod.CODE_TTL_S + 1)
    with pytest.raises(AuthError, match="expiré"):
        store.verify_code(email, code, "login")


def test_resend_cooldown(store):
    email, _ = _verified_user(store)
    store.issue_code(email, "login")
    with pytest.raises(AuthError, match="Patientez"):
        store.issue_code(email, "login")


def test_ui_signup_then_login_flow(tmp_path, monkeypatch):
    """Full UI flow; the email channel is replaced by an in-memory outbox."""
    from streamlit.testing.v1 import AppTest

    from edan_chat import config
    from edan_chat.auth import mailer
    monkeypatch.setattr(config, "USERS_DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(store_mod, "RESEND_COOLDOWN_S", 0)
    outbox: list[str] = []
    monkeypatch.setattr(mailer, "send_code", lambda to, name, code, purpose: outbox.append(code) or "email")

    def last_code() -> str:
        return outbox[-1]

    at = AppTest.from_file(str(config.ROOT / "src/edan_chat/app.py"), default_timeout=90).run()
    assert not at.tabs or at.tabs[0].label == "🔐 Se connecter"   # login screen, app hidden
    assert not any("Tableau de bord" in t.label for t in at.tabs)

    # sign up -> code -> logged in
    at.text_input[2].set_value("Awa")
    at.text_input[3].set_value("Koné")
    at.text_input(key="su_email").set_value("awa@example.com")
    at.text_input(key="su_p1").set_value("Secret123")
    at.text_input(key="su_p2").set_value("Secret123")
    at.button[1].click().run()  # "Créer mon compte"
    at.text_input[0].set_value(last_code())
    at.button[0].click().run()
    assert any("Tableau de bord" in t.label for t in at.tabs) and not at.exception

    # log out, then log in again with password + new code
    next(b for b in at.button if b.label == "🚪 Se déconnecter").click().run()
    at.text_input[0].set_value("awa@example.com")
    at.text_input[1].set_value("Secret123")
    at.button[0].click().run()
    at.text_input[0].set_value(last_code())
    at.button[0].click().run()
    assert any("Tableau de bord" in t.label for t in at.tabs) and not at.exception

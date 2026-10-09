"""Login / sign-up screens. Two factors: password + 6-digit code sent by email."""

from __future__ import annotations

import time

import streamlit as st

from edan_chat import config
from edan_chat.auth import mailer
from edan_chat.auth.store import AuthError, User, UserStore, normalize_email


@st.cache_resource
def get_store() -> UserStore:
    return UserStore()


def current_user() -> User | None:
    user, since = st.session_state.get("auth_user"), st.session_state.get("auth_since", 0)
    if user and time.time() - since > config.SESSION_TTL_S:  # session expiry
        logout()
        return None
    return user


def logout() -> None:
    for k in ("auth_user", "auth_since", "auth_step", "auth_email", "auth_purpose", "auth_name"):
        st.session_state.pop(k, None)


def _send_code(email: str, first_name: str, purpose: str) -> bool:
    try:
        code = get_store().issue_code(email, purpose)
        channel = mailer.send_code(email, first_name, code, purpose)
    except (AuthError, mailer.MailError) as e:
        st.error(str(e))
        return False
    st.session_state.update(auth_step="code", auth_email=email, auth_purpose=purpose, auth_name=first_name,
                            auth_channel=channel)
    return True


def _code_step() -> None:
    email, purpose = st.session_state.auth_email, st.session_state.auth_purpose
    if st.session_state.get("auth_channel") == "terminal":
        st.warning("**Mode démo** : aucun serveur d'email n'est configuré. Le code est affiché dans le "
                   "**terminal** où l'application tourne (voir `SMTP_*` dans `.env.example`).")
    else:
        st.success(f"📧 Un code à 6 chiffres a été envoyé à **{email}**. Vérifiez aussi vos spams.")
    with st.form("code_form"):
        code = st.text_input("Code reçu par email", max_chars=6, placeholder="123456")
        ok = st.form_submit_button("✅ Valider et accéder à l'application", type="primary", width="stretch")
    if ok:
        try:
            user = get_store().verify_code(email, code, purpose)
        except AuthError as e:
            st.error(str(e))
        else:
            st.session_state.update(auth_user=user, auth_since=time.time())
            for k in ("auth_step", "auth_email", "auth_purpose"):
                st.session_state.pop(k, None)
            st.rerun()
    a, b = st.columns(2)
    if a.button("🔁 Renvoyer un code", width="stretch") and _send_code(email, st.session_state.auth_name, purpose):
        st.toast("Nouveau code envoyé.")
    if b.button("← Revenir", width="stretch"):
        logout()
        st.rerun()


def _login_tab() -> None:
    with st.form("login_form"):
        email = st.text_input("Email", placeholder="prenom.nom@exemple.com")
        password = st.text_input("Mot de passe", type="password")
        ok = st.form_submit_button("Se connecter", type="primary", width="stretch")
    if ok:
        try:
            get_store().check_credentials(email, password)
        except AuthError as e:
            st.error(str(e))
            return
        user = get_store().get_user(email)
        if _send_code(normalize_email(email), user.first_name, "login"):
            st.rerun()


def _signup_tab() -> None:
    with st.form("signup_form"):
        a, b = st.columns(2)
        first = a.text_input("Prénom")
        last = b.text_input("Nom")
        email = st.text_input("Email", key="su_email", placeholder="prenom.nom@exemple.com")
        p1 = st.text_input("Mot de passe", type="password", key="su_p1",
                           help="8 caractères minimum, avec des lettres et des chiffres.")
        p2 = st.text_input("Confirmer le mot de passe", type="password", key="su_p2")
        ok = st.form_submit_button("Créer mon compte", type="primary", width="stretch")
    if ok:
        if p1 != p2:
            st.error("Les deux mots de passe ne correspondent pas.")
            return
        try:
            get_store().create_user(first, last, email, p1)
        except AuthError as e:
            st.error(str(e))
            return
        if _send_code(normalize_email(email), first.strip(), "signup"):
            st.rerun()


def require_login() -> User:
    """Render the auth screens and stop the script until the user is fully logged in."""
    user = current_user()
    if user:
        return user
    _, center, _ = st.columns([1, 2, 1])
    with center:
        st.markdown('<div class="hero auth"><span class="pill">Accès sécurisé</span>'
                    "<h1>EDAN 2025 · Chat</h1><p>Connectez-vous avec votre mot de passe, puis le code à "
                    "6 chiffres reçu par email.</p></div>", unsafe_allow_html=True)
        if st.session_state.get("auth_step") == "code":
            _code_step()
        else:
            login, signup = st.tabs(["🔐 Se connecter", "✨ Créer un compte"])
            with login:
                _login_tab()
            with signup:
                _signup_tab()
    st.stop()

"""Sends one-time codes by email over SMTP (e.g. Gmail with an app password).

Without SMTP settings the app runs in *demo mode*: the code is written to the server terminal
only (never shown in the web page). Demo mode is for local testing, not for production.
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from email.message import EmailMessage

from edan_chat import config

log = logging.getLogger("edan_chat.auth")


class MailError(RuntimeError):
    pass


def smtp_configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)


def send_code(to: str, first_name: str, code: str, purpose: str) -> str:
    """Send the code; returns 'email' or 'terminal' (demo mode)."""
    action = "confirmer votre inscription" if purpose == "signup" else "vous connecter"
    if not smtp_configured():
        print(f"\n[EDAN 2025 · MODE DÉMO] Code pour {to} ({purpose}) : {code}\n", flush=True)
        return "terminal"

    msg = EmailMessage()
    msg["Subject"] = f"{code} est votre code EDAN 2025"
    msg["From"] = config.SMTP_FROM or config.SMTP_USER
    msg["To"] = to
    msg.set_content(f"Bonjour {first_name},\n\nVotre code pour {action} : {code}\n\n"
                    "Il est valable 10 minutes et ne peut servir qu'une fois.\n"
                    "Si vous n'êtes pas à l'origine de cette demande, ignorez ce message.\n\n— EDAN 2025 Chat")
    msg.add_alternative(f"""\
<div style="font-family:Segoe UI,Arial,sans-serif;max-width:480px;margin:auto;border:1px solid #F3E3CF;border-radius:16px;overflow:hidden">
  <div style="height:6px;background:linear-gradient(90deg,#F77F00 0 33%,#fff 33% 66%,#009E60 66%)"></div>
  <div style="padding:24px">
    <h2 style="margin:0 0 8px;color:#D96A00">EDAN 2025 · Chat</h2>
    <p>Bonjour {html.escape(first_name)},</p>
    <p>Votre code pour {action} :</p>
    <p style="font-size:32px;font-weight:800;letter-spacing:8px;color:#1F2937;background:#FFF4E6;
              padding:14px;text-align:center;border-radius:12px">{code}</p>
    <p style="color:#6B7280;font-size:13px">Valable 10 minutes, utilisable une seule fois.
       Si vous n'êtes pas à l'origine de cette demande, ignorez ce message.</p>
  </div>
</div>""", subtype="html")
    try:
        if config.SMTP_PORT == 465:
            with smtplib.SMTP_SSL(config.SMTP_HOST, 465, context=ssl.create_default_context(), timeout=20) as s:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        log.error("SMTP error: %s", e)
        raise MailError("L'email n'a pas pu être envoyé. Vérifiez la configuration SMTP.") from e
    return "email"

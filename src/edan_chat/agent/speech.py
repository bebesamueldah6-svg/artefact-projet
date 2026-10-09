"""Speech-to-text for voice questions: Whisper through an OpenAI-compatible endpoint (Groq by default).

Only the recorded question audio is sent to the provider; nothing is stored server-side by the app.
"""

from __future__ import annotations

import httpx

from edan_chat import config


class STTError(RuntimeError):
    pass


def available() -> bool:
    return bool(config.STT_API_KEY and config.STT_BASE_URL and config.STT_MODEL)


def transcribe(audio: bytes, filename: str = "question.wav", mime: str = "audio/wav") -> str:
    if not available():
        raise STTError("Reconnaissance vocale non configurée (GROQ_API_KEY) / speech-to-text not configured.")
    try:
        r = httpx.post(
            f"{config.STT_BASE_URL.rstrip('/')}/audio/transcriptions",
            headers={"Authorization": f"Bearer {config.STT_API_KEY}"},
            files={"file": (filename, audio, mime)},
            data={"model": config.STT_MODEL, "temperature": "0",
                  # vocabulary hint: party acronyms and place names are rare words for the model
                  "prompt": "Législatives 2025 Côte d'Ivoire : RHDP, PDCI-RDA, FPI, indépendants, "
                            "Yopougon, Abidjan, Bouaké, Korhogo, participation, sièges, circonscription."},
            timeout=60,
        )
    except httpx.HTTPError as e:
        raise STTError(f"Service de transcription injoignable : {e}") from e
    if r.status_code != 200:
        raise STTError(f"Transcription HTTP {r.status_code} : {r.text[:200]}")
    return (r.json().get("text") or "").strip()

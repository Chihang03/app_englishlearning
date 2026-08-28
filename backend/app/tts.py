from __future__ import annotations

import subprocess
from dataclasses import dataclass


DEFAULT_VOICE = "Samantha"
DEFAULT_RATE = 175


@dataclass
class Voice:
    name: str
    locale: str
    description: str


def list_english_voices() -> list[Voice]:
    try:
        result = subprocess.run(
            ["/usr/bin/say", "-v", "?"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []

    voices: list[Voice] = []
    for line in result.stdout.splitlines():
        parts = line.split("#", 1)
        left = parts[0].strip()
        description = parts[1].strip() if len(parts) > 1 else ""
        columns = left.split()
        if len(columns) < 2:
            continue
        locale = columns[-1]
        name = " ".join(columns[:-1])
        if locale.startswith("en_"):
            voices.append(Voice(name=name, locale=locale, description=description))
    return voices


def speak(text: str, voice: str = DEFAULT_VOICE, rate: int = DEFAULT_RATE) -> None:
    clean_text = " ".join(text.split())
    if not clean_text:
        return
    safe_rate = max(80, min(320, int(rate)))
    subprocess.run(
        ["/usr/bin/say", "-v", voice or DEFAULT_VOICE, "-r", str(safe_rate), clean_text],
        check=False,
        timeout=30,
    )

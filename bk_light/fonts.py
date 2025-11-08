from __future__ import annotations
from pathlib import Path
from typing import Optional

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
FONT_EXTENSIONS = {".ttf", ".otf", ".ttc"}


def normalize(name: str) -> str:
    return "".join(ch.lower() for ch in name if ch.isalnum())


def resolve_font(reference: Optional[str]) -> Optional[Path]:
    if not reference:
        return None
    candidate = Path(reference)
    if candidate.exists():
        return candidate
    if not reference.lower().endswith(tuple(FONT_EXTENSIONS)) and FONTS_DIR.exists():
        target = normalize(reference)
        for entry in FONTS_DIR.iterdir():
            if entry.suffix.lower() not in FONT_EXTENSIONS:
                continue
            if normalize(entry.stem) == target:
                return entry
    relative_candidate = ASSETS_DIR / reference
    if relative_candidate.exists():
        return relative_candidate
    return candidate


def list_available_fonts() -> list[str]:
    if not FONTS_DIR.exists():
        return []
    names: list[str] = []
    for entry in FONTS_DIR.iterdir():
        if entry.is_file() and entry.suffix.lower() in FONT_EXTENSIONS:
            names.append(entry.stem)
    return sorted(names)

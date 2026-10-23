"""Compile tokens.json into CSS custom properties."""

from __future__ import annotations

import json
from importlib import resources

GROUPS = ("color", "space", "line", "font", "chart", "motion", "layout", "waterfall")


def load_tokens() -> dict:
    return json.loads(resources.files("rewdio.dashboard").joinpath("tokens.json").read_text())


def tokens_css(tokens: dict | None = None) -> str:
    t = tokens or load_tokens()
    lines = [f"  --{g}-{k}: {v};" for g in GROUPS for k, v in t[g].items() if not k.startswith("$")]
    return "\n".join([":root {", *lines, "}"]) + "\n"

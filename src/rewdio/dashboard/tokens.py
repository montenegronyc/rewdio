"""Compile tokens.json into CSS custom properties (light, dark, and an explicit data-theme override)."""

from __future__ import annotations

import json
from importlib import resources


def load_tokens() -> dict:
    return json.loads(resources.files("rewdio.dashboard").joinpath("tokens.json").read_text())


def _vars(prefix: str, d: dict) -> list[str]:
    return [f"  --{prefix}-{k}: {v};" for k, v in d.items() if not k.startswith("$")]


def tokens_css(tokens: dict | None = None) -> str:
    t = tokens or load_tokens()
    shared = []
    for group in ("space", "radius", "font", "chart"):
        shared += _vars(group, t[group])
    light = _vars("color", t["color"]["light"])
    dark = _vars("color", t["color"]["dark"])
    return "\n".join([
        ":root {", *shared, *light, "}",
        "@media (prefers-color-scheme: dark) {", ':root:not([data-theme="light"]) {', *dark, "}", "}",
        ':root[data-theme="dark"] {', *dark, "}",
    ]) + "\n"

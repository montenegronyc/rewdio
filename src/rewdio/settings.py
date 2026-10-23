"""The user's settings file, edited through the setup wizard (web Setup tab or Claude's setup tools).

Only values that differ from defaults.json are stored, so upgrades still bring new defaults. Every
change is validated against the full config schema before anything is written.
"""

from __future__ import annotations

import copy
import json
from importlib import resources
from pathlib import Path
from typing import Any

from .config import SystemConfig, deep_merge, default_dict


def wizard() -> dict:
    return json.loads(resources.files("rewdio").joinpath("wizard.json").read_text())


def read_overrides(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _set(d: dict, dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _get(d: dict, dotted: str) -> Any:
    for k in dotted.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def minimal_diff(full: Any, base: Any) -> Any:
    """What `full` changes relative to `base` (dicts recursed; anything else replaced whole)."""
    if isinstance(full, dict) and isinstance(base, dict):
        out = {}
        for k, v in full.items():
            if k not in base:
                out[k] = v
            else:
                d = minimal_diff(v, base[k])
                if d is not _SAME:
                    out[k] = d
        return out if out else _SAME
    return _SAME if full == base else full


_SAME = object()


def expand(updates: dict) -> dict:
    """Turn {"a.b": 1, "@minidsp_model": "2x4HD"} into nested config updates."""
    out: dict = {}
    for key, value in updates.items():
        if key == "@minidsp_model":
            preset = wizard()["minidsp_models"].get(value)
            if preset is None:
                raise ValueError(f"unknown miniDSP model {value!r}; choose one of {list(wizard()['minidsp_models'])}")
            for k, v in preset.items():
                _set(out, k, v)
        else:
            _set(out, key, value)
    return out


def save(path: Path, updates: dict) -> SystemConfig:
    """Validate updates on top of the current settings and write the minimal file. Returns the new config."""
    current = read_overrides(path)
    merged = deep_merge(current, expand(updates))
    seats = merged.get("seat_xyz")
    if isinstance(seats, dict):  # clearing a seat removes it
        merged["seat_xyz"] = {k: v for k, v in seats.items() if v is not None}
    cfg = SystemConfig.model_validate(deep_merge(default_dict(), merged))  # raises on anything invalid
    diff = minimal_diff(cfg.model_dump(mode="json"), default_dict())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({} if diff is _SAME else diff, indent=2) + "\n")
    tmp.replace(path)
    return cfg


def current_model(cfg: SystemConfig) -> str | None:
    want = {"dsp_sample_rate": cfg.dsp_sample_rate,
            "tuning.crosscheck.rew_equaliser.model": cfg.tuning.crosscheck.rew_equaliser.get("model")}
    for name, preset in wizard()["minidsp_models"].items():
        if all(want.get(k) == v for k, v in preset.items()):
            return name
    return None


def steps_with_values(cfg: SystemConfig) -> list[dict]:
    """The wizard steps with each field's current value filled in."""
    data = cfg.model_dump(mode="json")
    steps = copy.deepcopy(wizard()["steps"])
    for step in steps:
        for f in step["fields"]:
            f["value"] = current_model(cfg) if f["path"] == "@minidsp_model" else _get(data, f["path"])
            if f.get("options_from"):
                f["options"] = list(wizard()[f["options_from"]])
    return steps


def missing(cfg: SystemConfig) -> list[str]:
    """What still needs the user before a good calibration (not errors: Rewdio runs without them)."""
    out = []
    if not cfg.mic_cal_path:
        out.append("mic calibration file")
    if cfg.do_sub_crawl and not cfg.room_dims_m:
        out.append("room size (needed for the crawl grid)")
    if cfg.do_sub_crawl and "MLP" not in cfg.seat_xyz:
        out.append("main seat position")
    return out


def file_check(path: str | None) -> dict:
    if not path:
        return {"ok": None, "message": "not set"}
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "message": f"no file at {p} on this computer"}
    lines = [ln for ln in p.read_text(errors="replace").splitlines() if ln.strip() and ln.strip()[0] not in "*#;\"'"]
    pairs = sum(1 for ln in lines if len(ln.replace(",", " ").split()) >= 2)
    return {"ok": pairs >= 10, "message": f"{pairs} frequency points found" if pairs >= 10
            else "this doesn't look like a frequency/dB calibration file"}

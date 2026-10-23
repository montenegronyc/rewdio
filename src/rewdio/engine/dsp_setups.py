"""Named, dated DSP setups: save what's on the miniDSP, recall it later, A/B candidates by ear.

Each setup is one JSON file, `YYYY-MM-DD_HHMM_<slug>.json`, holding the full managed DSP state plus a small
response thumbnail when a sweep was taken with exactly that state. The active setup is whichever file
matches what's on the miniDSP now, so it is never stale.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np

from ..analysis.metrics import deviation
from ..analysis.response import smooth_db
from ..analysis.target import target_db
from ..minidsp.state import DspState
from .store import Session

_ID = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4,6}_[a-z0-9-]{1,48}$")


def folder(s: Session) -> Path:
    d = s.config.setups.dir
    p = Path(d).expanduser() if d else s.dir.parent / "setups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "setup"


def _sound(state: DspState, roles: list[str]) -> str:
    """What you hear: every managed setting except mute, as a comparable string."""
    return json.dumps({r: state.get(r).model_dump(mode="json", exclude={"mute"}) for r in roles}, sort_keys=True)


def _matching_sweep(s: Session):
    """The latest full-system sweep at the main seat taken with the DSP exactly as it is now.

    "listening" sweeps are full-system sweeps taken just to document a setup; the autopilot never reuses them.
    """
    roles = [c.role for c in s.config.channels]
    now = _sound(s.dsp, roles)
    seat = s.config.mic_positions[0]
    sweeps = sorted(s.find(kind="system", position=seat) + s.find(kind="listening", position=seat), key=lambda m: m.created)
    for m in reversed(sweeps):
        if _sound(m.snapshot, roles) == now:
            return m
    return None


def _thumb(s: Session, rec) -> tuple[list[list[float]], float]:
    cfg, t = s.config, s.config.setups
    r = s.response(rec.id)
    lo, hi = t.thumb_hz
    f = lo * 2.0 ** (np.arange(int(np.log2(hi / lo) * t.thumb_ppo) + 1) / t.thumb_ppo)
    y = np.interp(np.log(f), np.log(r.freqs), smooth_db(r.freqs, r.mag_db, t.thumb_smoothing))
    b = cfg.tuning.bands
    std = deviation(r.freqs, r.mag_db, target_db(r.freqs, cfg.target), *b.bass_eval_hz, b.bass_smoothing)["std_db"]
    return [[round(float(a), 1), round(float(v), 2)] for a, v in zip(f, y)], round(float(std), 2)


def _read(p: Path) -> dict:
    d = json.loads(p.read_text())
    d["id"] = p.stem
    return d


def list_all(s: Session) -> list[dict]:
    roles = [c.role for c in s.config.channels]
    now = _sound(s.dsp, roles)
    out = []
    for p in sorted(folder(s).glob("*.json"), reverse=True):
        try:
            d = _read(p)
            d["active"] = _sound(DspState.model_validate(d["dsp"]), roles) == now
        except (ValueError, KeyError):
            continue  # not ours, or hand-edited into something unreadable
        out.append(d)
    return out


def save(s: Session, name: str, note: str | None = None, source: str = "manual") -> dict:
    name = " ".join(str(name).split())[:80]
    if not name:
        return {"ok": False, "message": "give the setup a name"}
    for c in s.config.channels:
        s.dsp.get(c.role)  # every channel explicit, even ones never touched
    now = datetime.now().astimezone()
    stem = f"{now:%Y-%m-%d_%H%M}_{_slug(name)}"
    p = folder(s) / f"{stem}.json"
    if p.exists():
        stem = f"{now:%Y-%m-%d_%H%M%S}_{_slug(name)}"
        p = folder(s) / f"{stem}.json"
    d = {"name": name, "created": now.isoformat(timespec="seconds"), "note": note, "source": source,
         "pass": s.workflow.pass_no, "dsp": s.dsp.model_dump(mode="json"), "curve": None, "bass_std_db": None}
    rec = _matching_sweep(s)
    if rec is not None:
        d["curve"], d["bass_std_db"] = _thumb(s, rec)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(p)
    return {"ok": True, "id": stem, **d}


def _path(s: Session, sid: str) -> Path | None:
    if not _ID.match(sid or ""):
        return None
    p = folder(s) / f"{sid}.json"
    return p if p.is_file() else None


def find(s: Session, ref: str) -> dict | None:
    """By id, or by name (newest wins), case-insensitive."""
    p = _path(s, ref)
    if p:
        return _read(p)
    ref = (ref or "").strip().lower()
    return next((d for d in list_all(s) if d["name"].lower() == ref), None)


async def recall(rig, ref: str) -> dict:
    d = find(rig.s, ref)
    if d is None:
        return {"ok": False, "message": f"no setup called {ref!r}"}
    state = DspState.model_validate(d["dsp"])
    missing = [c.role for c in rig.cfg.channels if c.role not in state.outputs]
    if missing:
        return {"ok": False, "message": f"setup {d['name']!r} has no settings for {', '.join(missing)}"}
    await rig.apply(state, listen=True)
    return {"ok": True, "id": d["id"], "name": d["name"]}


def delete(s: Session, sid: str) -> dict:
    p = _path(s, sid)
    if p is None:
        return {"ok": False, "message": f"no setup {sid!r}"}
    p.unlink()
    return {"ok": True}


def rename(s: Session, sid: str, name: str) -> dict:
    p = _path(s, sid)
    name = " ".join(str(name).split())[:80]
    if p is None or not name:
        return {"ok": False, "message": "unknown setup or empty name"}
    d = json.loads(p.read_text())
    d["name"] = name
    new = p.with_name(p.stem.rsplit("_", 1)[0] + f"_{_slug(name)}.json")
    if new != p and new.exists():
        return {"ok": False, "message": "a setup with that name was saved in the same minute"}
    new.write_text(json.dumps(d, indent=1))
    if new != p:
        p.unlink()
    return {"ok": True, "id": new.stem}

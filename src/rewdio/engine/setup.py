"""Setup wizard actions shared by the web Setup tab and Claude's setup tools."""

from __future__ import annotations

from .. import settings


async def status(rt) -> dict:
    cfg = rt.session.config
    return {"settings_file": str(rt.session.settings_path), "steps": settings.steps_with_values(cfg),
            "missing": settings.missing(cfg), "busy": rt.busy}


async def save(rt, updates: dict) -> dict:
    """Validate and write; then reload and reconnect so the change takes effect now."""
    if rt.busy:
        return {"ok": False, "message": "a calibration step is running; save again when it finishes"}
    try:
        settings.save(rt.session.settings_path, updates)
    except Exception as e:  # pydantic ValidationError, bad model name, unwritable file
        return {"ok": False, "message": str(e)}
    rt.session.reload_config()
    await rt.reconnect()
    return {"ok": True, **(await status(rt))}


async def check(rt, names: list[str]) -> dict:
    cfg = rt.session.config
    out: dict = {}
    for name in names:
        try:
            if name == "rew":
                out[name] = {"ok": True, "message": f"REW {await rt.rew.version()} answering at {cfg.rew_url}"}
            elif name == "minidsp":
                devs = await rt.dsp.devices()
                out[name] = ({"ok": True, "message": f"found {devs[0].get('product_name')}"} if devs
                             else {"ok": False, "message": "minidspd is running but sees no miniDSP"})
            elif name == "mic_cal":
                out[name] = settings.file_check(cfg.mic_cal_path)
            elif name == "house_curve":
                out[name] = settings.file_check(cfg.target.house_curve_path)
        except Exception as e:  # unreachable service: report, don't raise
            out[name] = {"ok": False, "message": str(e).split(". ")[0]}
    return out

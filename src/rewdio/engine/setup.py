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
    async with rt.lock:  # nothing may sweep while the clients are swapped
        try:
            settings.save(rt.session.settings_path, updates)
        except Exception as e:  # pydantic ValidationError, bad model name, unwritable file
            return {"ok": False, "message": str(e)}
        rt.session.reload_config()
        await rt.reconnect()
    return {"ok": True, **(await status(rt))}


CHECKS = ("rew", "minidsp", "mic_cal", "house_curve")


async def check(rt, names: list[str] | None) -> dict:
    """Each named check (none named: all of them)."""
    cfg = rt.session.config
    out: dict = {}
    for name in names or CHECKS:
        try:
            if name == "rew":
                out[name] = {"ok": True, "message": f"REW {await rt.rew.version()} answering at {cfg.rew_url}"}
            elif name == "minidsp":
                devs = await rt.dsp.devices()
                need = sorted(set(cfg.dsp_devices))  # every unit a channel is on, not just the first
                missing = [d for d in need if d >= len(devs)]
                found = ", ".join(f"{d}: {devs[d].get('product_name')}" for d in need if d < len(devs))
                out[name] = ({"ok": False, "message": "minidspd is running but sees no miniDSP"} if not devs else
                             {"ok": False, "message": f"found {found or 'none'}; the config also uses device(s) "
                                                      f"{missing}, which minidspd does not see"} if missing else
                             {"ok": True, "message": f"found {found}"})
            elif name == "mic_cal":
                out[name] = settings.file_check(cfg.mic_cal_path)
            elif name == "house_curve":
                out[name] = settings.file_check(cfg.target.house_curve_path)
            else:
                out[name] = {"ok": False, "message": f"unknown check {name!r}; use {', '.join(CHECKS)}"}
        except Exception as e:  # unreachable service: report, don't raise
            out[name] = {"ok": False, "message": str(e).split(". ")[0]}
    return out

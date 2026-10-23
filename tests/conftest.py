import pytest

from rewdio.engine.runtime import Runtime


async def drive(rt: Runtime, max_rounds: int = 60) -> dict:
    """Run the autopilot to completion, performing user actions in the simulator."""
    rep = {}
    for _ in range(max_rounds):
        rep = await rt.autopilot.step()
        if rep["status"] == "user_action_required":
            rt.apply_sim_effects(rep["action"], None)
            assert rt.autopilot.confirm(rep["action"]["key"])["ok"]
            continue
        return rep
    raise AssertionError("autopilot did not finish")


@pytest.fixture
def sim_runtime(tmp_path):
    rts = []

    def make(**cfg):
        rt = Runtime("t", root=str(tmp_path), simulate=True)
        for k, v in cfg.items():
            setattr(rt.session.config, k, v)
        rt.session.save()
        rts.append(rt)
        return rt

    yield make


@pytest.fixture
def cfg():
    from rewdio.config import load_config
    from pathlib import Path
    return load_config(user_file=Path("/nonexistent"))

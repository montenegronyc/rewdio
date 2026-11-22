"""No channel may play loud before it has been heard quietly (2026-11-21: a sub sweep shook the house).

The run level is set from the mains. A sub in a corner, or a sub whose own gain is up, can be far louder than the mains
at that level, and a noisier room made SNR-based staging raise the level by itself. Each test replays one of those.
"""

from conftest import drive


async def _until_ask(rt, prefix: str, rounds: int = 40):
    """Step the autopilot, doing the user's routine tasks, until it asks something starting with `prefix`."""
    for _ in range(rounds):
        rep = await rt.autopilot.step()
        if rep["status"] != "user_action_required":
            return rep
        if rep["action"]["key"].startswith(prefix):
            return rep
        rt.apply_sim_effects(rep["action"], None)
        rt.autopilot.confirm(rep["action"]["key"])
    raise AssertionError(f"never asked {prefix!r}")


def _swept_at_run_level(rt, role: str) -> list:
    """Every sweep REW actually played of `role` alone, other than quiet probes (rejected sweeps included)."""
    return [m for m in rt.fake_rew.measurements
            if str(m.get("title", "")).startswith(f"{role} @") and "probe" not in str(m.get("title", ""))]


async def test_a_hot_sub_is_caught_by_a_quiet_probe_before_its_first_real_sweep(sim_runtime):
    rt = sim_runtime()
    rt.world.speaker("SUB1").sensitivity_db += 15  # plate gain way up, or a corner: 15 dB hotter than the mains
    rep = await _until_ask(rt, "probe:SUB1")
    assert rep["status"] == "user_action_required", rep
    assert "turn" in rep["action"]["instructions"].lower() and "down" in rep["action"]["instructions"].lower()
    assert not _swept_at_run_level(rt, "SUB1")  # it never played SUB1 at the run level
    assert all(d.mute for d in rt.world.dsp.values())  # silent while the user turns it down
    rt.world.speaker("SUB1").sensitivity_db -= 15  # the user turned it down
    rt.autopilot.confirm(rep["action"]["key"])
    assert (await drive(rt))["status"] == "done"  # re-probed quietly, then measured normally


async def test_a_sub_that_stays_hot_stops_the_run(sim_runtime):
    rt = sim_runtime()
    rt.world.speaker("SUB1").sensitivity_db += 15
    rep = {}
    for _ in range(40):
        rep = await rt.autopilot.step()
        if rep["status"] != "user_action_required":
            break
        rt.apply_sim_effects(rep["action"], None)
        rt.autopilot.confirm(rep["action"]["key"])  # "done", but nothing was turned down
    assert rep["status"] == "error" and "SUB1" in rep["error"]
    assert not _swept_at_run_level(rt, "SUB1")


async def test_staging_asks_before_it_turns_anything_up(sim_runtime):
    rt = sim_runtime()
    rt.world.noise_floor_spl += 25  # a noisier room: staging wants more level for its SNR target
    start = rt.session.config.sweep.level_dbfs
    rep = await _until_ask(rt, "staging:raise")
    assert rep["status"] == "user_action_required", rep
    assert all(m.level_dbfs <= start for m in rt.session.measurements)  # nothing above the start level yet
    assert "dBFS" in rep["action"]["instructions"]


async def test_no_sweep_or_probe_ever_exceeds_the_level_cap(sim_runtime):
    rt = sim_runtime()
    cap = -30.0
    rt.session.config.sweep.max_level_dbfs = cap
    rt.world.noise_floor_spl += 25
    played = []
    for _ in range(60):
        rep = await rt.autopilot.step()
        played.append(rt.fake_rew.level_dbfs)
        if rep["status"] != "user_action_required":
            break
        rt.apply_sim_effects(rep["action"], None)
        rt.autopilot.confirm(rep["action"]["key"])
    assert max(m.level_dbfs for m in rt.session.measurements) <= cap
    assert max(played) <= cap


async def test_tonights_case_a_sub_6_db_hot_in_a_noisier_room_is_caught(sim_runtime):
    rt = sim_runtime()
    rt.world.noise_floor_spl += 10  # the evening was noisier
    rt.world.speaker("SUB1").sensitivity_db += 6  # the sub from the corner came out about 5.5 dB over the mains
    rep = await _until_ask(rt, "probe:SUB1")
    assert rep["status"] == "user_action_required", rep
    assert not _swept_at_run_level(rt, "SUB1")


def _asleep_below(world, role: str, wake_dbfs: float):
    """Auto-standby (Rythmik): the sub stays silent until the drive is loud enough to wake it."""
    spk, orig = world.speaker(role), world.response
    awake_db = spk.sensitivity_db

    def response(f, level_dbfs, rew_output=None, noise=True):
        spk.sensitivity_db = awake_db if level_dbfs >= wake_dbfs else 0.0
        try:
            return orig(f, level_dbfs, rew_output, noise)
        finally:
            spk.sensitivity_db = awake_db

    world.response = response


async def test_a_probe_that_hears_nothing_never_vouches_for_a_hot_sub(sim_runtime):
    rt = sim_runtime()
    rt.world.speaker("SUB1").sensitivity_db += 15  # hot...
    _asleep_below(rt.world, "SUB1", wake_dbfs=-46.0)  # ...and asleep at the first, quietest probe (2026-11-21)
    rep = await _until_ask(rt, "probe:SUB1")
    assert rep["status"] == "user_action_required", rep
    assert "down" in rep["action"]["instructions"].lower()
    assert not _swept_at_run_level(rt, "SUB1")
    probes = [m["_level"] for m in rt.fake_rew.measurements if "probe" in str(m.get("title", ""))]
    step = rt.session.config.tuning.loudness_guard.probe_step_db
    assert len(probes) >= 2 and max(probes) <= rt.rig.level_dbfs - step  # stepped up, never near the run level


async def test_a_sub_the_probes_never_hear_is_not_swept_at_full_level(sim_runtime):
    rt = sim_runtime()
    rt.world.speaker("SUB1").sensitivity_db = 0.0  # off, unplugged, or asleep at every probe level
    rep = {}
    for _ in range(40):
        rep = await rt.autopilot.step()
        if rep["status"] != "user_action_required":
            break
        if rep["action"]["key"].startswith("probe:SUB1"):
            assert "hear" in rep["action"]["instructions"].lower()
        rt.apply_sim_effects(rep["action"], None)
        rt.autopilot.confirm(rep["action"]["key"])
    assert rep["status"] == "error" and "SUB1" in rep["error"]
    assert not _swept_at_run_level(rt, "SUB1")

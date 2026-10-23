from conftest import drive


async def test_autopilot_sum_fit_reaches_a_verified_solution(sim_runtime):
    rt = sim_runtime()
    rep = await drive(rt)
    assert rep["status"] == "done"
    lp = rt.session.workflow.last_proposal
    m = lp["measured"]
    assert m["prediction_error_db"] < 0.5  # the model matches the room
    assert m["mains_level_spread_db"] <= 0.5
    assert m["crossover_efficiency_db"] > -1.0
    # either converged or stopped at a diagnosed physical limit, never by running out of passes blindly
    assert lp["measured_ok"] or lp.get("limit_reached")
    # what the shadow state says is what the device has
    for c in rt.session.config.channels:
        o, dev = rt.session.dsp.get(c.role), rt.world.dsp[c.dsp_output]
        assert abs(o.gain_db - dev.gain_db) < 0.01 and abs(o.delay_ms - dev.delay_ms) < 0.01
        assert o.invert == dev.invert and not dev.mute
        assert len(o.peq) == len([s for s in dev.peq if s in rt.session.config.peq_slots])


async def test_reciprocity_crawl_beats_the_corner_placement(sim_runtime):
    corner = sim_runtime()
    await drive(corner)
    crawl = sim_runtime(do_sub_crawl=True)
    rep = await drive(crawl)
    assert rep["status"] == "done"
    res = crawl.session.workflow.crawl["result"]
    assert res["best_pairs"] and res["phase_mode"] == "geometric"
    lp_c, lp_k = corner.session.workflow.last_proposal, crawl.session.workflow.last_proposal
    # placement does what EQ cannot: a clearly flatter result with no more DSP than the corner run
    assert lp_k["measured"]["bass_deviation"]["std_db"] < lp_c["measured"]["bass_deviation"]["std_db"] - 1.0
    assert lp_k["dsp_footprint"]["filters"] <= lp_c["dsp_footprint"]["filters"]
    assert lp_k["measured_ok"] or lp_k.get("limit_reached")


async def test_autopilot_two_seats_asks_for_mic_moves(sim_runtime):
    rt = sim_runtime(mic_positions=["MLP", "SEAT2"])
    asked = []
    for _ in range(60):
        rep = await rt.autopilot.step()
        if rep["status"] != "user_action_required":
            break
        asked.append(rep["action"]["key"])
        rt.apply_sim_effects(rep["action"], None)
        rt.autopilot.confirm()
    assert rep["status"] == "done"
    assert any("SEAT2" in k for k in asked)
    assert rt.session.workflow.last_proposal["measured"]["prediction_error_db"] < 0.5


async def test_loopback_mode_skips_combined_sweeps(sim_runtime):
    from rewdio.config import TimingMode
    rt = sim_runtime(timing_mode=TimingMode.LOOPBACK)
    rep = await drive(rt)
    assert rep["status"] == "done"
    assert not rt.session.find(kind="combined")
    assert rt.session.workflow.last_proposal["measured"]["prediction_error_db"] < 0.5

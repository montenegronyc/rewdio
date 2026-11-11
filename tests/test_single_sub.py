"""One sub: the crawl places it at the best single spot, and the whole calibration runs without a second sub."""

from conftest import drive


async def test_single_sub_crawl_and_calibration(sim_runtime):
    rt = sim_runtime(do_sub_crawl=True)
    cfg = rt.session.config
    cfg.channels = [c for c in cfg.channels if c.role != "SUB2"]
    rt.world.speaker("SUB2").sensitivity_db = 0.0  # unplugged: only one sub in the room
    rt.session.save()

    rep = await drive(rt)
    assert rep["status"] == "done"
    res = rt.session.workflow.crawl["result"]
    placed = rt.session.workflow.crawl["placed"]
    assert list(placed) == ["SUB1"]
    assert placed["SUB1"] == res["best_single"][0]["position"]  # not half of the best pair
    assert rt.session.workflow.last_proposal["measured"]["prediction_error_db"] < 0.5

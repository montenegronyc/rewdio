import json

from mcp import Client


async def test_tools_drive_a_full_calibration(tmp_path, monkeypatch):
    monkeypatch.setenv("REWDIO_SIMULATE", "1")
    monkeypatch.setenv("REWDIO_HOME", str(tmp_path))
    import rewdio.server as server
    server._rt = None
    async with Client(server.mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"autopilot_step", "confirm_user_action", "configure", "get_report", "measure",
                "setup_status", "save_settings", "test_setup", "set_crossover", "speaker_plan", "speaker_step",
                "speaker_confirm", "speaker_nearfield", "export_frd", "speaker_report",
                "speaker_impedance", "speaker_distortion", "rig_selftest",
                "speaker_update", "speaker_transfer", "speaker_soak", "qa_run"} <= names

        def data(res):
            assert not res.is_error, res
            return res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)

        st = data(await c.call_tool("rewdio_status", {}))
        assert st["simulated"] and "error" not in st["rew"]
        data(await c.call_tool("configure", {"updates": {"crossover_hz": 80, "target": {"bass_boost_db": 3}}}))
        for _ in range(40):
            rep = data(await c.call_tool("autopilot_step", {}))
            if rep["status"] != "user_action_required":
                break
            data(await c.call_tool("confirm_user_action", {"key": rep["action"]["key"]}))
        assert rep["status"] == "done"
        report = data(await c.call_tool("get_report", {}))
        assert report["done"] and set(report["applied_settings"]) == {"L", "R", "SUB1", "SUB2"}
        m = data(await c.call_tool("measure", {"roles": ["SUB1"]}))
        assert m["summary"]["level_db"]["sub_band"] > 40
    server._rt = None


async def test_a_refusal_reaches_claude_with_its_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("REWDIO_SIMULATE", "1")
    monkeypatch.setenv("REWDIO_HOME", str(tmp_path))
    import rewdio.server as server
    server._rt = None
    async with Client(server.mcp) as c:
        res = await c.call_tool("set_channel", {"role": "NOPE", "gain_db": -3})
        assert res.is_error and "NOPE" in res.content[0].text, res
        res = await c.call_tool("speaker_step", {})
        assert res.is_error and "speaker_plan" in res.content[0].text, res

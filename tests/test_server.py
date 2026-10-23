import json

from mcp import Client


async def test_tools_drive_a_full_calibration(tmp_path, monkeypatch):
    monkeypatch.setenv("REWDIO_SIMULATE", "1")
    monkeypatch.setenv("REWDIO_HOME", str(tmp_path))
    import rewdio.server as server
    server._rt = None
    async with Client(server.mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"autopilot_step", "confirm_user_action", "configure", "get_report", "measure"} <= names

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

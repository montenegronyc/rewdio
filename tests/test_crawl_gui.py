import time

from starlette.testclient import TestClient

from rewdio.dashboard.app import build_app
from rewdio.dashboard.remote import token_for
from rewdio.engine.grid import build_grid, route


def test_grid_codes_distances_far_walls_and_corners(cfg):
    g = cfg.crawl_grid
    cells = build_grid((5.8, 4.3, 2.6), g)
    a1 = next(c for c in cells if c["code"] == "A1")
    assert (a1["from_front_m"], a1["from_left_m"]) == (g.origin_from_front_m, g.origin_from_left_m)
    last_col = max(c["col"] for c in cells)
    last_row = max(c["row"] for c in cells)
    far = next(c for c in cells if c["row"] == last_row and c["col"] == last_col)
    assert abs(far["from_right_m"] - g.origin_from_left_m) < 0.01 and abs(far["from_back_m"] - g.origin_from_front_m) < 0.01
    r = route(cells, 8)  # heavy thinning still keeps all four corners
    corners = {(c["row"], c["col"]) for c in r}
    assert {(0, 0), (0, last_col), (last_row, 0), (last_row, last_col)} <= corners
    full = route(cells, 99)
    rows = [c["row"] for c in full]
    assert rows == sorted(rows)  # row by row...
    b = [c["col"] for c in full if c["row"] == 1]
    assert b == sorted(b, reverse=True)  # ...snaking back on odd rows
    assert all(not c["enabled"] for c in build_grid((5.8, 4.3, 2.6), g, {"A2"}) if c["code"] == "A2")


def wait_idle(client, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = client.get("/api/state").json()
        if not s["busy"]:
            return s
        time.sleep(0.05)
    raise AssertionError("still busy")


def test_phone_pairing_rules(sim_runtime):
    rt = sim_runtime(do_sub_crawl=True)
    app = build_app(lambda: rt)
    local = TestClient(app)
    assert local.get("/api/state").status_code == 200  # this computer may look
    assert local.post("/api/step").status_code == 401  # but every write needs the token
    assert token_for(rt).encode() in local.get("/").content  # its own page gets the token injected
    phone = TestClient(app, client=("192.168.1.50", 50000))
    assert phone.get("/api/state").status_code == 401  # an unpaired phone sees nothing
    assert phone.get("/api/pair/qr.svg").status_code == 401  # nor the pairing QR
    assert phone.get("/api/state", headers={"x-rewdio-token": token_for(rt)}).status_code == 200
    assert b"__rewdioToken" not in phone.get("/").content


def test_whole_crawl_driven_from_the_phone(sim_runtime):
    rt = sim_runtime(do_sub_crawl=True)
    tok = {"x-rewdio-token": token_for(rt)}
    with TestClient(build_app(lambda: rt), client=("192.168.1.50", 50000)) as phone:
        phone.headers.update(tok)
        assert phone.post("/api/step").json()["running"]
        seen_cells, skipped = [], False
        for _ in range(80):
            s = wait_idle(phone)
            if s["done"]:
                break
            a = s["pending_action"]
            crawl = s["crawl"]
            if a and a["key"].startswith("crawl:mic:"):
                assert crawl["current"] == a["cell"] and crawl["progress"]["total"] >= 20
                if not skipped:  # sofa in the way: skip the first cell from the phone
                    skipped = True
                    assert phone.post("/api/crawl/cell", json={"code": a["cell"], "enabled": False}).json()["ok"]
                    continue
                seen_cells.append(a["cell"])
            assert phone.post("/api/confirm", json={"key": a["key"]}).json()["ok"]
        assert s["done"]
        measured = rt.session.workflow.crawl["measured"]
        assert len(measured) == len(seen_cells) and "A1" not in measured  # A1 was skipped
        assert rt.session.workflow.crawl["placed"]  # the placement step recorded where the subs went

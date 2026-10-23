# Polycam / RoomPlan scans for Rewdio: worth it?

## Recommendation: partly worth it. Use the scan for geometry input, not for acoustic simulation.

Import a scan to fill in `room_dims_m`, `seat_xyz`, speaker and sub coordinates, and a list of candidate sub spots that avoids furniture. Then pre-screen those spots with the existing rectangular modal model (`sim/world.py`) before asking the user to move the mic. Do not build FEM/BEM or image-source simulation on top of the scan. Rewdio's solvers use measured responses, and measurements will always beat a geometric prediction at the seat.

Effort: about 2-3 days for the basic version, plus about 1 day to pre-screen candidate spots.

## 1. Positioning (the main benefit)

**What it replaces.** Today the user types `room_dims_m` and `seat_xyz` by hand (`config.py:113-116`). `_crawl_spots` (`engine/workflow.py:168`) then builds a fixed ring of spots 0.35 m from the walls of a rectangle. That ring ignores doors, sofas, cabinets and room shapes that are not rectangular. A scan fixes all of this:

- **Room dimensions and origin.** Take them from the wall planes. Put the origin at the front-left floor corner (already Rewdio's convention).
- **Candidate sub spots.** Sample along the walls and corners. Drop any spot that falls inside a furniture or door bounding box, or in front of a door or opening.
- **Seat.** Use the sofa or chair object's centroid plus an ear height of about 1.1 m above the floor.
- **Speakers.** RoomPlan has no "speaker" category, so either the user tags speakers, or we pick the nearest object, such as a "storage" or "television" box.

**Accuracy.** Expect 1-3 cm on room dimensions. That is well below what matters for the bass band. The shortest wavelength of interest is about 2.9 m at 120 Hz, so a 3 cm error is about 1 % of a wavelength, or roughly 0.09 ms of geometric delay.

- Apple publishes no official RoomPlan accuracy figure.
- Developers report errors of up to about 3 cm, and sometimes up to about 15 cm ([Apple forum 712500](https://developer.apple.com/forums/thread/712500), [RoomPlan tag](https://developer.apple.com/forums/tags/room-plan/)).
- A review of an app built on LiDAR found about 1-3 cm indoors ([RoomScan Pro review](https://illustrarch.com/articles/design-softwares/110334-roomscan-pro-review.html)).
- A study comparing iPhone and iPad LiDAR with a tape measure reported high reliability ([study](https://radiopharmaconnect.srsweb.org/users/489992/articles/1253435-an-investigation-into-the-accuracy-of-lidar-technology-for-in-home-rehabilitation-planning-a-proof-of-concept-study)).
- RoomPlan can miss small walls or bulkheads.

Geometry from a scan is therefore more than good enough for Rewdio's `geometric` phase mode (`placement.py:30`). It is likely better than what users type in today.

**What Polycam exports, by tier.**

- **Free:** glTF only.
- **Basic:** about 12 formats, including OBJ, FBX, DAE, USDZ, STL and PLY.
- **Business and Enterprise:** everything, including point-cloud formats.
- **Floor-plan mode:** exports PDF, PNG, DXF, SVG and a CSV of room-by-room measurements. Sources disagree on whether floor-plan DXF needs Business ([Polycam export formats](https://learn.poly.cam/hc/en-us/articles/27756102599572), [Basic plan](https://poly.cam/pricing/basic-plan), [Floor plans](https://poly.cam/features/floorplan), [review](https://www.skyebrowse.com/news/posts/polycam-review.md)).
- I found no evidence that Polycam exports Apple's raw `CapturedRoom` JSON. Polycam's mesh exports are unlabelled triangle meshes.

**RoomPlan's semantic model.**

- `CapturedRoom` holds **surfaces** (wall, opening, window, door open/closed, plus floors in iOS 17) and **objects** (sofa, table, bed, storage, television and so on). Each has `dimensions`, a bounding box in metres, and a 4x4 `transform` ([WWDC23 10192](https://developer.apple.com/videos/play/wwdc2023/10192/), [CapturedRoom docs](https://developer.apple.com/documentation/roomplan/capturedroom)).
- `CapturedRoom` is Encodable to JSON, but re-importing the JSON has had bugs ([forum 715386](https://developer.apple.com/forums/thread/715386)).
- RoomPlan's own export is USD/USDZ only. Its parametric export writes walls and objects as named boxes ([forum 708791](https://developer.apple.com/forums/thread/708791)). Some exports omit the floor ([forum 727808](https://developer.apple.com/forums/thread/727808)).
- Parsing in Python is easy:
  - `usd-core` (pxr) opens USDZ and lets us walk the prims. Category comes from the prim name or path, such as `Wall0` or `Sofa1`; this needs checking against a real file.
  - JSON needs only the standard library.
  - `trimesh` loads GLB, OBJ or PLY meshes for the fallback that fits a bounding box.
  - `ezdxf` reads DXF floor plans.

**Practical input order, best first:**

1. A RoomPlan JSON or parametric USDZ, from any app that uses RoomPlan.
2. A Polycam DXF or CSV floor plan, which gives walls and dimensions only.
3. A Polycam GLB or OBJ mesh. Fit floor, ceiling and wall planes from it (oriented bounding box or RANSAC). This gives no furniture semantics.

## 2. Simulation: where geometry does and doesn't help

**Schroeder frequency.** For Rewdio's default room (64.8 m³, RT60 0.45 s), f_s = 2000·√(T/V) ≈ 167 Hz. The crawl band is 20-120 Hz, so the whole band is modal. Ray and image-source methods are poor models there.

| Option | Verdict |
|---|---|
| Rectangular modal sum on the scan's bounding box (current `Room`) | **Keep.** Good enough to rank candidate spots coarsely in rooms that are roughly rectangular. With real dimensions, the predicted axial and tangential mode frequencies become meaningful. |
| Image-source method, pyroomacoustics (supports arbitrary polyhedral rooms; [paper](https://arxiv.org/pdf/1710.04196)) | **No.** It is geometric and has no diffraction. Its visibility tests for re-entrant walls break the modal picture, and it is expensive for long low-frequency impulse responses. It is only near-exact for rigid rectangular rooms, where our modal sum already works. |
| FEM/BEM (FEniCSx, scikit-fem, bempp) for eigenmodes of the actual shape | **Not now.** It is technically right for L-shaped or open-plan rooms. Below 120 Hz a coarse tetrahedral mesh is enough (about 6 elements per wavelength, i.e. elements of about 0.5 m), so a Helmholtz eigen-solve with scikit-fem is feasible. But wall impedance and leakage are unknown, and real rooms have flexible drywall, open doorways and furniture. Predicted null depths and positions will be off by more than the decision margin between spots. Heavy dependencies, research-grade effort. |
| Absorption from surface labels | **Low value.** RoomPlan's labels carry no material information, and low-frequency loss is dominated by flexing walls and openings, not by surface absorbers. Better to fit one modal damping value from the RT60 or decay that Rewdio already measures. |

**Where measurements dominate.** Every EQ, delay and gain decision Rewdio makes comes from REW measurements. Even a perfect geometric model cannot predict SPL at the seat within ±3 dB below 120 Hz in a real room. So geometry helps in four places:

1. Generating candidate spots.
2. The `geometric` phase mode of the crawl, which gets better coordinates.
3. Pre-screening: rank N spots in the model, then ask the user to mic only the top K. This saves user effort, but it must never drop a spot that was measured.
4. Predicting variance between seats, to warn when a layout cannot be made even across seats.

Model predictions should only order and prioritise. They must not replace the crawl measurements.

## 3. Integration design

**New modules, each under 500 lines:**

- `src/rewdio/geometry/scan_import.py` (~300 lines)
  - `load_scan(path) -> RoomGeometry`. Detects the format: RoomPlan JSON, USDZ/USD via `pxr`, DXF via `ezdxf`, GLB/OBJ/PLY via `trimesh` plane fitting.
  - Normalises to Rewdio's frame: origin at the front-left floor corner, x running front to back, z up. The "front" wall is chosen by the user, or taken as the wall the TV is against.
- `src/rewdio/geometry/model.py` (~150 lines)
  - Dataclasses `RoomGeometry(bbox_dims, walls[], openings[], objects[], floor_polygon, rectangularity)`.
  - `free_floor_mask()`, which marks the floor that is not under furniture.
- `src/rewdio/geometry/candidates.py` (~150 lines)
  - `scan_crawl_spots(geom, n, wall_margin=0.35)`. Builds spots along walls and corners, removes obstructed ones, and spaces them by the minimum separation.
  - `prescreen(geom, seats, spots, top_k)`. Uses `sim.world.Room(bbox_dims, rt60)`.
- `workflow._crawl_spots`: use the spots stored by the scan import when they are present, otherwise keep the current ring.

**Config (JSON) additions to `SystemConfig`:**

- `room_scan_path`
- `room_scan_front_wall` (`"auto"` or a wall index)
- `speaker_xyz: {role: [x,y,z]}`
- `prescreen_top_k` (default `null`, which means off)
- `ear_height_m` (default 1.1)

`room_dims_m` and `seat_xyz` stay authoritative. The import only proposes values, and the user confirms them.

**MCP tools:**

- `import_room_scan(path, front_wall="auto")`. Returns dimensions, a rectangularity score (floor-polygon area ÷ bounding-box area), detected objects, proposed seat, speaker and candidate spots, and warnings (for example "L-shaped room: rectangular model unreliable", or "no floor found").
- `apply_room_scan(accept: {dims, seats, speakers, spots})`. Writes the accepted values to the config.
- `prescreen_sub_spots(top_k)` (optional).

**Dependencies:** an optional extra, `rewdio[scan]`, containing `trimesh`, `usd-core` and `ezdxf`. `usd-core` is large (about 100 MB+), so it must stay optional. The JSON path needs only the standard library.

**Effort:**

- JSON/USDZ/mesh import with tests: about 2 days.
- Candidate generation and wiring into the workflow: about 1 day.
- Pre-screening and a seat-variance warning: about 1 day.
- FEM is excluded; it would be weeks.

**Risks:**

- Polycam does not export RoomPlan semantics. Mesh-only input means a heuristic plane fit and no furniture.
- Polycam's tier gating changes, and floor-plan DXF may need Business.
- RoomPlan USD prim naming and its JSON schema change between iOS versions.
- The coordinate-frame convention is ambiguous (which wall is "front").
- Users may over-trust model predictions in non-rectangular or open-plan rooms. Mitigate by gating on the rectangularity score and always measuring.
- Seat height, and the real height of each sub's woofer, still need confirming by the user.

## Sources
- https://learn.poly.cam/hc/en-us/articles/27756102599572
- https://poly.cam/pricing/basic-plan
- https://poly.cam/features/floorplan
- https://poly.cam/blog/obj-vs-fbx-vs-glb-which-3d-scan-format-should-you-export
- https://www.skyebrowse.com/news/posts/polycam-review.md
- https://developer.apple.com/documentation/roomplan/capturedroom
- https://developer.apple.com/videos/play/wwdc2023/10192/
- https://developer.apple.com/forums/thread/712500
- https://developer.apple.com/forums/thread/715386
- https://developer.apple.com/forums/thread/708791
- https://developer.apple.com/forums/thread/727808
- https://illustrarch.com/articles/design-softwares/110334-roomscan-pro-review.html
- https://radiopharmaconnect.srsweb.org/users/489992/articles/1253435-an-investigation-into-the-accuracy-of-lidar-technology-for-in-home-rehabilitation-planning-a-proof-of-concept-study
- https://arxiv.org/pdf/1710.04196

Note: learn.poly.cam could not be fetched directly (DNS blocked), so the Polycam tier claims come from search-result snippets. Before building, check the current tier gating and whether Polycam can export RoomPlan USDZ, using a real export.

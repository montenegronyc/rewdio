"""In-process fakes of the REW REST API and the minidsp-rs HTTP API, backed by a World.

They plug into the real clients through httpx.MockTransport, so the wire code is
exercised exactly as it would be against the real programs.
"""

from __future__ import annotations

import json
import re
import uuid as uuidlib

import httpx
import numpy as np

from ..minidsp.biquad import Biquad
from ..rew.codec import encode_floats
from .world import P_REF, World

IR_FS = 12000.0
SWEEP_LENGTHS = {"128k", "256k", "512k", "1M", "2M", "4M"}


class FakeRew:
    def __init__(self, world: World):
        self.world = world
        self.level_dbfs = -12.0
        self.sweep = {"startFrequency": 10, "endFrequency": 20000, "length": "256k"}
        self.timing = "None"
        self.output_channel = "L+R"
        self.cal = {"calDataAllInputs": {"calFilePath": ""}}
        self.protection = {"clippingAbort": False, "splLimitAbort": False, "dBSPLLimit": 110.0}
        self.next_title = "meas"
        self.next_notes = ""
        self.measurements: list[dict] = []  # each: summary + _h (complex), _f
        self.last_error = ""
        self.sweeps_run = 0
        self.start_delay = 0.0
        self.align = {"mode": "Phase", "index-a": 1, "index-b": 2, "max-positive-delay": 10.0,
                      "max-negative-delay": 10.0, "delay-b": None, "invert-b": False}
        self.eq_filters: dict[str, list[dict]] = {}
        self.dither = False
        self.generator = {"signal": "pinknoise", "level": -20.0, "playing": False}
        self.loopback_connected = True  # tests unplug it
        self.fail_measures = 0  # tests: fail this many sweeps (fault injection)

    # -- helpers -----------------------------------------------------------------

    def _ok(self, body=None, status=200):
        if body is None:
            body = {"message": "OK"}
        return httpx.Response(status, content=json.dumps(body).encode(), headers={"content-type": "application/json"})

    def _err(self, msg, status=400, valid=None):
        body = {"message": msg}
        if valid:
            body["validValues"] = valid
        return httpx.Response(status, content=json.dumps(body).encode())

    def _find(self, ident: str) -> dict | None:
        if ident.isdigit():
            i = int(ident) - 1
            return self.measurements[i] if 0 <= i < len(self.measurements) else None
        return next((m for m in self.measurements if m["uuid"] == ident), None)

    def _measure(self):
        w = self.world
        sens = (self.cal.get("calDataAllInputs") or {}).get("calFilePath")
        ppo = 96
        f0, f1 = float(self.sweep["startFrequency"]), float(self.sweep["endFrequency"])
        n = int(np.floor(np.log2(f1 / f0) * ppo)) + 1
        f = f0 * 2.0 ** (np.arange(n) / ppo)
        h = w.response(f, self.level_dbfs, self.output_channel) / P_REF
        if not sens:
            # no mic cal: a fake rising error a real uncalibrated mic might show
            h = h * 10 ** ((np.log2(np.maximum(f, 20) / 1000) * 0.3) / 20)
        peak_spl = float(np.max(20 * np.log10(np.abs(h) + 1e-12)))
        if self.protection.get("splLimitAbort") and peak_spl > self.protection.get("dBSPLLimit", 200):
            self.last_error = f"Measurement aborted: SPL {peak_spl:.1f} dB exceeded limit"
            return self._err(self.last_error)
        if peak_spl > w.clip_spl:
            self.last_error = "Measurement aborted: input clipping"
            return self._err(self.last_error)
        t_ref = None
        if self.timing.lower().startswith("none"):
            # REW without a timing reference puts the IR peak at t=0
            fl = np.arange(1.0, 4000.0, 1.0)
            hl = w.response(fl, self.level_dbfs, self.output_channel, noise=False)
            spec = np.concatenate([[0], hl])
            ir = np.fft.irfft(spec, n=8000)
            t_peak = np.argmax(np.abs(ir)) / 8000.0
            h = h * np.exp(2j * np.pi * f * t_peak)
        else:
            t_ref = self.timing
        self.sweeps_run += 1
        noise_spl = w.noise_floor_spl
        db = 20 * np.log10(np.abs(h) + 1e-12)
        live = db > db.max() - 20  # SNR over the band the speaker actually reproduces, as REW reports it
        snr = float(np.mean(db[live]) - noise_spl)
        fl = np.arange(1.0, IR_FS / 2)  # impulse response for REW's /impulse-response, band-limited for speed
        ir = np.fft.irfft(np.concatenate([[0], w.response(fl, self.level_dbfs, self.output_channel, noise=False)
                                          / P_REF]), n=int(IR_FS))
        m = {
            "_ir": ir,
            "uuid": str(uuidlib.uuid4()), "title": self.next_title, "notes": self.next_notes,
            "startFreq": f0, "endFreq": f1, "timingReference": t_ref or "None",
            "signalToNoisedB": round(snr, 1), "_f": f, "_h": h, "_level": self.level_dbfs,
        }
        self.measurements.append(m)
        return self._ok({"message": "Measurement started"}, status=202)

    def _impedance(self):
        """A driver on an impedance jig: Re + voice-coil inductance + the motional resonance."""
        out = next((o for o, d in sorted(self.world.dsp.items()) if not d.mute), 0)
        spk = next((s for s in self.world.speakers if s.dsp_output == out), None)
        fs_res = float(spk.model[0][1]) if spk and spk.model and spk.model[0][0] == "HP" else 50.0
        re_, le, rms, q = 6.0, 0.05e-3, 30.0, 4.0

        def z(f):
            f = np.asarray(f, float)
            return re_ + 2j * np.pi * f * le + rms / (1 + 1j * q * (f / fs_res - fs_res / f))
        m = {"uuid": str(uuidlib.uuid4()), "title": self.next_title, "notes": self.next_notes,
             "startFreq": 10.0, "endFreq": 20000.0, "timingReference": "None", "_z": z, "_fs_res": fs_res}
        self.measurements.append(m)
        return self._ok({"message": "Measurement started"}, status=202)

    def _distortion(self, m: dict, ppo: int) -> dict:
        """Harmonics that rise with level and towards the bottom of the band, like a real driver."""
        f0 = max(m["startFreq"], 20.0)
        f = f0 * 2.0 ** (np.arange(int(np.log2(m["endFreq"] / f0) * ppo) + 1) / ppo)
        fund = np.interp(np.log(f), np.log(m["_f"]), 20 * np.log10(np.abs(m["_h"]) + 1e-12))
        drive = 10 ** ((m.get("_level", -20.0) + 20.0) / 20)  # 1.0 at -20 dBFS
        low = 1 + (np.maximum(f0, 200.0) * 2 / f) ** 2
        h2, h3 = 0.3 * drive * low, 0.1 * drive ** 2 * low
        thd = np.sqrt(h2 ** 2 + h3 ** 2)
        rows = [[float(a), float(b), float(c), 0.01, float(d), float(e)] for a, b, c, d, e in zip(f, fund, thd, h2, h3)]
        rows[-1] = rows[-1][:5]  # REW's rows can be ragged where a harmonic is above the band
        return {"columnHeaders": ["Freq (Hz)", "Fundamental (dB)", "THD (%)", "Noise (%)", "H2 (%)", "H3 (%)"],
                "data": rows}

    def _summary(self, m: dict) -> dict:
        return {k: v for k, v in m.items() if not k.startswith("_")}

    # -- router -------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        meth = request.method
        body = json.loads(request.content) if request.content else None
        if p == "/version":
            return self._ok({"message": "5.40 (simulated)"})
        if p == "/application/blocking":
            return self._ok()
        if p == "/application/last-error":
            return self._ok({"message": self.last_error})
        if p in ("/application/last-warning", "/application/errors", "/application/warnings"):
            return self._ok({"message": ""})
        if p == "/application/command":
            return self._ok()
        if p == "/measure/start-delay":
            if meth == "POST":
                self.start_delay = float(body)
                return self._ok()
            return self._ok(self.start_delay)
        if p == "/input-levels/command":
            return self._ok()
        if p == "/input-levels/last-levels":
            floor = self.world.noise_floor_spl - 120.0  # ~ -90 dBFS: a live mic hears room noise
            mic = floor
            if self.generator["playing"] and any(not o.mute for o in self.world.dsp.values()):
                mic = max(floor, self.generator["level"] - 30.0)  # speakers playing into the room
            ref = self.generator["level"] - 3.0 if (self.generator["playing"] and self.loopback_connected) else floor
            return self._ok({"unit": "dBFS", "rms": [mic, ref], "peak": [mic + 10, ref + 3], "timeSpanSeconds": 1.0})
        if p == "/measure/fill-silence-with-dither":
            if meth == "POST":
                self.dither = bool(body)
                return self._ok()
            return self._ok(self.dither)
        if p == "/generator/signal":
            self.generator["signal"] = body["signal"]
            return self._ok()
        if p == "/generator/level":
            self.generator["level"] = float(body["value"])
            return self._ok()
        if p == "/generator/command":
            self.generator["playing"] = body["command"] == "Play"
            return self._ok()
        if p.startswith("/alignment-tool/"):
            return self._alignment(p.split("/", 2)[2], meth, body)
        mm = re.fullmatch(r"/measurements/([^/]+)/(equaliser|filters|eq/frequency-response)", p)
        if mm:
            return self._eq(mm.group(1), mm.group(2), meth, body, request)
        if p == "/audio/driver":
            return self._ok({"driver": "Java"})
        if p == "/audio/java/output-channel":
            if meth == "POST":
                self.output_channel = body["channel"]
                return self._ok()
            return self._ok({"channel": self.output_channel})
        if p == "/audio/input-cal":
            if meth == "PUT":
                self.cal = body
                return self._ok()
            return self._ok(self.cal)
        if p == "/audio/samplerate":
            return self._ok({"value": 48000, "unit": "Hz"})
        if p == "/measure/sweep/configuration":
            if meth in ("POST", "PUT"):
                if body.get("length") and body["length"] not in SWEEP_LENGTHS:
                    return self._err("bad length", valid=sorted(SWEEP_LENGTHS))
                self.sweep.update(body)
                return self._ok()
            return self._ok(self.sweep)
        if p == "/measure/level":
            if meth == "POST":
                if body["value"] > 0:
                    return self._err("level must be <= 0 dBFS")
                self.level_dbfs = float(body["value"])
                return self._ok()
            return self._ok({"value": self.level_dbfs, "unit": "dBFS"})
        if p == "/measure/timing/reference/choices":
            return self._ok(["None", "Acoustic", "Loopback"])
        if p == "/measure/timing/reference":
            if meth == "POST":
                if body not in ("None", "Acoustic", "Loopback"):
                    return self._err("bad timing reference", valid=["None", "Acoustic", "Loopback"])
                self.timing = body
                return self._ok()
            return self._ok(self.timing)
        if p == "/measure/protection-options":
            if meth == "POST":
                self.protection = body
                return self._ok()
            return self._ok(self.protection)
        if p == "/measure/naming":
            self.next_title = body.get("title", self.next_title)
            return self._ok()
        if p == "/measure/notes":
            self.next_notes = body if isinstance(body, str) else ""
            return self._ok()
        if p == "/measure/command":
            if body.get("command") == "Impedance":
                return self._impedance()
            if body.get("command") != "SPL":
                return self._err("unsupported in simulator", valid=["SPL", "Impedance"])
            if self.fail_measures > 0:
                self.fail_measures -= 1
                return self._err("simulated sweep failure", status=500)
            return self._measure()
        if p == "/measurements":
            if meth == "DELETE":
                self.measurements.clear()
                return self._ok()
            return self._ok({str(i + 1): self._summary(m) for i, m in enumerate(self.measurements)})
        mm = re.fullmatch(r"/measurements/([^/]+)/distortion", p)
        if mm:
            m = self._find(mm.group(1))
            if m is None or m.get("_z") is not None:
                return self._err("no distortion data", status=404)
            return self._ok(self._distortion(m, int(request.url.params.get("ppo", 12))))
        mm = re.fullmatch(r"/measurements/([^/]+)/impulse-response", p)
        if mm:
            m = self._find(mm.group(1))
            if m is None:
                return self._err("no such measurement", status=404)
            return self._ok({"startTime": 0.0, "sampleRate": IR_FS, "data": encode_floats(m["_ir"])})
        mm = re.fullmatch(r"/measurements/([^/]+)(/frequency-response)?", p)
        if mm:
            m = self._find(mm.group(1))
            if m is None:
                return self._err("no such measurement", status=404)
            if mm.group(2) and m.get("_z") is not None:  # impedance: magnitude in ohms
                ppo = int(request.url.params.get("ppo", 96))
                f = 10.0 * 2.0 ** (np.arange(int(np.log2(20000 / 10.0) * ppo) + 1) / ppo)
                z = m["_z"](f)
                return self._ok({"unit": "ohm", "smoothing": "None", "startFreq": 10.0, "ppo": ppo,
                                 "magnitude": encode_floats(np.abs(z)), "phase": encode_floats(np.rad2deg(np.angle(z)))})
            if mm.group(2):
                ppo = int(request.url.params.get("ppo", 96))
                f0 = m["startFreq"]
                n = int(np.floor(np.log2(m["endFreq"] / f0) * ppo)) + 1
                f = f0 * 2.0 ** (np.arange(n) / ppo)
                lf, lg = np.log(m["_f"]), np.log(f)
                mag = np.interp(lg, lf, 20 * np.log10(np.abs(m["_h"]) + 1e-12))
                ph = np.interp(lg, lf, np.unwrap(np.angle(m["_h"])))
                ph = (np.rad2deg(ph) + 180) % 360 - 180  # REW reports wrapped phase
                return self._ok({"unit": "SPL", "smoothing": "None", "startFreq": f0, "ppo": ppo,
                                 "magnitude": encode_floats(mag), "phase": encode_floats(ph)})
            if meth == "DELETE":
                self.measurements.remove(m)
                return self._ok()
            if meth == "PUT":
                m.update({k: v for k, v in body.items() if k in ("title", "notes")})
                return self._ok()
            return self._ok(self._summary(m))
        return self._err(f"simulator does not implement {meth} {p}", status=404)


    def _alignment(self, key: str, meth: str, body):
        if key == "command":
            a, b = self.measurements[self.align["index-a"] - 1], self.measurements[self.align["index-b"] - 1]
            f = float(body["frequency"])
            ha = np.interp(np.log(f), np.log(a["_f"]), np.unwrap(np.angle(a["_h"])))
            hb = np.interp(np.log(f), np.log(b["_f"]), np.unwrap(np.angle(b["_h"])))
            # phase-match B to A at f with the smallest delay in range (what REW's "Align phase" does)
            period = 1000.0 / f
            d0 = ((hb - ha) / (2 * np.pi * f)) * 1000.0
            cands = [d0 + k * period for k in range(-20, 21)]
            ok = [d for d in cands if -self.align["max-negative-delay"] <= d <= self.align["max-positive-delay"]]
            self.align["delay-b"] = min(ok, key=abs) if ok else None
            return self._ok()
        if meth == "POST":
            self.align[key] = body
            return self._ok()
        return self._ok({"value": self.align.get(key)})

    def _eq(self, ident: str, what: str, meth: str, body, request):
        from ..minidsp.biquad import design
        m = self._find(ident)
        if m is None:
            return self._err("no such measurement", status=404)
        if what == "equaliser":
            return self._ok()
        if what == "filters":
            if meth == "POST":
                self.eq_filters[m["uuid"]] = body["filters"]
                return self._ok()
            return self._ok(self.eq_filters.get(m["uuid"], []))
        ppo = int(request.url.params.get("ppo", 96))
        f0 = m["startFreq"]
        n = int(np.floor(np.log2(m["endFreq"] / f0) * ppo)) + 1
        f = f0 * 2.0 ** (np.arange(n) / ppo)
        h = np.interp(np.log(f), np.log(m["_f"]), 20 * np.log10(np.abs(m["_h"]) + 1e-12))
        for flt in self.eq_filters.get(m["uuid"], []):
            bq = design("PK", flt["frequency"], 48000, flt["gaindB"], flt["q"])  # REW evaluates at its own rate
            h = h + 20 * np.log10(np.abs(bq.response(f, 48000)))
        return self._ok({"unit": "SPL", "startFreq": f0, "ppo": ppo, "magnitude": encode_floats(h)})


class FakeMiniDSP:
    def __init__(self, world: World):
        self.world = world
        self.writes: list[dict] = []
        self.fail_writes = 0  # tests: fail this many config writes (fault injection)

    def handle(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/devices":
            return httpx.Response(200, json=[{"product_name": "2x4HD (simulated)", "url": "sim://",
                                              "version": {"hw_id": 10, "dsp_version": 100, "serial": 1}}])
        if p == "/devices/0" and request.method == "GET":
            return httpx.Response(200, json={"master": {"preset": 0, "source": "Usb", "volume": self.world.master_volume_db,
                                                        "mute": False, "dirac": False},
                                             "input_levels": [-120.0, -120.0], "output_levels": [-120.0] * 4})
        if p == "/devices/0/config" and request.method == "POST":
            if self.fail_writes > 0:
                self.fail_writes -= 1
                return httpx.Response(500, json={"error": "simulated write failure"})
            cfg = json.loads(request.content)
            self.writes.append(cfg)
            for o in cfg.get("outputs", []):
                out = self.world.dsp.get(o["index"])
                if out is None:
                    return httpx.Response(500, json={"error": f"no output {o['index']}"})
                if o.get("gain") is not None:
                    out.gain_db = float(o["gain"])
                if o.get("mute") is not None:
                    out.mute = bool(o["mute"])
                if o.get("invert") is not None:
                    out.invert = bool(o["invert"])
                if o.get("delay") is not None:
                    out.delay_ms = o["delay"]["secs"] * 1000 + o["delay"]["nanos"] / 1e6
                for q in o.get("peq", []):
                    if q.get("bypass"):
                        out.peq.pop(q["index"], None)
                    elif q.get("coeff"):
                        out.peq[q["index"]] = Biquad.from_minidsp(q["coeff"])
                for g in o.get("crossover", []):  # a write replaces the scenario's own crossover
                    for c in g.get("coeff", []):
                        out.xslots[(g["index"], c["index"])] = Biquad.from_minidsp(c)
                    if g.get("bypass") is not None:
                        out.xbypass[g["index"]] = bool(g["bypass"])
                    out.xover = [bq for (gi, _), bq in sorted(out.xslots.items())
                                 if not out.xbypass.get(gi, False) and bq != Biquad.identity()]  # unity pads
            ms = cfg.get("master_status") or {}
            if ms.get("volume") is not None:
                self.world.master_volume_db = float(ms["volume"])
            return httpx.Response(200, text="")
        return httpx.Response(404, json={"error": f"not found: {p}"})


def transports(world: World) -> tuple[httpx.MockTransport, httpx.MockTransport, FakeRew, FakeMiniDSP]:
    rew, dsp = FakeRew(world), FakeMiniDSP(world)
    return httpx.MockTransport(rew.handle), httpx.MockTransport(dsp.handle), rew, dsp

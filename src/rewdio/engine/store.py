"""Persistent session: config, DSP shadow state, workflow state, measurements."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..analysis.response import Response
from ..config import SystemConfig
from ..minidsp.state import DspState


class MeasurementRecord(BaseModel):
    id: str  # REW uuid
    title: str
    kind: str  # staging | solo | combined | system | crawl
    roles: list[str]  # channels that were playing
    position: str
    pass_no: int = 0
    snapshot: DspState
    level_dbfs: float
    timing: str
    extra: dict[str, Any] = Field(default_factory=dict)
    created: float = Field(default_factory=time.time)


class WorkflowState(BaseModel):
    phase: str = "PREFLIGHT"
    pass_no: int = 0
    pending_action: dict | None = None
    mic_at: str | None = None  # position label where the mic currently is (as far as we know)
    crawl: dict = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list)
    last_proposal: dict | None = None
    best: dict | None = None
    done: bool = False


class Session:
    def __init__(self, root: Path | str | None = None, name: str = "default"):
        base = Path(root or os.environ.get("REWDIO_HOME", Path.home() / ".rewdio"))
        self.dir = base / name
        (self.dir / "data").mkdir(parents=True, exist_ok=True)
        self.config = self._load("config.json", SystemConfig)
        self.dsp = self._load("dsp.json", DspState)
        self.workflow = self._load("workflow.json", WorkflowState)
        self.measurements: list[MeasurementRecord] = [
            MeasurementRecord.model_validate(m) for m in self._read("measurements.json", [])]

    def _read(self, name: str, default):
        p = self.dir / name
        return json.loads(p.read_text()) if p.exists() else default

    def _load(self, name: str, model):
        raw = self._read(name, None)
        return model.model_validate(raw) if raw is not None else model()

    def _write(self, name: str, obj) -> None:
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1))
        tmp.replace(self.dir / name)

    def save(self) -> None:
        self._write("config.json", self.config.model_dump(mode="json"))
        self._write("dsp.json", self.dsp.model_dump(mode="json"))
        self._write("workflow.json", self.workflow.model_dump(mode="json"))
        self._write("measurements.json", [m.model_dump(mode="json") for m in self.measurements])

    def add_measurement(self, rec: MeasurementRecord, resp: Response) -> None:
        self._write(f"data/{rec.id}.json", resp.to_dict())
        self.measurements.append(rec)
        self.save()

    def response(self, mid: str) -> Response:
        return Response.from_dict(json.loads((self.dir / "data" / f"{mid}.json").read_text()))

    def find(self, **match) -> list[MeasurementRecord]:
        out = []
        for m in self.measurements:
            if all((getattr(m, k) if hasattr(m, k) else m.extra.get(k)) == v for k, v in match.items()):
                out.append(m)
        return out

    def reset_workflow(self) -> None:
        self.workflow = WorkflowState()
        self.save()

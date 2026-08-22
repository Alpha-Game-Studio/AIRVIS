from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import threading
import uuid
import json


@dataclass
class ScheduledJob:
    prompt: str
    run_at: datetime
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancelled: bool = False


class Scheduler:
    def __init__(self, path=None) -> None:
        self.path = path or __import__("pathlib").Path.home() / ".airvis" / "jobs.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, ScheduledJob] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._load()

    def once(self, prompt: str, delay_seconds: float, callback) -> ScheduledJob:
        job = ScheduledJob(prompt, datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
        self.jobs[job.id] = job
        timer = threading.Timer(max(0, delay_seconds), self._run, args=(job.id, callback))
        timer.daemon = True
        self._timers[job.id] = timer
        timer.start()
        self._save()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.cancelled = True
        timer = self._timers.pop(job_id, None)
        if timer:
            timer.cancel()
        self._save()
        return True

    def list(self) -> list[dict[str, object]]:
        return [{"id": job.id, "prompt": job.prompt, "run_at": job.run_at.isoformat(), "cancelled": job.cancelled} for job in self.jobs.values()]

    def _save(self) -> None:
        self.path.write_text(json.dumps({job_id: {"prompt": job.prompt, "run_at": job.run_at.isoformat(), "cancelled": job.cancelled} for job_id, job in self.jobs.items()}, ensure_ascii=False), encoding="utf-8")

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for job_id, item in data.items():
                self.jobs[job_id] = ScheduledJob(item["prompt"], datetime.fromisoformat(item["run_at"]), job_id, bool(item.get("cancelled", False)))
        except (OSError, ValueError, KeyError, TypeError):
            self.jobs = {}

    def _run(self, job_id: str, callback) -> None:
        job = self.jobs.get(job_id)
        if job and not job.cancelled:
            callback(job.prompt)

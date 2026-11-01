import os
import uuid
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from app.config import DATA_DIR, now_kolkata

REPORTS_DIR = DATA_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

class Job:
    def __init__(self, job_id: str, user_id: int, job_type: str, download_filename: str = "report.xlsx"):
        self.job_id = job_id
        self.user_id = user_id
        self.job_type = job_type
        self.download_filename = download_filename
        self.status = "QUEUED"  # QUEUED, RUNNING, COMPLETED, FAILED
        self.progress = 0
        self.created_at = now_kolkata()
        self.completed_at: Optional[datetime] = None
        self.file_path: Optional[str] = None
        self.error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "status": self.status,
            "progress": self.progress,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else "",
            "completed_at": self.completed_at.strftime("%Y-%m-%d %H:%M:%S") if self.completed_at else None,
            "download_filename": self.download_filename,
            "error_message": self.error_message,
            "download_url": f"/api/reports/async/download/{self.job_id}" if self.status == "COMPLETED" else None
        }

class JobManager:
    def __init__(self, max_concurrent: int = 3):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self.max_concurrent = max_concurrent
        self._semaphore = threading.Semaphore(max_concurrent)

    def cleanup_old_jobs(self, max_age_seconds: int = 7200):
        now = time.time()
        with self._lock:
            to_remove = []
            for jid, job in self._jobs.items():
                if job.completed_at:
                    age = (now_kolkata() - job.completed_at).total_seconds()
                    if age > max_age_seconds:
                        to_remove.append(jid)
                        if job.file_path and os.path.exists(job.file_path):
                            try:
                                os.remove(job.file_path)
                            except Exception:
                                pass
            for jid in to_remove:
                self._jobs.pop(jid, None)

    def submit_job(
        self,
        user_id: int,
        job_type: str,
        download_filename: str,
        worker_func: Callable[..., Any],
        *args,
        **kwargs
    ) -> Job:
        self.cleanup_old_jobs()
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job = Job(job_id=job_id, user_id=user_id, job_type=job_type, download_filename=download_filename)

        with self._lock:
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run_job_wrapper,
            args=(job, worker_func, args, kwargs),
            daemon=True
        )
        thread.start()
        return job

    def _run_job_wrapper(self, job: Job, worker_func: Callable, args: tuple, kwargs: dict):
        with self._semaphore:
            job.status = "RUNNING"
            job.progress = 10
            try:
                # Generate unique output path
                out_path = REPORTS_DIR / f"{job.job_id}_{job.download_filename}"
                # Worker function receives out_path and executes using its own session
                worker_func(str(out_path), *args, **kwargs)
                job.file_path = str(out_path)
                job.status = "COMPLETED"
                job.progress = 100
                job.completed_at = now_kolkata()
            except Exception as ex:
                job.status = "FAILED"
                job.error_message = str(ex)
                job.completed_at = now_kolkata()
                print(f"[JOB ERROR] Background job {job.job_id} failed: {ex}")

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

job_manager = JobManager(max_concurrent=3)


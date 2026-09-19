import json
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from email_workflow.core.locking import synchronized

class AuditLogger:
    def __init__(self, log_path: str = "audit.jsonl"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    @synchronized
    def log_event(
        self,
        event_type: str,
        message_id: str,
        thread_id: str,
        detail: str,
        run_metadata: Optional[Dict[str, Any]] = None,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log an event in append-only JSON lines format.
        """
        now_str = datetime.now(timezone.utc).isoformat()
        entry = {
            "timestamp": now_str,
            "event_type": event_type,
            "message_id": message_id,
            "thread_id": thread_id,
            "detail": detail,
            "run_metadata": run_metadata or {},
        }
        if extra_data:
            entry["extra_data"] = extra_data

        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def get_events_for_thread(self, thread_id: str) -> list:
        if not self.log_path.exists():
            return []
        events = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    if e.get("thread_id") == thread_id:
                        events.append(e)
                except Exception:
                    pass
        return events

    def get_all_events(self) -> list:
        if not self.log_path.exists():
            return []
        events = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
        return events

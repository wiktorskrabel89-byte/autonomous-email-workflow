import json
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timezone
from email_workflow.models.state import ProcessingStage, MessageRecord
from email_workflow.core.locking import synchronized

TERMINAL_STAGES = {
    ProcessingStage.SENT,
    ProcessingStage.ARCHIVED,
    ProcessingStage.ESCALATED,
    ProcessingStage.SUPERSEDED,
}

class IdempotencyManager:
    def __init__(self, store_path: str = "idempotency.json"):
        self.store_path = Path(store_path)
        self.records: Dict[str, MessageRecord] = {}
        self.load_state()

    @synchronized
    def load_state(self) -> None:
        if self.store_path.exists():
            try:
                with open(self.store_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.records = {
                    mid: MessageRecord.model_validate(rdata)
                    for mid, rdata in data.items()
                }
            except Exception:
                self.records = {}
        else:
            self.records = {}

    @synchronized
    def save_state(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {mid: r.model_dump() for mid, r in self.records.items()}
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def is_processed(self, message_id: str) -> bool:
        record = self.records.get(message_id)
        if record and record.current_stage in TERMINAL_STAGES:
            return True
        return False

    def get_stage(self, message_id: str) -> Optional[ProcessingStage]:
        record = self.records.get(message_id)
        return record.current_stage if record else None

    @synchronized
    def update_stage(self, message_id: str, thread_id: str, stage: ProcessingStage) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        record = self.records.get(message_id)
        if not record:
            record = MessageRecord(
                message_id=message_id,
                thread_id=thread_id,
                current_stage=stage,
                updated_at=now_str,
                history=[{"stage": stage.value, "timestamp": now_str}],
            )
            self.records[message_id] = record
        else:
            record.current_stage = stage
            record.updated_at = now_str
            record.history.append({"stage": stage.value, "timestamp": now_str})
        self.save_state()

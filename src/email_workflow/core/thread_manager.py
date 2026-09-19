import json
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from datetime import datetime, timezone
from email_workflow.models.state import (
    ThreadState,
    ThreadMessage,
    ActiveAction,
    ActionState,
    MessageStatus,
)
from email_workflow.models.email import EmailMessage
from email_workflow.core.locking import synchronized

class ThreadManager:
    def __init__(self, store_path: str = "state.json"):
        self.store_path = Path(store_path)
        self.threads: Dict[str, ThreadState] = {}
        self.load_state()

    @synchronized
    def load_state(self) -> None:
        if self.store_path.exists():
            try:
                with open(self.store_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.threads = {
                    tid: ThreadState.model_validate(tdata)
                    for tid, tdata in data.items()
                }
            except Exception:
                self.threads = {}
        else:
            self.threads = {}

    @synchronized
    def save_state(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {tid: t.model_dump() for tid, t in self.threads.items()}
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def get_thread(self, thread_id: str) -> Optional[ThreadState]:
        return self.threads.get(thread_id)

    @synchronized
    def record_incoming_message(
        self, email: EmailMessage
    ) -> Tuple[ThreadState, Optional[dict]]:
        """
        Record incoming email into thread state.
        If an in_progress active_action exists, SUPERSEDE it!
        Returns (updated_thread_state, superseded_info_if_any).
        """
        thread = self.threads.get(email.thread_id)
        if not thread:
            thread = ThreadState(
                thread_id=email.thread_id,
                canonical_subject=email.subject,
                participants=[email.sender.email],
                messages=[],
            )
            self.threads[email.thread_id] = thread

        # Add participant if not present
        if email.sender.email not in thread.participants:
            thread.participants.append(email.sender.email)

        superseded_info = None

        # Rule: a newer message on a thread with an in_progress active_action immediately supersedes it
        if thread.active_action and thread.active_action.state == ActionState.IN_PROGRESS:
            old_target_msg_id = thread.active_action.target_message_id
            thread.active_action.state = ActionState.CANCELLED

            # Update old message status to superseded
            for m in thread.messages:
                if m.message_id == old_target_msg_id:
                    m.status = MessageStatus.SUPERSEDED

            superseded_info = {
                "cancelled_action": thread.active_action.action_type,
                "superseded_message_id": old_target_msg_id,
                "new_message_id": email.message_id,
            }

        # Check if message already recorded
        existing_msg = next((m for m in thread.messages if m.message_id == email.message_id), None)
        if not existing_msg:
            thread.messages.append(
                ThreadMessage(
                    message_id=email.message_id,
                    in_reply_to=email.in_reply_to,
                    received_at=email.received_at,
                    status=MessageStatus.PENDING,
                )
            )

        self.save_state()
        return thread, superseded_info

    @synchronized
    def set_active_action(
        self, thread_id: str, action_type: str, target_message_id: str
    ) -> ActiveAction:
        thread = self.get_thread(thread_id)
        if not thread:
            raise ValueError(f"Thread {thread_id} not found.")

        now_str = datetime.now(timezone.utc).isoformat()
        action = ActiveAction(
            action_type=action_type,
            target_message_id=target_message_id,
            created_at=now_str,
            state=ActionState.IN_PROGRESS,
        )
        thread.active_action = action
        self.save_state()
        return action

    @synchronized
    def complete_active_action(self, thread_id: str) -> None:
        thread = self.get_thread(thread_id)
        if thread and thread.active_action:
            thread.active_action.state = ActionState.COMPLETED
            self.save_state()

    @synchronized
    def update_message_status(
        self, thread_id: str, message_id: str, status: MessageStatus
    ) -> None:
        thread = self.get_thread(thread_id)
        if thread:
            for m in thread.messages:
                if m.message_id == message_id:
                    m.status = status
            self.save_state()

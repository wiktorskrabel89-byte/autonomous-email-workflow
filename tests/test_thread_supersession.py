import pytest
from pathlib import Path
from email_workflow.models.email import EmailMessage, SenderInfo
from email_workflow.models.state import ActionState, MessageStatus
from email_workflow.core.thread_manager import ThreadManager

def test_thread_supersession(tmp_path):
    store_file = tmp_path / "test_state.json"
    manager = ThreadManager(store_path=str(store_file))

    # Message 1
    msg1 = EmailMessage(
        message_id="msg_001",
        thread_id="thread_alpha",
        sender=SenderInfo(name="Alice", email="alice@example.com"),
        subject="Meeting Proposal",
        body="Can we meet Tuesday?",
        received_at="2026-09-18T10:00:00Z",
    )

    thread, superseded_info = manager.record_incoming_message(msg1)
    assert thread.thread_id == "thread_alpha"
    assert len(thread.messages) == 1
    assert superseded_info is None

    # Set active action on Message 1
    manager.set_active_action("thread_alpha", "draft", "msg_001")
    thread_check = manager.get_thread("thread_alpha")
    assert thread_check.active_action is not None
    assert thread_check.active_action.state == ActionState.IN_PROGRESS
    assert thread_check.active_action.target_message_id == "msg_001"

    # Message 2 arrives on same thread while active_action is IN_PROGRESS
    msg2 = EmailMessage(
        message_id="msg_002",
        thread_id="thread_alpha",
        in_reply_to="msg_001",
        sender=SenderInfo(name="Alice", email="alice@example.com"),
        subject="Re: Meeting Proposal",
        body="Actually Wednesday works better",
        received_at="2026-09-18T10:30:00Z",
    )

    thread2, superseded_info = manager.record_incoming_message(msg2)
    assert superseded_info is not None
    assert superseded_info["superseded_message_id"] == "msg_001"
    assert superseded_info["cancelled_action"] == "draft"
    assert superseded_info["new_message_id"] == "msg_002"

    # Verify state of Message 1 is superseded and action is cancelled
    msg1_state = next(m for m in thread2.messages if m.message_id == "msg_001")
    assert msg1_state.status == MessageStatus.SUPERSEDED
    assert thread2.active_action.state == ActionState.CANCELLED

    # Verify Message 2 is recorded as pending
    msg2_state = next(m for m in thread2.messages if m.message_id == "msg_002")
    assert msg2_state.status == MessageStatus.PENDING

from typing import List, Dict, Any, Optional
from email_workflow.models.config import AppConfig
from email_workflow.models.email import EmailMessage, DecisionOption
from email_workflow.models.state import ProcessingStage, MessageStatus
from email_workflow.providers.base_ai import AIProvider
from email_workflow.providers.email_provider import EmailProvider
from email_workflow.core.decision import evaluate_decision
from email_workflow.core.thread_manager import ThreadManager
from email_workflow.core.idempotency import IdempotencyManager
from email_workflow.core.validation import validate_send_gate
from email_workflow.providers.key_pool import parallel_lanes
from email_workflow.core.audit import AuditLogger
from email_workflow.core.notifications import NotificationDispatcher

from email_workflow.core.known_facts import KnownFactsManager

class WorkflowPipeline:
    def __init__(
        self,
        config: AppConfig,
        ai_provider: AIProvider,
        email_provider: EmailProvider,
        store_path: str = "state.json",
        audit_path: str = "audit.jsonl",
        idempotency_path: str = "idempotency.json",
        known_facts_path: str = "known_facts.txt",
    ):
        self.config = config
        self.ai = ai_provider
        self.email = email_provider
        self.thread_mgr = ThreadManager(store_path=store_path)
        self.audit = AuditLogger(log_path=audit_path)
        self.idempotency = IdempotencyManager(store_path=idempotency_path)
        self.notifier = NotificationDispatcher(config.notifications)
        self.facts_mgr = KnownFactsManager(file_path=known_facts_path)

    def _second_opinion(self, email, reply, thread, known_facts, run_meta):
        """Have a different API key check a reply before it can be sent.

        Only worth asking when there is another key to ask: a review by the key
        that wrote the reply is the same model marking its own homework, so
        with a single key this returns None and nothing changes.

        Never fatal. If the review itself fails, the reply that was already
        written stands and the ordinary safety gate still applies - losing a
        good reply because the checker broke would be the worse outcome.
        """
        keys_cfg = getattr(self.config.ai, "keys", None)
        if keys_cfg is None or not getattr(keys_cfg, "review", False):
            return None
        if parallel_lanes(self.ai) < 2:
            return None

        try:
            review = self.ai.review_reply(email, reply, thread, known_facts)
        except Exception as e:
            self.audit.log_event(
                event_type="reply_review_failed",
                message_id=email.message_id,
                thread_id=email.thread_id,
                detail=f"The second opinion could not be obtained: {e}",
                run_metadata=run_meta,
            )
            return None

        self.audit.log_event(
            event_type="reply_reviewed",
            message_id=email.message_id,
            thread_id=email.thread_id,
            detail=(
                "A second key approved the reply."
                if review.approved
                else "A second key objected: " + "; ".join(review.concerns or ["no reason given"])
            ),
            run_metadata=run_meta,
            extra_data={
                "approved": review.approved,
                "concerns": review.concerns,
                "invented_details": review.invented_details,
            },
        )
        return review

    def process_email(
        self, email: EmailMessage, known_facts: str = ""
    ) -> Dict[str, Any]:
        """
        Executes full pipeline for a single email message.
        """
        if not known_facts:
            known_facts = self.facts_mgr.load_facts()
        # Ask the provider what it actually is. After a failover the live
        # provider differs from the one named in config.yaml, and the audit log
        # has to record the one that really answered.
        provider_name, model_name = self.ai.describe()
        run_meta = {
            "ai_mode": self.config.ai.mode.value,
            "provider": provider_name,
            "model": model_name,
        }

        # 1. Fetch & Normalize
        self.audit.log_event(
            event_type="received",
            message_id=email.message_id,
            thread_id=email.thread_id,
            detail=f"Received email '{email.subject}' from {email.sender.email}",
            run_metadata=run_meta,
        )

        # 2. Idempotency Check
        if self.idempotency.is_processed(email.message_id):
            stage = self.idempotency.get_stage(email.message_id)
            self.audit.log_event(
                event_type="no_op",
                message_id=email.message_id,
                thread_id=email.thread_id,
                detail=f"Message {email.message_id} already processed at stage '{stage}'. Skipping.",
                run_metadata=run_meta,
            )
            return {
                "message_id": email.message_id,
                "status": "skipped",
                "stage": stage,
            }

        self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.ANALYZING)

        # 3. Resolve Thread State & Handle Supersession
        thread, superseded_info = self.thread_mgr.record_incoming_message(email)

        if superseded_info:
            self.audit.log_event(
                event_type="superseded",
                message_id=superseded_info["superseded_message_id"],
                thread_id=email.thread_id,
                detail=(
                    f"Action '{superseded_info['cancelled_action']}' on message "
                    f"{superseded_info['superseded_message_id']} was SUPERSEDED by new message {email.message_id}"
                ),
                run_metadata=run_meta,
                extra_data=superseded_info,
            )
            self.idempotency.update_stage(
                superseded_info["superseded_message_id"],
                email.thread_id,
                ProcessingStage.SUPERSEDED,
            )

        # 4. Classify Category & Score Importance/Urgency
        analysis = self.ai.classify_email(email, thread, known_facts)
        run_meta["provider"], run_meta["model"] = self.ai.describe()

        run_meta["confidence"] = analysis.confidence
        run_meta["category"] = analysis.category.value
        run_meta["importance"] = analysis.importance.value
        run_meta["urgency"] = analysis.urgency.value

        self.audit.log_event(
            event_type="classified",
            message_id=email.message_id,
            thread_id=email.thread_id,
            detail=f"Classified as '{analysis.category.value}' (Confidence: {analysis.confidence:.2f})",
            run_metadata=run_meta,
        )

        self.audit.log_event(
            event_type="prioritized",
            message_id=email.message_id,
            thread_id=email.thread_id,
            detail=f"Importance: {analysis.importance.value}, Urgency: {analysis.urgency.value}",
            run_metadata=run_meta,
        )

        self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.ANALYZED)

        # 5. Deterministic Safety & Automation Decision
        decision, decision_reason = evaluate_decision(
            analysis, self.config.automation, email.subject, email.body
        )

        run_meta["decision"] = decision.value
        run_meta["reason"] = decision_reason

        self.audit.log_event(
            event_type="response_required_determined",
            message_id=email.message_id,
            thread_id=email.thread_id,
            detail=f"Response required: {analysis.response_required}. Decision: {decision.value}. Reason: {decision_reason}",
            run_metadata=run_meta,
        )

        self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.DECIDED)

        # 6. Branching Execution based on Decision
        draft_id = None
        sent_id = None
        final_action_summary = ""

        if decision == DecisionOption.ARCHIVE or decision == DecisionOption.IGNORE:
            self.email.archive_email(email.message_id)
            self.thread_mgr.update_message_status(email.thread_id, email.message_id, MessageStatus.ANALYZED)
            self.audit.log_event(
                event_type="archived",
                message_id=email.message_id,
                thread_id=email.thread_id,
                detail=f"Email archived/ignored: {decision_reason}",
                run_metadata=run_meta,
            )
            self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.ARCHIVED)
            final_action_summary = f"Archived ({decision_reason})"

        elif decision == DecisionOption.AUTOMATICALLY_REPLY:
            self.thread_mgr.set_active_action(email.thread_id, "auto_reply", email.message_id)
            self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.DRAFTING)

            reply = self.ai.generate_reply(email, thread, known_facts)
            run_meta["provider"], run_meta["model"] = self.ai.describe()
            validation = validate_send_gate(email, analysis, reply, thread, self.config.automation)

            # A second key reads the reply before it can go out. This is the
            # only place a review can still change the outcome, so it is the
            # only place one is asked for.
            review = self._second_opinion(email, reply, thread, known_facts, run_meta)
            if review is not None and not review.approved:
                validation.valid = False
                validation.failure_reasons = list(validation.failure_reasons) + [
                    "a second key checked the reply and objected: "
                    + "; ".join(review.concerns or ["no reason given"])
                ]

            # Sending is off by default. Rather than quietly doing nothing and
            # recording a reply that never left, keep the written reply as a
            # draft and say so.
            if validation.valid and not getattr(self.config.email, "allow_send", False):
                validation.valid = False
                validation.failure_reasons = list(validation.failure_reasons) + [
                    "sending is turned off (set email.allow_send: true in config.yaml "
                    "to let this app actually send replies)"
                ]

            if validation.valid:
                sent_id = self.email.send_email(
                    email.message_id, reply.reply_subject, reply.reply_body,
                    to_address=email.sender.email,
                )
                self.audit.log_event(
                    event_type="automatically_replied",
                    message_id=email.message_id,
                    thread_id=email.thread_id,
                    detail=f"Auto-replied via send ID {sent_id}. Subject: {reply.reply_subject}",
                    run_metadata=run_meta,
                    extra_data={"reply_body": reply.reply_body},
                )
                self.thread_mgr.complete_active_action(email.thread_id)
                self.thread_mgr.update_message_status(email.thread_id, email.message_id, MessageStatus.REPLIED)
                self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.SENT)
                self.notifier.notify(email, analysis, decision, draft_id=sent_id, reply_text=reply.reply_body)
                final_action_summary = f"Automatically replied (ID: {sent_id})"
            elif self.config.email.create_drafts:
                # The reply is written but must not go out unreviewed. Keeping
                # it as a draft means the work is not thrown away.
                fallback_reason = "Final send validation gate failed: " + "; ".join(validation.failure_reasons)
                draft_id = self.email.create_draft(
                    email.message_id, reply.reply_subject, reply.reply_body,
                    to_address=email.sender.email,
                )
                self.email.flag_email(email.message_id)

                self.audit.log_event(
                    event_type="draft_created",
                    message_id=email.message_id,
                    thread_id=email.thread_id,
                    detail=f"Draft {draft_id} created as fallback after send validation failure: {fallback_reason}",
                    run_metadata=run_meta,
                )
                self.audit.log_event(
                    event_type="approval_requested",
                    message_id=email.message_id,
                    thread_id=email.thread_id,
                    detail=f"Approval requested for draft {draft_id}",
                    run_metadata=run_meta,
                )
                self.thread_mgr.complete_active_action(email.thread_id)
                self.thread_mgr.update_message_status(email.thread_id, email.message_id, MessageStatus.ANALYZED)
                self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.AWAITING_APPROVAL)
                self.notifier.notify(email, analysis, DecisionOption.CREATE_DRAFT, draft_id=draft_id, reply_text=reply.reply_body)
                final_action_summary = f"Draft created ({draft_id}) due to validation fallback"

            else:
                # Drafts are switched off, and this reply did not clear the
                # safety gate. It is not sent and not saved - it comes to you
                # instead, with the written reply attached to the report so
                # nothing is lost.
                fallback_reason = (
                    "Not sent, and drafts are switched off: "
                    + "; ".join(validation.failure_reasons)
                )
                self.email.flag_email(email.message_id)
                self.audit.log_event(
                    event_type="escalated",
                    message_id=email.message_id,
                    thread_id=email.thread_id,
                    detail=fallback_reason,
                    run_metadata=run_meta,
                    extra_data={"unsent_reply": reply.reply_body},
                )
                self.thread_mgr.complete_active_action(email.thread_id)
                self.thread_mgr.update_message_status(email.thread_id, email.message_id, MessageStatus.ANALYZED)
                self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.ESCALATED)
                self.notifier.notify(
                    email, analysis, DecisionOption.ESCALATE,
                    draft_id=None, reply_text=reply.reply_body,
                )
                final_action_summary = "Escalated to you - not sent, no draft kept"

        elif decision in (DecisionOption.CREATE_DRAFT, DecisionOption.WAIT_FOR_APPROVAL):
            self.thread_mgr.set_active_action(email.thread_id, "draft", email.message_id)
            self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.DRAFTING)

            reply = self.ai.generate_reply(email, thread, known_facts)
            run_meta["provider"], run_meta["model"] = self.ai.describe()

            if not self.config.email.create_drafts:
                # Drafts are switched off. This email was never a candidate for
                # sending - the AI asked for a draft precisely because something
                # was missing or a promise was involved - so it comes to you.
                self.email.flag_email(email.message_id)
                self.audit.log_event(
                    event_type="escalated",
                    message_id=email.message_id,
                    thread_id=email.thread_id,
                    detail=f"Drafts are switched off: {decision_reason}",
                    run_metadata=run_meta,
                    extra_data={"unsent_reply": reply.reply_body},
                )
                self.thread_mgr.complete_active_action(email.thread_id)
                self.thread_mgr.update_message_status(email.thread_id, email.message_id, MessageStatus.ANALYZED)
                self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.ESCALATED)
                self.notifier.notify(
                    email, analysis, DecisionOption.ESCALATE,
                    draft_id=None, reply_text=reply.reply_body,
                )
                return {
                    "message_id": email.message_id,
                    "thread_id": email.thread_id,
                    "analysis": analysis,
                    "decision": DecisionOption.ESCALATE,
                    "decision_reason": decision_reason,
                    "draft_id": None,
                    "sent_id": None,
                    "summary": "Escalated to you - drafts are switched off",
                }

            draft_id = self.email.create_draft(
                    email.message_id, reply.reply_subject, reply.reply_body,
                    to_address=email.sender.email,
                )
            # A draft is mail still waiting on you, so it gets the same star
            # and label as anything else escalated - otherwise it is invisible
            # among everything already dealt with.
            self.email.flag_email(email.message_id)

            self.audit.log_event(
                event_type="draft_created",
                message_id=email.message_id,
                thread_id=email.thread_id,
                detail=f"Draft {draft_id} created. Subject: {reply.reply_subject}",
                run_metadata=run_meta,
                extra_data={"draft_body": reply.reply_body},
            )
            self.audit.log_event(
                event_type="approval_requested",
                message_id=email.message_id,
                thread_id=email.thread_id,
                detail=f"Approval requested for draft {draft_id}",
                run_metadata=run_meta,
            )
            self.thread_mgr.complete_active_action(email.thread_id)
            self.thread_mgr.update_message_status(email.thread_id, email.message_id, MessageStatus.ANALYZED)
            self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.AWAITING_APPROVAL)

            self.notifier.notify(email, analysis, decision, draft_id=draft_id, reply_text=reply.reply_body)
            final_action_summary = f"Draft created ({draft_id}) - Awaiting approval"

        elif decision in (DecisionOption.ESCALATE, DecisionOption.NOTIFY_ME):
            # Escalated mail stays in the inbox and unread on purpose, but a
            # star and a label make it findable among everything else - this is
            # the mail that actually needed a person.
            self.email.flag_email(email.message_id)

            self.audit.log_event(
                event_type="escalated",
                message_id=email.message_id,
                thread_id=email.thread_id,
                detail=f"Escalated message {email.message_id}: {decision_reason}",
                run_metadata=run_meta,
            )
            self.thread_mgr.update_message_status(email.thread_id, email.message_id, MessageStatus.ANALYZED)
            self.idempotency.update_stage(email.message_id, email.thread_id, ProcessingStage.ESCALATED)

            self.notifier.notify(email, analysis, decision, draft_id=None)
            final_action_summary = f"Escalated and starred ({decision_reason})"

        return {
            "message_id": email.message_id,
            "thread_id": email.thread_id,
            "analysis": analysis,
            "decision": decision,
            "decision_reason": decision_reason,
            "draft_id": draft_id,
            "sent_id": sent_id,
            "summary": final_action_summary,
        }

    def process_all_unprocessed(self, known_facts: str = "") -> List[Dict[str, Any]]:
        emails = self.email.fetch_unprocessed_emails()
        results = []
        for email in emails:
            res = self.process_email(email, known_facts=known_facts)
            results.append(res)
        return results

import os
import json
import httpx
from urllib.parse import quote
import smtplib
from email.mime.text import MIMEText
from typing import Optional, Dict, Any, List
from rich.console import Console
from rich.panel import Panel
from email_workflow.models.config import NotificationsConfig
from email_workflow.models.email import EmailMessage, DecisionOption
from email_workflow.models.analysis import EmailAnalysis

console = Console()

# Gmail can jump straight to a message by its RFC822 Message-ID, which is the
# id every mail server already puts on the message. That turns a report into
# something you can act on: one click and the email is open.
GMAIL_SEARCH = "https://mail.google.com/mail/u/0/#search/rfc822msgid:"


def gmail_link(message_id: str) -> Optional[str]:
    """A link that opens this message in Gmail, or None if it cannot be built.

    Demo fixtures use ids like "msg_004_phishing" that no mailbox has, so a
    link is only offered for something that looks like a real Message-ID.
    """
    if not message_id:
        return None
    cleaned = message_id.strip().strip("<>").strip()
    if "@" not in cleaned or " " in cleaned:
        return None
    return GMAIL_SEARCH + quote(cleaned, safe="")


def group_by_label(run_results: list) -> Dict[str, list]:
    """{label: [subject, ...]} for everything that was filed somewhere.

    Ordered by how full each label is, because the interesting question after
    a run of 145 emails is "where did it all go", and the biggest pile is the
    answer. Anything not filed is left out entirely rather than shown under an
    "(unlabelled)" heading nobody asked for.
    """
    grouped: Dict[str, list] = {}
    for result in run_results:
        label = (result.get("filed_under") or "").strip()
        if not label:
            continue
        analysis = result.get("analysis")
        subject = getattr(analysis, "subject", "") or result.get("message_id", "")
        grouped.setdefault(label, []).append(subject[:70])
    return dict(sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def reply_outcome(decision: DecisionOption, reply_id, tick: str = "") -> str:
    """Say what really happened to the written reply.

    The id was announced as "Draft Created" whichever path produced it, so a
    reply that had actually been SENT was reported as a draft still waiting
    for approval - the exact opposite of what happened. That is the one thing
    this report must never get wrong: a sent email cannot be unsent, and the
    report exists to say what was done in your name.
    """
    if not reply_id:
        return "No draft created"
    what = "SENT" if decision == DecisionOption.AUTOMATICALLY_REPLY else "Draft Created"
    return f"{what} ({tick}{reply_id}{tick})"


class NotificationDispatcher:
    def __init__(self, config: NotificationsConfig):
        self.config = config

    def notify(
        self,
        message: EmailMessage,
        analysis: EmailAnalysis,
        decision: DecisionOption,
        draft_id: Optional[str] = None,
        reply_text: Optional[str] = None,
    ) -> Dict[str, bool]:
        """
        Dispatch detailed notification report across configured channels (Terminal, Discord, Email, WhatsApp).
        """
        results = {}
        report_text = self._build_report_text(message, analysis, decision, draft_id, reply_text)

        channel = self.config.channel.lower()

        # Terminal channel
        if channel in ("terminal", "all"):
            results["terminal"] = self._send_terminal(report_text, decision)

        # Discord Webhook channel
        if channel in ("discord", "all"):
            results["discord"] = self._send_discord(report_text, message, analysis, decision, draft_id, reply_text)

        # Email channel
        if channel in ("email", "all"):
            results["email"] = self._send_email(report_text, message)

        # WhatsApp channel
        if channel in ("whatsapp", "all"):
            results["whatsapp"] = self._send_whatsapp(report_text)

        return results

    def send_run_digest(self, run_results: list, provider_name: str = "", model_name: str = "") -> Dict[str, bool]:
        """
        Send a batch run completion summary report across enabled channels.
        """
        if not run_results:
            return {}

        total = len(run_results)
        auto_replied = [r for r in run_results if r.get("decision") == DecisionOption.AUTOMATICALLY_REPLY]
        drafted = [r for r in run_results if r.get("decision") in (DecisionOption.CREATE_DRAFT, DecisionOption.WAIT_FOR_APPROVAL)]
        escalated = [r for r in run_results if r.get("decision") in (DecisionOption.ESCALATE, DecisionOption.NOTIFY_ME)]
        archived = [r for r in run_results if r.get("decision") in (DecisionOption.ARCHIVE, DecisionOption.IGNORE)]

        by_label = group_by_label(run_results)

        lines = [
            "=== BATCH RUN COMPLETION SUMMARY REPORT ===",
            f"AI Engine: {provider_name} ({model_name})",
            f"Total Emails Processed: {total}",
            f"  - Auto-Replied: {len(auto_replied)}",
            f"  - Drafts / Awaiting Approval: {len(drafted)}",
            f"  - Escalated / Human Review: {len(escalated)}",
            f"  - Archived / Ignored: {len(archived)}",
        ]

        # What went where. A count of "42 archived" says nothing about where
        # any of it ended up; this is the line that makes filing visible.
        if by_label:
            lines += ["", "--- Filed under your labels ---"]
            for label, subjects in by_label.items():
                lines.append(f"  {label}: {len(subjects)}")
                for subject in subjects[:5]:
                    lines.append(f"      - {subject}")
                if len(subjects) > 5:
                    lines.append(f"      ... and {len(subjects) - 5} more")

        # Off by default: it repeats what the run already printed line by line,
        # and the raw message ids turn into mailto: links in Discord.
        if getattr(self.config, "show_message_breakdown", False):
            lines += ["", "--- Detailed Breakdown by Message ---"]
            for r in run_results:
                msg_id = r.get("message_id")
                dec = r.get("decision")
                dec_str = dec.value.upper() if dec else "UNKNOWN"
                sum_str = r.get("summary", "")
                lines.append(f"* [{msg_id}] Decision: {dec_str} | {sum_str}")

        report_text = "\n".join(lines)
        results = {}
        channel = self.config.channel.lower()

        if channel in ("terminal", "all"):
            console.print(Panel(report_text, title="Batch Completion Digest Report", border_style="bold green"))
            results["terminal"] = True

        if channel in ("discord", "all"):
            url = os.getenv("DISCORD_WEBHOOK_URL")
            if url:
                embed = {
                    "title": "📊 Batch Completion Digest Summary Report",
                    "description": f"**AI Engine:** `{provider_name}` (`{model_name}`)\n**Total Emails Processed:** `{total}`",
                    "color": 0x00FF00,
                    "fields": [
                        {
                            "name": "📈 Status Breakdown",
                            "value": (
                                f"🟢 **Auto-Replied:** `{len(auto_replied)}`\n"
                                f"🟡 **Drafted / Awaiting Approval:** `{len(drafted)}`\n"
                                f"🔴 **Escalated / Blocked:** `{len(escalated)}`\n"
                                f"🔵 **Archived / Ignored:** `{len(archived)}`"
                            ),
                            "inline": False,
                        },
                    ] + [
                        # One field per label, so Discord shows them side by
                        # side as sections rather than as one wall of text.
                        {
                            "name": f"🏷️ {label} ({len(subjects)})",
                            "value": (
                                "\n".join(f"• {s}" for s in subjects[:8])
                                + (f"\n… and {len(subjects) - 8} more"
                                   if len(subjects) > 8 else "")
                            )[:1024] or "-",
                            "inline": True,
                        }
                        for label, subjects in by_label.items()
                    ] + [
                        {
                            "name": "📬 Processed Messages Breakdown",
                            "value": report_text[:1024],
                            "inline": False,
                        }
                    ],
                    "footer": {"text": "Autonomous Email Workflow System"},
                }
                payload = {"embeds": [embed]}
                try:
                    resp = httpx.post(url, json=payload, timeout=5.0)
                    results["discord"] = resp.status_code in (200, 204)
                except Exception:
                    results["discord"] = False

        if channel in ("email", "all"):
            sender = os.getenv("NOTIFICATION_SENDER_EMAIL")
            recipient = os.getenv("NOTIFICATION_RECIPIENT_EMAIL")
            if sender and recipient:
                try:
                    msg = MIMEText(report_text)
                    msg["Subject"] = "[Workflow Report] Batch Completion Summary"
                    msg["From"] = sender
                    msg["To"] = recipient
                    with smtplib.SMTP(os.getenv("SMTP_SERVER", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "587"))) as server:
                        server.starttls()
                        server.login(sender, os.getenv("NOTIFICATION_SENDER_PASSWORD", ""))
                        server.send_message(msg)
                    results["email"] = True
                except Exception:
                    results["email"] = False

        if channel in ("whatsapp", "all"):
            account_sid = os.getenv("TWILIO_ACCOUNT_SID")
            auth_token = os.getenv("TWILIO_AUTH_TOKEN")
            if account_sid and auth_token:
                try:
                    httpx.post(
                        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
                        data={"From": os.getenv("TWILIO_WHATSAPP_FROM"), "To": os.getenv("TWILIO_WHATSAPP_TO"), "Body": report_text},
                        auth=(account_sid, auth_token),
                        timeout=5.0
                    )
                    results["whatsapp"] = True
                except Exception:
                    results["whatsapp"] = False

        return results

    def _build_report_text(
        self,
        message: EmailMessage,
        analysis: EmailAnalysis,
        decision: DecisionOption,
        draft_id: Optional[str] = None,
        reply_text: Optional[str] = None,
    ) -> str:
        draft_status = reply_outcome(decision, draft_id)
        lines = [
            f"=== WORKFLOW NOTIFICATION REPORT ===",
            f"Subject: {message.subject}",
            f"Sender: {message.sender.name} <{message.sender.email}>",
            f"Category: {analysis.category.value} | Priority: {analysis.importance.value}/{analysis.urgency.value}",
            f"Decision Action: {decision.value.upper()}",
            f"Draft / Sent Status: {draft_status}",
        ]
        link = gmail_link(message.message_id)
        if link:
            lines.append(f"Open in Gmail: {link}")
        if analysis.missing_information:
            lines.append(f"Missing Required Info: {', '.join(analysis.missing_information)}")
        if reply_text:
            lines.append("\n--- AI Written Email Reply Preview ---")
            lines.append(reply_text)
        return "\n".join(lines)

    def _send_terminal(self, report_text: str, decision: DecisionOption) -> bool:
        color = "yellow"
        if decision == DecisionOption.ESCALATE:
            color = "bold red"
        elif decision == DecisionOption.AUTOMATICALLY_REPLY:
            color = "bold green"

        console.print(Panel(report_text, title="Notification Report", border_style=color))
        return True

    def _send_discord(
        self,
        report_text: str,
        message: EmailMessage,
        analysis: EmailAnalysis,
        decision: DecisionOption,
        draft_id: Optional[str] = None,
        reply_text: Optional[str] = None,
    ) -> bool:
        url = os.getenv("DISCORD_WEBHOOK_URL")
        if not url:
            console.print("[dim yellow]Discord webhook URL not found in DISCORD_WEBHOOK_URL env. Skipping Discord notification.[/dim yellow]")
            return False

        color_int = 0xFFD700  # Gold/Yellow
        status_icon = "🟡"
        if decision == DecisionOption.ESCALATE:
            color_int = 0xFF0000  # Red
            status_icon = "🔴"
        elif decision == DecisionOption.AUTOMATICALLY_REPLY:
            color_int = 0x00FF00  # Green
            status_icon = "🟢"

        draft_str = reply_outcome(decision, draft_id, tick="`")

        fields = [
            {
                "name": "📊 Category & Priority",
                "value": f"Category: `{analysis.category.value}`\nImportance: `{analysis.importance.value}` | Urgency: `{analysis.urgency.value}`",
                "inline": True,
            },
            {
                "name": "🛡️ Decision Action",
                "value": f"{status_icon} **`{decision.value.upper()}`**\n({draft_str})",
                "inline": True,
            },
        ]

        if analysis.missing_information:
            fields.append({
                "name": "⚠️ Missing Information Required",
                "value": "\n".join([f"• {item}" for item in analysis.missing_information]),
                "inline": False,
            })

        if reply_text:
            preview = reply_text[:1000] + ("..." if len(reply_text) > 1000 else "")
            fields.append({
                "name": "📝 AI Written Email Reply Preview",
                "value": f"```\n{preview}\n```",
                "inline": False,
            })

        link = gmail_link(message.message_id)
        if link:
            fields.append({
                "name": "📬 Open the email",
                "value": f"[Open this message in Gmail]({link})",
                "inline": False,
            })

        embed = {
            "title": f"📬 Workflow Report: {message.subject}",
            "description": f"**From:** {message.sender.name} `<{message.sender.email}>`",
            "color": color_int,
            "fields": fields,
            "footer": {"text": f"Message ID: {message.message_id} | Autonomous Email Workflow System"},
        }
        if link:
            embed["url"] = link      # makes the report title clickable too

        payload = {"embeds": [embed]}
        try:
            resp = httpx.post(url, json=payload, timeout=5.0)
            return resp.status_code in (200, 204)
        except Exception as e:
            console.print(f"[bold red]Failed to send Discord webhook: {e}[/bold red]")
            return False

    def _send_email(self, report_text: str, message: EmailMessage) -> bool:
        smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        sender = os.getenv("NOTIFICATION_SENDER_EMAIL")
        password = os.getenv("NOTIFICATION_SENDER_PASSWORD")
        recipient = os.getenv("NOTIFICATION_RECIPIENT_EMAIL")

        if not (sender and password and recipient):
            console.print("[dim yellow]Email notification settings not configured. Logged report locally.[/dim yellow]")
            return False

        try:
            msg = MIMEText(report_text)
            msg["Subject"] = f"[Workflow Report] {message.subject}"
            msg["From"] = sender
            msg["To"] = recipient

            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(sender, password)
                server.send_message(msg)
            return True
        except Exception as e:
            console.print(f"[bold red]Failed to send email notification: {e}[/bold red]")
            return False

    def _send_whatsapp(self, report_text: str) -> bool:
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        from_number = os.getenv("TWILIO_WHATSAPP_FROM")
        to_number = os.getenv("TWILIO_WHATSAPP_TO")

        if not (account_sid and auth_token and from_number and to_number):
            console.print("[dim yellow]WhatsApp/Twilio credentials not configured in env. Skipping WhatsApp notification.[/dim yellow]")
            return False

        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        data = {
            "From": from_number,
            "To": to_number,
            "Body": report_text,
        }
        try:
            resp = httpx.post(url, data=data, auth=(account_sid, auth_token), timeout=5.0)
            return resp.status_code in (200, 201)
        except Exception as e:
            console.print(f"[bold red]Failed to send WhatsApp message via Twilio: {e}[/bold red]")
            return False

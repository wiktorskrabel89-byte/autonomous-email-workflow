import json
import imaplib
import smtplib
import email
import os
import time
from abc import ABC, abstractmethod
from typing import List, Optional
from pathlib import Path
from datetime import datetime, timedelta
from email.header import decode_header
from email.mime.text import MIMEText
from email_workflow.models.config import EmailConfig
from email_workflow.models.email import EmailMessage, SenderInfo
from email_workflow.core.paths import resolve_project_file
from email_workflow.core.errors import EmailProviderError

# IMAP dates must be English (01-Jan-2026). strftime("%b") follows the machine
# locale, so on a Polish or German Windows it would emit a month name the
# server rejects. Spelling them out keeps this working on anyone's computer.
_IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _imap_date(when: datetime) -> str:
    return f"{when.day:02d}-{_IMAP_MONTHS[when.month - 1]}-{when.year}"


def _decode_str(header_val: str) -> str:
    if not header_val:
        return ""
    decoded_list = decode_header(header_val)
    parts = []
    for bytes_or_str, encoding in decoded_list:
        if isinstance(bytes_or_str, bytes):
            parts.append(bytes_or_str.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(str(bytes_or_str))
    return "".join(parts)

class EmailProvider(ABC):
    @abstractmethod
    def fetch_unprocessed_emails(self) -> List[EmailMessage]:
        """Fetch pending / unprocessed emails from mailbox."""
        pass

    @abstractmethod
    def create_draft(self, message_id: str, reply_subject: str, reply_body: str,
                     to_address: Optional[str] = None) -> str:
        """Create an email draft. Returns draft_id."""
        pass

    @abstractmethod
    def send_email(self, message_id: str, reply_subject: str, reply_body: str,
                   to_address: Optional[str] = None) -> str:
        """Send an email reply. Returns sent_message_id."""
        pass

    @abstractmethod
    def archive_email(self, message_id: str) -> None:
        """Archive an email: read, and out of the inbox."""
        pass

    def flag_email(self, message_id: str, label: Optional[str] = None) -> None:
        """Star and label an email worth coming back to. Optional."""
        return None

class MockEmailProvider(EmailProvider):
    def __init__(self, inbox_path: Optional[str] = None):
        self.inbox_path = inbox_path
        self.emails: List[EmailMessage] = []
        self.drafts: List[dict] = []
        self.sent: List[dict] = []
        self.archived: List[str] = []
        self.flagged: List[tuple] = []
        if inbox_path:
            self.load_inbox(inbox_path)

    def load_inbox(self, inbox_path: str) -> None:
        path = resolve_project_file(inbox_path)
        if not path.exists():
            raise FileNotFoundError(f"Mock inbox file not found: {path} (Resolved from: {inbox_path})")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.emails = [EmailMessage.model_validate(item) for item in data]

    def load_from_messages(self, messages: List[EmailMessage]) -> None:
        self.emails = messages

    def fetch_unprocessed_emails(self) -> List[EmailMessage]:
        return self.emails

    def create_draft(self, message_id: str, reply_subject: str, reply_body: str,
                     to_address: Optional[str] = None) -> str:
        draft_id = f"draft_{message_id}_{len(self.drafts) + 1}"
        self.drafts.append({
            "draft_id": draft_id,
            "target_message_id": message_id,
            "subject": reply_subject,
            "body": reply_body,
            "to": to_address,
        })
        return draft_id

    def send_email(self, message_id: str, reply_subject: str, reply_body: str,
                   to_address: Optional[str] = None) -> str:
        sent_id = f"sent_{message_id}_{len(self.sent) + 1}"
        self.sent.append({
            "sent_id": sent_id,
            "target_message_id": message_id,
            "subject": reply_subject,
            "body": reply_body,
            "to": to_address,
        })
        return sent_id

    def archive_email(self, message_id: str) -> None:
        self.archived.append(message_id)

    def flag_email(self, message_id: str, label: Optional[str] = None) -> None:
        self.flagged.append((message_id, label))

class GmailProvider(EmailProvider):
    """
    Real Gmail Provider connecting via IMAP (imap.gmail.com:993) and SMTP (smtp.gmail.com:587)
    using Gmail App Passwords.
    """
    def __init__(self, config: EmailConfig):
        self.config = config
        self.address = os.getenv("GMAIL_ADDRESS", config.account_ref)
        self.password = os.getenv("GMAIL_APP_PASSWORD", os.getenv("NOTIFICATION_SENDER_PASSWORD", ""))
        self.imap_server = os.getenv("IMAP_SERVER", "imap.gmail.com")
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.drafts_folder = os.getenv("IMAP_DRAFTS_FOLDER", '"[Gmail]/Drafts"')
        # X-GM-LABELS is a Gmail extension; other servers reject it.
        self.supports_gmail_labels = True
        self.last_archive_error = ""

    def fetch_unprocessed_emails(self) -> List[EmailMessage]:
        if not self.address or not self.password:
            raise ValueError(
                "Gmail credentials missing. Please set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env or via setup wizard."
            )

        max_age_days = max(1, getattr(self.config, "max_age_days", 7))
        # 0 (or anything below 1) means no limit.
        max_emails = getattr(self.config, "max_emails_per_run", 0) or 0

        messages = []
        try:
            with imaplib.IMAP4_SSL(self.imap_server) as mail:
                mail.login(self.address, self.password)
                mailbox = self.config.mailbox or "INBOX"
                mail.select(mailbox)

                # Unread AND recent. The server does the date filtering, so an
                # old mailbox never ships years of backlog over the wire.
                since = _imap_date(datetime.now() - timedelta(days=max_age_days))
                status, search_data = mail.search(None, "UNSEEN", "SINCE", since)
                if status != "OK" or not search_data or not search_data[0]:
                    return []

                msg_ids = search_data[0].split()
                # Oldest to newest. With max_emails set, keep only the newest N.
                if max_emails > 0:
                    msg_ids = msg_ids[-max_emails:]
                for m_id in msg_ids:
                    # BODY.PEEK[], never RFC822: a plain fetch sets the \Seen
                    # flag, so a crash mid-run would leave those emails marked
                    # read and they would never be processed again.
                    res, msg_data = mail.fetch(m_id, "(BODY.PEEK[])")
                    if res != "OK":
                        continue

                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])

                            subject = _decode_str(msg.get("Subject", "No Subject"))
                            from_hdr = _decode_str(msg.get("From", "Unknown"))
                            msg_id_hdr = msg.get("Message-ID", f"gmail_msg_{m_id.decode()}")
                            in_reply_to = msg.get("In-Reply-To", None)
                            date_str = msg.get("Date", "")

                            sender_name = from_hdr
                            sender_email = from_hdr
                            if "<" in from_hdr and ">" in from_hdr:
                                sender_name = from_hdr.split("<")[0].strip('" ')
                                sender_email = from_hdr.split("<")[1].split(">")[0]

                            body = ""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    ctype = part.get_content_type()
                                    cdisp = str(part.get("Content-Disposition"))
                                    if ctype == "text/plain" and "attachment" not in cdisp:
                                        payload = part.get_payload(decode=True)
                                        if payload:
                                            body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                                            break
                            else:
                                payload = msg.get_payload(decode=True)
                                if payload:
                                    body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

                            thread_id = in_reply_to or msg_id_hdr

                            email_obj = EmailMessage(
                                message_id=msg_id_hdr,
                                thread_id=thread_id,
                                in_reply_to=in_reply_to,
                                sender=SenderInfo(name=sender_name, email=sender_email, known_contact=True),
                                subject=subject,
                                body=body.strip() or "(No text content)",
                                received_at=date_str or time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            )
                            messages.append(email_obj)
        except Exception as e:
            raise RuntimeError(f"Gmail IMAP connection error: {e}")

        return messages

    def _build_reply(self, message_id: str, reply_subject: str, reply_body: str,
                     to_address: Optional[str]) -> MIMEText:
        msg = MIMEText(reply_body, "plain", "utf-8")
        msg["Subject"] = reply_subject
        msg["From"] = self.address
        # Without a To header the message has no recipient at all. The original
        # code set In-Reply-To but never To, so even with sending enabled the
        # reply could not have reached anybody.
        msg["To"] = to_address or self.address
        msg["In-Reply-To"] = message_id
        msg["References"] = message_id
        return msg

    def send_email(self, message_id: str, reply_subject: str, reply_body: str,
                   to_address: Optional[str] = None) -> str:
        if not self.address or not self.password:
            raise EmailProviderError(
                "Cannot send: the mailbox credentials are missing.",
                hint="Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your .env file.",
                kind="auth",
            )
        if not to_address:
            raise EmailProviderError(
                f"Cannot send a reply to {message_id}: no recipient address was given.",
                hint="This is a bug - the pipeline should pass the original sender.",
                kind="send_failed",
            )

        msg = self._build_reply(message_id, reply_subject, reply_body, to_address)
        try:
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.address, self.password)
                # This line used to be commented out, so send_email returned a
                # success id while nothing was ever sent - the audit log, the
                # digest and the user were all told a reply had gone out.
                # Reaching this method now requires email.allow_send: true.
                server.send_message(msg)
        except EmailProviderError:
            raise
        except Exception as e:
            raise EmailProviderError(
                f"Could not send the reply through {self.smtp_server}: {e}",
                hint="Check GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your .env file. "
                "Gmail needs an App Password, not your normal password.",
                kind="send_failed",
            )
        return f"sent_gmail_{message_id}"

    def create_draft(self, message_id: str, reply_subject: str, reply_body: str,
                     to_address: Optional[str] = None) -> str:
        msg = self._build_reply(message_id, reply_subject, reply_body, to_address)
        try:
            with imaplib.IMAP4_SSL(self.imap_server) as mail:
                mail.login(self.address, self.password)
                status, _ = mail.append(
                    self.drafts_folder,
                    "\\Draft",
                    imaplib.Time2Internaldate(time.time()),
                    msg.as_bytes(),
                )
                # Returning an id regardless of what happened meant a failed
                # APPEND still logged "Draft created". A draft the user cannot
                # find is worse than a clear error.
                if status != "OK":
                    raise EmailProviderError(
                        f"The mail server refused to save the draft (status {status}).",
                        hint=f"Check that the folder {self.drafts_folder} exists. "
                        "On a non-English Gmail the Drafts folder has a different "
                        "name - set IMAP_DRAFTS_FOLDER in your .env file.",
                        kind="draft_failed",
                    )
        except EmailProviderError:
            raise
        except Exception as e:
            raise EmailProviderError(
                f"Could not save the draft to {self.imap_server}: {e}",
                hint=f"Check your mailbox credentials and that the folder "
                f"{self.drafts_folder} exists. You can override it with "
                f"IMAP_DRAFTS_FOLDER in your .env file.",
                kind="draft_failed",
            )
        return f"draft_gmail_{message_id}"

    def archive_email(self, message_id: str) -> None:
        """Really archive it: mark read AND take it out of the inbox.

        Marking it read alone left everything sitting in the inbox, so
        nothing visibly happened. Removing the Inbox label looked like the
        answer and the server even replies OK, but Gmail ignores it and the
        mail stays put - that is the bug this replaced. What actually
        archives is deleting the message out of INBOX and expunging it,
        which Gmail turns into an archive; see _archive_on_gmail. Any other
        server would really delete it, so there we only mark it read.

        Deliberately non-fatal: a failure here leaves the email unarchived,
        and _archive_on_gmail puts it back to unread if it got as far as
        flagging it, so the next run picks it up again. Unlike a failed send,
        it cannot make the app claim something that did not happen.
        """
        if not self.config.archive_unimportant or not self.supports_gmail_labels:
            # Only Gmail turns an IMAP delete into an archive. On any other
            # server the same commands would really delete the mail, so there
            # we go no further than marking it read.
            self._mark(message_id, add_flags="\\Seen", what="mark as read")
            return

        self._archive_on_gmail(message_id)

    def _archive_on_gmail(self, message_id: str) -> None:
        """Take a message out of the inbox, the way Gmail actually allows.

        Removing the \\Inbox label with -X-GM-LABELS looks like it should work
        and even answers OK, but the message stays in the inbox. What Gmail
        documents instead is deleting it from the INBOX folder: with the
        default IMAP setting ("Archive the message") it simply loses the inbox
        label and stays in All Mail. Verified against a real mailbox - the
        message ended up in All Mail and not in Trash.

        UID EXPUNGE is used so only this message is affected; a plain EXPUNGE
        would also remove anything else in the inbox already marked deleted.
        """
        try:
            with imaplib.IMAP4_SSL(self.imap_server) as mail:
                mail.login(self.address, self.password)
                mail.select(self.config.mailbox or "INBOX")

                status, data = mail.uid("SEARCH", None, f'HEADER Message-ID "{message_id}"')
                if status != "OK" or not data or not data[0]:
                    self.last_archive_error = (
                        f"Could not find {message_id} in the inbox to archive it."
                    )
                    return

                for uid in data[0].split():
                    status, _ = mail.uid("STORE", uid, "+FLAGS", "(\\Seen \\Deleted)")
                    if status != "OK":
                        self.last_archive_error = (
                            f"The mail server refused to archive {message_id} "
                            f"(status {status})."
                        )
                        return

                    if not self._expunge_one(mail, uid):
                        # The flags are already set, and being unread is
                        # the only thing that brings an email back next
                        # run. Leaving it would strand the message: still
                        # in the inbox, never archived, never looked at
                        # again. Put it back the way it was.
                        mail.uid("STORE", uid, "-FLAGS", "(\\Seen \\Deleted)")
                        self.last_archive_error = (
                            f"Could not take {message_id} out of the inbox: "
                            f"the server would not expunge it. It has been put "
                            f"back as unread and will be tried again next run."
                        )
                        return
        except Exception as e:
            self.last_archive_error = (
                f"Could not archive {message_id} on {self.imap_server}: {e}. "
                f"It was not archived. If it is still unread, the next run will try again."
            )

    def flag_email(self, message_id: str, label: Optional[str] = None) -> None:
        """Star it, and file it under a label, so it is easy to come back to.

        This is for the mail that matters - financial, security, anything
        escalated. \\Flagged is what Gmail shows as a star, and Gmail creates a
        label the first time one is applied, so no setup is needed.
        """
        if not self.config.star_important:
            return

        self._mark(
            message_id,
            add_flags="\\Flagged",
            add_labels=label or self.config.important_label,
            what="star",
        )

    def _find(self, mail, message_id: str):
        status, data = mail.search(None, f'HEADER Message-ID "{message_id}"')
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def _expunge_one(self, mail, uid) -> bool:
        """Expunge just this message, and say whether it really went.

        UID EXPUNGE needs the UIDPLUS extension. Without it a server can answer
        BAD, which imaplib raises instead of returning, so the plain EXPUNGE
        fallback has to be reached through an except - a status check alone
        never sees it. The plain form expunges everything in this mailbox that
        is already flagged deleted; on Gmail that only files those messages
        away, which is why it is an acceptable last resort here and nowhere
        else.
        """
        try:
            status, _ = mail.uid("EXPUNGE", uid)
            if status == "OK":
                return True
        except Exception:
            pass
        try:
            status, _ = mail.expunge()
            return status == "OK"
        except Exception:
            return False

    def _mark(self, message_id: str, add_flags: Optional[str] = None,
              add_labels: Optional[str] = None, remove_labels: Optional[str] = None,
              what: str = "update") -> None:
        try:
            with imaplib.IMAP4_SSL(self.imap_server) as mail:
                mail.login(self.address, self.password)
                mail.select(self.config.mailbox or "INBOX")

                found = self._find(mail, message_id)
                if not found:
                    self.last_archive_error = (
                        f"Could not find {message_id} in the mailbox to {what} it."
                    )
                    return

                for num in found:
                    # The status was previously thrown away, so a command the
                    # server refused looked exactly like one it accepted.
                    if add_flags:
                        status, _ = mail.store(num, "+FLAGS", add_flags)
                        if status != "OK":
                            self.last_archive_error = (
                                f"The mail server refused to {what} {message_id} "
                                f"(status {status})."
                            )
                    if add_labels and self.supports_gmail_labels:
                        # Gmail makes the label exist on first use.
                        status, _ = mail.store(num, "+X-GM-LABELS", f'"{add_labels}"')
                        if status != "OK":
                            self.last_archive_error = (
                                f"The mail server refused the label "
                                f"'{add_labels}' on {message_id} (status {status})."
                            )
                    if remove_labels and self.supports_gmail_labels:
                        mail.store(num, "-X-GM-LABELS", remove_labels)
        except Exception as e:
            self.last_archive_error = (
                f"Could not {what} {message_id} on {self.imap_server}: {e}. "
                f"The email is untouched and will be seen again next run."
            )

class OutlookProvider(GmailProvider):
    """Outlook / Office365 provider using outlook.office365.com IMAP/SMTP."""
    def __init__(self, config: EmailConfig):
        super().__init__(config)
        self.imap_server = os.getenv("IMAP_SERVER", "outlook.office365.com")
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.office365.com")
        self.supports_gmail_labels = False

class IMAPProvider(GmailProvider):
    """Generic IMAP/SMTP provider using custom server endpoints."""
    def __init__(self, config: EmailConfig):
        super().__init__(config)
        self.imap_server = os.getenv("IMAP_SERVER", "imap.mail.com")
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.mail.com")
        self.supports_gmail_labels = False

def get_email_provider(config: EmailConfig, mock_inbox_path: Optional[str] = None) -> EmailProvider:
    prov = config.provider.lower()
    if prov == "mock":
        default_path = mock_inbox_path or "fixtures/demo_fixtures.json"
        return MockEmailProvider(inbox_path=default_path)
    elif prov == "gmail":
        return GmailProvider(config)
    elif prov == "outlook":
        return OutlookProvider(config)
    elif prov in ("imap", "imap_generic"):
        return IMAPProvider(config)
    else:
        raise ValueError(f"Unknown email provider '{prov}'. Supported: mock, gmail, outlook, imap_generic")

import json
import imaplib
import re
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


# One line of an IMAP LIST reply: (flags) "delimiter" name
_LIST_LINE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|NIL)\s+(?P<name>.+)$')

# Names a Drafts folder goes by when the server does not flag it as one.
# English first, then the Polish and German ones this ran into, then the plain
# IMAP layouts. Only ever used as a last resort - the server's own \Drafts flag
# is the answer on every modern mailbox, whatever language it is in.
_DRAFT_NAME_GUESSES = (
    "[Gmail]/Drafts",
    "[Google Mail]/Drafts",
    "[Gmail]/Wersje robocze",
    "[Gmail]/Entw&APw-rfe",
    "Drafts",
    "INBOX.Drafts",
    "INBOX/Drafts",
)


# The one thing people get stuck on, in one place so every message says it.
# The app-password page does not mention 2-Step Verification at all: with it
# off, Google simply says the setting is not available for your account, which
# reads as a broken page rather than as a missing step.
APP_PASSWORD_HELP = (
    "Gmail needs an App Password - a 16-character one made just for this app. "
    "Your normal Gmail password will not work.\n"
    "  1. Turn on 2-Step Verification: "
    "https://myaccount.google.com/signinoptions/twosv\n"
    "     Google does not offer app passwords until it is on, and the page "
    "below will only say the setting is not available for your account.\n"
    "  2. Create the password: https://myaccount.google.com/apppasswords\n"
    "  3. Put it in .env as GMAIL_APP_PASSWORD (spaces removed), with your "
    "address in GMAIL_ADDRESS.\n"
    "Or run 'email-workflow setup', which walks through all three."
)


# Names Gmail keeps for itself. Over IMAP these are system labels written with
# a leading backslash (\Important, \Starred); asking for the bare word instead
# is asking Gmail to make a user label it will not make, and it answers BAD.
#
# This is what broke starring: important_label was changed from "AI/Important"
# to "Important", every STORE was refused, and - because one refusal aborted
# the whole command - it was reported as "could not star", which was not even
# the part that failed.
RESERVED_GMAIL_LABELS = frozenset({
    "inbox", "starred", "sent", "draft", "drafts", "spam", "trash", "junk",
    "important", "all mail", "allmail", "unread", "read", "chat", "chats",
    "muted", "category", "personal", "social", "promotions", "updates",
    "forums",
})


# The same names in the languages this is actually used in. Gmail localises
# its system labels - the Polish account this was written for shows them as
# "Wazne", "Oznaczone gwiazdka", "Wszystkie", "Kosz", "Wersje robocze" - and a
# user label of that name collides exactly as "Important" did in English.
# Reading them off the server (system_label_names) covers every language; this
# list is what is used before the server has been asked.
RESERVED_LOCALISED = frozenset({
    # Polish
    "wazne", "ważne", "oznaczone gwiazdka", "oznaczone gwiazdką",
    "wszystkie", "cala poczta", "cała poczta", "kosz", "wyslane",
    "wysłane", "wersje robocze", "powiadomienia", "spolecznosci",
    "społeczności", "oferty",
    # German
    "wichtig", "markiert", "alle nachrichten", "papierkorb", "gesendet",
    "entwurfe", "entwürfe", "werbung", "soziale netzwerke",
    # Spanish / French / Italian
    "importante", "destacados", "todos", "papelera", "enviados", "borradores",
    "important", "suivis", "tous les messages", "corbeille", "envoyes",
    "envoyés", "brouillons", "importanti", "speciali", "cestino",
    "inviati", "bozze",
})


def gmail_label(name: str, also_reserved=()) -> str:
    """One label, ready to put in an X-GM-LABELS command.

    A name starting with a backslash is one of Gmail's own (\\Important) and
    goes through untouched and unquoted. Anything else is a label of yours: it
    is quoted, because most of them have spaces in, and moved out of Gmail's
    way if it collides with a reserved name - "Important" becomes
    "AI/Important", which is a label Gmail will happily create.
    """
    name = (name or "").strip()
    if not name:
        return ""
    if name.startswith("\\"):
        return name
    taken = {str(other).strip().lower() for other in (also_reserved or ())}
    if name.lower() in RESERVED_GMAIL_LABELS | RESERVED_LOCALISED | taken:
        name = f"AI/{name}"
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _looks_like_a_login_refusal(e: Exception) -> bool:
    """Whether the server turned the credentials down, rather than the network.

    IMAP and SMTP both answer a bad password with prose, not a status code, so
    the words are all there is to go on.
    """
    text = str(e).lower()
    return any(
        phrase in text
        for phrase in (
            "authenticationfailed",
            "authentication failed",
            "invalid credentials",
            "username and password not accepted",
            "application-specific password",
            "auth",
            "login",
        )
    )


def _parse_list_line(line) -> Optional[tuple]:
    """One LIST reply line as (flags, folder name). None if it cannot be read.

    imaplib hands back a tuple when the server sends the folder name as an IMAP
    literal - `(b'(\\HasNoChildren \\Drafts) "/" {14}', b'Wersje robocze')` -
    which is exactly the shape a name with non-ASCII characters can arrive in.
    Treating that as a string would have thrown, and the whole lookup would
    have fallen back to guessing English names on the one kind of mailbox this
    was written for.
    """
    if isinstance(line, (tuple, list)):
        line = b"".join(
            part if isinstance(part, (bytes, bytearray)) else str(part).encode("latin-1")
            for part in line
        )
        # Drop the "{14}" byte-count marker: the bytes it announced are now
        # joined on right behind it.
        line = re.sub(rb"\{\d+\}", b"", line, count=1)
    if isinstance(line, (bytes, bytearray)):
        # IMAP folder names are modified UTF-7; decoding as latin-1 keeps the
        # bytes intact so the name can be handed straight back to the server.
        line = line.decode("latin-1", errors="replace")
    if not isinstance(line, str):
        return None
    match = _LIST_LINE.match(line.strip())
    if not match:
        return None
    flags = [f.lower() for f in match.group("flags").split()]
    name = match.group("name").strip()
    if name.startswith('"') and name.endswith('"') and len(name) > 1:
        name = name[1:-1]
    return flags, name


def _quote_mailbox(name: str) -> str:
    """A folder name IMAP will accept, quoted exactly once.

    Drafts folders have spaces in them in most languages ("Wersje robocze"),
    and an unquoted name with a space is a different command to the server -
    which is why a folder that exists can still come back as "no such folder".
    """
    name = (name or "").strip()
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


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

    def system_label_names(self, mail) -> set:
        r"""What THIS server calls its own labels, in its own language.

        The hardcoded lists above are guesses; this is the answer. Every folder
        the server marks with a system-use flag (\Important, \Starred, \All,
        \Trash, \Sent, \Drafts, \Junk) has a display name, and on a Polish
        account those names are Wazne, Oznaczone gwiazdka, Wszystkie and so on.
        A user label of the same name collides exactly as "Important" did in
        English - so this is what makes the fix work in any language rather
        than only in the ones somebody thought to type out.

        Asked once per provider and remembered. Never raises: a lookup that
        fails falls back to the lists, which is where we were before.
        """
        if self._system_names is not None:
            return self._system_names

        # Structure, not system use: every folder has these.
        STRUCTURAL = {r"\hasnochildren", r"\haschildren", r"\noselect",
                      r"\noinferiors", r"\marked", r"\unmarked",
                      r"\subscribed"}
        names = set()
        try:
            status, lines = mail.list()
            if status == "OK":
                for line in lines or []:
                    parsed = _parse_list_line(line)
                    if not parsed:
                        continue
                    flags, name = parsed
                    if any(f.startswith("\\") and f not in STRUCTURAL
                           for f in flags):
                        # "[Gmail]/Wersje robocze" -> "Wersje robocze"
                        names.add(name.rsplit("/", 1)[-1].strip().lower())
        except Exception:
            pass

        self._system_names = names
        return names

    def apply_label(self, message_id: str, label: str) -> None:
        """File a message under one of the user's own labels.

        Not abstract, and does nothing by default: a mailbox with no notion of
        labels should quietly not label things, not stop the run.
        """
        return None

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
        self.labelled: List[tuple] = []
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

    def apply_label(self, message_id: str, label: str) -> None:
        if label:
            self.labelled.append((message_id, label))

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
        # Empty means "ask the server". Only set when the user names one.
        self.drafts_folder = os.getenv("IMAP_DRAFTS_FOLDER", "").strip()
        # What the server said its Drafts folder is, once it has been asked.
        self._found_drafts_folder = ""
        # What this server calls its own labels. None = not asked yet.
        self._system_names = None
        self._folders_seen: List[str] = []
        # X-GM-LABELS is a Gmail extension; other servers reject it.
        self.supports_gmail_labels = True
        self.last_archive_error = ""

    def fetch_unprocessed_emails(self) -> List[EmailMessage]:
        if not self.address or not self.password:
            raise EmailProviderError(
                "Gmail credentials missing: GMAIL_ADDRESS and "
                "GMAIL_APP_PASSWORD are not both set.",
                hint=APP_PASSWORD_HELP,
                kind="auth",
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
        except EmailProviderError:
            raise
        except Exception as e:
            # A rejected login used to come out as a bare RuntimeError, which
            # the CLI has no handling for: a traceback, and no word about the
            # app password that is nearly always the real cause.
            if _looks_like_a_login_refusal(e):
                raise EmailProviderError(
                    f"{self.imap_server} would not accept the login for "
                    f"{self.address or '(no address set)'}.",
                    hint=APP_PASSWORD_HELP,
                    kind="auth",
                ) from None
            raise EmailProviderError(
                f"Could not read the mailbox at {self.imap_server}: {e}",
                hint="Check your internet connection, then GMAIL_ADDRESS and "
                "GMAIL_APP_PASSWORD in your .env file.",
                kind="network",
            ) from None

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
                hint=APP_PASSWORD_HELP,
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
                hint=APP_PASSWORD_HELP if _looks_like_a_login_refusal(e)
                else "Check your internet connection and the SMTP settings in "
                "your .env file.",
                kind="send_failed",
            )
        return f"sent_gmail_{message_id}"

    def find_drafts_folder(self, mail) -> str:
        """Ask the server where its Drafts folder is, and remember the answer.

        "[Gmail]/Drafts" is only the English name. A Polish account calls it
        "[Gmail]/Wersje robocze" and a German one "[Gmail]/Entwurfe", so a
        hardcoded name makes every draft fail with a bare "status NO" on any
        mailbox that is not in English.

        The server knows: IMAP LIST marks the folder with the special-use flag
        \\Drafts whatever it is called. The name guesses below are only for a
        server old enough not to send those flags.
        """
        if self.drafts_folder:
            return self.drafts_folder
        if self._found_drafts_folder:
            return self._found_drafts_folder

        names = []
        try:
            status, lines = mail.list()
            if status == "OK":
                for line in lines or []:
                    parsed = _parse_list_line(line)
                    if not parsed:
                        continue
                    flags, name = parsed
                    names.append(name)
                    if "\\drafts" in flags:
                        self._found_drafts_folder = name
                        self._folders_seen = names
                        return name
        except Exception:
            # Never fail the draft over the lookup itself; fall through to the
            # guesses and let the APPEND give the real answer.
            pass

        self._folders_seen = names
        for guess in _DRAFT_NAME_GUESSES:
            if guess in names:
                self._found_drafts_folder = guess
                return guess

        return _DRAFT_NAME_GUESSES[0]

    def create_draft(self, message_id: str, reply_subject: str, reply_body: str,
                     to_address: Optional[str] = None) -> str:
        msg = self._build_reply(message_id, reply_subject, reply_body, to_address)
        folder = self.drafts_folder or "(not looked up yet)"
        try:
            with imaplib.IMAP4_SSL(self.imap_server) as mail:
                mail.login(self.address, self.password)
                folder = self.find_drafts_folder(mail)
                status, _ = mail.append(
                    _quote_mailbox(folder),
                    "\\Draft",
                    imaplib.Time2Internaldate(time.time()),
                    msg.as_bytes(),
                )
                # Returning an id regardless of what happened meant a failed
                # APPEND still logged "Draft created". A draft the user cannot
                # find is worse than a clear error.
                if status != "OK":
                    raise EmailProviderError(
                        f"The mail server refused to save the draft in "
                        f"'{folder}' (status {status}).",
                        hint=self._drafts_hint(folder),
                        kind="draft_failed",
                    )
        except EmailProviderError:
            raise
        except Exception as e:
            raise EmailProviderError(
                f"Could not save the draft to {self.imap_server}: {e}",
                hint="Check your mailbox credentials. " + self._drafts_hint(folder),
                kind="draft_failed",
            )
        return f"draft_gmail_{message_id}"

    def _drafts_hint(self, folder: str) -> str:
        """What to do about a Drafts folder that would not take the message."""
        hint = (
            f"'{folder}' is the folder that was tried. Your mailbox normally "
            f"tells the app which folder is Drafts, whatever language it is in; "
            f"if yours does not, put the right name in IMAP_DRAFTS_FOLDER in "
            f"your .env file."
        )
        if self._folders_seen:
            shown = ", ".join(self._folders_seen[:12])
            hint += f"\n\nFolders your mailbox has: {shown}"
        return hint

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
                    self._problem(
                        f"Could not find {message_id} in the inbox to archive it."
                    )
                    return

                for uid in data[0].split():
                    status, _ = mail.uid("STORE", uid, "+FLAGS", "(\\Seen \\Deleted)")
                    if status != "OK":
                        self._problem(
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
                        self._problem(
                            f"Could not take {message_id} out of the inbox: "
                            f"the server would not expunge it. It has been put "
                            f"back as unread and will be tried again next run."
                        )
                        return
        except Exception as e:
            self._problem(
                f"Could not archive {message_id} on {self.imap_server}: {e}. "
                f"It was not archived. If it is still unread, the next run will try again."
            )

    def apply_label(self, message_id: str, label: str) -> None:
        """File it under one of your own labels, without starring it.

        Separate from flag_email on purpose. Starring says "this needs you";
        a label says "this is what it is". A discount code wants the second
        and not the first, and star_important must not switch filing off.

        Called BEFORE the message is archived. On Gmail archiving means
        deleting it out of INBOX and expunging, and after that there is
        nothing left in INBOX to label.
        """
        if not label or not self.supports_gmail_labels:
            return
        self._mark(message_id, add_labels=label, what=f"file under {label}")

    def flag_email(self, message_id: str, label: Optional[str] = None) -> None:
        """Star it, and file it under a label, so it is easy to come back to.

        This is for the mail that matters - financial, security, anything
        escalated. \\Flagged is what Gmail shows as a star, and Gmail creates a
        label the first time one is applied, so no setup is needed.
        """
        label = label or self.config.important_label
        # The star and the label are two separate choices. They used to be one:
        # switching stars off also stopped the label going on, so mail that
        # needed a person got no mark of any kind and was indistinguishable
        # from everything else in the inbox. Turning stars off should mean "use
        # the label instead", not "stop marking it at all".
        star = "\\Flagged" if self.config.star_important else None
        if not star and not label:
            return

        self._mark(
            message_id,
            add_flags=star,
            add_labels=label,
            what="star" if star else f"file under {label}",
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
                    self._problem(
                        f"Could not find {message_id} in the mailbox to {what} it."
                    )
                    return

                for num in found:
                    # Each step stands on its own. They used to share one try,
                    # so a refused label threw away a star that had already
                    # been applied - and the failure was then reported as
                    # "could not star", which was not the part that failed.
                    if add_flags:
                        self._store(mail, num, "+FLAGS", add_flags,
                                    f"{what} {message_id}")
                    if add_labels and self.supports_gmail_labels:
                        # Gmail makes the label exist on first use - but not
                        # under a name it already uses for one of its own, in
                        # whatever language this account is in.
                        label = gmail_label(
                            add_labels, self.system_label_names(mail)
                        )
                        if label:
                            self._store(mail, num, "+X-GM-LABELS", label,
                                        f"put the label {label} on {message_id}")
                    if remove_labels and self.supports_gmail_labels:
                        label = gmail_label(
                            remove_labels, self.system_label_names(mail)
                        )
                        if label:
                            self._store(mail, num, "-X-GM-LABELS", label,
                                        f"take the label {label} off {message_id}")
        except Exception as e:
            self._problem(
                f"Could not {what} {message_id} on {self.imap_server}: {e}. "
                f"The email is untouched and will be seen again next run."
            )

    def _problem(self, text: str) -> None:
        """Record something the mailbox would not do, keeping the earlier ones.

        One line per thing that failed: a star being refused and a label being
        refused are different problems with different answers, and overwriting
        one with the other hides half of what happened.
        """
        if text not in (self.last_archive_error or ""):
            self.last_archive_error = (
                f"{self.last_archive_error}\n{text}".strip()
                if self.last_archive_error else text
            )

    def _store(self, mail, num, mode: str, value: str, what: str) -> bool:
        """One STORE. Records what happened and never raises.

        imaplib raises on a BAD reply rather than returning it, so a status
        check alone never sees the most interesting failure.
        """
        try:
            status, _ = mail.store(num, mode, value)
        except Exception as e:
            self._problem(f"The mail server would not {what}: {e}")
            return False
        if status != "OK":
            self._problem(f"The mail server refused to {what} (status {status}).")
            return False
        return True

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

import os
from pathlib import Path
from email_workflow.core.paths import resolve_project_file

# Your facts can live in the environment instead of the file. That is what
# makes them work on a scheduled cloud run: known_facts.txt is gitignored -
# it is personal - so it never reaches GitHub, and without this the model
# would arrive there knowing nothing about you and fill every reply with
# [NEEDS INPUT] placeholders.
FACTS_ENV_VAR = "KNOWN_FACTS"

DEFAULT_KNOWN_FACTS = """- User Name: [your name]
- Work Hours: 9:00 AM to 5:00 PM (Monday to Friday)
- Meeting Availability: [when you prefer to meet]
- Preferred Communication: Concise, professional tone.
- Contact Details: Email is the best contact method.
"""

class KnownFactsManager:
    def __init__(self, file_path: str = "known_facts.txt"):
        self.file_path = resolve_project_file(file_path)

    def load_facts(self) -> str:
        """The file if you have one, otherwise the environment.

        The file wins: it is the one you edit, and a stale variable left in a
        shell should never quietly override what you just typed. Nothing is
        written to disk when the facts come from the environment - on a cloud
        runner there is nowhere useful to write them.
        """
        if self.file_path.exists():
            text = self.file_path.read_text(encoding="utf-8")
            if text.strip():
                return text

        from_env = (os.getenv(FACTS_ENV_VAR) or "").strip()
        if from_env:
            return from_env

        if not self.file_path.exists():
            self.save_facts(DEFAULT_KNOWN_FACTS)
        return DEFAULT_KNOWN_FACTS

    def save_facts(self, text: str) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file_path, "w", encoding="utf-8") as f:
            f.write(text)


FACTS_MERGE_PROMPT_TEMPLATE = """Fold one new piece of information into someone's knowledge base.

This file is the ONLY thing an email assistant is allowed to state as fact about
its owner, so it is not a scratch pad - losing a line from it means the assistant
stops knowing something true about a real person.

## Rules, in order of importance
1. Keep every existing fact. The only reason to drop one is that the new
   information directly replaces it - a changed phone number, new working hours.
   When that happens, put the old line in "replaced" so the owner can see it.
2. Never invent. If the new information is vague, record exactly what was said
   and no more. Do not round a time, guess a surname or complete an address.
3. Group related things together, so the file reads like an organised note
   rather than a pile: name and contact, hours and availability, work and
   projects, money and billing, preferences.
4. One fact per line, each starting with "- ", each readable on its own without
   the line above it.
5. If the new information is already there in different words, do not add it
   twice - keep the clearer wording and say so in "what_changed".

=== THE KNOWLEDGE BASE AS IT STANDS ===
{existing}

=== THE NEW INFORMATION ===
{addition}

=== OUTPUT ===
Reply with JSON only. No prose, no markdown fence.
{{
  "facts": ["- the whole file, every line, grouped", "- ..."],
  "what_changed": "one plain sentence about what you did",
  "replaced": []
}}
"""


def facts_as_lines(text: str) -> list:
    """The individual facts in a knowledge base, blank lines and all dropped."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def append_fact(existing: str, addition: str) -> str:
    """Add something without an AI: the old file, then the new line.

    The fallback when no model is available. Dull, but it cannot lose anything,
    which is the property that matters most here.
    """
    lines = facts_as_lines(existing)
    for raw in facts_as_lines(addition):
        line = raw if raw.startswith("- ") else f"- {raw}"
        if line not in lines:
            lines.append(line)
    return "\n".join(lines) + "\n"


def facts_lost(before: str, after: str, replaced=None) -> list:
    """Facts that were in the file and are not in the new version.

    A model reorganising the file is useful; a model quietly dropping a line is
    the same bug as the one where saving replaced the whole file. Anything the
    model did not explicitly say it replaced has to be shown to the owner
    before this is written to disk.
    """
    allowed = {line.strip() for line in (replaced or [])}
    still_there = " ".join(facts_as_lines(after)).lower()
    missing = []
    for line in facts_as_lines(before):
        if line in allowed:
            continue
        core = line.lstrip("- ").strip().lower()
        if core and core not in still_there:
            missing.append(line)
    return missing


FACTS_FROM_EMAIL_PROMPT_TEMPLATE = """Find things the mailbox owner's assistant should know, from their email.

You are reading someone's incoming mail to suggest facts about THE OWNER of the
mailbox - not about the senders. These suggestions become the only things an
assistant is allowed to state as fact on their behalf, so a wrong one is worse
than no suggestion at all.

## Only suggest something when the email states it plainly
A supplier writing "as agreed, your Net-30 terms apply" states a fact about the
owner. A supplier writing "most clients choose Net-30" does not. If you are
inferring, guessing, or filling a gap, say nothing.

## Never suggest
* Anything already in the knowledge base below, in any wording.
* Anything about other people - their addresses, their phone numbers, their
  companies. This is the owner's knowledge base.
* Passwords, card numbers, account numbers, codes, or anything that would be
  harmful written down in a plain text file.
* One-off details of a single message ("Anna asked about Tuesday"). A fact is
  something still true next month.

## Good suggestions look like
- Work Hours: ...
- Office address: ...
- Project X: ...
- Billing: ...

=== WHAT THE ASSISTANT ALREADY KNOWS ===
{existing}

=== RECENT EMAIL ===
{emails}

=== OUTPUT ===
Reply with JSON only. No prose, no markdown fence. An empty list is a perfectly
good answer and much better than a guess.
{{
  "facts": ["- Work Hours: ...", "- ..."],
  "what_changed": "one sentence on where these came from",
  "replaced": []
}}
"""

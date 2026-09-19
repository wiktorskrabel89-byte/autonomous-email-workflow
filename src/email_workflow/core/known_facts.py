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

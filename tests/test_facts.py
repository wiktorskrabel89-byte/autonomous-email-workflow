"""The knowledge base: adding to it must never cost you what was already there.

The bug these exist to prevent was real and silent. "Edit your Known Facts"
took whatever you typed and wrote it over the whole file, so adding a phone
number deleted your name, your working hours and everything else. Nothing said
so, and the only copy was gone.
"""

import pytest
from typer.testing import CliRunner

from email_workflow.cli import cli as cli_module
from email_workflow.core.known_facts import (
    KnownFactsManager,
    append_fact,
    facts_as_lines,
    facts_lost,
)
from email_workflow.models.analysis import KnownFactsMerge
from email_workflow.providers.fake_ai import FakeAIProvider

runner = CliRunner()

BEFORE = "- User Name: Wiktor\n- Work Hours: 9:00 to 17:00\n- Billing: Net-30\n"


# --- the plain, no-model path -----------------------------------------------

def test_appending_keeps_every_existing_fact():
    after = append_fact(BEFORE, "my phone is 600 100 200")
    for kept in ("User Name", "Work Hours", "Billing"):
        assert kept in after, f"{kept} was lost by adding something"
    assert "- my phone is 600 100 200" in after


def test_a_bullet_is_not_doubled():
    assert append_fact("- A", "- B").count("- B") == 1
    assert append_fact("- A", "B").count("- B") == 1


def test_the_same_fact_twice_is_added_once():
    once = append_fact(BEFORE, "- Billing: Net-30")
    assert once.count("Billing: Net-30") == 1


def test_a_provider_with_no_model_still_loses_nothing():
    merged = FakeAIProvider().organise_facts(BEFORE, "I don't work Fridays")
    joined = "\n".join(merged.facts)
    for kept in ("User Name", "Work Hours", "Billing"):
        assert kept in joined
    assert "Fridays" in joined


# --- spotting a model that drops something ----------------------------------

def test_a_dropped_fact_is_spotted():
    after = "- User Name: Wiktor\n- Work Hours: 9:00 to 17:00\n"
    assert facts_lost(BEFORE, after) == ["- Billing: Net-30"]


def test_a_fact_the_model_said_it_replaced_is_not_a_loss():
    after = "- User Name: Wiktor\n- Work Hours: 8:00 to 16:00\n- Billing: Net-30\n"
    lost = facts_lost(BEFORE, after, replaced=["- Work Hours: 9:00 to 17:00"])
    assert lost == []


def test_rewording_is_not_a_loss():
    """The model is allowed to regroup and retype - only real disappearances
    count, or the warning would cry wolf on every tidy-up."""
    after = "- User Name: Wiktor\n- Work Hours: 9:00 to 17:00, Billing: Net-30\n"
    assert facts_lost(BEFORE, after) == []


def test_nothing_in_nothing_out():
    assert facts_lost("", "- A") == []
    assert facts_as_lines("\n\n  \n") == []


# --- through the real command -----------------------------------------------

@pytest.fixture()
def facts_file(tmp_path, monkeypatch):
    path = tmp_path / "known_facts.txt"
    path.write_text(BEFORE, encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "security:\n  require_login: false\n", encoding="utf-8"
    )
    resolve = lambda name: tmp_path / str(name)
    for module in ("email_workflow.cli.cli", "email_workflow.core.known_facts",
                   "email_workflow.core.auth", "email_workflow.core.usage"):
        monkeypatch.setattr(f"{module}.resolve_project_file", resolve, raising=False)
    monkeypatch.delenv("KNOWN_FACTS", raising=False)
    return path


def use_ai(monkeypatch, provider):
    monkeypatch.setattr(cli_module, "_facts_ai", lambda config: provider)


class Tidy(FakeAIProvider):
    """A well-behaved model: keeps everything and adds the new line."""

    def organise_facts(self, existing, addition):
        return KnownFactsMerge(
            facts=facts_as_lines(append_fact(existing, addition)),
            what_changed="filed it with the others",
        )


class Forgetful(FakeAIProvider):
    """A model that quietly drops a line while "tidying"."""

    def organise_facts(self, existing, addition):
        return KnownFactsMerge(facts=["- User Name: Wiktor", "- " + addition],
                               what_changed="tidied")


def test_adding_a_fact_keeps_the_others(facts_file, monkeypatch):
    """The whole bug, in one test."""
    use_ai(monkeypatch, Tidy())
    result = runner.invoke(cli_module.app, ["facts"],
                           input="1\nmy phone is 600 100 200\ny\n3\n")

    assert result.exit_code == 0
    saved = facts_file.read_text(encoding="utf-8")
    for kept in ("User Name", "Work Hours", "Billing"):
        assert kept in saved, f"adding a fact deleted: {kept}"
    assert "600 100 200" in saved


def test_the_previous_version_is_kept(facts_file, monkeypatch):
    use_ai(monkeypatch, Tidy())
    runner.invoke(cli_module.app, ["facts"], input="1\nsomething new\ny\n3\n")
    backup = facts_file.with_suffix(facts_file.suffix + ".bak")
    assert backup.exists(), "no way back from a bad edit"
    assert "Billing: Net-30" in backup.read_text(encoding="utf-8")


def test_a_model_that_loses_a_fact_is_caught_and_refused(facts_file, monkeypatch):
    use_ai(monkeypatch, Forgetful())
    # say no when it asks whether to save anyway
    result = runner.invoke(cli_module.app, ["facts"],
                           input="1\nmy phone is 600 100 200\nn\n3\n")

    assert "would disappear" in result.output
    assert "Work Hours" in result.output
    saved = facts_file.read_text(encoding="utf-8")
    assert saved == BEFORE, "the file changed after the loss was refused"


def test_you_can_still_accept_a_lossy_merge_on_purpose(facts_file, monkeypatch):
    use_ai(monkeypatch, Forgetful())
    runner.invoke(cli_module.app, ["facts"], input="1\nnew thing\ny\n3\n")
    assert "Work Hours" not in facts_file.read_text(encoding="utf-8")


def test_saying_no_at_the_end_changes_nothing(facts_file, monkeypatch):
    use_ai(monkeypatch, Tidy())
    runner.invoke(cli_module.app, ["facts"], input="1\nsomething\nn\n3\n")
    assert facts_file.read_text(encoding="utf-8") == BEFORE


def test_rewriting_everything_is_behind_its_own_confirmation(facts_file, monkeypatch):
    use_ai(monkeypatch, Tidy())
    # choose rewrite, then refuse
    runner.invoke(cli_module.app, ["facts"], input="2\nn\n3\n")
    assert facts_file.read_text(encoding="utf-8") == BEFORE


def test_just_looking_changes_nothing(facts_file, monkeypatch):
    use_ai(monkeypatch, Tidy())
    result = runner.invoke(cli_module.app, ["facts"], input="3\n")
    assert "Work Hours" in result.output
    assert facts_file.read_text(encoding="utf-8") == BEFORE


def test_an_empty_answer_does_not_wipe_anything(facts_file, monkeypatch):
    use_ai(monkeypatch, Tidy())
    runner.invoke(cli_module.app, ["facts"], input="1\n\n3\n")
    assert facts_file.read_text(encoding="utf-8") == BEFORE


def test_learning_from_email_is_hidden_and_unreachable_when_off(facts_file, monkeypatch):
    """Hidden AND unreachable. An option that still answers to a number
    nobody can see is a trap, and a gap in the numbering looks like a fault.
    """
    use_ai(monkeypatch, Tidy())
    result = runner.invoke(cli_module.app, ["facts"], input="3\n")

    assert "Learn from my recent email" not in result.output
    assert "3. Back" in result.output, "the numbering must not skip a number"
    assert facts_file.read_text(encoding="utf-8") == BEFORE


def test_learning_from_email_appears_once_switched_on(facts_file, monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "security:\n  require_login: false\nautomation:\n"
        "  learn_facts_from_email: true\n",
        encoding="utf-8",
    )
    use_ai(monkeypatch, Tidy())
    result = runner.invoke(cli_module.app, ["facts"], input="4\n")
    assert "Learn from my recent email" in result.output

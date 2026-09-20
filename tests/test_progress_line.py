"""The progress bar must never print itself twice.

What he saw: at the email "Otwieramy rezerwacje na ferie! [skier emoji]" the bar
stopped moving and printed a few hundred copies of itself instead.

The cause is one cell of disagreement about how wide that emoji is. U+26F7
followed by U+FE0F is an emoji-presentation sequence: rich measures it as ONE
cell, a terminal draws TWO. The bar is sized to fill the line exactly, so one
extra cell pushes it past the edge, the line wraps, and a wrapped line cannot
be overwritten in place - so every redraw, twelve a second, lands on a new line.

Emoji that both agree on (a rocket, a pair of eyes) were never a problem, which
is why only this one email spammed.
"""

from rich.text import Text

from email_workflow.cli.cli import _one_line, _short

# His actual subject lines, from the run he pasted.
FERIE = "Otwieramy rezerwacje na ferie! ⛷️"
ROCKET = "Czas na Twój upgrade! \U0001f680"
EYES = "\U0001f440 Hmm, ciekawe."
SKI_NO_SELECTOR = "ferie ⛷"


def terminal_cells(text: str) -> int:
    """What a terminal draws, as opposed to what rich counts.

    A symbol carrying U+FE0F is drawn as an emoji - two cells wide - however
    narrow the character is on its own.
    """
    width = 0
    previous = ""
    for ch in text:
        if ch == "️":
            # The selector itself is zero-wide but widens what came before.
            if previous and Text(previous).cell_len == 1:
                width += 1
            continue
        width += Text(ch).cell_len
        previous = ch
    return width


def test_the_emoji_that_caused_it_is_measured_differently_by_the_two():
    """The premise of the whole fix. If this ever stops being true, the rest
    of this file is testing nothing."""
    assert Text(FERIE).cell_len == 32
    assert terminal_cells(FERIE) == 33, "the terminal draws one cell more"


def test_after_cleaning_rich_and_the_terminal_agree():
    cleaned = _one_line(FERIE)
    assert Text(cleaned).cell_len == terminal_cells(cleaned), (
        "one cell of disagreement is all it takes to wrap the line"
    )


def test_the_subject_is_still_readable():
    assert _one_line(FERIE).startswith("Otwieramy rezerwacje na ferie!")


def test_emoji_the_terminal_and_rich_agree_on_are_left_alone():
    """Not a licence to strip every emoji - these never caused a problem."""
    assert "\U0001f680" in _one_line(ROCKET)
    assert "\U0001f440" in _one_line(EYES)
    for text in (ROCKET, EYES):
        assert Text(_one_line(text)).cell_len == terminal_cells(_one_line(text))


def test_a_bare_symbol_without_the_selector_is_removed_too():
    """U+26F7 alone is drawn wide by some terminals and narrow by others.
    A progress line is not the place to find out which."""
    cleaned = _one_line(SKI_NO_SELECTOR)
    assert Text(cleaned).cell_len == terminal_cells(cleaned)


def test_a_subject_cannot_smuggle_in_a_second_line():
    """Subjects are arbitrary text from strangers, and this one goes into a
    bar that redraws in place."""
    assert "\n" not in _one_line("first line\nsecond line")
    assert _one_line("first line\nsecond line") == "first line second line"


def test_a_subject_that_cleans_away_to_nothing_still_says_something():
    assert _short("️‍") == "(no subject)"
    assert _short("") == "(no subject)"


def test_long_subjects_are_still_cut_to_length():
    assert len(_short("x" * 200, width=40)) == 40

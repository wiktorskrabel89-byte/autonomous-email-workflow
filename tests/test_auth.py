"""Tests for the login gate.

The important properties: the password never lands on disk, the stored form
cannot be reversed, and a wrong username or password is refused.
"""

import json

import pytest

from email_workflow.core.auth import (
    ITERATIONS,
    MIN_PASSWORD_LENGTH,
    AuthManager,
    hash_password,
)

PASSWORD = "correct horse battery"
USERNAME = "testuser"


@pytest.fixture()
def auth(tmp_path):
    return AuthManager(store_path=str(tmp_path / "auth.json"))


# --- lifecycle --------------------------------------------------------------

def test_starts_unconfigured(auth):
    assert not auth.is_configured()
    assert auth.username == ""


def test_configured_after_setting_credentials(auth):
    auth.set_credentials(USERNAME, PASSWORD)
    assert auth.is_configured()
    assert auth.username == USERNAME


def test_disable_removes_the_login(auth):
    auth.set_credentials(USERNAME, PASSWORD)
    auth.disable()
    assert not auth.is_configured()


def test_disable_on_a_fresh_store_does_not_raise(auth):
    auth.disable()  # must not explode when there is nothing to remove


# --- verification -----------------------------------------------------------

def test_correct_credentials_verify(auth):
    auth.set_credentials(USERNAME, PASSWORD)
    assert auth.verify(USERNAME, PASSWORD)


def test_wrong_password_is_refused(auth):
    auth.set_credentials(USERNAME, PASSWORD)
    assert not auth.verify(USERNAME, "wrong password entirely")


def test_wrong_username_is_refused(auth):
    auth.set_credentials(USERNAME, PASSWORD)
    assert not auth.verify("someone-else", PASSWORD)


def test_username_is_case_insensitive_but_password_is_not(auth):
    auth.set_credentials(USERNAME, PASSWORD)
    assert auth.verify("TESTUSER", PASSWORD)
    assert not auth.verify(USERNAME, PASSWORD.upper())


def test_username_whitespace_is_tolerated(auth):
    auth.set_credentials(USERNAME, PASSWORD)
    assert auth.verify("  testuser  ", PASSWORD)


def test_verify_fails_cleanly_when_nothing_is_configured(auth):
    assert not auth.verify(USERNAME, PASSWORD)


def test_almost_right_password_is_still_refused(auth):
    auth.set_credentials(USERNAME, PASSWORD)
    assert not auth.verify(USERNAME, PASSWORD + "!")
    assert not auth.verify(USERNAME, PASSWORD[:-1])


# --- what is actually written to disk ---------------------------------------

def test_password_is_never_written_to_disk(auth):
    auth.set_credentials(USERNAME, PASSWORD)
    raw = auth.store_path.read_text(encoding="utf-8")
    assert PASSWORD not in raw
    for word in PASSWORD.split():
        assert word not in raw


def test_stored_record_has_salt_hash_and_cost(auth):
    auth.set_credentials(USERNAME, PASSWORD)
    data = json.loads(auth.store_path.read_text(encoding="utf-8"))
    assert set(data) == {"username", "salt", "hash", "iterations"}
    assert data["iterations"] == ITERATIONS
    assert len(bytes.fromhex(data["salt"])) == 16
    assert len(data["hash"]) == 64  # sha256, hex


def test_same_password_twice_produces_different_hashes(auth, tmp_path):
    auth.set_credentials(USERNAME, PASSWORD)
    first = json.loads(auth.store_path.read_text(encoding="utf-8"))

    other = AuthManager(store_path=str(tmp_path / "other.json"))
    other.set_credentials(USERNAME, PASSWORD)
    second = json.loads(other.store_path.read_text(encoding="utf-8"))

    assert first["salt"] != second["salt"], "each login needs its own salt"
    assert first["hash"] != second["hash"], "identical passwords must not look identical"


def test_hash_is_deterministic_for_a_given_salt():
    salt = b"0123456789abcdef"
    assert hash_password(PASSWORD, salt) == hash_password(PASSWORD, salt)
    assert hash_password(PASSWORD, salt) != hash_password(PASSWORD + "x", salt)


# --- input rules ------------------------------------------------------------

def test_short_password_is_rejected(auth):
    with pytest.raises(ValueError, match=str(MIN_PASSWORD_LENGTH)):
        auth.set_credentials(USERNAME, "a" * (MIN_PASSWORD_LENGTH - 1))
    assert not auth.is_configured(), "a rejected password must not be stored"


def test_password_of_exactly_the_minimum_length_is_accepted(auth):
    auth.set_credentials(USERNAME, "a" * MIN_PASSWORD_LENGTH)
    assert auth.verify(USERNAME, "a" * MIN_PASSWORD_LENGTH)


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_username_is_rejected(auth, bad):
    with pytest.raises(ValueError):
        auth.set_credentials(bad, PASSWORD)


def test_changing_the_password_invalidates_the_old_one(auth):
    auth.set_credentials(USERNAME, PASSWORD)
    auth.set_credentials(USERNAME, "a brand new password")
    assert not auth.verify(USERNAME, PASSWORD)
    assert auth.verify(USERNAME, "a brand new password")


# --- damaged store ----------------------------------------------------------

def test_corrupt_file_reads_as_unconfigured(auth):
    auth.store_path.parent.mkdir(parents=True, exist_ok=True)
    auth.store_path.write_text("this is not json", encoding="utf-8")
    assert not auth.is_configured()
    assert not auth.verify(USERNAME, PASSWORD)


def test_truncated_record_reads_as_unconfigured(auth):
    auth.store_path.parent.mkdir(parents=True, exist_ok=True)
    auth.store_path.write_text(json.dumps({"username": "testuser"}), encoding="utf-8")
    assert not auth.is_configured()


def test_unicode_password_round_trips(auth):
    secret = "zażółć gęślą jaźń 123"
    auth.set_credentials(USERNAME, secret)
    assert auth.verify(USERNAME, secret)
    assert not auth.verify(USERNAME, "zazolc gesla jazn 123")

"""Cisco type 9 (scrypt) secret verification.

The known-answer vector comes from hashcat module 09300 (ST_PASS=hashcat,
ST_HASH=$9$87023684531115$phio0TBQwaO7KZ8toQFyGFyDvyOzidaypRWN0uKX0hU).  A
passing test means the salt handling (raw 14 chars) and the itoa64 base64
encoding match real Cisco IOS type 9 hashes rather than an invented format.
"""
from __future__ import annotations

import pytest

from makemeswitch.core import cisco9

HASHCAT_HASH = "$9$87023684531115$phio0TBQwaO7KZ8toQFyGFyDvyOzidaypRWN0uKX0hU"


def test_hashcat_known_answer_vector():
    assert cisco9.verify("hashcat", HASHCAT_HASH) is True
    assert cisco9.verify("wrong-password", HASHCAT_HASH) is False


def test_round_trip_generated_hash_verifies():
    hashed = cisco9.make_hash("MakeMeSwitch")
    assert cisco9.TYPE9_RE.match(hashed)
    assert cisco9.verify("MakeMeSwitch", hashed)
    assert not cisco9.verify("makemeswitch", hashed)


def test_plaintext_is_rejected_with_actionable_message():
    with pytest.raises(cisco9.Cisco9Error) as excinfo:
        cisco9.validate_hash("MakeMeSwitch")
    message = str(excinfo.value)
    assert "not" not in message or "plaintext" in message
    assert "secret 0" in message
    assert "MakeMeSwitch" not in message


def test_malformed_hash_is_rejected():
    for value in (
        "$9$short$" + "A" * 43,
        "$9$87023684531115$" + "A" * 42,
        "$9$87023684531115$" + "A" * 44,
        "$9$87023684531115$" + "!" * 43,
        "87023684531115",
    ):
        with pytest.raises(cisco9.Cisco9Error):
            cisco9.validate_hash(value)

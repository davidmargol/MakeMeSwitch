"""CLI parser tests: modes, abbreviations, help, errors."""
from makemeswitch.cli.commands import COMMANDS
from makemeswitch.cli.parser import CommandParser

PARSER = CommandParser(COMMANDS)


def resolve(line, mode="priv"):
    return PARSER.resolve(PARSER.tokenize(line), mode)


def test_exact_command():
    match = resolve("show version")
    assert match.error is None
    assert match.spec.path == ("show", "version")


def test_abbreviation_resolves_uniquely():
    match = resolve("sh ver")
    assert match.error is None
    assert match.spec.path == ("show", "version")


def test_dynamic_argument_is_captured():
    match = resolve("interface ethernet 1/3", "config")
    assert match.error is None
    assert match.argv == ["1/3"]


def test_trailing_description():
    match = resolve("description uplink to core switch", "if")
    assert match.error is None
    assert match.argv == ["uplink", "to", "core", "switch"]


def test_ambiguous_prefix():
    match = resolve("show i", "priv")
    assert match.error in ("ambiguous", "incomplete")
    assert "interfaces" in match.candidates or "ip" in match.candidates


def test_invalid_command():
    match = resolve("frobnicate")
    assert match.error == "invalid"


def test_modes_are_isolated():
    assert resolve("hostname X", "priv").error == "invalid"
    assert resolve("hostname X", "config").error is None
    assert resolve("show version", "config").error == "invalid"


def test_help_candidates():
    rows = dict(PARSER.help_for(["show"], "priv"))
    assert "interfaces" in rows and "vlan" in rows and "version" in rows


def test_completion():
    completions = PARSER.complete("in", "sh in", "priv")
    assert "interfaces" in completions


def test_all_commands_have_help():
    for spec in COMMANDS:
        assert spec.help, f"missing help for {' '.join(spec.path)}"

"""Hostnames may be FQDNs (dots), like real Cisco ``hostname``."""
import pytest

from makemeswitch.core.models import ConfigError, SwitchConfig

VALID = [
    "Switch",
    "sw1",
    "cisco.skippy.link",
    "mms-01.lab.example.com",
    "a.b",
    "Ethernet-Switch.core",
]
INVALID = [
    "",
    "-switch",
    "switch-",
    ".switch",
    "switch.",
    "sw..itch",
    "sw itch",
    "sw_itch",
    "a" * 64,
    "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 63,
]


@pytest.mark.parametrize("name", VALID)
def test_valid_hostnames(name):
    assert SwitchConfig(hostname=name).hostname == name


@pytest.mark.parametrize("name", INVALID)
def test_invalid_hostnames(name):
    with pytest.raises(ConfigError, match="invalid hostname"):
        SwitchConfig(hostname=name)


def test_dotted_hostname_survives_round_trip():
    cfg = SwitchConfig(hostname="cisco.skippy.link")
    assert SwitchConfig.from_dict(cfg.to_dict()).hostname == "cisco.skippy.link"


def test_prompt_uses_dotted_hostname():
    from makemeswitch.cli.modes import Session

    class EngineStub:
        config = SwitchConfig(hostname="cisco.skippy.link")

    session = Session(engine=EngineStub(), role="admin", interactive=False)
    session.mode = "priv"
    assert session.prompt == "cisco.skippy.link#"

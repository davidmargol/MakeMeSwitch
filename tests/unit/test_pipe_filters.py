"""Cisco-style ``|`` pipe filters."""
import pytest

from makemeswitch.cli.pipe import FILTER_HELP, apply_filters, split_pipe
from makemeswitch.core.models import ConfigError

RUN = [
    "Building configuration...",
    "hostname cisco.skippy.link",
    "vlan 10",
    " name USERS",
    "vlan 20",
    " name VOICE",
    "interface Ethernet1",
    " switchport access vlan 10",
    "interface Ethernet2",
    " switchport mode trunk",
]


def test_split_without_pipe_returns_none():
    assert split_pipe("show running-config") == ("show running-config", None)


def test_split_single_filter():
    command, filters = split_pipe("show running-config | include vlan")
    assert command == "show running-config"
    assert [(f.name, f.arg) for f in filters] == [("include", "vlan")]


def test_split_chained_filters():
    _, filters = split_pipe("sh run | include vlan | count vlan")
    assert [(f.name, f.arg) for f in filters] == [("include", "vlan"), ("count", "vlan")]


def test_split_keeps_quoted_argument_together():
    _, filters = split_pipe('sh run | include "interface Ethernet"')
    assert filters[0].arg == "interface Ethernet"


def test_split_trailing_pipe_is_empty_list():
    command, filters = split_pipe("show run | ")
    assert command == "show run" and filters == []


def test_unknown_filter_is_rejected():
    with pytest.raises(ConfigError, match="unknown pipe filter: grepx"):
        split_pipe("show run | grepx vlan")


def test_include_and_exclude():
    _, filters = split_pipe("sh run | include vlan")
    assert apply_filters(RUN, filters).splitlines() == [
        "vlan 10",
        "vlan 20",
        " switchport access vlan 10",
    ]
    _, filters = split_pipe("sh run | exclude ^vlan")
    assert "vlan 10" not in apply_filters(RUN, filters).splitlines()
    assert "hostname cisco.skippy.link" in apply_filters(RUN, filters).splitlines()


def test_begin_keeps_matching_line_and_rest():
    _, filters = split_pipe("sh run | begin interface Ethernet2")
    out = apply_filters(RUN, filters).splitlines()
    assert out[0] == "interface Ethernet2"
    assert out[-1] == " switchport mode trunk"


def test_section_returns_only_the_block():
    _, filters = split_pipe("sh run | section interface Ethernet1")
    assert apply_filters(RUN, filters).splitlines() == [
        "interface Ethernet1",
        " switchport access vlan 10",
    ]


def test_count_reports_number_of_matches():
    _, filters = split_pipe("sh run | count vlan")
    assert apply_filters(RUN, filters) == "3"


def test_redirect_writes_and_stays_silent(tmp_path):
    target = tmp_path / "running-config.txt"
    _, filters = split_pipe(f"sh run | redirect {target}")
    assert apply_filters(RUN, filters) is None
    assert target.read_text().splitlines()[1] == "hostname cisco.skippy.link"


def test_redirect_overwrites_previous_content(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("stale\n")
    apply_filters(["one"], split_pipe(f"sh run | redirect {target}")[1])
    assert target.read_text() == "one\n"


def test_append_keeps_previous_content(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("first\n")
    assert apply_filters(["second"], split_pipe(f"sh run | append {target}")[1]) is None
    assert target.read_text() == "first\nsecond\n"


def test_tee_writes_and_prints(tmp_path):
    target = tmp_path / "tee.txt"
    out = apply_filters(RUN, split_pipe(f"sh run | tee {target}")[1])
    assert "hostname cisco.skippy.link" in out
    assert "hostname cisco.skippy.link" in target.read_text()


def test_pipe_chain_runs_in_order(tmp_path):
    target = tmp_path / "chain.txt"
    out = apply_filters(RUN, split_pipe(f"sh run | include vlan | exclude Ethernet | tee {target}")[1])
    assert out.splitlines() == ["vlan 10", "vlan 20", " switchport access vlan 10"]
    assert target.read_text().splitlines() == out.splitlines()


def test_unsupported_scheme_is_explained():
    with pytest.raises(ConfigError, match="tftp"):
        apply_filters(["x"], split_pipe("sh run | redirect tftp://10.0.0.5/x")[1])


def test_redirect_must_be_last():
    with pytest.raises(ConfigError, match="last filter"):
        apply_filters(["x"], split_pipe("sh run | redirect /tmp/x | include y")[1])


def test_invalid_regex_is_explained():
    with pytest.raises(ConfigError, match="invalid regular expression"):
        apply_filters(["x"], split_pipe("sh run | include [unclosed")[1])


def test_filter_without_argument_is_explained():
    with pytest.raises(ConfigError, match="needs an argument"):
        apply_filters(["x"], split_pipe("sh run | include")[1])


def test_format_uses_spec_file(tmp_path):
    spec = tmp_path / "ports.fmt"
    spec.write_text("# port template\nport={0} state={1}\n")
    _, filters = split_pipe(f"sh | format {spec}")
    assert apply_filters(["Ethernet1 up"], filters) == "port=Ethernet1 state=up"


def test_format_missing_spec_is_explained(tmp_path):
    with pytest.raises(ConfigError, match="format spec file not found"):
        apply_filters(["x"], split_pipe(f"sh | format {tmp_path / 'nope.fmt'}")[1])


def test_help_matches_ios_wording():
    words = dict(FILTER_HELP)
    assert list(words) == [
        "append",
        "begin",
        "count",
        "exclude",
        "format",
        "include",
        "redirect",
        "section",
        "tee",
    ]
    assert words["append"] == "Append redirected output to URL (URLs supporting append operation only)"
    assert words["tee"] == "Copy output to URL"


def test_filter_candidates_after_bare_pipe_lists_all():
    from makemeswitch.cli.pipe import filter_candidates

    assert [name for name, _ in filter_candidates("sh run |")] == [
        "append",
        "begin",
        "count",
        "exclude",
        "format",
        "include",
        "redirect",
        "section",
        "tee",
    ]


def test_filter_candidates_filter_by_prefix():
    from makemeswitch.cli.pipe import filter_candidates

    assert [name for name, _ in filter_candidates("sh run | inc")] == ["include"]
    assert filter_candidates("sh run | zzz") == []


def test_filter_candidates_stop_after_argument():
    from makemeswitch.cli.pipe import filter_candidates

    assert filter_candidates("sh run | include vlan") == []
    assert [name for name, _ in filter_candidates("sh run | include vlan | co")] == ["count"]


def test_next_filter_argument():
    from makemeswitch.cli.pipe import next_filter_argument

    assert next_filter_argument("sh run | include") == "include"
    assert next_filter_argument("sh run | include vlan") is None


def test_filter_names_accept_ios_abbreviations():
    from makemeswitch.cli.pipe import split_pipe

    for line, expected in [
        ("sh run | inc user", "include"),
        ("sh run | exc vlan", "exclude"),
        ("sh run | beg interface", "begin"),
        ("sh run | sec banner", "section"),
        ("sh run | cou vlan", "count"),
    ]:
        _, filters = split_pipe(line)
        assert filters[0].name == expected, line


def test_ambiguous_or_unknown_filter_is_rejected():
    import pytest

    from makemeswitch.cli.pipe import split_pipe
    from makemeswitch.core.models import ConfigError

    with pytest.raises(ConfigError):
        split_pipe("sh run | zzz vlan")

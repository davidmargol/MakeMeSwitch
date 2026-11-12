"""OVS JSON regressions; no live daemon or NIC is used."""
import json

import pytest

from makemeswitch.backends.ovs import OvsBackend, _parse_ovs_json


@pytest.mark.parametrize('trunks,expected', [(10, [10]), (['set', []], []), (['set', [10, 20]], [10, 20])])
def test_port_details_normalizes_integer_sets(monkeypatch, trunks, expected):
    ovs = OvsBackend()
    monkeypatch.setattr(ovs, 'bridge_exists', lambda: True)
    monkeypatch.setattr(ovs, 'list_ports', lambda: ['eth9'])
    ports = {'headings': ['name', 'tag', 'trunks', 'interfaces'], 'data': [['eth9', ['set', []], trunks, ['uuid', 'a']]]}
    interfaces = {'headings': ['_uuid', 'name', 'type', 'mtu_request'], 'data': [[['uuid', 'a'], 'eth9', '', 1500]]}
    monkeypatch.setattr(ovs, '_run', lambda args: json.dumps(ports if args[-1] == 'port' else interfaces))
    assert ovs.port_details()['eth9']['trunks'] == expected


def test_uuid_set_members_are_decoded():
    payload = {'headings': ['interfaces'], 'data': [[['set', [['uuid', 'a'], ['uuid', 'b']]]]]}
    assert _parse_ovs_json(json.dumps(payload)) == [{'interfaces': ['a', 'b']}]

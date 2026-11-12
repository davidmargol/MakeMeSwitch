"""Discovery must scale to any number of NICs and ignore bridge internals."""
import pytest

from makemeswitch.core import interfaces as netif
from makemeswitch.core.interfaces import LinkInfo


def _nic(name, mac, identity, kind="pci"):
    return LinkInfo(name=name, mac=mac, identity=identity, kind=kind, operstate="up", carrier=True)


def test_spanning_tree_of_nics_is_never_a_port(monkeypatch):
    """ovs-system / mms0 carry MACs but must never become switch ports."""
    links = [
        _nic("eth0", "aa:00:00:00:00:00", "pci:0000:00:12.0"),          # management
        _nic("ens19", "aa:00:00:00:00:19", "pci:0000:00:13.0"),
        LinkInfo(name="ovs-system", mac="26:c2:34:85:cd:32", kind="virtual"),
        LinkInfo(name="mms0", mac="bc:24:11:13:b4:55", kind="virtual"),
        LinkInfo(name="docker0", mac="02:42:ac:11:00:01", kind="virtual", identity="mac:02:42:ac:11:00:01"),
        LinkInfo(name="veth1a2b3c", mac="aa:bb:cc:dd:ee:ff", kind="virtual"),
        LinkInfo(name="ztabc12345", mac="aa:bb:cc:dd:ee:01", kind="virtual"),
    ]
    monkeypatch.setattr(netif, "list_links", lambda: links)
    monkeypatch.setattr(netif, "protected_interfaces", lambda extra=None: ["eth0"])

    names = [link.name for link in netif.physical_links()]
    assert names == ["ens19"]

    names = [link.name for link in netif.physical_links(exclude={"mms0"})]
    assert names == ["ens19"]


def test_nics_without_pci_topology_are_kept(monkeypatch):
    """Virtio/hypervisor NICs without a resolvable path are still real ports."""
    links = [
        _nic("eth1", "aa:00:00:00:00:01", None, kind="virtual"),
        _nic("enp1s3", "aa:00:00:00:00:03", None, kind="virtual"),
        LinkInfo(name="sit0", mac="", kind="virtual"),
    ]
    monkeypatch.setattr(netif, "list_links", lambda: links)
    monkeypatch.setattr(netif, "protected_interfaces", lambda extra=None: [])
    assert [link.name for link in netif.physical_links()] == ["eth1", "enp1s3"]


def test_many_nics_are_all_discovered(monkeypatch):
    links = [_nic(f"eth{i}", f"aa:00:00:00:01:{i:02x}", None, kind="virtual") for i in range(1, 65)]
    monkeypatch.setattr(netif, "list_links", lambda: links)
    monkeypatch.setattr(netif, "protected_interfaces", lambda extra=None: [])
    assert len(netif.physical_links()) == 64


def test_internal_link_names():
    for name in ("ovs-system", "mms0", "docker0", "br-ab12", "veth0", "ztxyz", "tun0", "bond0"):
        assert netif.is_internal_link(name), name
    for name in ("eth0", "ens19", "enp1s1", "enx001122", "Ethernet7"):
        assert not netif.is_internal_link(name), name

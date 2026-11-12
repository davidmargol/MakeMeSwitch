#!/bin/bash
# Demo/lab environment: 6 virtual Ethernet ports wired into MakeMeSwitch.
set -e
for i in 1 2 3 4 5 6; do
  ip netns del d$i 2>/dev/null || true
  ip link del osw$i 2>/dev/null || true
  ip netns add d$i
  ip link add osw$i type veth peer name p$i
  ip link set p$i netns d$i
  ip link set osw$i up
  ip netns exec d$i ip link set p$i up
  ip netns exec d$i ip link set lo up
done
ip netns exec d1 ip addr add 10.0.10.11/24 dev p1
ip netns exec d2 ip addr add 10.0.10.12/24 dev p2
ip netns exec d3 ip addr add 10.0.20.13/24 dev p3
ip netns exec d4 ip addr add 10.0.20.14/24 dev p4
ip netns exec d5 ip addr add 10.0.30.15/24 dev p5
ip netns exec d6 ip addr add 10.0.30.16/24 dev p6

# Continuous traffic so the dashboard shows real counters.
ip netns exec d1 setsid ping -i 0.05 -q 10.0.10.12 >/dev/null 2>&1 &
ip netns exec d3 setsid ping -i 0.10 -q 10.0.20.14 >/dev/null 2>&1 &
ip netns exec d5 setsid ping -i 0.20 -q 10.0.30.16 >/dev/null 2>&1 &
echo "demo topology ready"

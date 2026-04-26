#!/bin/bash
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root"
   exit 1
fi

if [ -z "$1" ]; then
	echo "Error: no interface specified"
	exit 1
fi

INTERFACE=$1

echo "Setting up ingress control on $INTERFACE"

sudo modprobe ifb
sudo ip link add ifb0 type ifb
sudo ip link set ifb0 up

sudo tc qdisc add dev $INTERFACE ingress

tc filter add dev $INTERFACE parent ffff: protocol ip u32 match u32 0 0 \
    action mirred egress redirect dev ifb0

ethtool -K $INTERFACE tso off gso off gro off lro off

# 1. Increase the Max/Default Buffer Sizes (Memory)
# These allow for larger TCP windows (bandwidth-delay product)
sysctl -w net.core.rmem_max=67108864
sysctl -w net.core.wmem_max=67108864
sysctl -w net.core.rmem_default=3145728
sysctl -w net.core.wmem_default=3145728

# 2. Increase TCP Mem limits (min, pressure, max) in 4KB pages
# This gives the TCP stack more room to breathe overall
sysctl -w net.ipv4.tcp_rmem='4096 87380 67108864'
sysctl -w net.ipv4.tcp_wmem='4096 65536 67108864'

# 3. Expand Packet Queues
# netdev_max_backlog: how many packets the CPU can queue when the NIC is faster
# tcp_max_syn_backlog: prevents drops during initial connection handshakes
sysctl -w net.core.netdev_max_backlog=10000
sysctl -w net.ipv4.tcp_max_syn_backlog=8192


echo "Done. Ingress traffic on $INTERFACE is now routed through ifb0."
echo "Check status with: tc -s qdisc show dev ifb0"

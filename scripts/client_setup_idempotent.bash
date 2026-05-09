#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "This script must be run as root. Use: sudo $0 <interface>" >&2
  exit 1
fi

if [[ $# -ne 1 || -z "${1:-}" ]]; then
  echo "Usage: sudo $0 <client-interface>" >&2
  echo "Example: sudo $0 ens160" >&2
  exit 1
fi

INTERFACE="$1"
IFB_DEV="ifb0"

if ! ip link show "${INTERFACE}" >/dev/null 2>&1; then
  echo "Error: interface '${INTERFACE}' does not exist. Check with: ip addr" >&2
  exit 1
fi

echo "Setting up client ingress shaping on ${INTERFACE} via ${IFB_DEV}"

modprobe ifb

if ! ip link show "${IFB_DEV}" >/dev/null 2>&1; then
  ip link add "${IFB_DEV}" type ifb
fi
ip link set "${IFB_DEV}" up

# Make the script safe to rerun after reboot or failed setup.
tc qdisc del dev "${INTERFACE}" ingress >/dev/null 2>&1 || true
tc qdisc add dev "${INTERFACE}" ingress

tc filter add dev "${INTERFACE}" parent ffff: protocol ip u32 match u32 0 0 \
  action mirred egress redirect dev "${IFB_DEV}"

# Disable hardware offloading where supported. Some virtual NICs do not support
# every flag, so warn rather than failing the whole setup.
for flag in tso gso gro lro; do
  if ! ethtool -K "${INTERFACE}" "${flag}" off >/dev/null 2>&1; then
    echo "Warning: could not disable ${flag} on ${INTERFACE}; continuing." >&2
  fi
done

# Increase TCP memory/buffer limits so the emulated link is the intended bottleneck.
sysctl -w net.core.rmem_max=67108864
sysctl -w net.core.wmem_max=67108864
sysctl -w net.core.rmem_default=3145728
sysctl -w net.core.wmem_default=3145728
sysctl -w net.ipv4.tcp_rmem='4096 87380 67108864'
sysctl -w net.ipv4.tcp_wmem='4096 65536 67108864'
sysctl -w net.core.netdev_max_backlog=10000
sysctl -w net.ipv4.tcp_max_syn_backlog=8192

echo "Done. Ingress traffic on ${INTERFACE} is redirected through ${IFB_DEV}."
echo "Check client shaping status with: tc -s qdisc show dev ${IFB_DEV}"

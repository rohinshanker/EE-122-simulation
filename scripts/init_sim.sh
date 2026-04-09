#!/usr/bin/env bash
set -Eeuo pipefail

# Quick start:
#   first: 
#     Server: ./init_sim.sh --role server
#     Client: ./init_sim.sh --role client --iface eth0 --server 10.0.0.2 --smoke-test --expect-cc reno,cubic,bbr
#   next:
#     On server: iperf3 -s
#     On client: sudo ./scripts/run_trial.sh ...

# Usage Guide:
# 1) Make script executable by running the following: chmod +x init_sim.sh
# 2) run it in one of these modes:
#    a) Server-only validation (run this on the server VM):
#       ./init_sim.sh --role server
#    b) Client-only validation (run this on the client VM, replace iface and server IP as needed):
#       ./init_sim.sh --role client --iface eth0 --server
#    c) Full validation (run this on a single machine that will act as both client and server, with optional smoke test):
#       ./init_sim.sh --role both --iface eth0 --server
# 3) example usage:
#    a) Validate the server VM:
#         ./init_sim.sh --role server
#    b) Validate the client VM on interface eth0 and check that the server is reachable:
#         ./init_sim.sh --role client --iface eth0 --server 10.0.0.2
#    c) Validate the client VM and run a safe tc/netem smoke test on loopback:
#         ./init_sim.sh --role client --iface eth0 --server 10.0.0.2 --smoke-test
#    d) Require specific congestion control algorithms:
#         ./init_sim.sh --role client --expect-cc reno,cubic,bbr
#    e) Choose a custom project directory for results and logs:
#         ./init_sim.sh --role client --project-root /path/to/project

# List of flags:
# --role <client|server|both>   Which machine is being validated
# --iface <name>                Interface to use on the client, for example eth0
# --server <ip>                 Optional server IP for reachability testing
# --project-root <path>         Where results/ and logs/ folders are created
# --expect-cc <a,b,c>           Required TCP congestion control algorithms
# --smoke-test                  Apply a temporary netem qdisc on loopback to verify tc works
# --help                        Show help



# ============================================================
# TCP Congestion Control: Bootstrap / environment validation script
# What it checks:
#   1) Linux environment
#   2) sudo / privilege availability
#   3) Required commands (tc, iperf3, ping, sysctl, ip)
#   4) Network interface discovery / validation
#   5) Available TCP congestion control algorithms
#   6) tc/netem readiness (+ optional smoke test on loopback)
#   7) Optional reachability to a server VM
#   8) Project directory structure
# ============================================================

ROLE="both"                       # client | server | both
IFACE=""
SERVER_IP=""
PROJECT_ROOT="$(pwd)"
SMOKE_TEST=0
EXPECT_CC="reno,cubic"           # comma-separated list of required CCAs
RECOMMEND_CC="bbr"               # warned about if missing
PING_COUNT=2

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
SUDO=""
APPLIED_QDISC_DEV=""

if [[ -t 1 ]]; then
  C_BLUE=$'\033[1;34m'
  C_GREEN=$'\033[1;32m'
  C_YELLOW=$'\033[1;33m'
  C_RED=$'\033[1;31m'
  C_BOLD=$'\033[1m'
  C_RESET=$'\033[0m'
else
  C_BLUE=""
  C_GREEN=""
  C_YELLOW=""
  C_RED=""
  C_BOLD=""
  C_RESET=""
fi

usage() {
  cat <<EOF
Usage: $0 [options]

Options:
  --role <client|server|both>   Which machine this is validating (default: both)
  --iface <name>                Interface to use on the client side (ex: eth0)
  --server <ip>                 Optional server IP to ping
  --project-root <path>         Directory where results/logs folders are created
  --expect-cc <a,b,c>           Required congestion control algorithms
  --smoke-test                  Run a temporary tc/netem smoke test on loopback
  --help                        Show this help

Examples:
  $0 --role server
  $0 --role client --iface eth0 --server 10.0.0.2
  $0 --role client --iface eth0 --server 10.0.0.2 --smoke-test --expect-cc reno,cubic,bbr
EOF
}

stage() {
  printf "\n${C_BLUE}${C_BOLD}=== Stage %s: %s ===${C_RESET}\n" "$1" "$2"
}

info() {
  printf "  [..] %s\n" "$*"
}

pass() {
  printf "  ${C_GREEN}[OK]${C_RESET} %s\n" "$*"
  PASS_COUNT=$((PASS_COUNT + 1))
}

warn() {
  printf "  ${C_YELLOW}[WARN]${C_RESET} %s\n" "$*"
  WARN_COUNT=$((WARN_COUNT + 1))
}

fail() {
  printf "  ${C_RED}[FAIL]${C_RESET} %s\n" "$*"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

cleanup() {
  if [[ -n "$APPLIED_QDISC_DEV" ]]; then
    ${SUDO:+$SUDO }tc qdisc del dev "$APPLIED_QDISC_DEV" root >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

require_cmd() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "Found command: $cmd"
  else
    fail "Missing required command: $cmd"
  fi
}

optional_cmd() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "Found optional command: $cmd"
  else
    warn "Optional command not found: $cmd"
  fi
}

contains_word() {
  local haystack="$1"
  local needle="$2"
  [[ " $haystack " == *" $needle "* ]]
}

trim_spaces() {
  echo "$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

# -----------------------------
# Parse arguments
# -----------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)
      ROLE="${2:-}"
      shift 2
      ;;
    --iface)
      IFACE="${2:-}"
      shift 2
      ;;
    --server)
      SERVER_IP="${2:-}"
      shift 2
      ;;
    --project-root)
      PROJECT_ROOT="${2:-}"
      shift 2
      ;;
    --expect-cc)
      EXPECT_CC="${2:-}"
      shift 2
      ;;
    --smoke-test)
      SMOKE_TEST=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo
      usage
      exit 1
      ;;
  esac
done

case "$ROLE" in
  client|server|both) ;;
  *)
    echo "Invalid --role value: $ROLE"
    usage
    exit 1
    ;;
esac

# -----------------------------
# Stage 1: OS / shell sanity
# -----------------------------
stage 1 "Operating system and shell checks"
OS_NAME="$(uname -s 2>/dev/null || true)"
KERNEL="$(uname -r 2>/dev/null || true)"

info "Detecting operating system..."
if [[ "$OS_NAME" == "Linux" ]]; then
  pass "Linux detected (${KERNEL})"
else
  fail "This script expects Linux because tc/netem/sysctl are Linux-based."
fi

if [[ -n "${BASH_VERSION:-}" ]]; then
  pass "Running under bash ${BASH_VERSION%%(*}"
else
  fail "This script must be run with bash."
fi

# -----------------------------
# Stage 2: Privilege checks
# -----------------------------
stage 2 "Privilege and sudo checks"
info "Checking whether privileged commands can be used safely..."

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  SUDO=""
  pass "Running as root."
else
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
    if sudo -n true >/dev/null 2>&1; then
      pass "sudo is available without a password prompt."
    else
      warn "sudo is available but may prompt for a password during privileged checks."
    fi
  else
    if [[ "$ROLE" == "client" || "$ROLE" == "both" || "$SMOKE_TEST" -eq 1 ]]; then
      fail "sudo is not available; tc/netem setup checks may not work."
    else
      warn "sudo is not available."
    fi
  fi
fi

# -----------------------------
# Stage 3: Required software
# -----------------------------
stage 3 "Checking required software"
info "Verifying the tools needed to run and validate experiments..."

require_cmd ip
require_cmd ping
require_cmd sysctl
require_cmd iperf3

if [[ "$ROLE" == "client" || "$ROLE" == "both" ]]; then
  require_cmd tc
fi

info "Checking optional but useful tools..."
optional_cmd python3
optional_cmd jq
optional_cmd tcpdump
optional_cmd modprobe

# -----------------------------
# Stage 4: Interface discovery
# -----------------------------
if [[ "$ROLE" == "client" || "$ROLE" == "both" ]]; then
  stage 4 "Client interface discovery"
  info "Determining which interface will be used for tc/netem and traffic generation..."

  if [[ -z "$IFACE" ]]; then
    IFACE="$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')"
    if [[ -z "$IFACE" ]]; then
      IFACE="$(ip -o link show 2>/dev/null | awk -F': ' '$2 != "lo" {print $2; exit}')"
    fi
  fi

  if [[ -z "$IFACE" ]]; then
    fail "Could not auto-detect a usable interface. Re-run with --iface <name>."
  elif ip link show "$IFACE" >/dev/null 2>&1; then
    pass "Using interface: $IFACE"
    info "Interface summary:"
    ip -brief addr show dev "$IFACE" 2>/dev/null || true
  else
    fail "Interface does not exist: $IFACE"
  fi
fi

# -----------------------------
# Stage 5: Congestion controls
# -----------------------------
if [[ "$ROLE" == "client" || "$ROLE" == "both" ]]; then
  stage 5 "TCP congestion control readiness"
  info "Inspecting the kernel's available TCP congestion control algorithms..."

  AVAILABLE_CC="$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || cat /proc/sys/net/ipv4/tcp_available_congestion_control 2>/dev/null || true)"
  CURRENT_CC="$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || cat /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null || true)"

  if [[ -n "$AVAILABLE_CC" ]]; then
    pass "Available CCAs: $AVAILABLE_CC"
  else
    fail "Could not read net.ipv4.tcp_available_congestion_control."
  fi

  if [[ -n "$CURRENT_CC" ]]; then
    pass "Current default CCA: $CURRENT_CC"
  else
    warn "Could not read net.ipv4.tcp_congestion_control."
  fi

  IFS=',' read -r -a EXPECTED_ARRAY <<< "$EXPECT_CC"
  MISSING_EXPECTED=()

  for cc in "${EXPECTED_ARRAY[@]}"; do
    cc="$(trim_spaces "$cc")"
    [[ -z "$cc" ]] && continue
    if ! contains_word "$AVAILABLE_CC" "$cc"; then
      MISSING_EXPECTED+=("$cc")
    fi
  done

  if [[ "${#MISSING_EXPECTED[@]}" -eq 0 ]]; then
    pass "All required CCAs are available: $EXPECT_CC"
  else
    fail "Missing required CCAs: ${MISSING_EXPECTED[*]}"
  fi

  if contains_word "$AVAILABLE_CC" "$RECOMMEND_CC"; then
    pass "Recommended CCA is available: $RECOMMEND_CC"
  else
    warn "Recommended CCA is not available: $RECOMMEND_CC"
  fi
fi

# -----------------------------
# Stage 6: tc / netem readiness
# -----------------------------
if [[ "$ROLE" == "client" || "$ROLE" == "both" ]]; then
  stage 6 "tc / netem readiness"
  info "Inspecting qdisc state and confirming that traffic shaping is possible..."

  if [[ -n "$IFACE" ]] && tc qdisc show dev "$IFACE" >/dev/null 2>&1; then
    CURRENT_QDISC="$(tc qdisc show dev "$IFACE" 2>/dev/null | tr '\n' '; ')"
    pass "qdisc inspection succeeded on $IFACE"
    info "Current qdisc(s): ${CURRENT_QDISC:-<none reported>}"
  else
    fail "Could not inspect qdisc state on interface ${IFACE:-<unset>}."
  fi

  if command -v modprobe >/dev/null 2>&1; then
    if modprobe -n sch_netem >/dev/null 2>&1; then
      pass "modprobe can resolve sch_netem (or it is built in)."
    else
      warn "Could not confirm sch_netem via modprobe -n."
    fi
  else
    warn "modprobe not present; skipping module-level check."
  fi

  if [[ "$SMOKE_TEST" -eq 1 ]]; then
    info "Running a safe temporary netem smoke test on loopback (lo)..."

    if [[ "${EUID:-$(id -u)}" -ne 0 && -z "$SUDO" ]]; then
      fail "Smoke test requested but neither root nor sudo is available."
    else
      if ${SUDO:+$SUDO }tc qdisc replace dev lo root netem delay 1ms >/dev/null 2>&1; then
        APPLIED_QDISC_DEV="lo"
        if ${SUDO:+$SUDO }tc qdisc show dev lo 2>/dev/null | grep -q netem; then
          pass "Temporary netem qdisc applied successfully on loopback."
        else
          fail "Smoke test applied a qdisc but could not confirm netem in tc output."
        fi

        ${SUDO:+$SUDO }tc qdisc del dev lo root >/dev/null 2>&1 || true
        APPLIED_QDISC_DEV=""
        pass "Loopback qdisc cleanup completed."
      else
        fail "Could not apply a temporary netem qdisc. Check privileges and kernel support."
      fi
    fi
  else
    info "Smoke test skipped. Re-run with --smoke-test to verify netem more directly."
  fi
fi

# -----------------------------
# Stage 7: Server reachability
# -----------------------------
if [[ -n "$SERVER_IP" ]]; then
  stage 7 "Server reachability"
  info "Checking whether the server VM is reachable with ping..."

  if ping -c "$PING_COUNT" -W 1 "$SERVER_IP" >/dev/null 2>&1; then
    pass "Server is reachable: $SERVER_IP"
  else
    fail "Server is not reachable via ping: $SERVER_IP"
  fi
else
  stage 7 "Server reachability"
  info "No --server IP supplied, so reachability testing is being skipped."
fi

# -----------------------------
# Stage 8: Workspace setup
# -----------------------------
stage 8 "Workspace setup"
info "Creating the directory structure for raw results, parsed results, figures, and logs..."

mkdir -p \
  "$PROJECT_ROOT/results/raw" \
  "$PROJECT_ROOT/results/parsed" \
  "$PROJECT_ROOT/results/figures" \
  "$PROJECT_ROOT/logs"

pass "Created/verified:"
info "  $PROJECT_ROOT/results/raw"
info "  $PROJECT_ROOT/results/parsed"
info "  $PROJECT_ROOT/results/figures"
info "  $PROJECT_ROOT/logs"

# -----------------------------
# Stage 9: Summary + next steps
# -----------------------------
stage 9 "Summary"
printf "  Passes : %s\n" "$PASS_COUNT"
printf "  Warnings: %s\n" "$WARN_COUNT"
printf "  Fails  : %s\n" "$FAIL_COUNT"

if [[ "$FAIL_COUNT" -eq 0 ]]; then
  printf "\n${C_GREEN}${C_BOLD}Initialization complete.${C_RESET}\n"

  case "$ROLE" in
    server)
      echo "Next: start the server side with:"
      echo "  iperf3 -s"
      ;;
    client)
      echo "Next: on the server VM run:"
      echo "  iperf3 -s"
      echo
      echo "Then on this client VM run your experiment harness, for example:"
      echo "  sudo ./scripts/run_trial.sh --iface ${IFACE:-<iface>} --server ${SERVER_IP:-<server-ip>} --cca cubic --delay 100ms --loss 0.1% --jitter 5ms --time 30"
      ;;
    both)
      echo "Next: if this machine will be the server, run:"
      echo "  iperf3 -s"
      echo
      echo "If this machine will be the client, run your trial harness, for example:"
      echo "  sudo ./scripts/run_trial.sh --iface ${IFACE:-<iface>} --server ${SERVER_IP:-<server-ip>} --cca cubic --delay 100ms --loss 0.1% --jitter 5ms --time 30"
      ;;
  esac

  exit 0
else
  printf "\n${C_RED}${C_BOLD}Initialization found blocking issues.${C_RESET}\n"
  echo "Fix the FAIL items above, then re-run this script."
  exit 1
fi
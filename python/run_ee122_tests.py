#!/usr/bin/env python3
"""
EE 122 TCP CCA overnight test runner.

Run this on the CLIENT VM after both VM setup scripts have been run and after
an iperf3 server is listening on the SERVER VM.

Default experiment count with the matrices below:
    23 cases * 3 algorithms * 5 trials = 345 iperf3 runs
At the default 300s duration + 60s omitted warmup, this can exceed one night.
Use --suites / --algorithms / --trials for smaller runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# =============================================================================
# EDIT TEST SUITES HERE
# =============================================================================

TRIALS = 5
IPERF_PORT = 65535
IPERF_DURATION_SECONDS = 180
IPERF_OMIT_SECONDS = 60
QDISC_DEVICE = "ifb0"
PACKET_BYTES_FOR_BDP = 1500
DEFAULT_BUFFER_MULTIPLIER = 2.0
LOSS_CORRELATION_PCT_DEFAULT = "0"

# If your kernel exposes BBRv3 using a different iperf/Linux name, change only
# iperf_name. The label is used for output paths/filenames.
ALGORITHMS = [
    {"label": "cubic", "iperf_name": "cubic"},
    {"label": "reno", "iperf_name": "reno"},
    {"label": "bbrv3", "iperf_name": "bbr"},
    {"label": "vegas", "iperf_name": "vegas"},
]


def bdp_queue_packets(
    *,
    delay_ms: float,
    rate_mbps: float,
    packet_bytes: int = PACKET_BYTES_FOR_BDP,
    multiplier: float = DEFAULT_BUFFER_MULTIPLIER,
) -> int:
    """Return queue depth in packets using multiplier * BDP."""
    packets = multiplier * (rate_mbps * 1_000_000.0) * (delay_ms / 1000.0) / (8.0 * packet_bytes)
    return max(1, int(round(packets)))


def tc_case(
    delay_ms: float,
    jitter_ms: float,
    rate_mbps: float,
    loss_pct: str | float,
    queue_packets: int | None = None,
    *,
    loss_corr_pct: str | float = LOSS_CORRELATION_PCT_DEFAULT,
    category: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Small helper so adding rows is a one-line edit."""
    if queue_packets is None:
        queue_packets = bdp_queue_packets(delay_ms=delay_ms, rate_mbps=rate_mbps)
    return {
        "delay_ms": delay_ms,
        "jitter_ms": jitter_ms,
        "rate_mbps": rate_mbps,
        "loss_pct": str(loss_pct),
        "loss_corr_pct": str(loss_corr_pct),
        "queue_packets": int(queue_packets),
        "category": category,
        "label": label,
    }


TEST_MATRICES = {
    "loss": [
        tc_case(50, 0, 50, "0", queue_packets=417),
        tc_case(50, 0, 50, "0.01", queue_packets=417),
        tc_case(50, 0, 50, "0.1", queue_packets=417),
        tc_case(50, 0, 50, "0.5", queue_packets=417),
        tc_case(50, 0, 50, "1", queue_packets=417),
        tc_case(50, 0, 50, "3", queue_packets=417),
        # Example: uncomment/add this one line to test 5% loss.
        # tc_case(50, 0, 50, "5", queue_packets=417),
    ],
    "delay": [
        tc_case(10, 0, 50, "0", queue_packets=83),
        tc_case(20, 0, 50, "0", queue_packets=167),
        tc_case(50, 0, 50, "0", queue_packets=417),
        tc_case(100, 0, 50, "0", queue_packets=833),
        tc_case(200, 0, 50, "0", queue_packets=1667),
        tc_case(500, 0, 50, "0", queue_packets=4167),
        tc_case(1000, 0, 50, "0", queue_packets=8333),
    ],
    "rate": [
        tc_case(50, 0, 1, "0", queue_packets=8),
        tc_case(50, 0, 5, "0", queue_packets=42),
        tc_case(50, 0, 10, "0", queue_packets=83),
        tc_case(50, 0, 25, "0", queue_packets=208),
        tc_case(50, 0, 50, "0", queue_packets=417),
        tc_case(50, 0, 75, "0", queue_packets=625),
    ],
    "common_links": [
        tc_case(20, 2, 90, "0.01", queue_packets=300, category="terrestrial"),
        tc_case(80, 8, 50, "0.3", queue_packets=667, category="leo"),
        tc_case(500, 50, 20, "1", queue_packets=1667, category="geo_500ms"),
        tc_case(1000, 100, 10, "3", queue_packets=1667, category="geo_1000ms"),
    ],
}

# =============================================================================
# END EDITABLE SECTION
# =============================================================================


@dataclass(frozen=True)
class Algorithm:
    label: str
    iperf_name: str


@dataclass(frozen=True)
class TestCase:
    suite: str
    index: int
    delay_ms: float
    jitter_ms: float
    rate_mbps: float
    loss_pct: str
    loss_corr_pct: str
    queue_packets: int
    category: str | None = None
    label: str | None = None

    @classmethod
    def from_row(cls, suite: str, index: int, row: dict[str, Any]) -> "TestCase":
        return cls(
            suite=suite,
            index=index,
            delay_ms=float(row["delay_ms"]),
            jitter_ms=float(row["jitter_ms"]),
            rate_mbps=float(row["rate_mbps"]),
            loss_pct=str(row["loss_pct"]),
            loss_corr_pct=str(row.get("loss_corr_pct", LOSS_CORRELATION_PCT_DEFAULT)),
            queue_packets=int(row["queue_packets"]),
            category=row.get("category"),
            label=row.get("label"),
        )

    @property
    def display_label(self) -> str:
        if self.label:
            return self.label
        if self.category:
            return self.category
        return f"case{self.index:02d}"

    @property
    def slug(self) -> str:
        pieces = []
        if self.category:
            pieces.append(sanitize_slug(self.category))
        else:
            pieces.append(f"case{self.index:02d}")
        pieces.extend(
            [
                f"delay-{fmt_number(self.delay_ms)}ms",
                f"jitter-{fmt_number(self.jitter_ms)}ms",
                f"rate-{fmt_number(self.rate_mbps)}mbps",
                f"loss-{pct_for_slug(self.loss_pct)}pct",
                f"corr-{pct_for_slug(self.loss_corr_pct)}pct",
                f"q{self.queue_packets}",
            ]
        )
        return "_".join(pieces)

    def filename_for(self, alg: Algorithm, trial: int) -> str:
        return (
            f"{sanitize_slug(alg.label)}_"
            f"{fmt_number(self.delay_ms)}ms_"
            f"{fmt_number(self.jitter_ms)}ms_"
            f"{fmt_number(self.rate_mbps)}mbps_"
            f"{pct_for_filename(self.loss_pct)}_"
            f"{pct_for_filename(self.loss_corr_pct)}_"
            f"trial{trial}.json"
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_number(value: float | int | str) -> str:
    if isinstance(value, str):
        value = float(value)
    value_float = float(value)
    if value_float.is_integer():
        return str(int(value_float))
    return ("%.10g" % value_float).rstrip("0").rstrip(".")


def pct_for_filename(value: str | float) -> str:
    return fmt_number(value)


def pct_for_slug(value: str | float) -> str:
    return pct_for_filename(value).replace(".", "p")


def sanitize_slug(text: str) -> str:
    allowed = []
    for ch in str(text).lower():
        if ch.isalnum() or ch in {"-", "_"}:
            allowed.append(ch)
        elif ch in {".", " "}:
            allowed.append("p" if ch == "." else "-")
        else:
            allowed.append("-")
    slug = "".join(allowed).strip("-_")
    return slug or "unnamed"


def command_to_string(cmd: Iterable[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def run_cmd(
    cmd: list[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: {command_to_string(cmd)}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def command_output(cmd: list[str], timeout: float = 10) -> str | None:
    try:
        result = run_cmd(cmd, timeout=timeout, check=False)
    except Exception:
        return None
    text = (result.stdout or result.stderr or "").strip()
    return text if text else None


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"Required command not found in PATH: {name}")


def read_file(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return None


def tcp_algorithm_snapshot() -> dict[str, list[str]]:
    available = read_file("/proc/sys/net/ipv4/tcp_available_congestion_control") or ""
    allowed = read_file("/proc/sys/net/ipv4/tcp_allowed_congestion_control") or ""
    return {
        "available": available.split(),
        "allowed": allowed.split(),
    }


def environment_snapshot(qdisc_dev: str) -> dict[str, Any]:
    return {
        "timestamp_utc": utc_now_iso(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "user": os.environ.get("USER") or os.environ.get("LOGNAME"),
        "cwd": os.getcwd(),
        "uname": command_output(["uname", "-a"]),
        "iperf3_version": command_output(["iperf3", "--version"]),
        "tc_version": command_output(["tc", "-V"]),
        "ip_addr_brief": command_output(["ip", "-br", "addr"]),
        "tcp_congestion_control": tcp_algorithm_snapshot(),
        "qdisc_show": command_output(["tc", "-s", "qdisc", "show", "dev", qdisc_dev]),
    }


def sudo_prefix(use_sudo: bool) -> list[str]:
    if not use_sudo or os.geteuid() == 0:
        return []
    return ["sudo"]


def ensure_sudo(use_sudo: bool) -> None:
    if use_sudo and os.geteuid() != 0:
        run_cmd(["sudo", "-v"], timeout=30, check=True)


def tc_netem_cmd(test: TestCase, qdisc_dev: str, use_sudo: bool) -> list[str]:
    cmd = sudo_prefix(use_sudo) + [
        "tc",
        "qdisc",
        "replace",
        "dev",
        qdisc_dev,
        "root",
        "netem",
        "limit",
        str(test.queue_packets),
        "delay",
        f"{fmt_number(test.delay_ms)}ms",
    ]
    if test.jitter_ms > 0:
        cmd.append(f"{fmt_number(test.jitter_ms)}ms")
    cmd.extend(["rate", f"{fmt_number(test.rate_mbps)}mbit", "loss", f"{pct_for_filename(test.loss_pct)}%"])
    if float(test.loss_corr_pct) != 0:
        cmd.append(f"{pct_for_filename(test.loss_corr_pct)}%")
    return cmd


def tc_cleanup_cmd(qdisc_dev: str, use_sudo: bool) -> list[str]:
    return sudo_prefix(use_sudo) + ["tc", "qdisc", "del", "dev", qdisc_dev, "root"]


def iperf_cmd(
    server: str,
    port: int,
    alg: Algorithm | None,
    duration: int,
    omit: int,
    connect_timeout_ms: int,
) -> list[str]:
    cmd = [
        "iperf3",
        "-c",
        server,
        "-p",
        str(port),
        "-t",
        str(duration),
        "-O",
        str(omit),
        "-R",
        "--json",
        "--connect-timeout",
        str(connect_timeout_ms),
    ]
    if alg is not None:
        cmd.extend(["-C", alg.iperf_name])
    return cmd


def iter_cases(suite_names: list[str]) -> list[TestCase]:
    cases: list[TestCase] = []
    for suite in suite_names:
        for idx, row in enumerate(TEST_MATRICES[suite], start=1):
            cases.append(TestCase.from_row(suite, idx, row))
    return cases


def validate_unique_paths(cases: list[TestCase], algorithms: list[Algorithm], trials: int) -> None:
    seen: set[tuple[str, str, str, str]] = set()
    for case in cases:
        for alg in algorithms:
            for trial in range(1, trials + 1):
                key = (case.suite, case.slug, alg.label, case.filename_for(alg, trial))
                if key in seen:
                    raise RuntimeError(f"Duplicate output path detected: {key}")
                seen.add(key)


def check_available_algorithms(algorithms: list[Algorithm]) -> None:
    snapshot = tcp_algorithm_snapshot()
    available = set(snapshot.get("available", []))
    missing = [alg.iperf_name for alg in algorithms if alg.iperf_name not in available]
    if missing:
        raise RuntimeError(
            "These iperf/Linux congestion-control names are not in "
            "/proc/sys/net/ipv4/tcp_available_congestion_control: "
            f"{missing}. Available: {sorted(available)}. "
            "Edit ALGORITHMS near the top of this script if BBRv3 is exposed under a different name."
        )


def preflight(args: argparse.Namespace, algorithms: list[Algorithm]) -> None:
    for cmd in ["iperf3", "tc", "ip"]:
        require_command(cmd)
    if args.use_sudo:
        require_command("sudo")
        ensure_sudo(True)

    qdisc_check = run_cmd(["ip", "link", "show", args.qdisc_dev], timeout=10, check=False)
    if qdisc_check.returncode != 0:
        raise RuntimeError(
            f"qdisc device {args.qdisc_dev!r} was not found. "
            "Run the client setup script first, for example: "
            "sudo ./client_setup_idempotent.bash <client-interface>"
        )

    check_available_algorithms(algorithms)

    if not args.skip_ping:
        ping = run_cmd(["ping", "-c", "1", "-W", "2", args.server], timeout=8, check=False)
        if ping.returncode != 0:
            print(
                "WARNING: ping did not succeed. Continuing because ping can be blocked. "
                f"STDERR: {ping.stderr.strip()}",
                file=sys.stderr,
            )

    if not args.skip_iperf_preflight:
        for alg in algorithms:
            cmd = iperf_cmd(args.server, args.port, alg, 1, 0, args.connect_timeout_ms)
            result = run_cmd(cmd, timeout=30, check=False)
            if result.returncode != 0:
                raise RuntimeError(
                    "Short iperf3 preflight failed. Confirm the server is running with "
                    f"`iperf3 -s -p {args.port}` and that algorithm {alg.iperf_name!r} is usable.\n"
                    f"Command: {command_to_string(cmd)}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
                )
            try:
                json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "iperf3 preflight did not return valid JSON.\n"
                    f"Command: {command_to_string(cmd)}\nError: {exc}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
                ) from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, sort_keys=True) + "\n")


def output_paths(output_root: Path, run_id: str, case: TestCase, alg: Algorithm, trial: int) -> tuple[Path, Path, Path]:
    out_dir = output_root / run_id / case.suite / case.slug / sanitize_slug(alg.label)
    raw_json = out_dir / case.filename_for(alg, trial)
    meta_json = out_dir / f"{raw_json.stem}.meta.json"
    stderr_log = out_dir / f"{raw_json.stem}.stderr.log"
    return raw_json, meta_json, stderr_log


def valid_existing_json(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def run_trial(
    *,
    args: argparse.Namespace,
    env: dict[str, Any],
    output_root: Path,
    run_id: str,
    case: TestCase,
    alg: Algorithm,
    trial: int,
) -> dict[str, Any]:
    raw_json_path, meta_json_path, stderr_log_path = output_paths(output_root, run_id, case, alg, trial)
    raw_json_path.parent.mkdir(parents=True, exist_ok=True)

    tc_cmd = tc_netem_cmd(case, args.qdisc_dev, args.use_sudo)
    ip_cmd = iperf_cmd(args.server, args.port, alg, args.duration, args.omit, args.connect_timeout_ms)

    metadata: dict[str, Any] = {
        "schema": "ee122_trial_metadata_v1",
        "run_id": run_id,
        "created_at_utc": utc_now_iso(),
        "suite": case.suite,
        "case_index": case.index,
        "case_slug": case.slug,
        "case": asdict(case),
        "algorithm": asdict(alg),
        "trial": trial,
        "tc_command": tc_cmd,
        "tc_command_string": command_to_string(tc_cmd),
        "iperf_command": ip_cmd,
        "iperf_command_string": command_to_string(ip_cmd),
        "raw_json_file": str(raw_json_path),
        "stderr_log_file": str(stderr_log_path),
        "environment_summary": {
            "hostname": env.get("hostname"),
            "platform": env.get("platform"),
            "iperf3_version": env.get("iperf3_version"),
            "tc_version": env.get("tc_version"),
            "tcp_congestion_control": env.get("tcp_congestion_control"),
        },
    }

    if raw_json_path.exists() and not args.overwrite:
        if valid_existing_json(raw_json_path):
            metadata.update(
                {
                    "status": "skipped_existing",
                    "completed_at_utc": utc_now_iso(),
                    "raw_json_sha256": sha256_file(raw_json_path),
                }
            )
            write_json(meta_json_path, metadata)
            return metadata
        raise RuntimeError(f"Output file exists but is not valid JSON: {raw_json_path}. Use --overwrite to replace it.")

    if args.dry_run:
        metadata.update({"status": "dry_run", "completed_at_utc": utc_now_iso()})
        write_json(meta_json_path, metadata)
        return metadata

    start = time.monotonic()
    start_wall = utc_now_iso()
    run_cmd(tc_cmd, timeout=30, check=True)
    if args.settle_seconds > 0:
        time.sleep(args.settle_seconds)

    timeout = args.duration + args.omit + args.iperf_timeout_margin_seconds
    result = run_cmd(ip_cmd, timeout=timeout, check=False)
    elapsed = time.monotonic() - start
    completed_wall = utc_now_iso()

    stderr_log_path.write_text(result.stderr or "", encoding="utf-8")

    if result.returncode != 0:
        failure_path = raw_json_path.with_suffix(".failure.json")
        failure = {
            **metadata,
            "status": "failed",
            "started_at_utc": start_wall,
            "completed_at_utc": completed_wall,
            "elapsed_seconds": elapsed,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "qdisc_after": command_output(["tc", "-s", "qdisc", "show", "dev", args.qdisc_dev]),
        }
        write_json(failure_path, failure)
        raise RuntimeError(
            f"iperf3 failed for {case.suite}/{case.slug}/{alg.label}/trial{trial}.\n"
            f"Command: {command_to_string(ip_cmd)}\nFailure details: {failure_path}"
        )

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        bad_path = raw_json_path.with_suffix(".invalid-json.txt")
        bad_path.write_text(result.stdout, encoding="utf-8")
        raise RuntimeError(f"iperf3 output was not valid JSON. Saved stdout to {bad_path}. Error: {exc}") from exc

    raw_json_path.write_text(result.stdout, encoding="utf-8")
    metadata.update(
        {
            "status": "completed",
            "started_at_utc": start_wall,
            "completed_at_utc": completed_wall,
            "elapsed_seconds": elapsed,
            "returncode": result.returncode,
            "raw_json_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
            "raw_json_top_level_keys": sorted(parsed.keys()) if isinstance(parsed, dict) else None,
            "qdisc_after": command_output(["tc", "-s", "qdisc", "show", "dev", args.qdisc_dev]),
        }
    )
    write_json(meta_json_path, metadata)
    return metadata


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(
    *,
    args: argparse.Namespace,
    run_id: str,
    cases: list[TestCase],
    algorithms: list[Algorithm],
    env: dict[str, Any],
) -> dict[str, Any]:
    total_trials = len(cases) * len(algorithms) * args.trials
    return {
        "schema": "ee122_run_manifest_v1",
        "run_id": run_id,
        "created_at_utc": utc_now_iso(),
        "server": args.server,
        "port": args.port,
        "qdisc_device": args.qdisc_dev,
        "duration_seconds": args.duration,
        "omit_seconds": args.omit,
        "trials_per_algorithm_per_case": args.trials,
        "suites": args.suites,
        "algorithms": [asdict(a) for a in algorithms],
        "case_count": len(cases),
        "planned_iperf_runs": total_trials,
        "estimated_upper_bound_hours": round(
            total_trials * (args.duration + args.omit + args.settle_seconds) / 3600.0, 3
        ),
        "cases": [asdict(c) | {"slug": c.slug} for c in cases],
        "environment": env,
        "completed_trials": [],
        "failed_trials": [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run EE 122 TCP CCA netem/iperf3 test matrices on the client VM."
    )
    parser.add_argument("--server", required=True, help="Server VM IPv4 address or hostname.")
    parser.add_argument("--port", type=int, default=IPERF_PORT, help="iperf3 server port.")
    parser.add_argument("--output-root", default="ee122_results", help="Root directory for all output.")
    parser.add_argument("--run-id", default=None, help="Optional run ID; defaults to timestamp.")
    parser.add_argument("--qdisc-dev", default=QDISC_DEVICE, help="Device with root netem qdisc, usually ifb0.")
    parser.add_argument("--duration", type=int, default=IPERF_DURATION_SECONDS, help="iperf3 -t duration in seconds.")
    parser.add_argument("--omit", type=int, default=IPERF_OMIT_SECONDS, help="iperf3 -O omitted warmup seconds.")
    parser.add_argument("--trials", type=int, default=TRIALS, help="Trials per case per algorithm.")
    parser.add_argument(
        "--suites",
        nargs="+",
        choices=sorted(TEST_MATRICES.keys()),
        default=list(TEST_MATRICES.keys()),
        help="Subset of test matrices to run.",
    )
    parser.add_argument(
        "--algorithms",
        nargs="+",
        default=[a["label"] for a in ALGORITHMS],
        help="Algorithm labels to run from ALGORITHMS. Default: all enabled labels.",
    )
    parser.add_argument("--settle-seconds", type=float, default=1.0, help="Sleep after each tc qdisc replace.")
    parser.add_argument("--connect-timeout-ms", type=int, default=5000, help="iperf3 control connection timeout.")
    parser.add_argument(
        "--iperf-timeout-margin-seconds",
        type=int,
        default=180,
        help="Extra timeout margin beyond duration+omit.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Write metadata and print plan without running tc/iperf3.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing JSON files instead of skipping them.")
    parser.add_argument("--skip-ping", action="store_true", help="Skip ping reachability check.")
    parser.add_argument("--skip-iperf-preflight", action="store_true", help="Skip short 1s iperf preflight for each algorithm.")
    parser.add_argument("--no-sudo", dest="use_sudo", action="store_false", help="Do not prefix tc commands with sudo.")
    parser.set_defaults(use_sudo=True)
    parser.add_argument(
        "--no-cleanup-at-end",
        dest="cleanup_at_end",
        action="store_false",
        help="Leave the final netem qdisc installed on qdisc-dev.",
    )
    parser.set_defaults(cleanup_at_end=True)
    parser.add_argument("--continue-on-error", action="store_true", help="Keep running after a failed trial.")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle run order to reduce temporal bias.")
    parser.add_argument("--seed", type=int, default=122, help="Shuffle seed.")
    return parser.parse_args()


def select_algorithms(labels: list[str]) -> list[Algorithm]:
    all_algorithms = {row["label"]: Algorithm(row["label"], row["iperf_name"]) for row in ALGORITHMS}
    missing = [label for label in labels if label not in all_algorithms]
    if missing:
        raise RuntimeError(f"Unknown algorithm labels {missing}. Known labels: {sorted(all_algorithms)}")
    return [all_algorithms[label] for label in labels]


def main() -> int:
    args = parse_args()
    algorithms = select_algorithms(args.algorithms)
    cases = iter_cases(args.suites)
    validate_unique_paths(cases, algorithms, args.trials)

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = Path(args.output_root).expanduser().resolve()
    run_root = output_root / run_id
    logs_dir = run_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_root / "manifest.json"
    index_path = run_root / "index.jsonl"

    total_runs = len(cases) * len(algorithms) * args.trials
    estimated_hours = total_runs * (args.duration + args.omit + args.settle_seconds) / 3600.0
    print(f"Run ID: {run_id}")
    print(f"Output root: {run_root}")
    print(f"Plan: {len(cases)} cases * {len(algorithms)} algorithms * {args.trials} trials = {total_runs} iperf3 runs")
    print(f"Estimated upper-bound runtime: {estimated_hours:.2f} hours")

    if not args.dry_run:
        preflight(args, algorithms)
    else:
        print("Dry run: skipping preflight and command execution.")

    env = environment_snapshot(args.qdisc_dev) if not args.dry_run else {"dry_run": True, "timestamp_utc": utc_now_iso()}
    manifest = build_manifest(args=args, run_id=run_id, cases=cases, algorithms=algorithms, env=env)
    write_json(manifest_path, manifest)

    plan: list[tuple[TestCase, Algorithm, int]] = []
    for case in cases:
        for alg in algorithms:
            for trial in range(1, args.trials + 1):
                plan.append((case, alg, trial))
    if args.shuffle:
        random.Random(args.seed).shuffle(plan)

    failures = 0
    try:
        for run_num, (case, alg, trial) in enumerate(plan, start=1):
            print(
                f"[{run_num}/{total_runs}] suite={case.suite} case={case.slug} "
                f"alg={alg.label}({alg.iperf_name}) trial={trial}",
                flush=True,
            )
            try:
                record = run_trial(
                    args=args,
                    env=env,
                    output_root=output_root,
                    run_id=run_id,
                    case=case,
                    alg=alg,
                    trial=trial,
                )
                manifest["completed_trials"].append(record)
                append_jsonl(index_path, record)
                write_json(manifest_path, manifest)
            except Exception as exc:
                failures += 1
                failure_record = {
                    "status": "failed_exception",
                    "timestamp_utc": utc_now_iso(),
                    "suite": case.suite,
                    "case_slug": case.slug,
                    "algorithm": asdict(alg),
                    "trial": trial,
                    "exception": str(exc),
                }
                manifest["failed_trials"].append(failure_record)
                append_jsonl(index_path, failure_record)
                write_json(manifest_path, manifest)
                print(f"ERROR: {exc}", file=sys.stderr, flush=True)
                if not args.continue_on_error:
                    raise
    finally:
        if args.cleanup_at_end and not args.dry_run:
            cleanup = tc_cleanup_cmd(args.qdisc_dev, args.use_sudo)
            result = run_cmd(cleanup, timeout=30, check=False)
            cleanup_record = {
                "timestamp_utc": utc_now_iso(),
                "cleanup_command": cleanup,
                "cleanup_command_string": command_to_string(cleanup),
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            write_json(logs_dir / "cleanup.json", cleanup_record)
            print(f"Cleanup qdisc command exit code: {result.returncode}")

    print(f"Finished. Completed records: {len(manifest['completed_trials'])}; failures: {failures}")
    print(f"Manifest: {manifest_path}")
    print(f"Index: {index_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Analyze EE 122 iperf3 congestion-control experiment results.

The script recursively discovers iperf3 JSON output, recovers metadata from
sidecars/manifests/indexes when available, writes summary tables, and generates
matplotlib figures suitable for reports.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import tempfile
from pathlib import Path
from typing import Any

RUNTIME_CACHE_DIR = Path(tempfile.gettempdir()) / "ee122_analysis_cache"
MPLCONFIGDIR = RUNTIME_CACHE_DIR / "matplotlib"
RUNTIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
MPLCONFIGDIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("XDG_CACHE_HOME", str(RUNTIME_CACHE_DIR))
os.environ.setdefault("MPLCONFIGDIR", str(MPLCONFIGDIR))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

try:
    from scipy import stats as scipy_stats
except Exception:  # pragma: no cover - scipy is optional.
    scipy_stats = None


KNOWN_SUITES = {"loss", "delay", "rate", "common_links"}
EXCLUDED_JSON_NAMES = {
    "manifest.json",
    "cleanup.json",
    "figure_manifest.json",
}

TRIAL_FILENAME_RE = re.compile(
    r"^(?P<algorithm>.+?)_"
    r"(?P<delay>[\dp.]+)ms_"
    r"(?P<jitter>[\dp.]+)ms_"
    r"(?P<rate>[\dp.]+)mbps_"
    r"(?P<loss>[\dp.]+)_"
    r"(?P<corr>[\dp.]+)_"
    r"trial(?P<trial>\d+)\.json$",
    re.IGNORECASE,
)

CONDITION_RE = re.compile(
    r"(?P<prefix>.*?)(?:case(?P<case_index>\d+)_)?"
    r"delay-(?P<delay>[\dp.]+)ms_"
    r"jitter-(?P<jitter>[\dp.]+)ms_"
    r"rate-(?P<rate>[\dp.]+)mbps_"
    r"loss-(?P<loss>[\dp.]+)pct_"
    r"corr-(?P<corr>[\dp.]+)pct_"
    r"q(?P<queue>\d+)",
    re.IGNORECASE,
)

TABLE_COLUMNS = [
    "run_id",
    "suite",
    "case_slug",
    "common_link_category",
    "algorithm",
    "algorithm_iperf_name",
    "trial",
    "delay_ms",
    "jitter_ms",
    "rate_mbps",
    "loss_pct",
    "loss_correlation_pct",
    "queue_depth_packets",
    "raw_file",
    "metadata_source",
    "throughput_mbps",
    "utilization",
    "total_bytes",
    "duration_seconds",
    "retransmits",
    "retransmits_per_second",
    "mean_rtt_ms",
    "p95_rtt_ms",
    "mean_cwnd_bytes",
    "mean_snd_wnd_bytes",
    "interval_count",
    "convergence_time_s",
]

AGG_COLUMNS = [
    "suite",
    "delay_ms",
    "jitter_ms",
    "rate_mbps",
    "loss_pct",
    "loss_correlation_pct",
    "queue_depth_packets",
    "common_link_category",
    "algorithm",
    "n_trials",
    "n_valid_throughput",
    "mean_throughput_mbps",
    "median_throughput_mbps",
    "std_throughput_mbps",
    "sem_throughput_mbps",
    "ci95_throughput_mbps",
    "mean_utilization",
    "median_utilization",
    "std_utilization",
    "sem_utilization",
    "ci95_utilization",
    "mean_retransmits",
    "mean_retransmits_per_second",
    "std_retransmits_per_second",
    "sem_retransmits_per_second",
    "ci95_retransmits_per_second",
    "mean_rtt_ms",
    "p95_rtt_ms",
    "mean_cwnd_bytes",
    "mean_snd_wnd_bytes",
    "mean_convergence_time_s",
    "ci95_convergence_time_s",
]

SMOKE_ONLY_FIGURES = [
    Path("plots/loss/smoke_throughput.png"),
    Path("plots/delay/smoke_throughput.png"),
    Path("plots/rate/smoke_throughput.png"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze EE 122 iperf3 congestion-control experiment results."
    )
    parser.add_argument(
        "--results-root",
        required=True,
        type=Path,
        help="Root directory containing iperf3 result JSON files.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output directory for analysis tables, figures, and reports.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Parse all data but generate only a small plot subset.",
    )
    return parser.parse_args()


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def coalesce(*values: Any) -> Any:
    for value in values:
        if not is_missing(value):
            return value
    return None


def parse_number_token(value: Any) -> float:
    if value is None:
        return math.nan
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        return math.nan
    text = text.replace("%", "")
    if "p" in text and "." not in text:
        text = text.replace("p", ".")
    try:
        return float(text)
    except ValueError:
        return math.nan


def parse_int_token(value: Any) -> float:
    number = parse_number_token(value)
    if math.isnan(number):
        return math.nan
    return int(number)


def fmt_number(value: Any) -> str:
    number = parse_number_token(value)
    if math.isnan(number):
        return "unknown"
    if float(number).is_integer():
        return str(int(number))
    return f"{number:g}"


def pct_slug(value: Any) -> str:
    return fmt_number(value).replace(".", "p")


def safe_filename(text: str) -> str:
    cleaned = []
    for ch in str(text).lower():
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in {".", "-"}:
            cleaned.append(ch.replace(".", "p"))
        else:
            cleaned.append("_")
    slug = "".join(cleaned).strip("_")
    slug = re.sub(r"_+", "_", slug)
    return slug or "figure"


def friendly_category(value: Any) -> str | None:
    if is_missing(value):
        return None
    text = str(value).strip()
    lower = text.lower()
    if lower == "leo":
        return "LEO"
    if lower == "geo":
        return "GEO"
    if lower.startswith("geo_"):
        return "GEO " + lower.split("_", 1)[1]
    if lower == "terrestrial":
        return "Terrestrial"
    return text.replace("_", " ").title()


def ensure_output_dirs(out: Path) -> None:
    for relative in [
        "tables",
        "plots/loss",
        "plots/delay",
        "plots/rate",
        "plots/common_links",
        "plots/summary",
        "plots/time_series",
    ]:
        (out / relative).mkdir(parents=True, exist_ok=True)


def clear_smoke_only_figures(out: Path) -> None:
    for relative_path in SMOKE_ONLY_FIGURES:
        path = out / relative_path
        if path.exists():
            path.unlink()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def looks_like_iperf_json(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    return bool({"end", "intervals", "start"} & set(data.keys()))


def discover_files(results_root: Path) -> tuple[list[Path], dict[str, Any]]:
    """Recursively discover likely raw iperf JSON files."""
    raw_files: list[Path] = []
    skipped: list[dict[str, str]] = []

    for path in sorted(results_root.rglob("*.json")):
        if path.name.endswith(".meta.json"):
            continue
        if path.name in EXCLUDED_JSON_NAMES:
            continue
        if path.name.endswith(".failure.json"):
            skipped.append({"path": str(path), "reason": "failure sidecar"})
            continue

        try:
            data = load_json(path)
        except Exception:
            raw_files.append(path)
            continue

        if looks_like_iperf_json(data):
            raw_files.append(path)
        else:
            skipped.append({"path": str(path), "reason": "not iperf JSON"})

    return raw_files, {"skipped_json": skipped}


def suffix_key(path: str | Path) -> str:
    parts = Path(str(path)).parts
    for idx, part in enumerate(parts):
        if part in KNOWN_SUITES:
            return "/".join(parts[idx:])
    return Path(str(path)).name


def load_metadata_catalog(results_root: Path) -> dict[str, Any]:
    manifests: list[dict[str, Any]] = []
    case_by_suite_slug: dict[tuple[str, str], dict[str, Any]] = {}
    case_by_condition: list[dict[str, Any]] = []
    algorithm_by_label: dict[str, str] = {}
    algorithm_by_iperf: dict[str, str] = {}
    expected_trials: int | None = None
    index_by_suffix: dict[str, dict[str, Any]] = {}
    index_by_name: dict[str, list[dict[str, Any]]] = {}
    index_errors: list[dict[str, str]] = []

    for manifest_path in sorted(results_root.rglob("manifest.json")):
        try:
            manifest = load_json(manifest_path)
        except Exception as exc:
            index_errors.append({"path": str(manifest_path), "error": str(exc)})
            continue

        manifests.append({"path": manifest_path, "data": manifest})
        expected = manifest.get("trials_per_algorithm_per_case")
        if expected is not None:
            expected_value = int(expected)
            expected_trials = max(expected_trials or 0, expected_value)

        for algorithm in manifest.get("algorithms", []) or []:
            if not isinstance(algorithm, dict):
                continue
            label = algorithm.get("label")
            iperf_name = algorithm.get("iperf_name")
            if label and iperf_name:
                algorithm_by_label[str(label)] = str(iperf_name)
                algorithm_by_iperf[str(iperf_name)] = str(label)

        for case in manifest.get("cases", []) or []:
            if not isinstance(case, dict):
                continue
            suite = str(case.get("suite", "unknown"))
            slug = case.get("slug")
            if slug:
                case_by_suite_slug[(suite, str(slug))] = case
            case_by_condition.append(case)

    for index_path in sorted(results_root.rglob("index.jsonl")):
        try:
            with index_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except Exception as exc:
                        index_errors.append(
                            {
                                "path": str(index_path),
                                "error": f"line {line_number}: {exc}",
                            }
                        )
                        continue
                    raw_name = record.get("raw_json_file")
                    if not raw_name:
                        continue
                    key = suffix_key(raw_name)
                    index_by_suffix[key] = record
                    index_by_name.setdefault(Path(str(raw_name)).name, []).append(record)
        except Exception as exc:
            index_errors.append({"path": str(index_path), "error": str(exc)})

    return {
        "manifests": manifests,
        "case_by_suite_slug": case_by_suite_slug,
        "case_by_condition": case_by_condition,
        "algorithm_by_label": algorithm_by_label,
        "algorithm_by_iperf": algorithm_by_iperf,
        "expected_trials": expected_trials,
        "index_by_suffix": index_by_suffix,
        "index_by_name": index_by_name,
        "metadata_errors": index_errors,
    }


def infer_from_path(raw_path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "run_id": None,
        "suite": "unknown",
        "case_slug": None,
        "common_link_category": None,
        "algorithm": None,
        "algorithm_iperf_name": None,
        "trial": math.nan,
        "delay_ms": math.nan,
        "jitter_ms": math.nan,
        "rate_mbps": math.nan,
        "loss_pct": math.nan,
        "loss_correlation_pct": math.nan,
        "queue_depth_packets": math.nan,
        "raw_file": str(raw_path),
        "metadata_source": "path",
    }

    parts = list(raw_path.parts)
    suite_index = None
    for idx, part in enumerate(parts):
        if part in KNOWN_SUITES:
            suite_index = idx
            metadata["suite"] = part
            break

    if suite_index is not None:
        if suite_index > 0:
            metadata["run_id"] = parts[suite_index - 1]
        if len(parts) > suite_index + 1:
            metadata["case_slug"] = parts[suite_index + 1]
        if len(parts) > suite_index + 2:
            metadata["algorithm"] = parts[suite_index + 2]
    elif raw_path.parent.name:
        metadata["algorithm"] = raw_path.parent.name

    name_match = TRIAL_FILENAME_RE.match(raw_path.name)
    if name_match:
        metadata.update(
            {
                "algorithm": name_match.group("algorithm"),
                "trial": parse_int_token(name_match.group("trial")),
                "delay_ms": parse_number_token(name_match.group("delay")),
                "jitter_ms": parse_number_token(name_match.group("jitter")),
                "rate_mbps": parse_number_token(name_match.group("rate")),
                "loss_pct": parse_number_token(name_match.group("loss")),
                "loss_correlation_pct": parse_number_token(name_match.group("corr")),
            }
        )

    condition_source = metadata.get("case_slug") or "/".join(parts)
    condition_match = CONDITION_RE.search(str(condition_source))
    if condition_match:
        metadata.update(
            {
                "delay_ms": parse_number_token(condition_match.group("delay")),
                "jitter_ms": parse_number_token(condition_match.group("jitter")),
                "rate_mbps": parse_number_token(condition_match.group("rate")),
                "loss_pct": parse_number_token(condition_match.group("loss")),
                "loss_correlation_pct": parse_number_token(condition_match.group("corr")),
                "queue_depth_packets": parse_int_token(condition_match.group("queue")),
            }
        )
        if not metadata.get("case_slug"):
            metadata["case_slug"] = condition_match.group(0)
        prefix = condition_match.group("prefix").strip("_-")
        if metadata["suite"] == "common_links" and prefix and not prefix.startswith("case"):
            metadata["common_link_category"] = friendly_category(prefix)

    if metadata["suite"] == "common_links" and not metadata.get("common_link_category"):
        slug = metadata.get("case_slug")
        if slug and "_delay-" in slug:
            metadata["common_link_category"] = friendly_category(slug.split("_delay-", 1)[0])

    return metadata


def metadata_from_case(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "suite": case.get("suite"),
        "common_link_category": friendly_category(case.get("category")),
        "delay_ms": parse_number_token(case.get("delay_ms")),
        "jitter_ms": parse_number_token(case.get("jitter_ms")),
        "rate_mbps": parse_number_token(case.get("rate_mbps")),
        "loss_pct": parse_number_token(case.get("loss_pct")),
        "loss_correlation_pct": parse_number_token(
            coalesce(case.get("loss_correlation_pct"), case.get("loss_corr_pct"))
        ),
        "queue_depth_packets": parse_int_token(
            coalesce(case.get("queue_depth_packets"), case.get("queue_packets"))
        ),
        "case_slug": case.get("slug"),
    }


def metadata_from_trial_record(record: dict[str, Any]) -> dict[str, Any]:
    algorithm = record.get("algorithm") or {}
    if not isinstance(algorithm, dict):
        algorithm = {"label": algorithm}
    case = record.get("case") or {}
    if not isinstance(case, dict):
        case = {}

    metadata = metadata_from_case(case)
    metadata.update(
        {
            "run_id": record.get("run_id"),
            "suite": coalesce(record.get("suite"), metadata.get("suite")),
            "case_slug": coalesce(record.get("case_slug"), metadata.get("case_slug")),
            "algorithm": coalesce(algorithm.get("label"), algorithm.get("iperf_name")),
            "algorithm_iperf_name": algorithm.get("iperf_name"),
            "trial": parse_int_token(record.get("trial")),
        }
    )
    return metadata


def overlay_metadata(base: dict[str, Any], update: dict[str, Any], source: str) -> None:
    for key, value in update.items():
        if key not in base:
            continue
        if not is_missing(value):
            base[key] = value
    if update:
        base["metadata_source"] = source


def find_manifest_case(metadata: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any] | None:
    suite = metadata.get("suite")
    slug = metadata.get("case_slug")
    if suite and slug:
        case = catalog["case_by_suite_slug"].get((str(suite), str(slug)))
        if case:
            return case

    for case in catalog["case_by_condition"]:
        if suite and case.get("suite") != suite:
            continue
        fields_match = True
        for field, case_field in [
            ("delay_ms", "delay_ms"),
            ("jitter_ms", "jitter_ms"),
            ("rate_mbps", "rate_mbps"),
            ("loss_pct", "loss_pct"),
            ("loss_correlation_pct", "loss_corr_pct"),
            ("queue_depth_packets", "queue_packets"),
        ]:
            left = parse_number_token(metadata.get(field))
            right = parse_number_token(case.get(case_field))
            if math.isnan(left) or math.isnan(right) or abs(left - right) > 1e-9:
                fields_match = False
                break
        if fields_match:
            return case
    return None


def matching_index_record(raw_path: Path, catalog: dict[str, Any]) -> dict[str, Any] | None:
    key = suffix_key(raw_path)
    if key in catalog["index_by_suffix"]:
        return catalog["index_by_suffix"][key]

    records = catalog["index_by_name"].get(raw_path.name, [])
    if len(records) == 1:
        return records[0]
    return None


def parse_metadata(raw_path: Path, catalog: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Recover trial metadata using sidecar, index, manifest, and path fallback."""
    warnings: list[str] = []
    metadata = infer_from_path(raw_path)

    manifest_case = find_manifest_case(metadata, catalog)
    if manifest_case:
        overlay_metadata(metadata, metadata_from_case(manifest_case), "manifest")

    index_record = matching_index_record(raw_path, catalog)
    if index_record:
        overlay_metadata(metadata, metadata_from_trial_record(index_record), "index.jsonl")

    sidecar = raw_path.with_name(f"{raw_path.stem}.meta.json")
    if sidecar.exists():
        try:
            sidecar_data = load_json(sidecar)
            overlay_metadata(metadata, metadata_from_trial_record(sidecar_data), ".meta.json")
        except Exception as exc:
            warnings.append(f"{sidecar}: failed to parse sidecar metadata: {exc}")

    algorithm = metadata.get("algorithm")
    if algorithm and is_missing(metadata.get("algorithm_iperf_name")):
        metadata["algorithm_iperf_name"] = catalog["algorithm_by_label"].get(str(algorithm))
    iperf_name = metadata.get("algorithm_iperf_name")
    if iperf_name and is_missing(metadata.get("algorithm")):
        metadata["algorithm"] = catalog["algorithm_by_iperf"].get(str(iperf_name), iperf_name)

    if metadata.get("common_link_category"):
        metadata["common_link_category"] = friendly_category(metadata["common_link_category"])

    return metadata, warnings


def nested_dict(data: dict[str, Any], path: list[str]) -> dict[str, Any] | None:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def first_summary(end: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    for key in ["sum_received", "sum", "sum_sent"]:
        candidate = end.get(key)
        if isinstance(candidate, dict) and candidate.get("bits_per_second") is not None:
            return key, candidate
    return None, None


def candidate_dicts(value: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if isinstance(value, dict):
        candidates.append(value)
        for key in ["sum", "sum_sent", "sum_received", "sender", "receiver"]:
            child = value.get(key)
            if isinstance(child, dict):
                candidates.extend(candidate_dicts(child))
        streams = value.get("streams")
        if streams is not None:
            candidates.extend(candidate_dicts(streams))
    elif isinstance(value, list):
        for item in value:
            candidates.extend(candidate_dicts(item))
    return candidates


def first_number(
    candidates: list[dict[str, Any]],
    names: list[str],
    *,
    positive_only: bool = False,
) -> float:
    for candidate in candidates:
        for name in names:
            if name not in candidate:
                continue
            value = parse_number_token(candidate.get(name))
            if math.isnan(value):
                continue
            if positive_only and value <= 0:
                continue
            return value
    return math.nan


def extract_tcp_info(candidates: list[dict[str, Any]]) -> dict[str, float]:
    rtt_us = first_number(candidates, ["rtt", "mean_rtt", "max_rtt", "min_rtt"], positive_only=True)
    rttvar_us = first_number(candidates, ["rttvar"], positive_only=True)
    snd_cwnd = first_number(candidates, ["snd_cwnd", "max_snd_cwnd"], positive_only=True)
    snd_wnd = first_number(candidates, ["snd_wnd", "max_snd_wnd"], positive_only=True)
    return {
        "rtt_ms": rtt_us / 1000.0 if not math.isnan(rtt_us) else math.nan,
        "rttvar_ms": rttvar_us / 1000.0 if not math.isnan(rttvar_us) else math.nan,
        "snd_cwnd_bytes": snd_cwnd,
        "snd_wnd_bytes": snd_wnd,
    }


def interval_retransmits(interval: dict[str, Any]) -> float:
    sum_dict = interval.get("sum")
    if isinstance(sum_dict, dict) and sum_dict.get("retransmits") is not None:
        return parse_number_token(sum_dict.get("retransmits"))

    values: list[float] = []
    for candidate in candidate_dicts(interval.get("streams")):
        if "retransmits" in candidate:
            value = parse_number_token(candidate.get("retransmits"))
            if not math.isnan(value):
                values.append(value)
    if values:
        return float(sum(values))
    return math.nan


def parse_iperf_json(raw_path: Path) -> dict[str, Any]:
    """Parse iperf3 JSON and return trial summary plus non-omitted intervals."""
    data = load_json(raw_path)
    if not looks_like_iperf_json(data):
        raise ValueError("JSON does not look like iperf3 output")

    end = data.get("end") if isinstance(data.get("end"), dict) else {}
    summary_key, summary = first_summary(end)
    if summary is None:
        raise ValueError("iperf JSON is missing end summary throughput")

    throughput_bps = parse_number_token(summary.get("bits_per_second"))
    throughput_mbps = throughput_bps / 1_000_000.0 if not math.isnan(throughput_bps) else math.nan
    total_bytes = parse_number_token(summary.get("bytes"))
    duration = parse_number_token(summary.get("seconds"))
    if math.isnan(duration):
        start = parse_number_token(summary.get("start"))
        finish = parse_number_token(summary.get("end"))
        if not math.isnan(start) and not math.isnan(finish):
            duration = max(0.0, finish - start)

    retransmits = math.nan
    sum_sent = nested_dict(data, ["end", "sum_sent"])
    if sum_sent and sum_sent.get("retransmits") is not None:
        retransmits = parse_number_token(sum_sent.get("retransmits"))

    interval_rows: list[dict[str, Any]] = []
    intervals = data.get("intervals") or []
    if not isinstance(intervals, list):
        intervals = []

    interval_retransmit_values: list[float] = []
    for idx, interval in enumerate(intervals):
        if not isinstance(interval, dict):
            continue

        interval_sum = interval.get("sum")
        if not isinstance(interval_sum, dict):
            interval_sum = {}
        omitted = bool(interval_sum.get("omitted"))
        if omitted:
            continue

        bps = parse_number_token(interval_sum.get("bits_per_second"))
        interval_throughput = bps / 1_000_000.0 if not math.isnan(bps) else math.nan
        start = parse_number_token(interval_sum.get("start"))
        finish = parse_number_token(interval_sum.get("end"))
        interval_duration = parse_number_token(interval_sum.get("seconds"))
        interval_retx = interval_retransmits(interval)
        if not math.isnan(interval_retx):
            interval_retransmit_values.append(interval_retx)

        tcp_info = extract_tcp_info(candidate_dicts(interval))
        interval_rows.append(
            {
                "interval_index": idx,
                "interval_start_s": start,
                "interval_end_s": finish,
                "interval_mid_s": (start + finish) / 2.0
                if not math.isnan(start) and not math.isnan(finish)
                else math.nan,
                "interval_seconds": interval_duration,
                "interval_throughput_mbps": interval_throughput,
                "interval_retransmits": interval_retx,
                "rtt_ms": tcp_info["rtt_ms"],
                "rttvar_ms": tcp_info["rttvar_ms"],
                "snd_cwnd_bytes": tcp_info["snd_cwnd_bytes"],
                "snd_wnd_bytes": tcp_info["snd_wnd_bytes"],
            }
        )

    if math.isnan(retransmits) and interval_retransmit_values:
        retransmits = float(sum(interval_retransmit_values))

    if math.isnan(duration) and interval_rows:
        ends = [row["interval_end_s"] for row in interval_rows if not math.isnan(row["interval_end_s"])]
        if ends:
            duration = max(ends)

    interval_df = pd.DataFrame(interval_rows)
    end_tcp = extract_tcp_info(candidate_dicts(end))
    mean_rtt_ms = end_tcp["rtt_ms"]
    p95_rtt_ms = math.nan
    mean_cwnd = end_tcp["snd_cwnd_bytes"]
    mean_snd_wnd = end_tcp["snd_wnd_bytes"]

    if not interval_df.empty:
        if interval_df["rtt_ms"].notna().any():
            mean_rtt_ms = float(interval_df["rtt_ms"].mean())
            p95_rtt_ms = float(interval_df["rtt_ms"].quantile(0.95))
        if interval_df["snd_cwnd_bytes"].notna().any():
            mean_cwnd = float(interval_df["snd_cwnd_bytes"].mean())
        if interval_df["snd_wnd_bytes"].notna().any():
            mean_snd_wnd = float(interval_df["snd_wnd_bytes"].mean())

    convergence_time = compute_convergence_time(interval_df, throughput_mbps)

    return {
        "summary_source": summary_key,
        "summary": {
            "throughput_mbps": throughput_mbps,
            "total_bytes": total_bytes,
            "duration_seconds": duration,
            "retransmits": retransmits,
            "mean_rtt_ms": mean_rtt_ms,
            "p95_rtt_ms": p95_rtt_ms,
            "mean_cwnd_bytes": mean_cwnd,
            "mean_snd_wnd_bytes": mean_snd_wnd,
            "interval_count": len(interval_rows),
            "convergence_time_s": convergence_time,
        },
        "intervals": interval_rows,
    }


def compute_convergence_time(interval_df: pd.DataFrame, final_throughput_mbps: float) -> float:
    if interval_df.empty or math.isnan(final_throughput_mbps) or final_throughput_mbps <= 0:
        return math.nan
    if "interval_throughput_mbps" not in interval_df or "interval_end_s" not in interval_df:
        return math.nan

    threshold = final_throughput_mbps * 0.90
    throughputs = interval_df["interval_throughput_mbps"].tolist()
    ends = interval_df["interval_end_s"].tolist()
    for idx in range(0, max(0, len(throughputs) - 2)):
        window = throughputs[idx : idx + 3]
        if all(pd.notna(value) and value >= threshold for value in window):
            end_value = ends[idx]
            return float(end_value) if pd.notna(end_value) else math.nan
    return math.nan


def build_dataframes(
    raw_files: list[Path],
    catalog: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    trial_rows: list[dict[str, Any]] = []
    interval_rows: list[dict[str, Any]] = []
    quality: dict[str, Any] = {
        "raw_files_found": len(raw_files),
        "parse_failures": [],
        "metadata_warnings": list(catalog.get("metadata_errors", [])),
    }

    for raw_path in raw_files:
        metadata, metadata_warnings = parse_metadata(raw_path, catalog)
        for warning in metadata_warnings:
            quality["metadata_warnings"].append({"path": str(raw_path), "error": warning})

        try:
            parsed = parse_iperf_json(raw_path)
        except Exception as exc:
            quality["parse_failures"].append({"path": str(raw_path), "error": str(exc)})
            continue

        row = {**metadata, **parsed["summary"]}
        rate = parse_number_token(row.get("rate_mbps"))
        throughput = parse_number_token(row.get("throughput_mbps"))
        duration = parse_number_token(row.get("duration_seconds"))
        retransmits = parse_number_token(row.get("retransmits"))
        row["utilization"] = (
            throughput / rate if not math.isnan(throughput) and not math.isnan(rate) and rate > 0 else math.nan
        )
        row["retransmits_per_second"] = (
            retransmits / duration
            if not math.isnan(retransmits) and not math.isnan(duration) and duration > 0
            else math.nan
        )

        trial_rows.append(row)

        for interval in parsed["intervals"]:
            interval_row = {
                "suite": row.get("suite"),
                "case_slug": row.get("case_slug"),
                "common_link_category": row.get("common_link_category"),
                "algorithm": row.get("algorithm"),
                "trial": row.get("trial"),
                "delay_ms": row.get("delay_ms"),
                "jitter_ms": row.get("jitter_ms"),
                "rate_mbps": row.get("rate_mbps"),
                "loss_pct": row.get("loss_pct"),
                "loss_correlation_pct": row.get("loss_correlation_pct"),
                "queue_depth_packets": row.get("queue_depth_packets"),
                "raw_file": row.get("raw_file"),
                **interval,
            }
            interval_rows.append(interval_row)

    trials_df = pd.DataFrame(trial_rows)
    intervals_df = pd.DataFrame(interval_rows)

    for column in TABLE_COLUMNS:
        if column not in trials_df:
            trials_df[column] = pd.NA
    trials_df = trials_df[TABLE_COLUMNS]

    numeric_columns = [
        "trial",
        "delay_ms",
        "jitter_ms",
        "rate_mbps",
        "loss_pct",
        "loss_correlation_pct",
        "queue_depth_packets",
        "throughput_mbps",
        "utilization",
        "total_bytes",
        "duration_seconds",
        "retransmits",
        "retransmits_per_second",
        "mean_rtt_ms",
        "p95_rtt_ms",
        "mean_cwnd_bytes",
        "mean_snd_wnd_bytes",
        "interval_count",
        "convergence_time_s",
    ]
    for column in numeric_columns:
        trials_df[column] = pd.to_numeric(trials_df[column], errors="coerce")

    if not intervals_df.empty:
        for column in [
            "trial",
            "delay_ms",
            "jitter_ms",
            "rate_mbps",
            "loss_pct",
            "loss_correlation_pct",
            "queue_depth_packets",
            "interval_index",
            "interval_start_s",
            "interval_end_s",
            "interval_mid_s",
            "interval_seconds",
            "interval_throughput_mbps",
            "interval_retransmits",
            "rtt_ms",
            "rttvar_ms",
            "snd_cwnd_bytes",
            "snd_wnd_bytes",
        ]:
            if column in intervals_df:
                intervals_df[column] = pd.to_numeric(intervals_df[column], errors="coerce")

    quality["parsed_files"] = len(trials_df)
    return trials_df, intervals_df, quality


def ci_multiplier(n: int) -> float:
    if n <= 1:
        return 0.0
    if scipy_stats is not None:
        return float(scipy_stats.t.ppf(0.975, n - 1))
    return 1.96


def stat_series(group: pd.DataFrame, column: str) -> dict[str, float]:
    values = pd.to_numeric(group[column], errors="coerce").dropna()
    n = int(values.count())
    if n == 0:
        return {"mean": math.nan, "median": math.nan, "std": math.nan, "sem": math.nan, "ci95": math.nan, "n": 0}
    mean = float(values.mean())
    median = float(values.median())
    std = float(values.std(ddof=1)) if n > 1 else 0.0
    sem = std / math.sqrt(n) if n > 1 else 0.0
    ci95 = ci_multiplier(n) * sem
    return {"mean": mean, "median": median, "std": std, "sem": sem, "ci95": ci95, "n": n}


def aggregate_results(trials_df: pd.DataFrame, intervals_df: pd.DataFrame | None = None) -> pd.DataFrame:
    if trials_df.empty:
        return pd.DataFrame(columns=AGG_COLUMNS)

    group_cols = [
        "suite",
        "delay_ms",
        "jitter_ms",
        "rate_mbps",
        "loss_pct",
        "loss_correlation_pct",
        "queue_depth_packets",
        "common_link_category",
        "algorithm",
    ]

    rows: list[dict[str, Any]] = []
    grouped = trials_df.groupby(group_cols, dropna=False, sort=True)
    for key, group in grouped:
        row = dict(zip(group_cols, key))
        throughput = stat_series(group, "throughput_mbps")
        utilization = stat_series(group, "utilization")
        retx_rate = stat_series(group, "retransmits_per_second")
        convergence = stat_series(group, "convergence_time_s")

        row.update(
            {
                "n_trials": int(len(group)),
                "n_valid_throughput": throughput["n"],
                "mean_throughput_mbps": throughput["mean"],
                "median_throughput_mbps": throughput["median"],
                "std_throughput_mbps": throughput["std"],
                "sem_throughput_mbps": throughput["sem"],
                "ci95_throughput_mbps": throughput["ci95"],
                "mean_utilization": utilization["mean"],
                "median_utilization": utilization["median"],
                "std_utilization": utilization["std"],
                "sem_utilization": utilization["sem"],
                "ci95_utilization": utilization["ci95"],
                "mean_retransmits": stat_series(group, "retransmits")["mean"],
                "mean_retransmits_per_second": retx_rate["mean"],
                "std_retransmits_per_second": retx_rate["std"],
                "sem_retransmits_per_second": retx_rate["sem"],
                "ci95_retransmits_per_second": retx_rate["ci95"],
                "mean_rtt_ms": stat_series(group, "mean_rtt_ms")["mean"],
                "p95_rtt_ms": stat_series(group, "p95_rtt_ms")["mean"],
                "mean_cwnd_bytes": stat_series(group, "mean_cwnd_bytes")["mean"],
                "mean_snd_wnd_bytes": stat_series(group, "mean_snd_wnd_bytes")["mean"],
                "mean_convergence_time_s": convergence["mean"],
                "ci95_convergence_time_s": convergence["ci95"],
            }
        )
        rows.append(row)

    agg_df = pd.DataFrame(rows)

    if intervals_df is not None and not intervals_df.empty:
        interval_group_cols = [
            "suite",
            "delay_ms",
            "jitter_ms",
            "rate_mbps",
            "loss_pct",
            "loss_correlation_pct",
            "queue_depth_packets",
            "common_link_category",
            "algorithm",
        ]
        interval_aggs = []
        for key, group in intervals_df.groupby(interval_group_cols, dropna=False, sort=True):
            interval_aggs.append(
                {
                    **dict(zip(interval_group_cols, key)),
                    "interval_mean_rtt_ms": float(group["rtt_ms"].mean())
                    if "rtt_ms" in group and group["rtt_ms"].notna().any()
                    else math.nan,
                    "interval_p95_rtt_ms": float(group["rtt_ms"].quantile(0.95))
                    if "rtt_ms" in group and group["rtt_ms"].notna().any()
                    else math.nan,
                    "interval_mean_cwnd_bytes": float(group["snd_cwnd_bytes"].mean())
                    if "snd_cwnd_bytes" in group and group["snd_cwnd_bytes"].notna().any()
                    else math.nan,
                }
            )
        interval_agg_df = pd.DataFrame(interval_aggs)
        if not interval_agg_df.empty:
            agg_df = agg_df.merge(interval_agg_df, on=interval_group_cols, how="left")
            agg_df["mean_rtt_ms"] = agg_df["interval_mean_rtt_ms"].combine_first(agg_df["mean_rtt_ms"])
            agg_df["p95_rtt_ms"] = agg_df["interval_p95_rtt_ms"].combine_first(agg_df["p95_rtt_ms"])
            agg_df["mean_cwnd_bytes"] = agg_df["interval_mean_cwnd_bytes"].combine_first(
                agg_df["mean_cwnd_bytes"]
            )
            agg_df = agg_df.drop(
                columns=[
                    "interval_mean_rtt_ms",
                    "interval_p95_rtt_ms",
                    "interval_mean_cwnd_bytes",
                ]
            )

    for column in AGG_COLUMNS:
        if column not in agg_df:
            agg_df[column] = pd.NA
    return agg_df[AGG_COLUMNS]


def condition_label_from_row(row: pd.Series | dict[str, Any]) -> str:
    suite = row.get("suite", "unknown")
    category = row.get("common_link_category")
    if suite == "common_links" and not is_missing(category):
        return str(category)
    pieces = [str(suite)]
    if not is_missing(row.get("loss_pct")):
        pieces.append(f"loss={fmt_number(row.get('loss_pct'))}%")
    if not is_missing(row.get("delay_ms")):
        pieces.append(f"delay={fmt_number(row.get('delay_ms'))}ms")
    if not is_missing(row.get("rate_mbps")):
        pieces.append(f"rate={fmt_number(row.get('rate_mbps'))}Mbps")
    if not is_missing(row.get("jitter_ms")) and parse_number_token(row.get("jitter_ms")) != 0:
        pieces.append(f"jitter={fmt_number(row.get('jitter_ms'))}ms")
    return " | ".join(pieces)


def constants_for_title(data: pd.DataFrame, varying: str) -> str:
    parts: list[str] = []
    fields = [
        ("delay_ms", "delay", "ms"),
        ("jitter_ms", "jitter", "ms"),
        ("rate_mbps", "rate", "Mbps"),
        ("loss_pct", "loss", "%"),
        ("loss_correlation_pct", "corr", "%"),
        ("queue_depth_packets", "q", " packets"),
    ]
    for column, label, unit in fields:
        if column == varying or column not in data:
            continue
        values = pd.to_numeric(data[column], errors="coerce").dropna().unique()
        if len(values) == 1:
            value = values[0]
            if unit == " packets":
                parts.append(f"{label}={int(value)}{unit}")
            else:
                parts.append(f"{label}={fmt_number(value)}{unit}")
    return ", ".join(parts)


def save_figure(
    fig: plt.Figure,
    path: Path,
    manifest: list[dict[str, Any]],
    out_root: Path,
    description: str,
    source_data: str,
    subset: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)
    manifest.append(
        {
            "figure": str(path.relative_to(out_root)),
            "description": description,
            "source_data": source_data,
            "subset": subset,
        }
    )


def sorted_algorithms(data: pd.DataFrame) -> list[str]:
    preferred = ["cubic", "reno", "bbrv3", "bbr", "vegas"]
    present = [str(value) for value in data["algorithm"].dropna().unique()]
    ordered = [algorithm for algorithm in preferred if algorithm in present]
    ordered.extend(sorted([algorithm for algorithm in present if algorithm not in ordered]))
    return ordered


def line_error_plot(
    data: pd.DataFrame,
    x_col: str,
    y_col: str,
    yerr_col: str | None,
    xlabel: str,
    ylabel: str,
    title: str,
    path: Path,
    manifest: list[dict[str, Any]],
    out_root: Path,
    description: str,
    subset: str,
    *,
    ideal_line: bool = False,
) -> bool:
    plot_data = data.dropna(subset=[x_col, y_col])
    if plot_data.empty:
        return False

    fig, ax = plt.subplots(figsize=(7.6, 4.8))
    for algorithm in sorted_algorithms(plot_data):
        alg_data = plot_data[plot_data["algorithm"] == algorithm].sort_values(x_col)
        if alg_data.empty:
            continue
        yerr = None
        if yerr_col and yerr_col in alg_data:
            yerr = pd.to_numeric(alg_data[yerr_col], errors="coerce").fillna(0.0)
        ax.errorbar(
            alg_data[x_col],
            alg_data[y_col],
            yerr=yerr,
            marker="o",
            capsize=3,
            linewidth=2,
            label=algorithm,
        )

    if ideal_line:
        values = pd.to_numeric(plot_data[x_col], errors="coerce").dropna()
        if not values.empty:
            low = float(values.min())
            high = float(values.max())
            ax.plot([low, high], [low, high], color="black", linestyle="--", linewidth=1.4, label="ideal")

    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.3)
    ax.legend()
    save_figure(fig, path, manifest, out_root, description, "tables/aggregated_results.csv", subset)
    return True


def make_loss_plots(agg_df: pd.DataFrame, out: Path, manifest: list[dict[str, Any]]) -> int:
    data = agg_df[agg_df["suite"] == "loss"].copy()
    if data.empty:
        return 0
    constants = constants_for_title(data, "loss_pct")
    suffix = f" ({constants})" if constants else ""
    count = 0
    count += int(
        line_error_plot(
            data,
            "loss_pct",
            "mean_throughput_mbps",
            "ci95_throughput_mbps",
            "Loss (%)",
            "Throughput (Mbps)",
            f"Loss Suite: Throughput vs Loss{suffix}",
            out / "plots/loss/throughput_vs_loss_pct.png",
            manifest,
            out,
            "Mean receiver-side throughput versus configured loss with 95% confidence intervals.",
            "suite=loss",
        )
    )
    count += int(
        line_error_plot(
            data,
            "loss_pct",
            "mean_utilization",
            "ci95_utilization",
            "Loss (%)",
            "Utilization (throughput / configured rate)",
            f"Loss Suite: Utilization vs Loss{suffix}",
            out / "plots/loss/utilization_vs_loss_pct.png",
            manifest,
            out,
            "Mean link utilization versus configured loss.",
            "suite=loss",
        )
    )
    count += int(
        line_error_plot(
            data,
            "loss_pct",
            "mean_retransmits_per_second",
            "ci95_retransmits_per_second",
            "Loss (%)",
            "Retransmits per second",
            f"Loss Suite: Retransmits vs Loss{suffix}",
            out / "plots/loss/retransmits_per_second_vs_loss_pct.png",
            manifest,
            out,
            "Mean retransmits per second versus configured loss.",
            "suite=loss",
        )
    )
    count += make_optional_metric_plots(data, "loss", "loss_pct", "Loss (%)", out, manifest)
    return count


def make_delay_plots(agg_df: pd.DataFrame, out: Path, manifest: list[dict[str, Any]]) -> int:
    data = agg_df[agg_df["suite"] == "delay"].copy()
    if data.empty:
        return 0
    constants = constants_for_title(data, "delay_ms")
    suffix = f" ({constants})" if constants else ""
    count = 0
    count += int(
        line_error_plot(
            data,
            "delay_ms",
            "mean_throughput_mbps",
            "ci95_throughput_mbps",
            "Delay (ms)",
            "Throughput (Mbps)",
            f"Delay Suite: Throughput vs Delay{suffix}",
            out / "plots/delay/throughput_vs_delay_ms.png",
            manifest,
            out,
            "Mean receiver-side throughput versus configured one-way delay.",
            "suite=delay",
        )
    )
    count += int(
        line_error_plot(
            data,
            "delay_ms",
            "mean_utilization",
            "ci95_utilization",
            "Delay (ms)",
            "Utilization (throughput / configured rate)",
            f"Delay Suite: Utilization vs Delay{suffix}",
            out / "plots/delay/utilization_vs_delay_ms.png",
            manifest,
            out,
            "Mean link utilization versus configured delay.",
            "suite=delay",
        )
    )
    if data["mean_convergence_time_s"].notna().any():
        count += int(
            line_error_plot(
                data,
                "delay_ms",
                "mean_convergence_time_s",
                "ci95_convergence_time_s",
                "Delay (ms)",
                "Convergence time (s)",
                f"Delay Suite: Convergence Time vs Delay{suffix}",
                out / "plots/delay/convergence_time_vs_delay_ms.png",
                manifest,
                out,
                "First time to reach at least 90% of final throughput for three consecutive intervals.",
                "suite=delay",
            )
        )
    count += make_optional_metric_plots(data, "delay", "delay_ms", "Delay (ms)", out, manifest)
    return count


def make_rate_plots(agg_df: pd.DataFrame, out: Path, manifest: list[dict[str, Any]]) -> int:
    data = agg_df[agg_df["suite"] == "rate"].copy()
    if data.empty:
        return 0
    constants = constants_for_title(data, "rate_mbps")
    suffix = f" ({constants})" if constants else ""
    count = 0
    count += int(
        line_error_plot(
            data,
            "rate_mbps",
            "mean_throughput_mbps",
            "ci95_throughput_mbps",
            "Configured rate (Mbps)",
            "Measured throughput (Mbps)",
            f"Rate Suite: Throughput vs Configured Rate{suffix}",
            out / "plots/rate/throughput_vs_rate_mbps.png",
            manifest,
            out,
            "Mean receiver-side throughput versus configured netem rate, with ideal y=x line.",
            "suite=rate",
            ideal_line=True,
        )
    )
    count += int(
        line_error_plot(
            data,
            "rate_mbps",
            "mean_utilization",
            "ci95_utilization",
            "Configured rate (Mbps)",
            "Utilization (throughput / configured rate)",
            f"Rate Suite: Utilization vs Configured Rate{suffix}",
            out / "plots/rate/utilization_vs_rate_mbps.png",
            manifest,
            out,
            "Mean link utilization versus configured netem rate.",
            "suite=rate",
        )
    )
    count += make_optional_metric_plots(data, "rate", "rate_mbps", "Configured rate (Mbps)", out, manifest)
    return count


def make_optional_metric_plots(
    data: pd.DataFrame,
    suite: str,
    x_col: str,
    xlabel: str,
    out: Path,
    manifest: list[dict[str, Any]],
) -> int:
    count = 0
    if "mean_rtt_ms" in data and data["mean_rtt_ms"].notna().any():
        count += int(
            line_error_plot(
                data,
                x_col,
                "mean_rtt_ms",
                None,
                xlabel,
                "RTT (ms)",
                f"{suite.replace('_', ' ').title()} Suite: RTT vs Condition",
                out / f"plots/{suite}/rtt_vs_{x_col}.png",
                manifest,
                out,
                "Mean RTT from iperf TCP info where available.",
                f"suite={suite}",
            )
        )
    if "mean_cwnd_bytes" in data and data["mean_cwnd_bytes"].notna().any():
        count += int(
            line_error_plot(
                data,
                x_col,
                "mean_cwnd_bytes",
                None,
                xlabel,
                "Congestion window (bytes)",
                f"{suite.replace('_', ' ').title()} Suite: Mean cwnd vs Condition",
                out / f"plots/{suite}/mean_cwnd_vs_{x_col}.png",
                manifest,
                out,
                "Mean congestion window from iperf TCP info where available.",
                f"suite={suite}",
            )
        )
    return count


def grouped_bar_plot(
    data: pd.DataFrame,
    y_col: str,
    yerr_col: str | None,
    ylabel: str,
    title: str,
    path: Path,
    manifest: list[dict[str, Any]],
    out: Path,
    description: str,
) -> bool:
    plot_data = data.dropna(subset=["common_link_category", y_col])
    if plot_data.empty:
        return False

    categories = list(dict.fromkeys(plot_data.sort_values(["delay_ms", "rate_mbps"])["common_link_category"]))
    algorithms = sorted_algorithms(plot_data)
    if not categories or not algorithms:
        return False

    fig, ax = plt.subplots(figsize=(8.2, 4.8))
    x_positions = list(range(len(categories)))
    width = min(0.8 / max(1, len(algorithms)), 0.22)

    for alg_index, algorithm in enumerate(algorithms):
        alg_data = plot_data[plot_data["algorithm"] == algorithm].set_index("common_link_category")
        offsets = [x + (alg_index - (len(algorithms) - 1) / 2.0) * width for x in x_positions]
        values = [alg_data[y_col].get(category, math.nan) for category in categories]
        yerr = None
        if yerr_col and yerr_col in alg_data:
            yerr = [alg_data[yerr_col].get(category, 0.0) for category in categories]
        ax.bar(offsets, values, width=width, yerr=yerr, capsize=3, label=algorithm)

    ax.set_xticks(x_positions)
    ax.set_xticklabels(categories, rotation=15, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend()
    save_figure(fig, path, manifest, out, description, "tables/aggregated_results.csv", "suite=common_links")
    return True


def make_common_link_plots(agg_df: pd.DataFrame, out: Path, manifest: list[dict[str, Any]]) -> int:
    data = agg_df[agg_df["suite"] == "common_links"].copy()
    if data.empty:
        return 0
    count = 0
    count += int(
        grouped_bar_plot(
            data,
            "mean_throughput_mbps",
            "ci95_throughput_mbps",
            "Throughput (Mbps)",
            "Common Links: Throughput by Category",
            out / "plots/common_links/throughput_by_category_algorithm.png",
            manifest,
            out,
            "Grouped bar chart of mean receiver-side throughput by common link category and algorithm.",
        )
    )
    count += int(
        grouped_bar_plot(
            data,
            "mean_utilization",
            "ci95_utilization",
            "Utilization (throughput / configured rate)",
            "Common Links: Utilization by Category",
            out / "plots/common_links/utilization_by_category_algorithm.png",
            manifest,
            out,
            "Grouped bar chart of mean utilization by common link category and algorithm.",
        )
    )
    count += int(
        grouped_bar_plot(
            data,
            "mean_retransmits_per_second",
            "ci95_retransmits_per_second",
            "Retransmits per second",
            "Common Links: Retransmits by Category",
            out / "plots/common_links/retransmits_per_second_by_category_algorithm.png",
            manifest,
            out,
            "Grouped bar chart of retransmits per second by common link category and algorithm.",
        )
    )
    return count


def make_summary_plots(
    agg_df: pd.DataFrame,
    out: Path,
    manifest: list[dict[str, Any]],
    *,
    smoke: bool = False,
) -> pd.DataFrame:
    if agg_df.empty:
        return pd.DataFrame()

    condition_cols = [
        "suite",
        "delay_ms",
        "jitter_ms",
        "rate_mbps",
        "loss_pct",
        "loss_correlation_pct",
        "queue_depth_packets",
        "common_link_category",
    ]
    plot_data = agg_df.copy()
    plot_data["condition_label"] = plot_data.apply(condition_label_from_row, axis=1)

    pivot = plot_data.pivot_table(
        index="condition_label",
        columns="algorithm",
        values="mean_utilization",
        aggfunc="mean",
        dropna=False,
    )
    if not pivot.empty:
        pivot = pivot.reindex(columns=sorted_algorithms(plot_data))
        matrix = pivot.to_numpy(dtype=float)
        fig_height = max(5.0, min(12.0, 0.38 * len(pivot.index) + 1.8))
        fig_width = max(7.5, 1.3 * max(1, len(pivot.columns)) + 4.0)
        fig, ax = plt.subplots(figsize=(fig_width, fig_height))
        cmap = plt.get_cmap("viridis").copy()
        cmap.set_bad("#f0f0f0")
        image = ax.imshow(matrix, aspect="auto", cmap=cmap, vmin=0)
        ax.set_xticks(range(len(pivot.columns)))
        ax.set_xticklabels(pivot.columns, rotation=20, ha="right")
        ax.set_yticks(range(len(pivot.index)))
        ax.set_yticklabels(pivot.index)
        ax.set_title("Utilization by Condition and Algorithm")
        cbar = fig.colorbar(image, ax=ax)
        cbar.set_label("Utilization")
        if len(pivot.index) <= 35 and len(pivot.columns) <= 8:
            for row_idx in range(matrix.shape[0]):
                for col_idx in range(matrix.shape[1]):
                    value = matrix[row_idx, col_idx]
                    if not math.isnan(value):
                        ax.text(col_idx, row_idx, f"{value:.2f}", ha="center", va="center", color="white")
        save_figure(
            fig,
            out / "plots/summary/utilization_by_condition_heatmap.png",
            manifest,
            out,
            "Heatmap-style summary of mean utilization by condition and algorithm.",
            "tables/aggregated_results.csv",
            "all suites",
        )

    winners: list[dict[str, Any]] = []
    for _, group in plot_data.groupby(condition_cols, dropna=False, sort=True):
        condition = condition_label_from_row(group.iloc[0])
        throughput_group = group.dropna(subset=["mean_throughput_mbps"])
        utilization_group = group.dropna(subset=["mean_utilization"])
        throughput_winner = (
            throughput_group.loc[throughput_group["mean_throughput_mbps"].idxmax()]
            if not throughput_group.empty
            else None
        )
        utilization_winner = (
            utilization_group.loc[utilization_group["mean_utilization"].idxmax()]
            if not utilization_group.empty
            else None
        )
        winners.append(
            {
                "condition": condition,
                "throughput_winner": throughput_winner["algorithm"] if throughput_winner is not None else pd.NA,
                "winner_throughput_mbps": throughput_winner["mean_throughput_mbps"]
                if throughput_winner is not None
                else math.nan,
                "utilization_winner": utilization_winner["algorithm"] if utilization_winner is not None else pd.NA,
                "winner_utilization": utilization_winner["mean_utilization"]
                if utilization_winner is not None
                else math.nan,
            }
        )

    winners_df = pd.DataFrame(winners)
    winners_df.to_csv(out / "tables/winner_table.csv", index=False)

    if not smoke and not winners_df.empty:
        visible = winners_df.copy()
        visible["winner_throughput_mbps"] = visible["winner_throughput_mbps"].map(
            lambda value: f"{value:.2f}" if pd.notna(value) else ""
        )
        visible["winner_utilization"] = visible["winner_utilization"].map(
            lambda value: f"{value:.3f}" if pd.notna(value) else ""
        )
        fig_height = max(4.0, min(14.0, 0.35 * len(visible) + 1.2))
        fig, ax = plt.subplots(figsize=(11, fig_height))
        ax.axis("off")
        table = ax.table(
            cellText=visible.values,
            colLabels=[
                "Condition",
                "Best Throughput",
                "Mbps",
                "Best Utilization",
                "Util.",
            ],
            cellLoc="left",
            loc="center",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(7.5)
        table.scale(1, 1.25)
        ax.set_title("Winner Table by Condition", pad=12)
        save_figure(
            fig,
            out / "plots/summary/winner_table.png",
            manifest,
            out,
            "Table of the algorithm with highest mean throughput and highest mean utilization per condition.",
            "tables/aggregated_results.csv and tables/winner_table.csv",
            "all suites",
        )

    return winners_df


def condition_mask(df: pd.DataFrame, condition: dict[str, Any]) -> pd.Series:
    mask = df["suite"] == condition["suite"]
    for column, value in condition.items():
        if column == "suite" or column not in df:
            continue
        if is_missing(value):
            continue
        if isinstance(value, str):
            mask &= df[column] == value
        else:
            mask &= pd.to_numeric(df[column], errors="coerce").sub(float(value)).abs() < 1e-9
    return mask


def pick_low_mid_high(values: list[float]) -> list[float]:
    if not values:
        return []
    values = sorted(set(values))
    picks = [values[0], values[len(values) // 2], values[-1]]
    return list(dict.fromkeys(picks))


def representative_conditions(trials_df: pd.DataFrame, suite: str) -> list[dict[str, Any]]:
    data = trials_df[trials_df["suite"] == suite]
    if data.empty:
        return []
    conditions: list[dict[str, Any]] = []
    if suite == "loss":
        for value in pick_low_mid_high(pd.to_numeric(data["loss_pct"], errors="coerce").dropna().tolist()):
            conditions.append({"suite": suite, "loss_pct": value})
    elif suite == "delay":
        values = sorted(set(pd.to_numeric(data["delay_ms"], errors="coerce").dropna().tolist()))
        picks = [values[0]] if values else []
        if 50.0 in values:
            picks.append(50.0)
        elif values:
            picks.append(values[len(values) // 2])
        if values:
            picks.append(values[-1])
        for value in list(dict.fromkeys(picks)):
            conditions.append({"suite": suite, "delay_ms": value})
    elif suite == "rate":
        values = sorted(set(pd.to_numeric(data["rate_mbps"], errors="coerce").dropna().tolist()))
        picks = [values[0]] if values else []
        if 50.0 in values:
            picks.append(50.0)
        elif values:
            picks.append(values[len(values) // 2])
        if values:
            picks.append(values[-1])
        for value in list(dict.fromkeys(picks)):
            conditions.append({"suite": suite, "rate_mbps": value})
    elif suite == "common_links":
        for category in data["common_link_category"].dropna().drop_duplicates().tolist():
            conditions.append({"suite": suite, "common_link_category": category})
    return conditions


def choose_median_trial(sub_trials: pd.DataFrame) -> str | None:
    values = sub_trials.dropna(subset=["throughput_mbps", "raw_file"])
    if values.empty:
        return None
    median = statistics.median(values["throughput_mbps"].tolist())
    closest = values.iloc[(values["throughput_mbps"] - median).abs().argsort().iloc[0]]
    return str(closest["raw_file"])


def condition_name(condition: dict[str, Any]) -> str:
    if condition["suite"] == "loss":
        return f"loss_{pct_slug(condition.get('loss_pct'))}pct"
    if condition["suite"] == "delay":
        return f"delay_{fmt_number(condition.get('delay_ms'))}ms"
    if condition["suite"] == "rate":
        return f"rate_{fmt_number(condition.get('rate_mbps'))}mbps"
    if condition["suite"] == "common_links":
        return safe_filename(str(condition.get("common_link_category", "category")))
    return "condition"


def make_single_timeseries_plot(
    interval_data: pd.DataFrame,
    trial_data: pd.DataFrame,
    y_col: str,
    ylabel: str,
    title: str,
    path: Path,
    manifest: list[dict[str, Any]],
    out: Path,
    description: str,
) -> bool:
    plot_data = interval_data.dropna(subset=["interval_mid_s", y_col])
    if plot_data.empty:
        return False

    algorithms = sorted_algorithms(plot_data)
    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    fig, ax = plt.subplots(figsize=(8, 4.8))

    for alg_index, algorithm in enumerate(algorithms):
        color = colors[alg_index % len(colors)]
        alg_intervals = plot_data[plot_data["algorithm"] == algorithm]
        alg_trials = trial_data[trial_data["algorithm"] == algorithm]
        median_raw = choose_median_trial(alg_trials)

        for raw_file, trial_intervals in alg_intervals.groupby("raw_file"):
            trial_intervals = trial_intervals.sort_values("interval_mid_s")
            is_median = median_raw is not None and str(raw_file) == median_raw
            ax.plot(
                trial_intervals["interval_mid_s"],
                trial_intervals[y_col],
                color=color,
                linewidth=2.4 if is_median else 0.8,
                alpha=0.95 if is_median else 0.18,
                label=algorithm if is_median else None,
            )

        if median_raw is None and not alg_intervals.empty:
            first = alg_intervals.sort_values("interval_mid_s")
            ax.plot(first["interval_mid_s"], first[y_col], color=color, linewidth=2.0, label=algorithm)

    ax.set_title(title)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.3)
    ax.legend()
    save_figure(fig, path, manifest, out, description, "parsed interval data and tables/raw_trials.csv", title)
    return True


def make_time_series_plots(
    trials_df: pd.DataFrame,
    intervals_df: pd.DataFrame,
    out: Path,
    manifest: list[dict[str, Any]],
    *,
    smoke: bool = False,
) -> int:
    if intervals_df.empty or trials_df.empty:
        return 0

    count = 0
    suites = ["loss", "delay", "rate", "common_links"]
    if smoke:
        suites = [suite for suite in suites if suite in set(trials_df["suite"].dropna())][:1]

    for suite in suites:
        conditions = representative_conditions(trials_df, suite)
        if smoke:
            conditions = conditions[:1]
        for condition in conditions:
            sub_intervals = intervals_df[condition_mask(intervals_df, condition)]
            sub_trials = trials_df[condition_mask(trials_df, condition)]
            if sub_intervals.empty:
                continue
            name = f"{suite}_{condition_name(condition)}"
            title = f"{suite.replace('_', ' ').title()} Time Series: {condition_name(condition).replace('_', ' ')}"
            count += int(
                make_single_timeseries_plot(
                    sub_intervals,
                    sub_trials,
                    "interval_throughput_mbps",
                    "Throughput (Mbps)",
                    title,
                    out / f"plots/time_series/{safe_filename(name)}_throughput.png",
                    manifest,
                    out,
                    "Interval throughput over time; all trials are faint, median-throughput trial is emphasized.",
                )
            )
            if not smoke and "snd_cwnd_bytes" in sub_intervals and sub_intervals["snd_cwnd_bytes"].notna().any():
                count += int(
                    make_single_timeseries_plot(
                        sub_intervals,
                        sub_trials,
                        "snd_cwnd_bytes",
                        "snd_cwnd (bytes)",
                        f"{title}: cwnd",
                        out / f"plots/time_series/{safe_filename(name)}_cwnd.png",
                        manifest,
                        out,
                        "Congestion window over time where iperf exposes TCP info.",
                    )
                )
    return count


def write_tables(out: Path, trials_df: pd.DataFrame, agg_df: pd.DataFrame) -> None:
    trials_df.to_csv(out / "tables/raw_trials.csv", index=False)
    agg_df.to_csv(out / "tables/aggregated_results.csv", index=False)


def compute_missing_trials(trials_df: pd.DataFrame, catalog: dict[str, Any]) -> pd.DataFrame:
    if trials_df.empty:
        return pd.DataFrame()

    condition_cols = [
        "suite",
        "delay_ms",
        "jitter_ms",
        "rate_mbps",
        "loss_pct",
        "loss_correlation_pct",
        "queue_depth_packets",
        "common_link_category",
    ]
    algorithms = sorted([str(value) for value in trials_df["algorithm"].dropna().unique()])
    expected_trial_count = catalog.get("expected_trials")
    rows: list[dict[str, Any]] = []

    for _, condition_group in trials_df.groupby(condition_cols, dropna=False, sort=True):
        condition = condition_label_from_row(condition_group.iloc[0])
        if expected_trial_count:
            expected_trials = set(range(1, int(expected_trial_count) + 1))
        else:
            expected_trials = set(
                int(value)
                for value in pd.to_numeric(condition_group["trial"], errors="coerce").dropna().tolist()
            )
        for algorithm in algorithms:
            alg_group = condition_group[condition_group["algorithm"] == algorithm]
            actual_trials = set(
                int(value)
                for value in pd.to_numeric(alg_group["trial"], errors="coerce").dropna().tolist()
            )
            missing = sorted(expected_trials - actual_trials)
            if missing:
                rows.append(
                    {
                        "condition": condition,
                        "algorithm": algorithm,
                        "expected_trials": len(expected_trials),
                        "actual_trials": len(actual_trials),
                        "missing_count": len(missing),
                        "missing_trials": ", ".join(str(value) for value in missing),
                    }
                )
    return pd.DataFrame(rows)


def suspicious_results(trials_df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if trials_df.empty:
        return {}
    throughput = pd.to_numeric(trials_df["throughput_mbps"], errors="coerce")
    rate = pd.to_numeric(trials_df["rate_mbps"], errors="coerce")
    trial = pd.to_numeric(trials_df["trial"], errors="coerce")
    return {
        "throughput_mbps <= 0": trials_df[throughput <= 0],
        "throughput_mbps > 1.2 * configured rate_mbps": trials_df[
            throughput.notna() & rate.notna() & (throughput > 1.2 * rate)
        ],
        "missing algorithm": trials_df[trials_df["algorithm"].isna() | (trials_df["algorithm"].astype(str) == "")],
        "missing trial": trials_df[trial.isna()],
        "missing configured rate": trials_df[rate.isna()],
    }


def markdown_table(df: pd.DataFrame, columns: list[str], max_rows: int = 50) -> str:
    if df.empty:
        return "None.\n"
    visible = df[columns].head(max_rows).fillna("")
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for _, row in visible.iterrows():
        lines.append("| " + " | ".join(str(row[column]) for column in columns) + " |")
    if len(df) > max_rows:
        lines.append(f"\nShowing {max_rows} of {len(df)} rows.")
    return "\n".join(lines) + "\n"


def write_quality_report(
    out: Path,
    trials_df: pd.DataFrame,
    agg_df: pd.DataFrame,
    quality: dict[str, Any],
    catalog: dict[str, Any],
) -> pd.DataFrame:
    missing_df = compute_missing_trials(trials_df, catalog)
    suspicious = suspicious_results(trials_df)
    algorithms = sorted([str(value) for value in trials_df["algorithm"].dropna().unique()])
    suites = sorted([str(value) for value in trials_df["suite"].dropna().unique()])

    lines = [
        "# Data Quality Report",
        "",
        f"- Raw JSON files found: {quality.get('raw_files_found', 0)}",
        f"- Successfully parsed: {quality.get('parsed_files', 0)}",
        f"- Parse failures: {len(quality.get('parse_failures', []))}",
        f"- Algorithms detected: {', '.join(algorithms) if algorithms else 'None'}",
        f"- Suites detected: {', '.join(suites) if suites else 'None'}",
        "",
        "## Parse Failures",
        "",
    ]
    if quality.get("parse_failures"):
        for failure in quality["parse_failures"]:
            lines.append(f"- `{failure['path']}`: {failure['error']}")
    else:
        lines.append("None.")

    if quality.get("metadata_warnings"):
        lines.extend(["", "## Metadata Warnings", ""])
        for warning in quality["metadata_warnings"]:
            lines.append(f"- `{warning.get('path', '')}`: {warning.get('error', '')}")

    lines.extend(["", "## Missing Trials", ""])
    lines.append(
        markdown_table(
            missing_df,
            ["condition", "algorithm", "expected_trials", "actual_trials", "missing_count", "missing_trials"],
            max_rows=200,
        )
    )

    lines.extend(["", "## Suspicious Results", ""])
    columns = ["suite", "algorithm", "trial", "throughput_mbps", "rate_mbps", "raw_file"]
    for label, data in suspicious.items():
        lines.extend([f"### {label}", ""])
        lines.append(markdown_table(data, columns, max_rows=50))

    lines.extend(["", "## Aggregated Row Count", "", f"- Aggregated condition/algorithm rows: {len(agg_df)}"])
    (out / "data_quality.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return missing_df


def write_analysis_readme(out: Path) -> None:
    readme = """# EE 122 Results Analysis

## How to Run

```bash
python analyze_results.py --results-root ee122_results --out analysis
python analyze_results.py --results-root ee122_results --out analysis --smoke
```

Use the actual result directory path for `--results-root`; this repo also supports paths such as `Final Test Outputs`.

## Tables

- `tables/raw_trials.csv` has one row per parsed iperf3 JSON file.
- `tables/aggregated_results.csv` groups trials by suite, condition parameters, common link category, and algorithm.
- `tables/winner_table.csv` lists the algorithm with the highest mean throughput and highest mean utilization for each condition.

## Metrics

- Throughput uses receiver-side iperf3 output when available: `end.sum_received.bits_per_second`, then `end.sum.bits_per_second`, then `end.sum_sent.bits_per_second`, converted to Mbps.
- Utilization is `throughput_mbps / configured rate_mbps`.
- Retransmits use `end.sum_sent.retransmits`; if absent, interval retransmits are summed when present. Missing retransmit data is left as NaN.
- Confidence intervals are computed across trials. If SciPy is available, a t critical value is used; otherwise the script uses `1.96 * SEM`.
- Convergence time is the first non-omitted interval where throughput reaches at least 90% of that trial's final throughput for three consecutive intervals.

## Figures

- Loss plots compare algorithms as packet loss changes while other parameters are held constant.
- Delay plots compare behavior as configured delay changes and include convergence time when interval data supports it.
- Rate plots compare measured throughput against configured rate and include an ideal `y=x` reference line.
- Common-link plots compare Terrestrial, LEO, and GEO-like emulated paths when those categories are present.
- Summary plots show utilization by condition and algorithm and identify winners by throughput and utilization.
- Time-series plots show interval throughput over time; all trials are faint and the median-throughput trial is emphasized.

## Caveats

- These experiments use the configured queue depth from metadata. In the default runner, queue depth is a deep buffer of roughly `2 * BDP` unless metadata says otherwise.
- RTT and congestion-window plots are generated only when the iperf3 JSON exposes usable TCP info fields such as `rtt`, `rttvar`, `snd_cwnd`, or `snd_wnd`.
- Some platforms report TCP info only as zero or omit it entirely; those values are treated as unavailable.
"""
    (out / "README.md").write_text(readme, encoding="utf-8")


def make_smoke_plots(
    agg_df: pd.DataFrame,
    trials_df: pd.DataFrame,
    intervals_df: pd.DataFrame,
    out: Path,
    manifest: list[dict[str, Any]],
) -> int:
    count = 0
    for suite, x_col, xlabel, plot_dir in [
        ("loss", "loss_pct", "Loss (%)", "loss"),
        ("delay", "delay_ms", "Delay (ms)", "delay"),
        ("rate", "rate_mbps", "Configured rate (Mbps)", "rate"),
    ]:
        data = agg_df[agg_df["suite"] == suite].copy()
        if data.empty:
            continue
        count += int(
            line_error_plot(
                data,
                x_col,
                "mean_throughput_mbps",
                "ci95_throughput_mbps",
                xlabel,
                "Throughput (Mbps)",
                f"Smoke: {suite.title()} Throughput",
                out / f"plots/{plot_dir}/smoke_throughput.png",
                manifest,
                out,
                "Smoke-mode throughput plot for one available suite.",
                f"suite={suite}",
            )
        )
        break

    make_summary_plots(agg_df, out, manifest, smoke=True)
    if count == 0:
        count += make_time_series_plots(trials_df, intervals_df, out, manifest, smoke=True)
    return count


def write_figure_manifest(out: Path, manifest: list[dict[str, Any]]) -> None:
    (out / "figure_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    results_root = args.results_root.expanduser().resolve()
    out = args.out.expanduser().resolve()

    if not results_root.exists():
        raise SystemExit(f"results root does not exist: {results_root}")
    if not results_root.is_dir():
        raise SystemExit(f"results root is not a directory: {results_root}")

    ensure_output_dirs(out)
    if not args.smoke:
        clear_smoke_only_figures(out)
    plt.rcParams.update(
        {
            "figure.dpi": 120,
            "savefig.dpi": 180,
            "axes.grid": False,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "font.size": 10,
            "axes.titlesize": 12,
            "axes.labelsize": 10,
            "legend.fontsize": 9,
        }
    )

    raw_files, discovery_quality = discover_files(results_root)
    catalog = load_metadata_catalog(results_root)
    trials_df, intervals_df, quality = build_dataframes(raw_files, catalog)
    quality.update(discovery_quality)
    agg_df = aggregate_results(trials_df, intervals_df)

    write_tables(out, trials_df, agg_df)
    manifest: list[dict[str, Any]] = []

    if args.smoke:
        make_smoke_plots(agg_df, trials_df, intervals_df, out, manifest)
    else:
        make_loss_plots(agg_df, out, manifest)
        make_delay_plots(agg_df, out, manifest)
        make_rate_plots(agg_df, out, manifest)
        make_common_link_plots(agg_df, out, manifest)
        make_summary_plots(agg_df, out, manifest, smoke=False)
        make_time_series_plots(trials_df, intervals_df, out, manifest, smoke=False)

    write_figure_manifest(out, manifest)
    write_quality_report(out, trials_df, agg_df, quality, catalog)
    write_analysis_readme(out)

    print(f"Raw JSON files found: {quality.get('raw_files_found', 0)}")
    print(f"Successfully parsed: {quality.get('parsed_files', 0)}")
    print(f"Parse failures: {len(quality.get('parse_failures', []))}")
    print(f"Generated figures: {len(manifest)}")
    print(f"Wrote analysis to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# EE 122 Results Analysis

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

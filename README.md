# EE 122 TCP CCA Test Runner

This package contains a client-side automation script for the EE 122 BBRv3/CUBIC/Reno experiment, plus safer setup scripts for the client and server VMs.

## Files

- `run_ee122_tests.py`: run this on the **client VM**. It configures `tc netem` on `ifb0`, runs every iperf3 test, and writes raw JSON plus metadata.
- `client_setup_idempotent.bash`: improved client setup script. It redirects ingress traffic from the real NIC to `ifb0`, disables offloading, and is safe to rerun.
- `server_setup_fixed.bash`: improved server setup script. It uses the interface argument instead of hardcoding `ens160`.

## Server VM setup

Find the server interface and IP address:

```bash
ip addr
```

Then run:

```bash
chmod +x server_setup_fixed.bash
sudo ./server_setup_fixed.bash <server-interface>
iperf3 -s -p 65535
```

Leave the iperf3 server running.

## Client VM setup

Find the client interface:

```bash
ip addr
```

Then run:

```bash
chmod +x client_setup_idempotent.bash run_ee122_tests.py
sudo ./client_setup_idempotent.bash <client-interface>
```

## Recommended smoke test

Run this first to verify the server, interface setup, iperf3, `tc`, and one congestion-control algorithm:

```bash
./run_ee122_tests.py \
  --server <SERVER_IPV4> \
  --suites loss \
  --algorithms cubic \
  --trials 1 \
  --duration 10 \
  --omit 2 \
  --output-root ee122_smoke
```

## Full default run

```bash
./run_ee122_tests.py --server <SERVER_IPV4> --output-root ee122_results
```

The default run executes all matrices with `cubic`, `reno`, and `bbrv3` labels for 5 trials each.

Important runtime note: the default matrix contains 23 cases. With 3 algorithms and 5 trials, that is 345 iperf3 runs. At 300 seconds per run plus a 60 second omitted warmup and a short settle delay, this is more than a typical overnight run. Use `--suites`, `--algorithms`, or `--trials` to subset.

Examples:

```bash
# Only delay tests, all algorithms, 5 trials
./run_ee122_tests.py --server <SERVER_IPV4> --suites delay --output-root ee122_delay

# Only common link tests for BBRv3 and CUBIC
./run_ee122_tests.py --server <SERVER_IPV4> --suites common_links --algorithms bbrv3 cubic

# Dry-run all commands without changing tc or running iperf
./run_ee122_tests.py --server <SERVER_IPV4> --dry-run
```

## Output layout

Each run creates a run directory like:

```text
ee122_results/
  20260509T123456Z_<host>_<id>/
    manifest.json
    index.jsonl
    loss/
      case01_delay-50ms_jitter-0ms_rate-50mbps_loss-0pct_corr-0pct_q417/
        cubic/
          cubic_50ms_0ms_50mbps_0_0_trial1.json
          cubic_50ms_0ms_50mbps_0_0_trial1.meta.json
          cubic_50ms_0ms_50mbps_0_0_trial1.stderr.log
```

`manifest.json` records run-level configuration, commands, host information, available TCP congestion controls, selected algorithms, and the full expanded test plan. `index.jsonl` has one JSON object per attempted trial for easier downstream analysis.

The raw iperf3 JSON files follow the project filename pattern:

```text
<algorithm>_<delay>ms_<jitter>ms_<rate>mbps_<loss%>_<loss_correlation%>_trial<trial #>.json
```

## Editing matrices

Open `run_ee122_tests.py` and edit the top `TEST_MATRICES` section. Adding a new test row is one line. For example, to add 5% loss:

```python
TEST_MATRICES = {
    "loss": [
        tc_case(50, 0, 50, "0", queue_packets=417),
        # ...
        tc_case(50, 0, 50, "5", queue_packets=417),
    ],
}
```

`tc_case(...)` can also calculate queue depth automatically if `queue_packets` is omitted, using the `2 * BDP` helper at the top of the file.

## BBRv3 algorithm naming

The default config labels results as `bbrv3` but passes `-C bbr` to iperf3/Linux:

```python
{"label": "bbrv3", "iperf_name": "bbr"}
```

Before the overnight run, check what your kernel exposes:

```bash
cat /proc/sys/net/ipv4/tcp_available_congestion_control
```

If your VM exposes BBRv3 as `bbr3`, change the config to:

```python
{"label": "bbrv3", "iperf_name": "bbr3"}
```

The runner will fail preflight if a configured congestion-control algorithm is unavailable.

## Failure behavior

By default, the runner fails fast on the first failed trial and writes a `.failure.json` next to the intended output. To continue collecting data after failures:

```bash
./run_ee122_tests.py --server <SERVER_IPV4> --continue-on-error
```

## End-of-run cleanup

By default, the runner removes the root qdisc from `ifb0` at the end. To leave the last emulated condition installed for inspection:

```bash
./run_ee122_tests.py --server <SERVER_IPV4> --no-cleanup-at-end
```

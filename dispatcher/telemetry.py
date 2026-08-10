"""Streaming latency stats (Phase H): TTFT, inter-token latency percentiles,
throughput — computed from delta-arrival timestamps the quick path already
sees. Metric selection after openjarvis telemetry/itl.py (Apache-2.0,
references/openjarvis); the math is elementary and reimplemented.
"""

from __future__ import annotations


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Nearest-rank percentile on an already-sorted list."""
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1,
                   round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def stream_stats(start: float, delta_times: list[float],
                 output_tokens: int | None = None,
                 streamed: bool = True) -> dict:
    """start + delta_times are monotonic seconds. Returns {} when no deltas
    arrived (failed/empty runs write no latency columns). tokens_per_s uses
    real token counts when the backend reported them, else the delta count
    as a floor estimate.

    streamed=False means the backend produced no incremental text and the
    caller synthesized ONE delta from the final result. There is no
    time-to-first-token in that run — the first "delta" arrived when the run
    ended — so the latency columns are omitted rather than recorded as a
    ~30s TTFT that would sit in /stats' quick_latency averages looking exactly
    like a real measurement (fixed 2026-08-10).
    """
    if not delta_times or not streamed:
        return {}
    ttft_ms = (delta_times[0] - start) * 1000
    gaps = sorted((b - a) * 1000 for a, b in zip(delta_times, delta_times[1:]))
    duration = delta_times[-1] - start
    units = output_tokens if output_tokens else len(delta_times)
    return {
        "ttft_ms": round(ttft_ms, 1),
        "itl_p95_ms": round(_percentile(gaps, 95), 1) if gaps else None,
        "tokens_per_s": round(units / duration, 2) if duration > 0 else None,
    }

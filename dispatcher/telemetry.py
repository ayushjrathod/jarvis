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
                 output_tokens: int | None = None) -> dict:
    """start + delta_times are monotonic seconds. Returns {} when no deltas
    arrived (failed/empty runs write no latency columns). tokens_per_s uses
    real token counts when the backend reported them, else the delta count
    as a floor estimate."""
    if not delta_times:
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

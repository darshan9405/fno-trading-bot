#!/usr/bin/env python3
"""Grid runner for `breakout.lookback_days` x `breakout.swing_k`.

Wraps `scripts/backtest_strategy.py` (consecutive mode), captures the SUMMARY
and BY-PATTERN block from each cell, ranks by win_rate, and writes:

  - backtests/results/sweep_<TS>/cells.json     -- raw per-cell data
  - backtests/results/sweep_<TS>/summary.md     -- human-readable ranking
  - backtests/results/sweep_<TS>/cell_L*.log    -- per-cell raw backtest stdout

UPSTOX_INTEGRATION_TOKEN must be in the environment. The script NEVER echoes,
writes or logs the token.

Usage:
    UPSTOX_INTEGRATION_TOKEN=... python scripts/sweep_lookback.py
    UPSTOX_INTEGRATION_TOKEN=... python scripts/sweep_lookback.py --instrument NIFTY --days 90
    UPSTOX_INTEGRATION_TOKEN=... python scripts/sweep_lookback.py --all --days 180
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from itertools import product
from pathlib import Path

LOOKBACK_VALUES = [30, 45, 60, 75, 90, 120, 150, 200, 250]
SWING_K_VALUES = [3, 5, 7]

# Decision rule (kept here, not in CLI, so the audit trail is reproducible).
MIN_SIGNALS = 50
WORST_PCT_GUARDRAIL = -8.0

SUMMARY_RE = re.compile(
    r"=== SUMMARY ===\s*\n"
    r"Signals:\s*(\d+)\s*\|\s*Win rate:\s*(\d+)/(\d+)\s*\(([0-9.]+)%\)\s*\n"
    r"Avg return:\s*([+\-]?[0-9.]+)%\s*\|\s*Total \(sum\):\s*([+\-]?[0-9.]+)%\s*\n"
    r"Best:\s*([+\-]?[0-9.]+)%\s*\|\s*Worst:\s*([+\-]?[0-9.]+)%\s*\n",
    re.DOTALL,
)

BY_PATTERN_RE = re.compile(
    r"=== BY PATTERN ===\s*\n+(.*?)(?:\n\s*\n|\Z)",
    re.DOTALL,
)

PATTERN_ROW_RE = re.compile(
    r"^\s+(\w+)\s+n=(\d+)\s+win=([0-9.]+)%\s+avg=([+\-]?[0-9.]+)%\s*$",
    re.MULTILINE,
)


def _sanitize(text: str) -> str:
    """Strip any token-like strings from accidental echo before persisting."""
    if not text:
        return text
    return re.sub(r"ey[A-Za-z0-9_\-]{60,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", "<jwt-redacted>", text)


def _parse_summary(out: str) -> dict | None:
    m = SUMMARY_RE.search(out)
    if not m:
        return None
    n_sig, wins, _t, win_rate, avg, total, best, worst = m.groups()
    return {
        "n_signals": int(n_sig),
        "wins": int(wins),
        "win_rate_pct": float(win_rate),
        "avg_return_pct": float(avg),
        "total_return_pct": float(total),
        "best_pct": float(best),
        "worst_pct": float(worst),
    }


def _parse_by_pattern(out: str) -> dict:
    block = BY_PATTERN_RE.search(out)
    if not block:
        return {}
    per: dict[str, dict] = {}
    for pat, n, wr, avg in PATTERN_ROW_RE.findall(block.group(1)):
        per[pat] = {
            "n": int(n),
            "win_rate_pct": float(wr),
            "avg_return_pct": float(avg),
        }
    return per


def _run_cell(lookback: int, swing_k: int, days: int, warmup: int,
              instrument: str | None, use_all: bool, throttle: float,
              require_volume_spike: bool,
              extra_sets: list[tuple[str, object]],
              results_dir: Path) -> dict:
    args = [
        sys.executable, "scripts/backtest_strategy.py",
        "--days", str(days),
        "--warmup", str(warmup),
        # IMPORTANT: until --set promotes the full pattern set, --set
        # lookback/swing_k has no effect on the only enabled detector
        # (volume_breakout). Force the canonical 6 patterns for the sweep
        # so the parameters under test actually influence detection.
        # Use a JSON array so the backtest's _coerce -> set_setting ->
        # _decode(loads) round-trips back to a Python list and the
        # detector's `set(patterns_enabled)` works.
        "--set", 'breakout.patterns_enabled=["horizontal_range","trendline","triangle","flag_pennant","head_shoulders","volume_breakout"]',
        "--set", f"breakout.require_volume_spike={'true' if require_volume_spike else 'false'}",
        "--set", f"breakout.lookback_days={lookback}",
        "--set", f"breakout.swing_k={swing_k}",
    ]
    for k, v in extra_sets:
        args += ["--set", f"{k}={v}"]
    if use_all:
        args.append("--all")
    if instrument:
        args += ["--instrument", instrument]	

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    print(f"[cell] lb={lookback:>3} k={swing_k} days={days} warmup={warmup} "
          f"{'--all' if use_all else f'--instrument {instrument}' if instrument else 'default-universe'} ...",
          flush=True)

    proc = subprocess.run(args, env=env, capture_output=True, text=True, cwd=os.getcwd())
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    summary = _parse_summary(stdout)
    by_pattern = _parse_by_pattern(stdout)

    log_path = results_dir / f"cell_L{lookback:>03}_K{swing_k}.log"
    log_path.write_text(
        f"--- CMD ---\n{' '.join(args)}\n--- EXITCODE ---\n{proc.returncode}\n"
        f"--- STDOUT ---\n{_sanitize(stdout)}\n--- STDERR ---\n{_sanitize(stderr)}\n"
    )

    if throttle > 0:
        time.sleep(throttle)

    return {
        "lookback_days": lookback,
        "swing_k": swing_k,
        "summary": summary,
        "by_pattern": by_pattern,
        "exit_code": proc.returncode,
        "stderr_tail": [ln for ln in (stderr.strip().splitlines() or [])[-5:]],
    }


def _select_winner(cells: list[dict], min_signals: int = MIN_SIGNALS) -> dict | None:
    eligible = []
    for c in cells:
        s = c.get("summary")
        if not s:
            continue
        if s["n_signals"] < min_signals:
            continue
        if s["worst_pct"] < WORST_PCT_GUARDRAIL:
            continue
        eligible.append(c)

    if not eligible:
        return None

    # Sort: highest win_rate, then highest avg_return, then lowest lookback
    # (signal volume matters for options; tighter lookbacks reduce regime pessimism
    # only when they keep the same win_rate).
    eligible.sort(
        key=lambda c: (
            -c["summary"]["win_rate_pct"],
            -c["summary"]["avg_return_pct"],
            c["lookback_days"],
        )
    )
    return eligible[0]


def _render_markdown(cells: list[dict], winner: dict | None,
                     results_dir: Path, days: int, warmup: int,
                     universe: str) -> str:
    lines = [
        "# Lookback sweep results",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Output dir: `{results_dir}`",
        f"- Backtest window: {days} trading days, warmup {warmup}",
        f"- Universe: {universe}",
        f"- Decision rule: max `win_rate_pct`, tie-break by `avg_return_pct`, "
        f"tie-break by smallest `lookback_days`. Eligible cells: "
        f"`n_signals >= {MIN_SIGNALS}` and `worst_pct > {WORST_PCT_GUARDRAIL}%`.",
        "",
        "## Summary table (sorted by win_rate)",
        "",
        "| lookback | swing_k | n_signals | wins | win_rate | avg_ret | total_ret | best | worst |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    def _key(c):
        s = c.get("summary") or {}
        wr = s.get("win_rate_pct", -1.0) if s else -1.0
        return (-wr, c["lookback_days"])

    for c in sorted(cells, key=_key):
        s = c.get("summary")
        if not s:
            lines.append(f"| {c['lookback_days']} | {c['swing_k']} | "
                         f"(no SUMMARY) | | | | | | |")
            continue
        lines.append(
            f"| {c['lookback_days']} | {c['swing_k']} | {s['n_signals']} | "
            f"{s['wins']} | {s['win_rate_pct']:.1f}% | "
            f"{s['avg_return_pct']:+.2f}% | {s['total_return_pct']:+.2f}% | "
            f"{s['best_pct']:+.2f}% | {s['worst_pct']:+.2f}% |"
        )

    lines += ["", "## Per-pattern breakdown (winner only)"]
    if winner:
        s = winner["summary"]
        lines += [
            "",
            f"**WINNER:** `breakout.lookback_days={winner['lookback_days']}`, "
            f"`breakout.swing_k={winner['swing_k']}`  →  "
            f"win_rate {s['win_rate_pct']:.1f}%, n={s['n_signals']}, "
            f"avg {s['avg_return_pct']:+.2f}%, worst {s['worst_pct']:+.2f}%",
            "",
            "| Pattern | n | win_rate | avg_ret |",
            "|---|---|---|---|",
        ]
        for pat, p in sorted((winner.get("by_pattern") or {}).items()):
            lines.append(
                f"| {pat} | {p['n']} | {p['win_rate_pct']:.1f}% | "
                f"{p['avg_return_pct']:+.2f}% |"
            )
    else:
        lines += [
            "",
            "**No eligible winner** — every cell either produced fewer than "
            f"{MIN_SIGNALS} signals or had a worst-case drawdown worse than "
            f"{WORST_PCT_GUARDRAIL}%. Lower MIN_SIGNALS or investigate.",
        ]

    lines += ["", "## Per-pattern breakdown (all cells)"]
    # Per-pattern macro: which value of (lb, k) is best for each pattern
    pat_best: dict[str, list[tuple[int, int, dict]]] = {}
    for c in cells:
        for pat, p in (c.get("by_pattern") or {}).items():
            pat_best.setdefault(pat, []).append((c["lookback_days"], c["swing_k"], p))

    for pat in sorted(pat_best):
        rows = pat_best[pat]
        rows.sort(
            key=lambda r: (
                -r[2]["win_rate_pct"],
                -r[2]["avg_return_pct"],
                r[0],
            )
        )
        if not rows:
            continue
        lb, kk, p = rows[0]
        lines.append(
            f"- `{pat}` → best at lookback={lb}, swing_k={kk}  "
            f"(n={p['n']}, win={p['win_rate_pct']:.1f}%, avg={p['avg_return_pct']:+.2f}%)"
        )

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--warmup", type=int, default=270)
    parser.add_argument("--all", action="store_true", help="use full F&O universe")
    parser.add_argument("--instrument", default=None, help="single instrument (for sanity tests)")
    parser.add_argument("--throttle", type=float, default=2.0, help="seconds sleep between cells")
    parser.add_argument("--lookbacks", default=",".join(str(v) for v in LOOKBACK_VALUES),
                        help="comma-separated lookback values")
    parser.add_argument("--swing-ks", default=",".join(str(v) for v in SWING_K_VALUES),
                        help="comma-separated swing_k values")
    parser.add_argument("--require-volume-spike", action="store_true",
                        help="gate signals on volume spike (default OFF — pattern-only sweep)")
    parser.add_argument("--min-confidence", type=float, default=0.0,
                        help="floor for lead acceptance in sweep (default 0.0 — let every lead through)")
    parser.add_argument("--min-signals", type=int, default=MIN_SIGNALS,
                        help="min signals per cell to be eligible (default 50)")
    args = parser.parse_args()

    token = os.environ.get("UPSTOX_INTEGRATION_TOKEN")
    if not token:
        print("FATAL: UPSTOX_INTEGRATION_TOKEN not set", file=sys.stderr)
        return 2

    lookbacks = [int(v) for v in args.lookbacks.split(",") if v.strip()]
    swing_ks = [int(v) for v in args.swing_ks.split(",") if v.strip()]

    if args.warmup < max(lookbacks) + 20:
        print(f"WARN: warmup={args.warmup} < max_lookback+20 ({max(lookbacks)+20}); "
              f"first few days may fire fewer signals.", file=sys.stderr)

    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    results_dir = Path("backtests/results") / f"sweep_{timestamp}"
    results_dir.mkdir(parents=True, exist_ok=True)

    universe_label = (
        "--all" if args.all
        else (f"--instrument {args.instrument}" if args.instrument else "default-10-underlyings")
    )

    print(f"# Sweep start: {datetime.now().isoformat(timespec='seconds')}")
    print(f"# Grid: {len(lookbacks)} lookbacks x {len(swing_ks)} swing_ks = "
          f"{len(lookbacks)*len(swing_ks)} cells")
    print(f"# Output: {results_dir}")
    print(f"# Universe: {universe_label}")
    print()

    cells: list[dict] = []
    extra_sets: list[tuple[str, object]] = [
        ("breakout.min_confidence", float(args.min_confidence)),
        ("breakout.top_k_per_instrument", 10),
        ("breakout.proximity_pct", 1.0),
    ]
    for lb, kk in product(lookbacks, swing_ks):
        cell = _run_cell(lb, kk, args.days, args.warmup, args.instrument,
                         args.all, args.throttle,
                         require_volume_spike=args.require_volume_spike,
                         extra_sets=extra_sets,
                         results_dir=results_dir)
        cells.append(cell)
        # Incremental save so we don't lose progress on a 14-minute run
        (results_dir / "cells.json").write_text(json.dumps(cells, indent=2))
        s = cell["summary"]
        if s:
            print(f"   lb={lb:>3} k={kk} → n={s['n_signals']:>3} win={s['win_rate_pct']:.1f}% "
                  f"avg={s['avg_return_pct']:+.2f}% worst={s['worst_pct']:+.2f}%",
                  flush=True)
        else:
            print(f"   lb={lb:>3} k={kk} → (no SUMMARY parsed; see {results_dir}/cell_L{lb}_K{kk}.log)",
                  flush=True)

    winner = _select_winner(cells, min_signals=args.min_signals)
    md = _render_markdown(cells, winner, results_dir, args.days, args.warmup, universe_label)
    (results_dir / "summary.md").write_text(md)
    print()
    print(md)
    print(f"# Wrote summary: {results_dir}/summary.md")
    print(f"# Wrote raw     : {results_dir}/cells.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Suite A: cold-start check-in reliability measurement.

Runs N trials: clear ~/.chronos_*, start agent script, wait for
callback ID or timeout, kill agent, record result.

Environment:
  CHECKIN_AGENT       Path to built agent script (default: ./chronos_agent.py)
  CHECKIN_PYTHON      Python interpreter (default: python3)
  CHECKIN_TRIALS      Number of trials (default: 50)
  CHECKIN_TRIAL_TIMEOUT  Per-trial seconds cap (default: 300)
  CHECKIN_OUT_DIR     Output directory (default: ./checkin_suite_a)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = Path(os.environ.get("CHECKIN_AGENT", _REPO_ROOT / "chronos_agent.py"))
PYTHON = os.environ.get("CHECKIN_PYTHON", sys.executable)
OUT_DIR = Path(os.environ.get("CHECKIN_OUT_DIR", _REPO_ROOT / "checkin_suite_a"))
N_TRIALS = int(os.environ.get("CHECKIN_TRIALS", "50"))
TRIAL_TIMEOUT = int(os.environ.get("CHECKIN_TRIAL_TIMEOUT", "300"))


def clear_persistence():
    home = Path.home()
    for p in home.glob(".chronos_*"):
        p.unlink(missing_ok=True)


def run_trial(i: int) -> dict:
    if not AGENT.exists():
        raise FileNotFoundError(
            f"Agent not found: {AGENT}. Build a Chronos payload and set CHECKIN_AGENT."
        )

    clear_persistence()
    log_path = OUT_DIR / f"trial_{i:03d}.log"
    t0 = time.time()
    proc = subprocess.Popen(
        [PYTHON, str(AGENT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ.copy(),
        cwd=str(AGENT.parent),
    )
    lines = []
    success = False
    latency = None
    reason = "timeout"
    try:
        deadline = t0 + TRIAL_TIMEOUT
        while time.time() < deadline:
            if proc.poll() is not None:
                rest = proc.stdout.read() if proc.stdout else ""
                if rest:
                    lines.extend(rest.splitlines())
                reason = "agent_exited"
                break
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                time.sleep(0.1)
                continue
            line = line.rstrip("\n")
            lines.append(line)
            if "Got callback ID:" in line:
                success = True
                latency = time.time() - t0
                reason = "callback"
                if "latency=" in line:
                    try:
                        latency = float(line.split("latency=")[1].split("s")[0])
                    except Exception:
                        pass
                break
            if (
                "Checkin FAILED" in line
                or "first-run; not treating payload UUID" in line
                or "Checkin FAILED — no callback ID received. Exiting" in line
            ):
                reason = "checkin_failed"
                break
    finally:
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception:
                proc.kill()
        log_path.write_text("\n".join(lines) + "\n")

    return {
        "trial": i,
        "success": success,
        "reason": reason,
        "latency_s": round(latency, 2) if latency is not None else None,
        "wall_s": round(time.time() - t0, 2),
        "log": str(log_path),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    print(f"Suite A: {N_TRIALS} cold-start trials, timeout={TRIAL_TIMEOUT}s, agent={AGENT}")
    for i in range(1, N_TRIALS + 1):
        r = run_trial(i)
        results.append(r)
        status = "PASS" if r["success"] else "FAIL"
        print(
            f"[{i:03d}/{N_TRIALS}] {status} reason={r['reason']} "
            f"latency={r['latency_s']} wall={r['wall_s']}s",
            flush=True,
        )
        time.sleep(5)

    successes = sum(1 for r in results if r["success"])
    latencies = [r["latency_s"] for r in results if r["latency_s"] is not None]
    latencies_sorted = sorted(latencies)
    summary = {
        "n": N_TRIALS,
        "successes": successes,
        "failures": N_TRIALS - successes,
        "success_rate": round(successes / N_TRIALS, 4) if N_TRIALS else 0,
        "failure_rate": round((N_TRIALS - successes) / N_TRIALS, 4) if N_TRIALS else 0,
        "latency_p50": latencies_sorted[len(latencies_sorted) // 2] if latencies_sorted else None,
        "latency_p95": latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies_sorted else None,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }
    out = OUT_DIR / "summary.json"
    out.write_text(json.dumps(summary, indent=2))
    md = OUT_DIR / "SUMMARY.md"
    md.write_text(
        f"# Check-in Suite A results\n\n"
        f"- Trials: {summary['n']}\n"
        f"- Success: {summary['successes']} ({summary['success_rate']*100:.1f}%)\n"
        f"- Failure: {summary['failures']} ({summary['failure_rate']*100:.1f}%)\n"
        f"- Latency p50: {summary['latency_p50']}s\n"
        f"- Latency p95: {summary['latency_p95']}s\n"
        f"- Target: ≥90% success (≤10% failure)\n"
    )
    print(json.dumps({k: summary[k] for k in summary if k != "results"}, indent=2))
    print(f"Wrote {out}")
    if summary["success_rate"] < 0.9:
        sys.exit(1)


if __name__ == "__main__":
    main()

"""処理時間・メモリの計測（計画書 8 章）。手法ごとに子プロセスで測る。"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def _environment(threads: int) -> dict:
    info = {"python": platform.python_version(), "platform": platform.platform(), "machine": platform.machine(), "threads": threads}
    try:
        info["cpu"] = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5).stdout.strip()
        info["memory_gb"] = round(int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip()) / 2**30, 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        info["cpu"] = platform.processor()
    info["device"] = "cpu"
    return info


def run_benchmark(run, methods: list[str] | None = None, repeats: int | None = None) -> Path:
    """bench/benchmark.csv（生データ）と benchmark_summary.csv（中央値と 95% 区間）を書く。"""
    cfg = run.config["benchmark"]
    repeats = repeats or cfg["repeats"]
    methods = methods or ["B1", "B2", "B3", "P1", "P2", "P3"]
    out = run.root / "bench"
    out.mkdir(exist_ok=True)
    env = dict(os.environ, OMP_NUM_THREADS=str(cfg["threads"]), MKL_NUM_THREADS=str(cfg["threads"]), NUMBA_NUM_THREADS=str(cfg["threads"]), TOKENIZERS_PARALLELISM="false", PYTHONWARNINGS="ignore")
    config_path = Path(run.config.get("_config_path", "config/experiment.yaml"))
    rows = []
    for m in methods:
        print(f"計測中: {m}", flush=True)
        p = subprocess.run(
            [sys.executable, "-m", "ontomap.bench_worker", "--config", str(config_path), "--run", run.root.name, "--method", m, "--repeats", str(repeats), "--warmup", str(cfg["warmup"])],
            capture_output=True, text=True, env=env, cwd=Path(__file__).resolve().parent.parent,
        )
        if p.returncode != 0:
            print(p.stderr[-2000:], file=sys.stderr)
            continue
        rows += [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
    llm_usage = run.root / "graphs" / "llm" / "usage.csv"
    if llm_usage.exists():
        u = pd.read_csv(llm_usage)
        rows.append({"method": "LLM graph", "stage": "extract", "cold_or_warm": "cold", "repeat": 0, "seconds": float(u.seconds.sum()), "peak_ram_mb": np.nan, "tokens": int(u.input_tokens.sum() + u.output_tokens.sum())})
    df = pd.DataFrame(rows)
    df.to_csv(out / "benchmark.csv", index=False)
    warm = df[df.cold_or_warm == "warm"]
    summ = warm.groupby(["method", "stage"]).seconds.agg(median="median", p2_5=lambda x: np.percentile(x, 2.5), p97_5=lambda x: np.percentile(x, 97.5), n="count").reset_index()
    cold = df[df.cold_or_warm == "cold"].groupby(["method", "stage"]).seconds.first().rename("cold_seconds").reset_index()
    peak = df.groupby("method").peak_ram_mb.max().rename("peak_ram_mb").reset_index()
    summ = summ.merge(cold, on=["method", "stage"], how="outer").merge(peak, on="method", how="left")
    summ.to_csv(out / "benchmark_summary.csv", index=False)
    (out / "environment.json").write_text(json.dumps(_environment(cfg["threads"]), ensure_ascii=False, indent=2), encoding="utf-8")
    return out / "benchmark_summary.csv"

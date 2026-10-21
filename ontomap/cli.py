"""コマンドライン: ontomap <コマンド> --config config/experiment.yaml"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import pipeline as pl


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_run_all(a) -> None:
    run = pl.open_run(a.config, a.run)
    skip = set(a.skip or [])
    steps = [
        ("① データ確定", lambda: pl.step_data(run)),
        ("②③ グラフ層", lambda: pl.step_graphs(run)),
        ("④⑤ 類似度", lambda: pl.step_similarity(run, skip)),
        ("⑦ 評価ペアの抽出", lambda: pl.step_labels(run)),
        ("重み調整（train 群）", lambda: pl.step_tune(run)),
        ("⑥ 二次元マップ", lambda: pl.step_maps(run).round(3).to_dict("records")),
        ("手法間の移動量", lambda: f"{len(pl.step_movement(run))} 行"),
        ("⑧ 評価", lambda: pl.step_evaluate(run).round(3).to_dict("records")),
        ("H3 近傍保持", lambda: pl.step_label_preservation(run).round(3).to_dict("records")),
    ]
    for name, fn in steps:
        print(f"== {name}", flush=True)
        out = fn()
        if a.verbose:
            _print(out)
    pl.write_manifest(run)
    from .viewer import build_viewer

    build_viewer(run)
    from .report import build_report

    build_report(run)
    print(f"完了: {run.root}")


def cmd_viewer(a) -> None:
    run = pl.open_run(a.config, a.run)
    pl.ensure_docs(run)
    pl.load_similarities(run)
    from .viewer import build_viewer

    print(build_viewer(run))


def cmd_report(a) -> None:
    run = pl.open_run(a.config, a.run)
    pl.ensure_docs(run)
    pl.load_similarities(run)
    from .report import build_report

    print(build_report(run))


def cmd_reevaluate(a) -> None:
    """人手ラベル（labels/pair_labels.csv）を入れた後に、評価と報告書だけ作り直す。"""
    run = pl.open_run(a.config, a.run)
    pl.ensure_docs(run)
    pl.load_similarities(run)
    pl.step_tune(run)
    pl.step_evaluate(run)
    pl.step_label_preservation(run)
    from .report import build_report

    print(build_report(run))


def cmd_import_labels(a) -> None:
    from .labels import import_labels

    run = pl.open_run(a.config, a.run)
    sheets = [Path(p) for p in a.sheets]
    lab, stats = import_labels(sheets, Path(a.adjudication) if a.adjudication else None, pl.split_of(run))
    lab.to_csv(run.root / "labels" / "pair_labels.csv", index=False)
    _print(stats)


def cmd_extract_llm(a) -> None:
    from .extract.llm import build_llm_layer

    run = pl.open_run(a.config, a.run)
    pl.ensure_docs(run)
    _print(build_llm_layer(run, limit=a.limit))


def cmd_bench(a) -> None:
    from .bench import run_benchmark

    run = pl.open_run(a.config, a.run)
    print(run_benchmark(run, methods=a.methods, repeats=a.repeats))


def main(argv: list[str] | None = None) -> None:
    """エントリポイント。"""
    p = argparse.ArgumentParser(prog="ontomap", description="オントロジー型文書マッピングの比較実験")
    p.add_argument("--config", default="config/experiment.yaml")
    p.add_argument("--run", default=None, help="runs/ の下の名前（既定は設定の run_name）")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run-all", help="①〜⑧を順に実行し、ビューアと報告書を作る")
    s.add_argument("--skip", nargs="*", help="省く手法（例: B3）")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_run_all)
    sub.add_parser("viewer", help="マップ比較ビューア（HTML）を作り直す").set_defaults(fn=cmd_viewer)
    sub.add_parser("report", help="報告書（md / docx）を作り直す").set_defaults(fn=cmd_report)
    sub.add_parser("reevaluate", help="人手ラベルで評価と報告書を作り直す").set_defaults(fn=cmd_reevaluate)
    s = sub.add_parser("import-labels", help="採点済み盲検シートを pair_labels.csv にまとめる")
    s.add_argument("sheets", nargs="+")
    s.add_argument("--adjudication", default=None)
    s.set_defaults(fn=cmd_import_labels)
    s = sub.add_parser("extract-llm", help="LLM graph を作る（API 料金がかかる）")
    s.add_argument("--limit", type=int, default=None)
    s.set_defaults(fn=cmd_extract_llm)
    s = sub.add_parser("bench", help="処理時間とメモリを工程別に測る")
    s.add_argument("--methods", nargs="*", default=None)
    s.add_argument("--repeats", type=int, default=None)
    s.set_defaults(fn=cmd_bench)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main(sys.argv[1:])

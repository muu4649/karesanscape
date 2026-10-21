"""生データ（raw/*.json）の明細書から用途の文を切り出し直し、別ファイルに書く（元の documents_sections.csv は変えない）。

例:
    .venv/bin/python scripts/rebuild_use_text.py datasets/mof648
    → datasets/mof648/documents_sections_use2.csv（use_text・use_source を新しい方式に、前の値は use_text_v1・use_source_v1 に残す）
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ontomap.patents.use_extract import extract_use, raw_description  # noqa: E402


def raw_index(raw_dir: Path) -> dict[str, dict]:
    """文献番号の表記（特許1234567 / JPpat1234567 など）→ 生データ。"""
    idx: dict[str, dict] = {}
    for f in sorted(glob.glob(str(raw_dir / "*.json"))):
        r = json.loads(Path(f).read_text(encoding="utf-8"))
        keys = [r.get("input"), r.get("matched_publication_no"), r.get("content_source_publication_no"), r.get("pub_number"), r.get("doc_id"),
                *(r.get("publication_no") or []), *(r.get("publication_nos") or [])]
        for k in keys:
            if k:
                idx.setdefault(str(k), r)
    return idx


def claims_of(r: dict) -> str:
    c = r["sections"].get("claims", "") if "sections" in r else r.get("claims", "")
    return re.sub(r"<[^>]+>", "", c or "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    src = a.dataset / "documents_sections.csv"
    out = a.out or a.dataset / "documents_sections_use2.csv"
    d = pd.read_csv(src, dtype=str, keep_default_na=False)
    idx = raw_index(a.dataset / "raw")
    new_t, new_s, missing = [], [], 0
    for _, row in d.iterrows():
        r = idx.get(row.get("doc_id", "")) or idx.get(row["pub_number"])
        if r is None:
            missing += 1
            new_t.append(row["use_text"])
            new_s.append(row["use_source"] + "（生データなし・前の方式のまま）")
            continue
        t, s, _ = extract_use(raw_description(r), row.get("title", ""), claims_of(r))
        new_t.append(t)
        new_s.append(s)
    d["use_text_v1"], d["use_source_v1"] = d["use_text"], d["use_source"]
    d["use_text"], d["use_source"] = new_t, new_s
    d.to_csv(out, index=False)
    kinds = pd.Series([s.split(":")[0] for s in new_s]).value_counts().to_dict()
    print(f"{out}: {len(d)} 件, 生データなし {missing}, 出所 {kinds}")


if __name__ == "__main__":
    main()

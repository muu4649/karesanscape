"""Fターム の観点・コードから、部品の木の下書き（config/modules/<部品>.draft.tree）を作る。

使い方: .venv/bin/python scripts/fterm_draft.py battery

やり方:
- config/modules/fterm_aspects.json の割り当て（観点 → P/M/U）に従い、datasets/tech_pilot/fterm/<テーマ>.json のコードを概念にする。
- コード名を本文に出る言い方に直す:
  - 「又は」「、」などで分けて別名にする（例: サイクル寿命又はサイクル特性 → サイクル寿命, サイクル特性）
  - 課題（P）は「〜の向上」「〜の防止」などを外す（例: 自己放電の抑制 → 自己放電）
  - 「〜を主体とするもの」「その他」などの分類用の言い回しは表記にしない
- 開発用の文書（doc_id 奇数）の課題・解決手段・用途の段落に 2 件以上出る表記だけを残す。
  表記が 1 つも残らないコードは、残った子を持つときだけ中間概念として残す。
- 同じ型で同じ表記になるコードは 1 つの概念にまとめる（例: 5H050AA07 と 5H029AJ05 のサイクル特性）。
- 下書きは人が直す前提。直したものを config/modules/<部品>.tree にして scripts/build_module.py build で YAML にする。
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CORPUS = {"battery": ROOT / "datasets/battery600/documents_sections.csv", "semicon": ROOT / "datasets/semicon600/documents_sections.csv"}
PREFIX = {"battery": "BAT", "semicon": "SEM"}
SPLIT = re.compile(r"又は|若しくは|もしくは|あるいは|及び|並びに|、|，|,|；|;|/")
P_SUFFIX = re.compile(r"(化)?(の)?(向上|改善|改良|抑制|防止|軽減化|軽減|低減|回復|確保|対策|削減|短縮|増大|増加|簡易化|簡略化|容易化|安定化|高速化|長寿命化|最適化|除去)$")
BAD = re.compile(r"もの|主体|有する|その他|特定|関係|組合せ|組み合わせ|種別|一般|形態$|態様|手段$|方法$|に関する|による|において|を用いた|の場合|以外")


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def surfaces(label: str, top: str) -> list[str]:
    t = norm(label).lstrip("・").rstrip("*◎").strip()
    t = re.sub(r"[（(][^）)]*[）)]", "", t)
    out = []
    for part in SPLIT.split(t):
        p = part.strip(" ・")
        if top == "P":
            p = P_SUFFIX.sub("", p).strip(" ・の")
        if len(p) < 2 or BAD.search(p) or re.fullmatch(r"[A-Za-z0-9]{1,2}", p):
            continue
        out.append(p)
    return list(dict.fromkeys(out))


def main(name: str) -> None:
    conf = json.loads((ROOT / "config/modules/fterm_aspects.json").read_text(encoding="utf-8"))[name]
    asp_top = {a: t for t in ("P", "M", "U") for a in conf[t]}
    docs = pd.read_csv(CORPUS[name], dtype=str, keep_default_na=False)
    num = docs["doc_id"].str.extract(r"(\d+)")[0].astype(int)
    dev = docs[num % 2 == 1]
    texts = [norm(" ".join(r[c] for c in ("problem", "solution", "use_text"))).lower() for _, r in dev.iterrows()]

    def df(s: str) -> int:
        s = s.lower()
        return sum(1 for t in texts if s in t)

    concepts: dict[str, dict] = {}  # コード → 概念
    for theme in conf["themes"]:
        d = json.loads((ROOT / f"datasets/tech_pilot/fterm/{theme}.json").read_text(encoding="utf-8"))
        for a in d["aspects"]:
            key = theme + a["aspect"]
            top = asp_top.get(key)
            if not top:
                continue
            for c in a["codes"]:
                forms = surfaces(c["label"], top)
                kept = [(s, df(s)) for s in forms]
                concepts[c["code"]] = {"code": c["code"], "top": top, "label_raw": c["label"], "aspect": a["label"], "theme": theme,
                                       "forms": [s for s, n in kept if n >= 2], "df": max([n for _, n in kept] or [0]),
                                       "parents": [p for p in c["parents"] if not p.endswith("00")]}
    # 子が残るかで中間概念を決める
    keep = {k for k, v in concepts.items() if v["forms"]}
    changed = True
    while changed:
        changed = False
        for k, v in concepts.items():
            if k in keep:
                for p in v["parents"]:
                    if p in concepts and p not in keep:
                        keep.add(p)
                        changed = True
    # 同じ型・同じ主表記をまとめる
    owner: dict[tuple[str, str], str] = {}
    merged: dict[str, list[str]] = defaultdict(list)
    for k in sorted(keep):
        v = concepts[k]
        if not v["forms"]:
            continue
        key = (v["top"], v["forms"][0])
        if key in owner:
            merged[owner[key]].append(k)
            v["merged_into"] = owner[key]
        else:
            owner[key] = k
    pre = PREFIX[name]
    cid = lambda k: f"{pre}_{concepts[k]['top']}_{k}"  # noqa: E731

    def parent_of(k: str) -> str:
        for p in concepts[k]["parents"]:
            if p in keep and not concepts[p].get("merged_into"):
                return cid(p)
        return concepts[k]["top"]

    lines = [f"# 部品「{name}」の下書き（scripts/fterm_draft.py が Fターム から作成。人が直してから {name}.tree にする）",
             "# 書き方: ID: 表示名 | 別名, 別名   # コード 観点 / 元の名前 / 開発用の文書数", ""]
    by_parent: dict[str, list[str]] = defaultdict(list)
    for k in sorted(keep):
        if concepts[k].get("merged_into"):
            continue
        by_parent[parent_of(k)].append(k)

    def emit(pid: str, depth: int) -> None:
        for k in by_parent.get(pid, []):
            v = concepts[k]
            forms = v["forms"] + [f for m in merged.get(k, []) for f in concepts[m]["forms"]]
            forms = list(dict.fromkeys(forms))
            label = forms[0] if forms else norm(v["label_raw"]).lstrip("・").rstrip("*")
            alias = (" | " + ", ".join(forms[1:])) if len(forms) > 1 else ""
            codes = ",".join([k] + merged.get(k, []))
            lines.append(f"{'  ' * depth}{cid(k)}: {label}{alias}   # {codes} {v['aspect'].rstrip('*')} / {v['label_raw']} / {v['df']}")
            emit(cid(k), depth + 1)

    for top in ("P", "M", "U"):
        lines.append(top)
        emit(top, 1)
        lines.append("")
    out = ROOT / f"config/modules/{name}.draft.tree"
    out.write_text("\n".join(lines), encoding="utf-8")
    n = sum(1 for k in keep if not concepts[k].get("merged_into"))
    print(f"{out}: 概念 {n}（コード {len(concepts)} のうち残したもの {len(keep)}、まとめたもの {sum(len(x) for x in merged.values())}）。開発用 {len(dev)} 件")


if __name__ == "__main__":
    main(sys.argv[1])

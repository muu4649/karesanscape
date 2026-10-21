"""技術オントロジー試作の地図への効き目: 電池・半導体それぞれ 600 件で、関係の近さ（P2）の近い 10 件が
同じ下位分野（Fターム のテーマで分けた 5 分野）になる割合を、照合のしかた A/B/C と文埋め込みで比べる。

A = inorg-1.0 だけ、B = 全部品を合わせた一枚の辞書、C = 分野で部品を切り替え、別名も分野で限る。
下位分野のラベルは抽出の枠組み（どのテーマで検索したか）から付いたもので、オントロジーとは独立。
使い方: HF_HUB_OFFLINE=1 .venv/bin/python scripts/tech_pilot_eval.py → runs/tech_pilot/map_eval.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from build_module import CORPORA, compose_all  # noqa: E402

from ontomap.app import engine  # noqa: E402
from ontomap.modular import build_section_layer_scoped  # noqa: E402
from ontomap.ontology import Ontology  # noqa: E402
from ontomap.patents.data import DEFAULT_SECTIONS, load_patent_csv  # noqa: E402
from ontomap.patents.extract import build_section_layer  # noqa: E402
from ontomap.similarity.graph_sim import RelationConfig, relation_similarity  # noqa: E402

K = 10


def purity(S: np.ndarray, labels: list[str], k: int = K, mask: np.ndarray | None = None) -> tuple[float, float, float]:
    """近い k 件のうち同じラベルの割合の平均と、文書単位のブートストラップ 95% 区間。mask があればその文書だけで平均する
    （近傍は全文書から選ぶ）。部品を作るのに使っていない確認用の文書だけで測るときに使う。"""
    S = np.nan_to_num(S.astype(float), nan=-1.0)
    np.fill_diagonal(S, -np.inf)
    lab = np.array(labels)
    nn = np.argsort(-S, axis=1)[:, :k]
    per = (lab[nn] == lab[:, None]).mean(axis=1)
    if mask is not None:
        per = per[mask]
    rng = np.random.default_rng(0)
    boots = [per[rng.integers(0, len(per), len(per))].mean() for _ in range(1000)]
    return float(per.mean()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def main() -> None:
    from ontomap.patents.vectors import concat_similarity

    inorg = Ontology.load(ROOT / "config/ontology_inorganic_v1.yaml")
    mo = compose_all()
    rel_cfg = RelationConfig.from_dict(engine.RELATION)
    rows = []
    for name in ("battery", "semicon"):
        recs, _ = load_patent_csv(CORPORA[name], {"sections": DEFAULT_SECTIONS, "normalize": "NFKC"})
        ids = [r.doc_id for r in recs]
        src = pd.read_csv(CORPORA[name], dtype=str, keep_default_na=False).set_index("doc_id").loc[ids]
        labels = list(src["domain"])
        check = np.array([int("".join(ch for ch in i if ch.isdigit())) % 2 == 0 for i in ids])
        base = 1 / 5
        S_emb, _, _ = concat_similarity(engine._encoder(), [r.text for r in recs])
        m, lo, hi = purity(S_emb, labels)
        mc, loc, hic = purity(S_emb, labels, mask=check)
        rows.append({"コーパス": name, "方式": "文埋め込み", "近い10件の同じ下位分野": round(m, 3), "95%区間": f"{lo:.3f}〜{hi:.3f}",
                     "確認用だけ": round(mc, 3), "確認用の95%区間": f"{loc:.3f}〜{hic:.3f}", "三つ組なし": 0, "無作為": base})
        for cfg in ("A", "B", "C"):
            if cfg == "A":
                layer, st = build_section_layer(recs, inorg, engine.EXTRACTION)
                onto = inorg
            else:
                layer, st = build_section_layer_scoped(recs, mo, engine.EXTRACTION, scoped=(cfg == "C"))
                onto = mo.onto
            S, _ = relation_similarity(layer, ids, onto, rel_cfg)
            m, lo, hi = purity(S, labels)
            mc, loc, hic = purity(S, labels, mask=check)
            rows.append({"コーパス": name, "方式": f"関係 {cfg}", "近い10件の同じ下位分野": round(m, 3), "95%区間": f"{lo:.3f}〜{hi:.3f}",
                         "確認用だけ": round(mc, 3), "確認用の95%区間": f"{loc:.3f}〜{hic:.3f}", "三つ組なし": len(st["no_triples"]), "無作為": base})
            print(rows[-1], flush=True)
    out = ROOT / "runs/tech_pilot"
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out / "map_eval.csv", index=False)
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()

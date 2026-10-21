"""明細書から「用途」の文を切り出す（文単位・オントロジーを使わない）。

前の方式（段落単位）の外れ方（MOF 614 件で概念が拾えなかった 83 件から 14 件を抜き取り、筆者判定）:
- 用途でない段落を選ぶ（8 件）: 「溶媒として使用」「バインダとして用いる」のような、材料の使い方の文に反応していた
- 【産業上の利用可能性】が中身のない定型文（3 件）: 「本開示は、吸着システムについて有用である」
- 段落は正しく、辞書の語が足りない（3 件）

この方式の決まり:
1. 文に分け、用途を表す言い回し（用途・好適・有用・応用・〜に用いる 等）で点を付ける
2. 点が 4 以上の文を、点の高い順に 4 文まで選ぶ。「溶媒として用いる」のような材料の使い方の文、測定・合成手順の文、実施例の段落は除くか減点する
3. 題名に出る語と決まり文句しか中身のない文（定型文）は減点する
4. 見出しで重みを変える（産業上の利用可能性・技術分野・発明の効果を上げ、背景技術を下げる。課題・解決手段の見出しは他の列と重なるので使わない）
5. 点 4 の文が無ければ点 3 の文を 2 つまで採る。それも無いときだけ、題名の「〜用」を手がかりにする
辞書（オントロジー）で文を選ぶと、同じ辞書で測ったときに自己採点になるため使わない。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

PARA_RE = re.compile(r"^【[０-９0-9]+】$")
BRACKET_PARA_RE = re.compile(r"^\[(\d{4,5})\]\s*$")
HEAD_RE = re.compile(r"^【([^】]+)】$")
INLINE_HEAD_RE = re.compile(r"^[［\[]([^］\]]{2,20})[］\]]\s*")

SKIP_HEADS = ("特許文献", "非特許文献", "先行技術文献", "符号の説明", "図面の簡単な説明", "発明が解決しようとする課題", "課題を解決するための手段",
              "解決しようとする課題", "課題を解決する手段", "解決手段")
HEAD_WEIGHT = {"産業上の利用可能性": 2, "技術分野": 1, "産業上の利用分野": 1, "発明の効果": 1, "背景技術": -1, "従来の技術": -1, "発明の背景": -1, "背景": -1}

# 用途を表す言い回し（NFKC にそろえたあとの文で探す）
# 「Xが利用可能」（部品として使える）は用途ではない。「Xに利用できる」「Xとして有用」「Xへの応用」のように、向け先を伴う形だけを数える
STRONG = re.compile(r"用途|に有用|として(好適|有用)|に応用|に適し|(への|に)(応用|適用)(が|でき|可能|され)"
                    r"|(に|へ|として)(用い|利用|使用|適用|応用)(る|する|される|られる)?(こと)?(が|も)?(でき|可能)")
MEDIUM = re.compile(r"に(用い|使用|利用|適用|使われ)(る|られ|され|でき|する|して)|として(用い|使用|利用|使われ)(る|られ|され|でき|する|して|うる|得る)|に供され|分野(において|で)(用い|使用|利用)|に好適"
                    r"|[一-龥ァ-ヴー]{2,}(?<!形成)(?<!製造)(?<!合成)(?<!調製)(?<!作製)(?<!測定)用(の|に|として|途)")
# 材料の使い方（用途ではない）
INSTRUMENT = re.compile(r"(溶媒|溶剤|原料|出発物質|配位子|リンカー|バインダ|バインダー|前駆体|試薬|塩基|酸|添加剤|鋳型|テンプレート|モノマー|分散媒|還元剤|酸化剤|界面活性剤|架橋剤|開始剤|担体|基材|支持体|金属源|金属塩|中心金属|金属イオン)(として|に)は?(用い|使用|利用)"
                        r"|としては.{0,60}(が|を)(利用|使用|用い)(可能|でき|られ|る)|を(利用|使用)(でき|可能)")
STEP = re.compile(r"を用いて|を使用して|を利用して")
EXCLUDE = re.compile(r"実施例|比較例|測定(装置|方法|した|され|を行|条件|値)|を用いて測定|収率|図\d|表\d|理解されたい|援用|本明細書(で|において)(使用|用い)|用語|特許文献|非特許文献|化学式|一般式|式\(|wt%|質量%|mol%|\d+(\.\d+)?\s*(℃|mL|ml|mmol|mg|g/|rpm|時間|分間)")
SELF = re.compile(r"本発明|本開示|本実施形態|本実施の形態|本願|本明細書の|前記|当該|上記|このような|かかる|斯かる|係る|得られ(る|た)")
EXAMPLE = re.compile(r"例えば|等の用途|などの用途|等に|などに|が挙げられ")
# 定型文の判定で中身と見なさない語
GENERIC_WORDS = {"本発明", "本開示", "本実施形態", "本願", "産業", "産業上", "分野", "各種", "種々", "様々", "利用", "有用", "可能", "可能性", "製造", "方法", "製造方法",
                 "材料", "用途", "提供", "使用", "適用", "応用", "前記", "当該", "上記", "以上", "説明", "発明", "技術", "効果", "期待", "民生", "広範", "幅広", "好適",
                 "金属有機構造体", "構造体", "組成物", "化合物", "多孔性", "多孔質", "金属錯体", "錯体", "形態", "実施", "記載", "目的", "ため", "非常", "有効"}
TITLE_USE = re.compile(r"([一-龥ァ-ヴーA-Za-z0-9]{2,12})(?<!形成)(?<!製造)(?<!合成)(?<!調製)(?<!作製)用(の|に|と|として|である)?")
TERM_RE = re.compile(r"[一-龥ァ-ヴー々]{2,}|[A-Za-z][A-Za-z0-9\-]{1,}")


@dataclass
class Picked:
    text: str
    head: str
    pno: str
    score: float
    why: list[str] = field(default_factory=list)


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def raw_description(raw: dict) -> str:
    """生データ（Summaria-MCP の取得結果）から明細書の本文を取り出す。追加取得した続きがあればつなぐ。
    2 つの保存形式に対応する: datasets/mof648（sections.description + description_tail）と
    datasets/inorganic1200（description_fetched + description_cont）。"""
    if "sections" in raw:
        head = raw["sections"].get("description", "") or ""
        tail = raw.get("description_tail", "") or ""
    else:
        head = raw.get("description_fetched", "") or ""
        tail = raw.get("description_cont", "") or ""
    head, tail = re.sub(r"<[^>]+>", "", head), re.sub(r"<[^>]+>", "", tail)
    return merge(head, tail) if tail else head


def merge(a: str, b: str) -> str:
    """最初の取得分と続きをつなぐ。重なった段落（同じ段落番号）は続きの側から除く。"""
    seen = {_pno(s.strip()) for s in a.split("\n") if _pno(s.strip())}
    out, skip = [], False
    for line in b.split("\n"):
        p = _pno(line.strip())
        if p:
            skip = p in seen
        if not skip:
            out.append(line)
    return a + "\n" + "\n".join(out)


def _pno(s: str) -> str:
    if PARA_RE.match(s):
        return norm(s)[1:-1].lstrip("0")
    m = BRACKET_PARA_RE.match(s)
    return m.group(1).lstrip("0") if m else ""


def split_paras(desc: str) -> list[tuple[str, str, str]]:
    """明細書を (見出し, 段落番号, 本文) に分ける。【０００１】と [0001] の両方の書式、行頭の［背景技術］のような見出しに対応する。"""
    out: list[tuple[str, str, str]] = []
    head, pno, buf = "", "", []

    def flush() -> None:
        if buf:
            t = "\n".join(buf).strip()
            if t:
                out.append((head, pno, t))

    for line in desc.split("\n"):
        s = line.strip()
        p = _pno(s)
        if p:
            flush()
            buf, pno = [], f"【{int(p):04d}】"
            continue
        m = HEAD_RE.match(s)
        if m and not re.match(r"^(化|式|数|表|図|請求項)[０-９0-9]", m.group(1)):
            flush()
            buf, pno, head = [], "", m.group(1)
            continue
        if not buf:
            m2 = INLINE_HEAD_RE.match(norm(s))
            if m2 and not re.match(r"^(化|式|数|表|図|請求項)\d", m2.group(1)):
                head = m2.group(1)
                s = norm(s)[m2.end():]
                line = s
                if not s:
                    continue
        buf.append(line)
    flush()
    return out


def sentences(text: str) -> list[str]:
    t = norm(text).replace("\n", "")
    parts = re.split(r"(?<=。)", t)
    return [p.strip() for p in parts if len(p.strip()) >= 8]


def content_terms(sent: str, title_terms: set[str]) -> set[str]:
    """文の中身の語（題名に出る語と決まり文句を除く）。空なら定型文と見なす。"""
    terms = set(TERM_RE.findall(sent))
    out = set()
    for t in terms:
        if t in GENERIC_WORDS or t in title_terms:
            continue
        if any(t in g or g in t for g in ("本発明", "本開示", "可能性")):
            continue
        if any(t == tt or (len(t) >= 3 and t in tt) for tt in title_terms):
            continue
        out.add(t)
    return out


NOT_USE = re.compile(r"用途(等)?に応じ|用途によ(り|って)|用途に合わせ|用途を問わ|用途に限定|用途は特に")


def score_sentence(sent: str, head: str, title_terms: set[str]) -> tuple[float, list[str]]:
    why: list[str] = []
    s = 0.0
    sent_cue = NOT_USE.sub("", sent)  # 「用途に応じて設定」のような、用途を述べていない言い回しは数えない
    if not (STRONG.search(sent_cue) or MEDIUM.search(sent_cue)):
        return 0.0, why
    if STRONG.search(sent):
        s += 3
        why.append("用途の言い回し（強）")
    elif MEDIUM.search(sent):
        s += 2
        why.append("用途の言い回し")
    else:
        return 0.0, why
    if INSTRUMENT.search(sent_cue):
        rest = INSTRUMENT.sub("", sent_cue)
        if not (STRONG.search(rest) or MEDIUM.search(rest)):  # 材料の使い方を除いても用途の言い回しが残る文は減点しない
            s -= 3
            why.append("材料の使い方")
    if STEP.search(sent) and not STRONG.search(sent):
        s -= 1
        why.append("手順の文")
    if EXCLUDE.search(sent):
        s -= 3
        why.append("実験・測定・参照の文")
    if "用途" in sent_cue:
        s += 1
        why.append("「用途」の語")
    if SELF.search(sent):
        s += 1
        why.append("発明そのものの文")
    if re.search(r"(てきた|されてきた)", sent) and not SELF.search(sent):
        s -= 1
        why.append("従来の話")
    if EXAMPLE.search(sent):
        s += 1
        why.append("用途の例示")
    w = HEAD_WEIGHT.get(head, 0)
    if w:
        s += w
        why.append(f"見出し「{head}」{w:+d}")
    if not content_terms(sent, set()):
        s -= 3
        why.append("定型文（中身の語が無い）")
    elif not content_terms(sent, title_terms):
        s -= 1
        why.append("題名と同じ語だけ")
    if len(sent) > 400:
        s -= 1
        why.append("長すぎる文")
    return s, why


def extract_use(desc: str, title: str = "", claims: str = "", max_sents: int = 4, min_score: float = 4.0) -> tuple[str, str, list[Picked]]:
    """用途の文を選ぶ。返り値は (用途テキスト, 出所の説明, 選んだ文)。"""
    title_n = norm(title)
    title_terms = set(TERM_RE.findall(title_n))
    cands: list[tuple[float, int, Picked]] = []
    order = 0
    for head, pno, body in split_paras(desc):
        if head in SKIP_HEADS or head.startswith(("実施例", "比較例")):
            continue
        for sent in sentences(body):
            order += 1
            sc, why = score_sentence(sent, head, title_terms)
            if sc >= min_score:
                cands.append((sc, order, Picked(sent, head, pno, sc, why)))
    if not cands:
        if min_score > 3.0:
            # 点が 4 に届く文が無いときは、3 の文を 2 つまで採る
            return extract_use(desc, title, claims, max_sents=2, min_score=3.0)
        return _hints(title_n)
    cands.sort(key=lambda z: (-z[0], z[1]))
    picked: list[tuple[int, Picked]] = []
    seen: set[str] = set()
    for sc, o, p in cands:
        if p.text in seen:
            continue
        seen.add(p.text)
        picked.append((o, p))
        if len(picked) >= max_sents:
            break
    picked.sort(key=lambda z: z[0])
    if picked:
        ps = [p for _, p in picked]
        src = "用途の文: " + ", ".join(f"{p.head or '本文'}{p.pno}" for p in ps)
        return "\n".join(p.text for p in ps), src, ps
    return _hints(title_n)


def _hints(title_n: str) -> tuple[str, str, list[Picked]]:
    """選べる文が無いときの手がかり: 題名の「〜用」（例: 水素貯蔵用 → 水素貯蔵）。
    請求項の「〜を備える〇〇装置」も試したが、拾えるのは発明の製品そのもの（芳香装置・組電池など）で用途ではなかったため使わない。"""
    hints = []
    for m in TITLE_USE.finditer(title_n):
        if m.group(1) not in GENERIC_WORDS:
            hints.append(Picked(f"{m.group(1)}用", "題名", "", 1.0, ["題名の「〜用」"]))
    uniq = list(dict.fromkeys(h.text for h in hints))[:3]
    if uniq:
        ps = [next(h for h in hints if h.text == u) for u in uniq]
        return "\n".join(uniq), "手がかり: " + ", ".join(p.head for p in ps), ps
    return "", "なし", []


# ---------- 段落単位（前の方式。地図の計算に使う） ----------
# datasets/inorganic1200/_work/pipeline.py.txt の extract() の用途の部分と同じ規則。
# 用途でない段落も混ざるが、分野の語（電池・触媒など）を多く含むため、IPC 用途で見た地図のまとまりはこちらのほうがよかった
# （MOF 614 件、関係方式の近い 10 件の用途一致 0.292 対 文単位 0.262。_drafts/20261020_0950_use_extract_eval_v1.md）。
PARA_USE_H = ("産業上の利用可能性",)
PARA_STRONG = re.compile(r"用途")
PARA_WEAK = re.compile(r"(用い|使用|適用)(る|られ|でき|され|可能|する|し得)")
PARA_SKIP = re.compile(r"(実施例|比較例)[0-9]*(で|に|と|の)|を用いて測定|測定(装置|方法)|装置を用い|試料を|gを|mL|ml")
PARA_SKIP_HEADS = ("特許文献", "非特許文献", "先行技術文献", "符号の説明", "図面の簡単な説明")


def extract_use_paragraphs(desc: str, max_paras: int = 3) -> tuple[str, str]:
    """用途の段落を選ぶ（段落単位）。【産業上の利用可能性】があればそれ、無ければ用途の言い回しを含む段落を点の高い順に最大 3 つ。"""
    paras = split_paras(desc)
    u = [p for p in paras if p[0] in PARA_USE_H]
    if u:
        return "\n".join(p[2] for p in u), "用途の段落: 産業上の利用可能性"
    cand = []
    for i, (head, pno, body) in enumerate(paras):
        if head in PARA_SKIP_HEADS or head.startswith(("実施例", "比較例")):
            continue
        t = norm(body)
        if not (PARA_STRONG.search(t) or PARA_WEAK.search(t)) or PARA_SKIP.search(t):
            continue
        sc = (2 if PARA_STRONG.search(t) else 0) + (1 if re.search(r"として(用い|使用|適用)|に(用い|使用|適用)|用途", t) else 0)
        if head in ("技術分野", "発明の効果", "産業上の利用分野", "発明を実施するための形態"):
            sc += 1
        if head in ("背景技術", "従来の技術", "発明の背景"):
            sc -= 1
        if sc >= 2:
            cand.append((sc, i, head, pno, body))
    cand.sort(key=lambda z: (-z[0], z[1]))
    top = sorted(cand[:max_paras], key=lambda z: z[1])
    if not top:
        return "", "なし"
    return "\n".join(z[4] for z in top), "用途の段落: " + ",".join(z[3] or z[2] for z in top)

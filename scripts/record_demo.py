"""KareSanScape のプレイ動画（操作のデモ）を録る。

使い方:
    1. アプリを起動しておく（例: .venv/bin/ontomap-app serve --port 8810 --no-browser）
    2. Playwright を別の環境に入れる（このプロジェクトの依存には入れていない）: uv pip install playwright
    3. python scripts/record_demo.py --base http://localhost:8810 --mof <MOF のマップ ID> --mix <3 分野のマップ ID> --out demo.mp4

やり方: Chrome（インストール済みのもの）を Playwright で操作し、Chrome DevTools の画面配信（Page.startScreencast）で
コマ（JPEG と時刻）を受け取り、ffmpeg で時刻どおりにつないで MP4 にする。画面録画にはカーソルが写らないので、
動くカーソルと字幕を画面に重ねる。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

OVERLAY = r"""
(() => {
  if (window.__demo) return;
  window.__demo = true;
  const st = document.createElement("style");
  st.textContent = `
    #__cur{position:fixed;left:0;top:0;width:22px;height:22px;z-index:2147483647;pointer-events:none;transform:translate(-3px,-2px);transition:transform .04s}
    #__ring{position:fixed;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;border:3px solid #D15B3C;z-index:2147483646;pointer-events:none;opacity:0;transition:opacity .35s, transform .35s;transform:scale(.4)}
    #__cap{position:fixed;left:50%;bottom:34px;transform:translateX(-50%);z-index:2147483645;pointer-events:none;background:rgba(16,47,73,.92);color:#fff;
      font:600 22px/1.5 "Hiragino Sans",system-ui,sans-serif;padding:12px 26px;border-radius:14px;box-shadow:0 10px 30px rgba(16,47,73,.25);opacity:0;transition:opacity .35s;max-width:80vw;text-align:center}
    #__cap small{display:block;font-weight:400;font-size:15px;opacity:.85}`;
  const cur = document.createElement("div");
  cur.id = "__cur";
  cur.innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22"><path d="M2 1 L2 18 L6.5 13.8 L9.6 20.5 L12.6 19.2 L9.6 12.6 L15.5 12.6 Z" fill="#fff" stroke="#102F49" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  const ring = document.createElement("div"); ring.id = "__ring";
  const cap = document.createElement("div"); cap.id = "__cap";
  // ページの中身ができる前に動くので、差し込みは body ができてから
  const add = () => { if (!document.body) return; if (!st.isConnected) document.head.appendChild(st); for (const e of [cur, ring, cap]) if (!e.isConnected) document.body.appendChild(e); };
  if (document.body) add(); else document.addEventListener("DOMContentLoaded", add);
  window.addEventListener("mousemove", e => { cur.style.left = e.clientX + "px"; cur.style.top = e.clientY + "px"; }, true);
  window.addEventListener("mousedown", e => {
    ring.style.left = e.clientX + "px"; ring.style.top = e.clientY + "px";
    ring.style.transition = "none"; ring.style.opacity = 1; ring.style.transform = "scale(.4)";
    requestAnimationFrame(() => { ring.style.transition = "opacity .5s, transform .5s"; ring.style.opacity = 0; ring.style.transform = "scale(1.4)"; });
  }, true);
  window.__caption = (t, sub) => { add(); cap.innerHTML = t ? t + (sub ? `<small>${sub}</small>` : "") : ""; cap.style.opacity = t ? 1 : 0; };
})();
"""


class Demo:
    def __init__(self, page, cdp):
        self.page, self.cdp = page, cdp
        self.frames: list[tuple[float, bytes]] = []

    def on_frame(self, params: dict) -> None:
        self.frames.append((params["metadata"]["timestamp"], base64.b64decode(params["data"])))
        asyncio.ensure_future(self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))

    async def cap(self, text: str, sub: str = "", wait: float = 0.0) -> None:
        await self.page.evaluate("([t, s]) => window.__caption(t, s)", [text, sub])
        if wait:
            await asyncio.sleep(wait)

    async def move(self, x: float, y: float, steps: int = 28) -> None:
        await self.page.mouse.move(x, y, steps=steps)

    async def center(self, sel: str) -> tuple[float, float]:
        loc = self.page.locator(sel).first
        await loc.scroll_into_view_if_needed()
        b = await loc.bounding_box()
        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2

    async def click(self, sel: str, pause: float = 0.35, after: float = 0.6) -> None:
        x, y = await self.center(sel)
        await self.move(x, y)
        await asyncio.sleep(pause)
        await self.page.mouse.click(x, y)
        await asyncio.sleep(after)

    async def scroll(self, sel: str, dy: int, wait: float = 1.2) -> None:
        await self.page.evaluate("([s, dy]) => document.querySelector(s).scrollBy({top: dy, behavior: 'smooth'})", [sel, dy])
        await asyncio.sleep(wait)

    async def doc_point(self, doc_id: str) -> tuple[float, float]:
        i = await self.page.evaluate("id => S.map.docs.findIndex(d => d.id === id)", doc_id)
        return await self.center(f'#map [data-i="{i}"]')


async def scenario(d: Demo, base: str, mof: str, mix: str) -> None:
    p = d.page
    await p.goto(base + "/#/")
    await asyncio.sleep(1.2)
    await d.move(700, 450, steps=1)
    await d.cap("KareSanScape", "特許の一覧から、技術の俯瞰マップをつくる", 2.4)
    await d.click(f'[data-open="{mof}"]', after=2.2)
    await d.cap("意味の類似性", "文章の内容が似ている特許が近くに並ぶ", 2.6)
    await d.click('#methodSeg button[data-m="rel"]', after=0.2)
    await d.cap("技術関係の類似性", "課題・解決手段・用途の関係が似ている特許が近くに並ぶ（同じ特許が動いて移る）", 3.0)
    await d.click('#methodSeg button[data-m="hyb"]', after=0.2)
    await d.cap("併用", "2 つの近さを合わせる", 2.0)
    await d.click('#methodSeg button[data-m="rel"]', after=1.2)
    # 用途で色分け
    await d.cap("用途で色分け", "IPC から判定した用途の分布を見る")
    x, y = await d.center("#colorBy")
    await d.move(x, y)
    await asyncio.sleep(0.3)
    await p.select_option("#colorBy", "usage")
    await asyncio.sleep(0.6)
    for k in range(3):
        await d.click(f"#colorPick .chip >> nth={k}", pause=0.2, after=0.5)
    await asyncio.sleep(1.2)
    # 拡大・移動
    await d.cap("拡大・移動", "ホイールで拡大、ドラッグで移動")
    mx, my = await d.doc_point("D0259")
    await d.move(mx, my)
    for _ in range(6):
        await p.mouse.wheel(0, -160)
        await asyncio.sleep(0.12)
    await asyncio.sleep(0.8)
    await p.mouse.down()
    await d.move(mx + 120, my + 60, steps=30)
    await p.mouse.up()
    await asyncio.sleep(0.8)
    # 特許を選ぶ
    mx, my = await d.doc_point("D0259")
    await d.move(mx, my)
    await asyncio.sleep(0.9)
    await d.cap("特許を選ぶ", "右の欄に概要（課題・解決手段・用途）")
    await p.mouse.click(mx, my)
    await asyncio.sleep(2.2)
    await d.click('.rtabs button[data-r="near"]', after=1.4)
    await d.cap("近い特許", "意味・技術関係・併用で並べ替え", 1.4)
    await d.click("#nbList li[data-j] >> nth=0", after=1.6)
    await d.cap("類似の根拠", "要約・類似度・一致した技術関係・根拠の原文・相違点", 2.4)
    await d.scroll("#side", 520, 2.2)
    await d.scroll("#side", 520, 2.2)
    await d.click("#zFit", after=0.8)
    # 異用途の類似技術
    await d.cap("異用途の類似技術", "技術関係は近いのに、用途が重ならない組")
    await d.click("#tBridges", after=2.6)
    await d.click("#brList .bridge-row >> nth=0", after=1.6)
    await d.cap("2 件を比較", "共通する課題・解決手段と、異なる用途", 2.4)
    await d.scroll("#side", 600, 2.4)
    # 3 分野のマップ
    await d.cap("")
    await d.click('#sideNav a[data-nav="home"]', after=1.2)
    await d.click(f'[data-open="{mix}"]', after=2.0)
    await d.click('#methodSeg button[data-m="rel"]', after=1.6)
    await d.cap("分野別辞書", "電池・半導体・無機材料を、文書の分類で辞書を切り替えて比べる")
    x, y = await d.center("#colorBy")
    await d.move(x, y)
    await p.select_option("#colorBy", "module")
    await asyncio.sleep(0.6)
    for k in range(3):
        await d.click(f"#colorPick .chip >> nth={k}", pause=0.2, after=0.5)
    await asyncio.sleep(2.0)
    # 辞書を改善
    await d.click("#tDict", after=2.2)
    await d.cap("辞書を改善", "抽出できない語を、例文を見て辞書に足す", 2.2)
    await d.scroll("#viewOnto", 620, 2.4)
    await d.scroll("#viewOnto", -620, 1.2)
    await d.cap("KareSanScape", "github.com/muu4649/karesanscape", 3.2)


async def record(base: str, mof: str, mix: str, out: Path) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="ja-JP")
        await ctx.add_init_script(OVERLAY)
        page = await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)
        d = Demo(page, cdp)
        cdp.on("Page.screencastFrame", d.on_frame)
        await page.goto(base + "/#/")
        await asyncio.sleep(1.0)
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 90, "everyNthFrame": 1})
        await scenario(d, base, mof, mix)
        await asyncio.sleep(0.5)
        await cdp.send("Page.stopScreencast")
        await browser.close()
    encode(d.frames, out)


def encode(frames: list[tuple[float, bytes]], out: Path) -> None:
    """時刻つきのコマを、ffmpeg の concat で時刻どおりにつなぐ（画面が変わらない間はコマが来ないため）。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        lines = []
        for k, (ts, jpg) in enumerate(frames):
            f = tmp / f"f{k:05d}.jpg"
            f.write_bytes(jpg)
            dur = (frames[k + 1][0] - ts) if k + 1 < len(frames) else 2.0
            lines += [f"file '{f}'", f"duration {max(dur, 1 / 60):.4f}"]
        lines.append(f"file '{tmp / f'f{len(frames) - 1:05d}.jpg'}'")
        (tmp / "list.txt").write_text("\n".join(lines))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"),
                        "-vf", "fps=30,scale=1920:-2:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "slow",
                        "-movflags", "+faststart", str(out)], check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{out}: コマ {len(frames)}, 長さ {frames[-1][0] - frames[0][0] + 2:.1f} 秒")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8810")
    ap.add_argument("--mof", required=True)
    ap.add_argument("--mix", required=True)
    ap.add_argument("--out", type=Path, default=Path("demo.mp4"))
    a = ap.parse_args()
    asyncio.run(record(a.base, a.mof, a.mix, a.out))


if __name__ == "__main__":
    main()

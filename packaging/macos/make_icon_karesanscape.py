"""KareSanScape のアイコンを、ロゴの一覧画像（brand/karesanscape_sheet.webp）から切り出して作る（仮）。

元画像のアイコンは 450px ほどなので、1024px は引き伸ばし。元データ（1024px 以上の PNG か SVG）が来たら差し替える。
使い方: .venv/bin/python packaging/macos/make_icon_karesanscape.py
出力: packaging/macos/icon_1024.png・icon.icns・splash_mark.png、ontomap/app/web/logo_mark.png・favicon.png
"""
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
WEB = HERE.parents[1] / "ontomap" / "app" / "web"
SHEET = HERE / "brand" / "karesanscape_sheet.webp"
BOX = (1037, 50, 1473, 486)  # 一覧画像の右上の大きいアイコン（角丸の内側の正方形）
BG = (16, 47, 73)  # 深藍 #102F49


def rounded(img: Image.Image, radius: float) -> Image.Image:
    s = img.size[0]
    mask = Image.new("L", (s * 4, s * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s * 4 - 1, s * 4 - 1), radius=int(radius * 4), fill=255)
    mask = mask.resize((s, s), Image.LANCZOS)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def main() -> None:
    src = Image.open(SHEET).convert("RGB").crop(BOX)
    # 角の外（白地・影）が残らないよう、内側を少し広めに深藍で塗った正方形に重ねる
    art = Image.new("RGB", src.size, BG)
    art.paste(src.crop((8, 8, src.size[0] - 8, src.size[1] - 8)).resize(src.size, Image.LANCZOS), (0, 0))
    big = art.resize((824, 824), Image.LANCZOS).filter(ImageFilter.UnsharpMask(radius=1.2, percent=60))
    tile = rounded(big, 824 * 0.225)
    icon = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    icon.paste(tile, (100, 100), tile)  # macOS のアイコンの格子（824px の図柄を中央に）
    icon.save(HERE / "icon_1024.png")
    rounded(art.resize((256, 256), Image.LANCZOS), 256 * 0.225).save(HERE / "splash_mark.png")
    rounded(art.resize((64, 64), Image.LANCZOS), 64 * 0.24).save(WEB / "logo_mark.png")
    rounded(art.resize((64, 64), Image.LANCZOS), 64 * 0.24).save(WEB / "favicon.png")
    with tempfile.TemporaryDirectory() as td:
        iset = Path(td) / "icon.iconset"
        iset.mkdir()
        for s in (16, 32, 128, 256, 512):
            icon.resize((s, s), Image.LANCZOS).save(iset / f"icon_{s}x{s}.png")
            icon.resize((s * 2, s * 2), Image.LANCZOS).save(iset / f"icon_{s}x{s}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iset), "-o", str(HERE / "icon.icns")], check=True)
    print("ok")


if __name__ == "__main__":
    main()

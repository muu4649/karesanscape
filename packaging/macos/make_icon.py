"""アイコン（地図の等高線と中心の点）を作る。python make_icon.py → icon_1024.png と splash_mark.png"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyBboxPatch


def draw(path: str, size: int, rounded: bool) -> None:
    fig = plt.figure(figsize=(size / 100, size / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    fig.patch.set_alpha(0)
    if rounded:
        ax.add_patch(FancyBboxPatch((0.06, 0.06), 0.88, 0.88, boxstyle="round,pad=0,rounding_size=0.2", fc="#F8F7F4", ec="#DCD8CF", lw=size / 400))
    x, y = np.meshgrid(np.linspace(0, 1, 400), np.linspace(0, 1, 400))
    z = np.exp(-((x - 0.42) ** 2 + (y - 0.56) ** 2) / 0.03) + 0.8 * np.exp(-((x - 0.64) ** 2 + (y - 0.40) ** 2) / 0.02)
    mask = ((x - 0.5) ** 2 + (y - 0.5) ** 2) < 0.36 ** 2
    z = np.where(mask, z, np.nan)
    ax.contour(x, y, z, levels=np.linspace(0.12, 0.95, 7), colors="#4F7FB0", linewidths=size / 220)
    ax.add_patch(plt.Circle((0.5, 0.5), 0.36, fill=False, ec="#172B3A", lw=size / 70))
    ax.add_patch(plt.Circle((0.64, 0.40), 0.055, fc="#B94B42", ec="none"))
    fig.savefig(path, dpi=100, transparent=True)
    plt.close(fig)


draw("icon_1024.png", 1024, True)
draw("splash_mark.png", 256, False)

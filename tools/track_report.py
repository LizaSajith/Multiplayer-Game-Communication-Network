"""Print track diagnostics and (optionally) draw the circuit with matplotlib.

    python tools/track_report.py                 # numbers only
    python tools/track_report.py --png track.png # numbers + picture
"""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import track as T


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--png", help="write a picture of the track")
    ap.add_argument("--seed", type=int, default=17)
    args = ap.parse_args()
    tr = T.build_track(T.TRACK_CIRCUIT, args.seed)
    info = T.analyze(tr)
    for k, v in info.items():
        print("%-16s %s" % (k, v))
    if not args.png:
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Rectangle, Polygon

    fig, ax = plt.subplots(figsize=(12, 8))
    hw = tr.half_width
    left = [(p[0] + n[0] * hw, p[1] + n[1] * hw) for p, n in zip(tr.pts, tr.nrm)]
    right = [(p[0] - n[0] * hw, p[1] - n[1] * hw) for p, n in zip(tr.pts, tr.nrm)]
    for i in range(tr.n):
        j = (i + 1) % tr.n
        ax.add_patch(Polygon([left[i], left[j], right[j], right[i]], closed=True,
                             fc="#444", ec="none"))
    ax.plot([p[0] for p in tr.pts] + [tr.pts[0][0]], [p[1] for p in tr.pts] + [tr.pts[0][1]],
            "w--", lw=0.6)
    for name, e in (("l", left), ("r", right)):
        ax.plot([p[0] for p in e] + [e[0][0]], [p[1] for p in e] + [e[0][1]], "c-", lw=1)
    for k, c in enumerate(tr.checkpoints, 1):
        x, y, tx, ty, nx, ny = tr.frame_at(c)
        ax.plot([x + nx * hw, x - nx * hw], [y + ny * hw, y - ny * hw], "y-", lw=3)
        ax.text(x, y, "CP%d" % k, color="yellow", fontsize=9)
    x, y, tx, ty, nx, ny = tr.frame_at(0.0)
    ax.plot([x + nx * hw, x - nx * hw], [y + ny * hw, y - ny * hw], "w-", lw=4)
    ax.text(x, y + 40, "START/FINISH", color="white")
    for o in tr.obstacles:
        cx, cy = o.center(0.0)
        if o.kind == T.KIND_CIRCLE:
            ax.add_patch(Circle((cx, cy), o.rx, fc="orange" if o.moving else "#bbb", ec="k"))
            if o.moving:
                a = (o.ax + o.nx * (o.v0 - o.amp), o.ay + o.ny * (o.v0 - o.amp))
                b = (o.ax + o.nx * (o.v0 + o.amp), o.ay + o.ny * (o.v0 + o.amp))
                ax.plot([a[0], b[0]], [a[1], b[1]], "orange", lw=0.8)
        else:
            c = [(-o.rx, -o.ry), (o.rx, -o.ry), (o.rx, o.ry), (-o.rx, o.ry)]
            pts = [(cx + o.tx * u + o.nx * v, cy + o.ty * u + o.ny * v) for u, v in c]
            ax.add_patch(Polygon(pts, closed=True, fc="#bbb", ec="k"))
    for i in range(12):
        gx, gy, *_ = tr.grid_slot(i)
        ax.plot(gx, gy, "go", ms=4)
    ax.set_aspect("equal")
    ax.invert_yaxis()
    ax.set_facecolor("#1b5e20")
    ax.set_title("circuit: %.0f m, %d obstacles, hash %08x" % (info["length_m"], info["obstacles"], tr.hash))
    fig.savefig(args.png, dpi=80, bbox_inches="tight")
    print("wrote", args.png)


if __name__ == "__main__":
    main()

"""
Export the desktop avatar's processed head mesh for the web renderer.

The face on the HUD is MediaPipe's measured canonical face (core/face_model.obj,
Apache-2.0) with a generated cranium and neck, plus the jaw/brow/lip rigs built
in core/avatar_mesh.py. Rather than reimplement that geometry in JavaScript,
this dumps the finished arrays once; web/src/avatar.js poses and draws the same
character.

Run from the repository root:

    python server/tools/export_head.py
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.avatar_mesh import get_head_mesh  # noqa: E402

OUT = ROOT / "web" / "assets" / "head.json"


def r(values, digits=4):
    return [round(float(v), digits) for v in values]


def main() -> None:
    h = get_head_mesh()
    data = {
        "v": [r(row) for row in h["verts"]],
        "n": [r(row) for row in h["normals"]],
        "f": [list(map(int, face)) for face in h["faces"]],
        "e": [list(map(int, edge)) for edge in h["edges"]],
        "lm": {k: list(map(int, v)) for k, v in h["landmarks"].items()},
        "jaw": r(h["jaw"], 3),
        "brow": r(h["brow"], 3),
        "lips": r(h["lips"], 3),
        "fade": r(h["fade"], 3),
        "grp": r(h["face_group"], 0),
        "lipc": r(h["lip_centre"], 4),
        "span": [float(h["span"][0]), float(h["span"][1])],
        "nface": int(h["n_face"]),
        "nhead": int(h["n_head"]),
        "source": "MediaPipe canonical face model (Apache-2.0) + generated cranium/neck",
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} "
          f"({OUT.stat().st_size / 1024:.0f} KB, {len(data['v'])} verts, "
          f"{len(data['f'])} faces)")


if __name__ == "__main__":
    main()

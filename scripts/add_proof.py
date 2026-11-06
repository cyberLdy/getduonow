#!/usr/bin/env python3
"""Add a real order screenshot to the /stories "Real orders" wall: blur personal details, convert, append to proof.json.

    python3 scripts/add_proof.py shot.png --blur 40,210,700,260 --blur 40,300,500,340 \
        --en "Picked up in Springfield, 3 min after the alert" --zh "收到提醒 3 分钟后在斯普林菲尔德取到"

Runs on the system python3 (has Pillow); the app itself does not need Pillow.

--blur x1,y1,x2,y2 (pixels of the original image; repeat for each name / email / order number / address / phone).
Always look at the result (static/proof/<name>.webp) before deploying: nothing personal may stay readable.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from PIL import Image, ImageFilter

STATIC = Path(__file__).resolve().parents[1] / "duo_monitor" / "static" / "proof"
MAX_W = 900


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--blur", action="append", default=[], help="x1,y1,x2,y2 in original pixels")
    ap.add_argument("--en", required=True, help="English caption")
    ap.add_argument("--zh", default="", help="Chinese caption (other languages fall back to English)")
    a = ap.parse_args()

    im = Image.open(a.image).convert("RGB")
    for box in a.blur:
        x1, y1, x2, y2 = (int(v) for v in box.split(","))
        region = im.crop((x1, y1, x2, y2))
        im.paste(region.filter(ImageFilter.GaussianBlur(radius=max(12, (y2 - y1) // 2))), (x1, y1))
    if im.width > MAX_W:
        im = im.resize((MAX_W, round(im.height * MAX_W / im.width)), Image.LANCZOS)

    STATIC.mkdir(parents=True, exist_ok=True)
    manifest = STATIC / "proof.json"
    items = json.loads(manifest.read_text()) if manifest.exists() else []
    name = re.sub(r"[^a-z0-9]+", "-", Path(a.image).stem.lower()).strip("-") or "order"
    out = STATIC / f"{len(items) + 1:02d}-{name}.webp"
    im.save(out, "WEBP", quality=82, method=6)
    items.insert(0, {"src": f"/static/proof/{out.name}", "en": a.en, **({"zh": a.zh} if a.zh else {})})   # newest first
    manifest.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
    print(f"saved {out} ({out.stat().st_size // 1024} KB); {len(items)} screenshot(s) on the wall")


if __name__ == "__main__":
    main()

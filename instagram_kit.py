"""Instagram投稿キットを作る。

使い方:
    python instagram_kit.py item_20261121_02,item_20261125_14

作るもの（GitHubにpushするので、スマホからも開けます）:
    instagram/{id}.jpg        4:5（1080x1350）に切り出した投稿用画像
    instagram/captions.md     投稿本文（コピーして貼り付け用）
"""
import json
import subprocess
import sys

from PIL import Image

import auto_updater as a

OUT = a.ROOT / "instagram"
SITE = "https://hiro202610.github.io/picks"
IG_SIZE = (1080, 1350)
IG_DISCLOSURE = "As an Amazon Associate I earn from qualifying purchases."
COMMON_TAGS = ["#amazonfinds", "#founditonamazon"]
BRAND_TAGS = {
    "COSRX": ["#COSRX", "#koreanskincare", "#kbeauty"],
}


def crop_4x5(src, dst):
    im = Image.open(src).convert("RGB")
    w, h = im.size
    target_h = round(w * 5 / 4)
    if target_h <= h:
        top = (h - target_h) // 2
        im = im.crop((0, top, w, top + target_h))
    else:
        target_w = round(h * 4 / 5)
        left = (w - target_w) // 2
        im = im.crop((left, 0, left + target_w, h))
    im.resize(IG_SIZE, Image.LANCZOS).save(dst, "JPEG", quality=92)


def caption(p, row):
    title = row["Pin_Title"].removeprefix(a.SPONSORED_TITLE_PREFIX)
    first = f"#ad Sponsored | {title}" if p.get("sponsored") else title
    tags = [w for w in row["Pin_Description"].split() if w.startswith("#") and w.lower() != "#ad"]
    for brand, extra in BRAND_TAGS.items():
        if brand.lower() in p["title"].lower():
            tags += extra
    seen, uniq = set(), []
    for t in tags + COMMON_TAGS:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    tags = uniq[:10]
    lines = [first, "", p["summary"], ""]
    lines += [f"✨ {pt}" for pt in p["points"]]
    lines += ["", "🛒 Shop it: tap the link in my bio → The Curated Nest", "", IG_DISCLOSURE, "", " ".join(tags)]
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    ids = [i.strip() for i in sys.argv[1].split(",") if i.strip()]
    products = {p["id"]: p for p in json.loads(a.PRODUCTS_JSON.read_text(encoding="utf-8"))}
    rows = {a.row_id(r): r for r in a.load_batch_rows()}
    OUT.mkdir(exist_ok=True)
    md_path = OUT / "captions.md"
    md = md_path.read_text(encoding="utf-8") if md_path.exists() else "# Instagram投稿キット\n\n画像をスマホに保存し、本文をコピーして投稿してください。\n"
    for i in ids:
        p, row = products[i], rows[i]
        crop_4x5(a.ROOT / "images" / f"{i}.png", OUT / f"{i}.jpg")
        cap = caption(p, row)
        if f"## {i}" in md:
            print(f"⏭  {i} は captions.md に既にあります（画像のみ更新）")
        else:
            badge = "【キャンペーン】" if p.get("sponsored") else ""
            md += f"\n## {i} {badge}{p['title']}\n\n画像: {SITE}/instagram/{i}.jpg\n\n```\n{cap}\n```\n"
        print(f"✅ {i}  本文{len(cap)}文字")
    md_path.write_text(md, encoding="utf-8")
    subprocess.run(["git", "add", "instagram", "instagram_kit.py"], cwd=a.ROOT, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"Add Instagram kit: {', '.join(ids)}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"], cwd=a.ROOT, check=True)
    subprocess.run(["git", "push", "-q"], cwd=a.ROOT, check=True)
    print(f"🚀 push済み。スマホで開く: https://github.com/Hiro202610/picks/blob/main/instagram/captions.md")


if __name__ == "__main__":
    main()

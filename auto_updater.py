"""
auto_updater.py — 商品リサーチ → products.json 更新 → GitHub Pages 公開 → Pinterest用CSV出力

使い方:
  # A) Claude Code がリサーチした結果ファイルを使う（APIキー不要）
  python auto_updater.py --genre "Cozy Home Decor" --count 3 --input research/cozy_home_decor.json

  # B) スクリプト単体で Claude API + Web検索 を使って自動リサーチ（ANTHROPIC_API_KEY が必要）
  python auto_updater.py --genre "Beauty" --count 5

  # 引数なしで実行すると、ジャンルと件数を対話形式で聞きます
  python auto_updater.py

  # GitHubの images/ に画像をアップロードした後：取り込み → サイト用JPEG作成 → push
  python auto_updater.py --sync-images

  # Pinterest一括アップロード用CSVを作り直す（予約なし = すぐ公開）
  python auto_updater.py --pinterest-csv

  # 予約投稿：明日から1日2件、米国東部時間 11:00 と 20:30 に公開。在庫切れの商品は外す
  python auto_updater.py --schedule --exclude item_20261116_14,item_20261116_15
  #   --start 2026-11-20   開始日を指定 / --per-day 3 --times "9:00,13:00,20:30"  件数と時刻を変更
  #   --clear-schedule     予約を消して「すぐ公開」に戻す
  #   --only id,id         指定した商品だけでCSVを作る（アップロード済みの予約には触れない）

オプション:
  --no-push   products.json と CSV は更新するが git commit / push はしない（確認用）
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

# ===== 設定 =====
TRACKING_ID = "awd2501-20"
SITE_URL = "https://hiro202610.github.io/picks/"
MODEL = "claude-opus-5-5"

ROOT = Path(__file__).resolve().parent
PRODUCTS_JSON = ROOT / "products.json"
PINTEREST_CSV = ROOT / "pinterest_batch.csv"     # 管理用（Excelで見る用・BOM付き）
PINTEREST_UPLOAD_CSV = ROOT / "pinterest_upload.csv"  # Pinterest公式の一括アップロード用

# サイトのカテゴリ名 → Pinterestのボード名（ボード名はPinterest上の名前と完全一致させる）
BOARDS = {
    "Cozy Decor": "Cozy Decor Ideas",
    "Dreamy Nursery": "Dreamy Nursery Ideas",
    "Beauty": "Beauty Finds",
}
# 入力されたジャンル名に含まれる語 → 正式カテゴリ名（例: "Cozy Home Decor" → "Cozy Decor"）
CATEGORY_KEYWORDS = [
    (("nursery", "nersery", "baby", "kids"), "Dreamy Nursery"),
    (("beauty", "skin", "cosmetic", "makeup", "k-beauty", "hair"), "Beauty"),
    (("decor", "home", "interior", "cozy", "kitchen"), "Cozy Decor"),
]
IMAGES_DIR = ROOT / "images"          # Pinterest用の元画像（{id}.png）
WEB_IMAGES_DIR = IMAGES_DIR / "web"   # サイト表示用の軽量JPEG（自動生成）
WEB_IMAGE_WIDTH = 800

# 予約投稿：ターゲット（米国）の時間帯で指定し、PinterestにはUTCで渡す
TARGET_TZ = "America/New_York"
VIEWER_TZ = "Asia/Tokyo"            # 確認用に日本時間も表示する
DEFAULT_POST_TIMES = "11:00,20:30"  # 米国東部時間の昼休みと夜（Pinterestがよく見られる時間帯）
SCHEDULE_MAX_DAYS = 14              # Pinterestで予約できるのは約2週間先まで

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
ASIN_IN_URL_RE = re.compile(r"/dp/([A-Z0-9]{10})")
PIN_TITLE_MAX = 100
PIN_DESC_MAX = 500  # Pinterest の説明文上限
DISCLOSURE = "(Affiliate link: I may earn a small commission at no extra cost to you.)"
# ブランドのキャンペーン商品（リサーチファイルで "sponsored": true）に付ける表記。先頭に置いて必ず見えるようにする
SPONSORED_TITLE_PREFIX = "Ad | "
SPONSORED_PREFIX = "#ad Sponsored: I earn a commission from this brand. "

IMAGE_PROMPT_RULES = (
    "Vertical 2:3 aspect ratio (1000x1500 px), Pinterest-style lifestyle photograph. "
    "Soft natural window light, warm neutral palette of cream, beige, oatmeal and light wood, "
    "minimal and airy styling, shallow depth of field, editorial interior-magazine quality, "
    "photorealistic. Show the product true to its real appearance, as the clear focal point."
)
# 文字なしルール（コスメ以外）。コスメはパッケージの商品名がないと何の商品か分からないので残す
NO_TEXT_RULE = "IMPORTANT: absolutely no text, letters, words, numbers, logos, brand names, labels or watermarks anywhere in the image."
LABEL_TEXT_RULE = (
    "Keep the brand name and product name printed on the packaging exactly as on the real product, sharp and legible. "
    "Do not add any other text, captions, prices or watermarks."
)
LABEL_CATEGORIES = {"Beauty"}
# ChatGPTにはAmazonの商品写真を添付して、見た目を実物に合わせてもらう
IMAGE_REFERENCE_RULE = "Use the attached product photo as the reference and match the product's real shape, color and texture exactly."
# カテゴリ別の追加ルール
CATEGORY_IMAGE_RULES = {
    "Dreamy Nursery": (
        "Do not show any babies or children. If a crib appears, it must be completely empty "
        "(no blankets, pillows, bumpers, toys, canopies or netting inside or hanging over it), following safe-sleep guidelines."
    ),
    "Beauty": "Product-focused still life (vanity, bathroom shelf or flat lay); do not show human faces.",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ---------- 入力 ----------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Amazon picks auto updater")
    p.add_argument("--genre", help='対象ジャンル（例: "Home & Kitchen", "Beauty", "Nursery", "Gadgets"）')
    p.add_argument("--count", type=int, help="取得件数（デフォルト5）")
    p.add_argument("--input", help="リサーチ結果のJSONファイル（指定時はAPIを使わない）")
    p.add_argument("--no-push", action="store_true", help="git commit / push を行わない")
    p.add_argument("--sync-images", action="store_true",
                   help="GitHubから画像を取り込み、サイト用JPEGを作ってpushする（商品追加はしない）")
    p.add_argument("--pinterest-csv", action="store_true",
                   help="pinterest_batch.csv から Pinterest一括アップロード用CSVを作り直す（手直し後に使う）")
    s = p.add_argument_group("予約投稿（--pinterest-csv と一緒に使う）")
    s.add_argument("--schedule", action="store_true", help="公開日時を自動で割り振る")
    s.add_argument("--start", help="予約の開始日（米国東部時間の日付 YYYY-MM-DD。省略時は明日）")
    s.add_argument("--per-day", type=int, default=2, help="1日あたりの投稿数（デフォルト2）")
    s.add_argument("--times", default=DEFAULT_POST_TIMES,
                   help=f"投稿時刻（米国東部時間・カンマ区切り。デフォルト {DEFAULT_POST_TIMES}）")
    s.add_argument("--exclude", default="", help="アップロードから外す商品id（カンマ区切り。例: 在庫切れ商品）")
    s.add_argument("--only", default="", help="この商品idだけでCSVを作る（他の行の予約はそのまま残す）")
    s.add_argument("--clear-schedule", action="store_true", help="予約日時を消して「すぐ公開」に戻す")
    args = p.parse_args()
    if args.schedule or args.clear_schedule or args.exclude or args.only:
        args.pinterest_csv = True
    if args.sync_images or args.pinterest_csv:
        return args

    if not args.genre:
        args.genre = input("対象ジャンルを入力してください（例: Home & Kitchen）: ").strip() or "Home & Kitchen"
    if args.count is None:
        raw = input("取得件数を入力してください [5]: ").strip()
        args.count = int(raw) if raw else 5
    if args.count < 1:
        sys.exit("取得件数は1以上にしてください。")
    canonical = canonical_category(args.genre)
    if canonical != args.genre:
        print(f"ℹ️  ジャンル「{args.genre}」はカテゴリ「{canonical}」（ボード: {BOARDS[canonical]}）として登録します。")
    args.genre = canonical
    return args


def canonical_category(genre: str) -> str:
    g = genre.lower()
    for words, category in CATEGORY_KEYWORDS:
        if any(w in g for w in words):
            return category
    return genre


def board_for(category: str) -> str:
    return BOARDS.get(category) or BOARDS.get(canonical_category(category), "")


def load_products() -> list[dict]:
    if not PRODUCTS_JSON.exists():
        return []
    data = json.loads(PRODUCTS_JSON.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        sys.exit("products.json の形式が不正です（配列である必要があります）。")
    return data


def existing_asins(products: list[dict]) -> set[str]:
    found = set()
    for p in products:
        m = ASIN_IN_URL_RE.search(p.get("affiliateUrl", ""))
        if m:
            found.add(m.group(1))
    return found


# ---------- リサーチ（Claude API + Web検索） ----------
RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "asin": {"type": "string"},
                    "source_url": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "points": {"type": "array", "items": {"type": "string"}},
                    "badge": {"type": "string"},
                    "pin_title": {"type": "string"},
                    "pin_description": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "image_scene": {"type": "string"},
                },
                "required": [
                    "asin", "source_url", "title", "summary", "points", "badge",
                    "pin_title", "pin_description", "hashtags", "image_scene",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["products"],
    "additionalProperties": False,
}


def research_with_claude(genre: str, count: int, skip_asins: set[str]) -> list[dict]:
    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic ライブラリがありません: python -m pip install anthropic")

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY などを環境から読み込む
    beta_kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

    research_prompt = f"""Research currently popular, highly rated products sold on Amazon.com (US) in the genre "{genre}".
Find {count} distinct products that appeal to US Pinterest users who love cozy lifestyle and interior styling.

Rules:
- Use web search. Prefer Amazon Best Sellers pages, Amazon product pages and reputable review roundups.
- Only include a product if you saw its real 10-character ASIN in a source (e.g. an amazon.com/.../dp/ASIN URL). Never guess or invent an ASIN.
- Skip these ASINs (already on the site): {", ".join(sorted(skip_asins)) or "none"}.
- For each product report: ASIN, the source URL where you saw the ASIN, the exact product name, and the key facts (material, size, what people like).
- Do not report prices."""

    print(f"🔎 Claude ({MODEL}) で「{genre}」をWeb検索中…（数分かかることがあります）")
    messages = [{"role": "user", "content": research_prompt}]
    for _ in range(6):
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            output_config={"effort": "high"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 15}],
            messages=messages,
            **beta_kwargs,
        )
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    if resp.stop_reason == "refusal":
        sys.exit("リサーチがモデルに拒否されました。ジャンルを変えて再実行してください。")
    notes = "\n".join(b.text for b in resp.content if b.type == "text")

    format_prompt = f"""Below are research notes about Amazon.com products in the genre "{genre}".
Turn up to {count} of them into site and Pinterest copy. Use only products whose ASIN appears in the notes, copied exactly.

For each product write:
- title: an attractive, clear English product name (no brand-stuffing, under 70 characters)
- summary: 2-3 English sentences that make a US shopper want it (warm, honest, no prices, no unverifiable claims)
- points: exactly 3 short English benefit bullets
- badge: one short label such as "Best Seller", "Top Rated", "Editor's Pick" or "Cozy Favorite" (use "Best Seller"/"Top Rated" only if the notes support it)
- pin_title: Pinterest-SEO English title, max 100 characters, keyword-rich and natural
- pin_description: 2-3 English sentences that spark purchase intent (no hashtags here, no prices)
- hashtags: exactly 5 relevant hashtags, each starting with #
- image_scene: one detailed English sentence describing a stylish real-life room scene where this product sits naturally

Research notes:
{notes}"""

    print("✍️  商品データとPinterest用テキストを生成中…")
    resp2 = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": RESEARCH_SCHEMA}},
        messages=[{"role": "user", "content": format_prompt}],
        **beta_kwargs,
    )
    if resp2.stop_reason == "refusal":
        sys.exit("データ生成がモデルに拒否されました。")
    text = next(b.text for b in resp2.content if b.type == "text")
    return json.loads(text)["products"]


def load_research_file(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["products"] if isinstance(data, dict) else data


# ---------- データ生成 ----------
def next_ids(products: list[dict], n: int) -> list[str]:
    prefix = f"item_{date.today():%Y%m%d}_"
    used = [int(p["id"][len(prefix):]) for p in products
            if str(p.get("id", "")).startswith(prefix) and p["id"][len(prefix):].isdigit()]
    start = max(used, default=0) + 1
    return [f"{prefix}{i:02d}" for i in range(start, start + n)]


def normalize_hashtag(tag: str) -> str:
    tag = re.sub(r"[^0-9A-Za-z]", "", tag)
    return f"#{tag}" if tag else ""


def hashtag_to_words(tag: str) -> str:
    """#CozyHomeDecor → cozy home decor（PinterestのKeywords列用）"""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tag.lstrip("#")).lower()


def default_hashtags(genre: str) -> list[str]:
    extra = {
        "Cozy Decor": ["#CozyDecor", "#HomeDecorIdeas", "#CozyHome", "#NeutralDecor", "#AmazonHomeFinds"],
        "Dreamy Nursery": ["#NurseryIdeas", "#NurseryDecor", "#BabyRegistry", "#DreamyNursery", "#AmazonBabyFinds"],
        "Beauty": ["#BeautyFinds", "#SkincareRoutine", "#KBeauty", "#SelfCare", "#AmazonBeautyFinds"],
    }.get(canonical_category(genre), [normalize_hashtag(genre), "#AmazonFinds"])
    return list(dict.fromkeys(t for t in extra if t))[:5]


def build_items(raw: list[dict], genre: str, count: int, products: list[dict]) -> list[tuple[dict, dict]]:
    """(products.json用の商品, CSV用の行) のリストを返す"""
    skip = existing_asins(products)
    picked = []
    for r in raw:
        asin = str(r.get("asin", "")).strip().upper()
        if not ASIN_RE.match(asin):
            print(f"⚠️  ASINの形式が不正なのでスキップ: {r.get('title')} ({asin})")
            continue
        if asin in skip:
            print(f"↪️  掲載済みのためスキップ: {r.get('title')} ({asin})")
            continue
        skip.add(asin)
        picked.append((asin, r))
        if len(picked) == count:
            break

    items = []
    for item_id, (asin, r) in zip(next_ids(products, len(picked)), picked):
        title = r["title"].strip()
        # リサーチファイルで商品ごとに category を指定すると、複数ジャンルを1回で処理できる
        category = canonical_category(r.get("category") or genre)
        product = {
            "id": item_id,
            "title": title,
            "category": category,
            "summary": r["summary"].strip(),
            "points": [p.strip() for p in r["points"]][:3],
            "affiliateUrl": f"https://www.amazon.com/dp/{asin}?tag={TRACKING_ID}",
            "badge": r.get("badge") or "Editor's Pick",
        }
        # Creator Connections のキャンペーン商品は「Ad」を見える位置に入れる（規約: unavoidable disclosure）
        sponsored = bool(r.get("sponsored"))
        if sponsored:
            product["sponsored"] = True
            if r.get("campaign_end"):
                product["campaignEnd"] = r["campaign_end"]

        tags = [t for t in (normalize_hashtag(h) for h in r.get("hashtags", [])) if t][:5]
        if len(tags) < 5:
            tags = list(dict.fromkeys(tags + default_hashtags(category)))[:5]
        body = (r.get("pin_description") or product["summary"]).strip()
        head = SPONSORED_PREFIX if sponsored else ""
        brand_note = f" {r['brand_disclosure'].strip()}" if r.get("brand_disclosure") else ""
        tail = f" {DISCLOSURE}{brand_note}\n\n{' '.join(tags)}"
        if len(head) + len(body) + len(tail) > PIN_DESC_MAX:
            body = body[: PIN_DESC_MAX - len(head) - len(tail) - 1].rsplit(" ", 1)[0] + "…"
        body = head + body

        pin_title = (r.get("pin_title") or f"{title} | {category} Idea").strip()
        title_head = SPONSORED_TITLE_PREFIX if sponsored else ""
        if len(title_head) + len(pin_title) > PIN_TITLE_MAX:
            pin_title = pin_title[: PIN_TITLE_MAX - len(title_head) - 1].rsplit(" ", 1)[0] + "…"
        pin_title = title_head + pin_title

        scene = (r.get("image_scene") or f"A cozy, styled living space featuring the {title}.").strip()
        row = {
            "Product_Name": title,
            "Pin_Title": pin_title,
            "Pin_Description": body + tail,
            "Destination_URL": f"{SITE_URL}?id={item_id}",
            "ChatGPT_Image_Prompt": " ".join(filter(None, [
                f"Product: {title} ({category}). Scene: {scene}",
                IMAGE_REFERENCE_RULE, CATEGORY_IMAGE_RULES.get(category, ""), IMAGE_PROMPT_RULES,
                LABEL_TEXT_RULE if category in LABEL_CATEGORIES else NO_TEXT_RULE,
            ])),
            # 生成した画像を GitHub の images/ に「{id}.png」の名前で置くと、このURLで公開される
            "Image_URL": f"{SITE_URL}images/{item_id}.png",
            "Pinterest_Board": board_for(category),
            "Keywords": ", ".join(hashtag_to_words(t) for t in tags),
        }
        items.append((product, row))
    return items


# ---------- 出力 ----------
def save_products(new: list[dict], products: list[dict]) -> None:
    PRODUCTS_JSON.write_text(json.dumps(new + products, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_batch_rows() -> list[dict]:
    if not PINTEREST_CSV.exists():
        return []
    with PINTEREST_CSV.open(encoding="utf-8-sig", newline="") as f:
        return [{k: v for k, v in r.items() if k} for r in csv.DictReader(f)]


def save_csv(rows: list[dict]) -> None:
    fields: list[str] = []
    for r in rows:  # 行ごとに列が違っても（古い形式の行など）すべての列を残す
        fields += [k for k in r if k not in fields]
    with PINTEREST_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL, restval="")
        w.writeheader()
        w.writerows(rows)


UPLOAD_COLUMNS = ["Title", "Media URL", "Pinterest board", "Thumbnail", "Description", "Link", "Publish date", "Keywords"]
# Pinterestからダウンロードした公式ひな形をここに置くと、その列名・列順でCSVを作る
TEMPLATE_CSV = ROOT / "templates" / "pinterest_template.csv"


def upload_columns() -> list[str]:
    if not TEMPLATE_CSV.exists():
        return UPLOAD_COLUMNS
    with TEMPLATE_CSV.open(encoding="utf-8-sig", newline="") as f:
        header = [h.strip() for h in next(csv.reader(f), []) if h.strip()]
    unknown = [h for h in header if h not in UPLOAD_COLUMNS]
    lacking = [h for h in UPLOAD_COLUMNS if h not in header]
    if unknown:
        print(f"⚠️  ひな形に未対応の列があります（空欄で出力）: {', '.join(unknown)}")
    if lacking:
        print(f"⚠️  ひな形にない列は出力しません: {', '.join(lacking)}")
    return header or UPLOAD_COLUMNS


def save_upload_csv(rows: list[dict]) -> list[str]:
    """管理用の行から Pinterest公式の一括アップロード用CSVを書き出す。問題点のリストを返す"""
    problems, out = [], []
    for r in rows:
        name = r.get("Product_Name", "")
        if not r.get("Pinterest_Board"):
            problems.append(f"{name}: ボードが決まっていません（BOARDS に追加してください）")
        if len(r.get("Pin_Title", "")) > PIN_TITLE_MAX:
            problems.append(f"{name}: タイトルが{PIN_TITLE_MAX}文字を超えています")
        if len(r.get("Pin_Description", "")) > PIN_DESC_MAX:
            problems.append(f"{name}: 説明文が{PIN_DESC_MAX}文字を超えています")
        # セル内の改行はPinterestが行区切りと誤認する（「200行を超えています」エラーになる）ので1行にする
        one_line = {k: " ".join(str(v).split()) for k, v in r.items()}
        out.append({
            "Title": one_line.get("Pin_Title", ""),
            "Media URL": one_line.get("Image_URL", ""),
            "Pinterest board": one_line.get("Pinterest_Board", ""),
            "Thumbnail": "",  # 動画ピン用。画像ピンでは空欄
            "Description": one_line.get("Pin_Description", ""),
            "Link": one_line.get("Destination_URL", ""),
            "Publish date": one_line.get("Publish_Date", ""),  # 空欄 = すぐ公開
            "Keywords": one_line.get("Keywords", ""),
        })
    # Pinterest用はBOMなしUTF-8（BOMがあると先頭列名 "Title" が認識されないことがあるため）
    columns = upload_columns()
    with PINTEREST_UPLOAD_CSV.open("w", encoding="utf-8", newline="") as f:
        # 公式ひな形と同じ書式（必要なセルだけ引用符・CRLF改行）
        w = csv.DictWriter(f, fieldnames=columns, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n",
                           extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(out)
    return problems


def check_media_urls(rows: list[dict]) -> list[str]:
    """Media URL が実際に画像として公開されているか確認する"""
    import urllib.request
    missing = []
    for r in rows:
        url = r.get("Image_URL", "")
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as res:
                ok = res.status == 200 and res.headers.get("Content-Type", "").startswith("image/")
        except Exception:
            ok = False
        if not ok:
            missing.append(url)
    return missing


def row_id(r: dict) -> str:
    return r.get("Destination_URL", "").split("id=")[-1]


def interleave_by_board(rows: list[dict]) -> list[dict]:
    """同じボードが連続しないよう、ボードごとに順番に並べ替える"""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("Pinterest_Board", ""), []).append(r)
    out = []
    while any(groups.values()):
        for g in groups.values():
            if g:
                out.append(g.pop(0))
    return out


def apply_schedule(rows: list[dict], start: str | None, per_day: int, times: str) -> list[dict]:
    """rows に Publish_Date（UTC）と確認用の日本時間・米国東部時間を書き込む。予約できない行は返す"""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    tz, viewer = ZoneInfo(TARGET_TZ), ZoneInfo(VIEWER_TZ)
    slots = [t.strip() for t in times.split(",") if t.strip()]
    per_day = max(1, min(per_day, len(slots)))
    now = datetime.now(timezone.utc)
    day = (datetime.strptime(start, "%Y-%m-%d").date() if start
           else (now.astimezone(tz) + timedelta(days=1)).date())
    limit = now + timedelta(days=SCHEDULE_MAX_DAYS)

    too_late = []
    for i, r in enumerate(interleave_by_board(rows)):
        d = day + timedelta(days=i // per_day)
        hh, mm = map(int, slots[i % per_day].split(":"))
        local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
        utc = local.astimezone(timezone.utc)
        if utc <= now:
            print(f"⚠️  {row_id(r)} の予約日時が過去です（すぐ公開されます）")
        if utc > limit:
            too_late.append(r)
            r["Publish_Date"] = r["Publish_ET"] = r["Publish_JST"] = ""
            continue
        r["Publish_Date"] = utc.strftime("%Y-%m-%dT%H:%M:%S")
        r["Publish_ET"] = local.strftime("%m/%d %H:%M ET")
        r["Publish_JST"] = utc.astimezone(viewer).strftime("%m/%d %H:%M JST")
    return too_late


def regenerate_upload_csv(args: argparse.Namespace | None = None) -> None:
    if not PINTEREST_CSV.exists():
        sys.exit("pinterest_batch.csv がありません。")
    with PINTEREST_CSV.open(encoding="utf-8-sig", newline="") as f:
        rows = [{k: v for k, v in r.items() if k} for r in csv.DictReader(f)]
    # 古い形式のCSV（ボード列・キーワード列なし）を補完する
    categories = {p["id"]: p.get("category", "") for p in load_products()}
    for r in rows:
        pid = r.get("Destination_URL", "").split("id=")[-1]
        # ボード名は常に最新の BOARDS 設定から決め直す（ボード名変更に追従するため）
        r["Pinterest_Board"] = board_for(categories.get(pid, "")) or r.get("Pinterest_Board", "")
        if not r.get("Keywords"):
            r["Keywords"] = ", ".join(hashtag_to_words(t) for t in re.findall(r"#\w+", r.get("Pin_Description", "")))
        if not r.get("Image_URL"):
            r["Image_URL"] = f"{SITE_URL}images/{pid}.png"

    exclude = {x.strip() for x in (args.exclude.split(",") if args and args.exclude else []) if x.strip()}
    only = {x.strip() for x in (args.only.split(",") if args and args.only else []) if x.strip()}
    unknown = (exclude | only) - {row_id(r) for r in rows}
    if unknown:
        print(f"⚠️  CSVに見つからないid（無視します）: {', '.join(sorted(unknown))}")
    if only:
        # アップロード済みの行は予約記録を残したまま、指定した行だけを対象にする
        targets = [r for r in rows if row_id(r) in only and row_id(r) not in exclude]
    else:
        targets = [r for r in rows if row_id(r) not in exclude]
        for r in rows:
            if row_id(r) in exclude:  # 外した行は予約も消しておく
                r["Publish_Date"] = r["Publish_ET"] = r["Publish_JST"] = ""

    if args and args.clear_schedule:
        for r in targets:
            r["Publish_Date"] = r["Publish_ET"] = r["Publish_JST"] = ""
        print("🗑  予約日時を消しました（アップロードするとすぐ公開されます）。")
    elif args and args.schedule:
        too_late = apply_schedule(targets, args.start, args.per_day, args.times)
        if too_late:
            print(f"⚠️  {SCHEDULE_MAX_DAYS}日より先になる {len(too_late)}件は今回のCSVから外しました（後日もう一度 --schedule してください）:")
            for r in too_late:
                print(f"    - {row_id(r)}  {r['Product_Name']}")
            late_ids = {row_id(r) for r in too_late}
            targets = [r for r in targets if row_id(r) not in late_ids]
        targets.sort(key=lambda r: r.get("Publish_Date", ""))

    save_csv(rows)
    if exclude:
        print(f"⏸  アップロードから外した商品: {', '.join(sorted(exclude & {row_id(r) for r in rows}))}")
    report_upload_csv(targets)


def report_upload_csv(rows: list[dict]) -> None:
    problems = save_upload_csv(rows)
    print(f"📌 Pinterest一括アップロード用CSV: {PINTEREST_UPLOAD_CSV.name}（{len(rows)}行）")
    for r in rows:
        when = f"{r['Publish_JST']}（{r['Publish_ET']}）" if r.get("Publish_Date") else "すぐ公開"
        print(f"    {when:<30} {r['Pinterest_Board'] or '（ボード未設定）':<22} {row_id(r)}  {r['Product_Name']}")
    missing = check_media_urls(rows)
    for url in missing:
        problems.append(f"画像がまだ公開されていません: {url}")
    if problems:
        print("⚠️  アップロード前に確認してください:")
        for p in problems:
            print(f"    - {p}")
    else:
        print("✅ 画像URL・文字数・ボードの確認OK。Pinterestにアップロードできます。")


def optimize_images() -> list[str]:
    """images/{id}.png から サイト用の images/web/{id}.jpg（幅800px）を作る。作成したidを返す"""
    try:
        from PIL import Image
    except ImportError:
        print("⚠️  Pillow がないためサイト用画像を作れません: python -m pip install pillow")
        return []
    WEB_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    made = []
    for src in sorted(IMAGES_DIR.glob("*")):
        if src.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or not src.is_file():
            continue
        dst = WEB_IMAGES_DIR / f"{src.stem}.jpg"
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            continue
        with Image.open(src) as im:
            im = im.convert("RGB")
            if im.width > WEB_IMAGE_WIDTH:
                im = im.resize((WEB_IMAGE_WIDTH, round(im.height * WEB_IMAGE_WIDTH / im.width)), Image.LANCZOS)
            im.save(dst, "JPEG", quality=82, optimize=True, progressive=True)
        made.append(src.stem)
        print(f"🖼  サイト用画像を作成: images/web/{dst.name}（{dst.stat().st_size // 1024} KB）")
    return made


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def sync_images(push: bool) -> None:
    pull = git("pull", "--ff-only")
    if pull.returncode != 0:
        sys.exit("⚠️  git pull に失敗しました:\n" + pull.stderr.strip())
    made = optimize_images()
    if not made:
        print("✅ 新しい画像はありませんでした（サイト用画像はすべて最新です）。")
        return
    if push:
        publish_message(f"Add site images: {', '.join(made)}")


def publish_message(msg: str) -> bool:
    git("add", ".")
    c = git("commit", "-m", msg)
    if c.returncode != 0:
        print("⚠️  git commit に失敗しました:\n" + (c.stdout + c.stderr).strip())
        return False
    p = git("push")
    if p.returncode != 0:
        print("⚠️  git push に失敗しました（ローカルにはコミット済み）:\n" + p.stderr.strip())
        return False
    print(f"🚀 GitHubへpushしました: {msg}")
    return True


def publish(genre: str, n: int) -> bool:
    optimize_images()
    return publish_message(f"Add {n} new picks: {genre} ({date.today():%Y-%m-%d})")


def main() -> None:
    args = parse_args()
    if args.sync_images:
        sync_images(push=not args.no_push)
        return
    if args.pinterest_csv:
        regenerate_upload_csv(args)
        return
    products = load_products()

    if args.input:
        raw = load_research_file(args.input)
    else:
        raw = research_with_claude(args.genre, args.count, existing_asins(products))

    items = build_items(raw, args.genre, args.count, products)
    if not items:
        sys.exit("追加できる新商品がありませんでした。")

    new_products = [p for p, _ in items]
    save_products(new_products, products)
    # 管理用CSVは過去の行（予約記録・在庫切れで保留中の行）を残したまま追記する
    save_csv(load_batch_rows() + [r for _, r in items])

    print(f"\n✅ {len(items)}件を products.json の先頭に追加しました（合計 {len(products) + len(items)}件）")
    for p, r in items:
        print(f"  - {p['id']}  {p['title']}\n    {p['affiliateUrl']}\n    {r['Destination_URL']}")
    print(f"📄 Pinterest用CSV: {PINTEREST_CSV.name}（UTF-8 BOM付き / {len(items)}行）")
    print("🖼  画像はGitHubの images/ フォルダに、次の名前でアップロードしてください:")
    for p, _ in items:
        print(f"    {p['id']}.png")
    save_upload_csv([r for _, r in items])
    print(f"📌 Pinterest一括アップロード用CSV: {PINTEREST_UPLOAD_CSV.name}"
          "（画像をアップロードして --sync-images した後、--pinterest-csv で最終確認してください）")

    if args.no_push:
        print("ℹ️  --no-push 指定のため git push はスキップしました。")
    else:
        publish(args.genre, len(items))


if __name__ == "__main__":
    main()

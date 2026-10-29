"""
楽天ランキング取得 → 前日比較（急上昇の抽出）→ 価格差チェック → 記事下書き生成
"""

import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).strftime("%Y-%m-%d")

DATA_DIR = ROOT / "data" / "rankings"
DRAFT_DIR = ROOT / "drafts" / TODAY
DRY_RUN = os.getenv("DRY_RUN") == "1"


# ---------- 楽天API ----------

def rakuten_get(url: str, params: dict) -> dict:
    base = {
        "applicationId": os.environ["RAKUTEN_APP_ID"],
        "accessKey": os.environ["RAKUTEN_ACCESS_KEY"],
        "format": "json",
        "formatVersion": 2,
    }
    aff = os.getenv("RAKUTEN_AFFILIATE_ID")
    if aff:
        base["affiliateId"] = aff
　　　   ref = os.environ["RAKUTEN_REFERER"].strip()
    origin = "/".join(ref.split("/")[:3])
    headers = {"Referer": ref, "Origin": origin}


    for attempt in range(3):
        r = requests.get(url, params={**base, **params}, headers=headers, timeout=20)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"楽天API エラー {r.status_code}: {r.text[:300]}")
        time.sleep(CONFIG["request_interval_sec"])
        return r.json()
    raise RuntimeError("楽天API: リトライ上限に達しました")


def normalize_items(resp: dict) -> list:
    items = resp.get("Items", [])
    return [i["Item"] if isinstance(i, dict) and "Item" in i else i for i in items]


def fetch_ranking(genre_id: str) -> list:
    out = []
    for page in range(1, CONFIG["ranking_pages"] + 1):
        params = {"page": page}
        if genre_id != "0":
            params["genreId"] = genre_id
        items = normalize_items(rakuten_get(CONFIG["ranking_endpoint"], params))
        for it in items:
            out.append({
                "rank": it.get("rank"),
                "itemCode": it.get("itemCode"),
                "itemName": it.get("itemName", ""),
                "itemPrice": it.get("itemPrice"),
                "reviewCount": it.get("reviewCount", 0),
                "reviewAverage": it.get("reviewAverage", 0),
                "shopName": it.get("shopName", ""),
                "itemUrl": it.get("itemUrl", ""),
                "affiliateUrl": it.get("affiliateUrl", ""),
                "catchcopy": it.get("catchcopy", ""),
                "itemCaption": (it.get("itemCaption") or "")[:600],
            })
        if len(items) < 30:
            break
    return out


def search_keyword(name: str) -> str:
    name = re.sub(r"[【\[（(＼<《].*?[】\]）)／>》]", " ", name)
    name = re.sub(r"[!！★☆♪◆■●※＼／|｜]", " ", name)
    words = [w for w in name.split() if len(w) > 1]
    return " ".join(words[:4])[:60]


def price_spread(item: dict) -> dict | None:
    kw = search_keyword(item["itemName"])
    if not kw:
        return None
    resp = rakuten_get(CONFIG["search_endpoint"], {"keyword": kw, "hits": 30, "sort": "+itemPrice"})
    prices = [i.get("itemPrice") for i in normalize_items(resp) if i.get("itemPrice")]
    if len(prices) < 2:
        return None
    return {
        "keyword": kw,
        "min": min(prices),
        "max": max(prices),
        "median": int(statistics.median(prices)),
        "count": len(prices),
        "ranking_price_vs_min": (item["itemPrice"] or 0) - min(prices),
    }


# ---------- 前日比較 ----------

def load_previous(genre_id: str) -> dict:
    files = sorted(DATA_DIR.glob(f"*_g{genre_id}.json"))
    files = [f for f in files if not f.name.startswith(TODAY)]
    if not files:
        return {}
    prev = json.loads(files[-1].read_text(encoding="utf-8"))
    return {i["itemCode"]: i["rank"] for i in prev}


def find_rising(today: list, prev: dict) -> list:
    rising = []
    for it in today:
        old = prev.get(it["itemCode"])
        if old is None:
            if prev:
                it["trend"] = "新規ランクイン"
                it["score"] = 1000 - it["rank"]
                rising.append(it)
        else:
            up = old - it["rank"]
            if up >= CONFIG["min_rank_up"]:
                it["trend"] = f"{old}位→{it['rank']}位（+{up}）"
                it["score"] = up * 10 - it["rank"]
                rising.append(it)
    return sorted(rising, key=lambda x: -x["score"])


# ---------- 下書き生成 ----------

SYSTEM_PROMPT = """あなたは楽天アフィリエイト記事の編集者です。
与えられた商品データだけを根拠に、記事の「下書き」を日本語で作ります。
厳守事項:
- 使用感・体験談・感想を創作しない。体験部分は必ず指定のプレースホルダーを残す
- レビュー件数・平均点・価格など、データにある数字だけを使う。効果効能を断定しない
- 冒頭にPR表記を入れる
"""

USER_TEMPLATE = """以下の商品について、Markdownで出力してください。

# 出力構成
1. 冒頭のPR表記（「※本記事はアフィリエイト広告（PR）を含みます」）
2. ## 想定読者（どの層に刺さるか）: 仮説を3つ。それぞれ「誰が／どんな悩みで／なぜこの商品か」
3. ## 記事タイトル案: 3つ（32文字以内）
4. ## 構成案: H2/H3の見出しリスト
5. ## 商品説明パート（下書き）: 400〜600字。スペックと特徴を読者の悩みに結びつけて説明
6. ## 実際に使ってみた
   【要追記：使用写真と本音（良かった点・気になった点）をここに書く】
7. ## 価格・購入のポイント: 価格データがあれば、ショップ間の価格差に触れる
8. ## SNS告知文の下書き: X用（100字以内）を2パターン

# 商品データ
{data}
"""


def generate_draft(item: dict) -> str:
    data = json.dumps({k: item.get(k) for k in [
        "itemName", "itemPrice", "reviewCount", "reviewAverage", "shopName",
        "catchcopy", "itemCaption", "trend", "price"
    ]}, ensure_ascii=False, indent=2)

    if DRY_RUN:
        return f"（DRY_RUN）下書き生成をスキップしました\n\n```json\n{data}\n```"

    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={
            "model": os.getenv("OPENAI_MODEL") or CONFIG["openai_model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_TEMPLATE.format(data=data)},
            ],
        },
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI エラー {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def write_draft(item: dict, genre_name: str, body: str, idx: int) -> Path:
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w]+", "_", search_keyword(item["itemName"]))[:30] or "item"
    path = DRAFT_DIR / f"{idx:02d}_{slug}.md"
    link = item["affiliateUrl"] or item["itemUrl"]
    header = (
        f"---\n"
        f"date: {TODAY}\n"
        f"genre: {genre_name}\n"
        f"trend: {item.get('trend', '')}\n"
        f"rank: {item['rank']}\n"
        f"price: {item['itemPrice']}\n"
        f"review: {item['reviewAverage']}（{item['reviewCount']}件）\n"
        f"link: {link}\n"
        f"status: 下書き（実体験の追記待ち）\n"
        f"---\n\n"
        f"# {item['itemName'][:80]}\n\n"
    )
    path.write_text(header + body + "\n", encoding="utf-8")
    return path


# ---------- メイン ----------

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    summary = [f"# {TODAY} 急上昇まとめ\n"]
    idx = 0
    seen = set()

    for genre in CONFIG["genres"]:
        gid, gname = str(genre["id"]), genre["name"]
        print(f"[{gname}] ランキング取得中…")
        today = fetch_ranking(gid)
        prev = load_previous(gid)
        (DATA_DIR / f"{TODAY}_g{gid}.json").write_text(
            json.dumps(today, ensure_ascii=False, indent=1), encoding="utf-8")

        if not prev:
            print(f"[{gname}] 前日データなし。今日のデータを保存のみ（明日から比較できます）")
            summary.append(f"## {gname}\n- 前日データなし（初回）\n")
            continue

        rising = find_rising(today, prev)
        rising = [i for i in rising
                  if i["reviewCount"] >= CONFIG["min_review_count"] and i["itemCode"] not in seen]
        picks = rising[: CONFIG["drafts_per_genre"]]
        summary.append(f"## {gname}（急上昇 {len(rising)}件中 上位{len(picks)}件）")

        for it in picks:
            seen.add(it["itemCode"])
            try:
                it["price"] = price_spread(it)
            except Exception as e:
                print(f"  価格差取得スキップ: {e}")
                it["price"] = None
            idx += 1
            path = write_draft(it, gname, generate_draft(it), idx)
            p = it["price"]
            price_note = f" / 価格帯 {p['min']:,}〜{p['max']:,}円" if p else ""
            summary.append(
                f"- {it['trend']} {it['itemName'][:40]}… "
                f"{it['itemPrice']:,}円 ★{it['reviewAverage']}（{it['reviewCount']}件）"
                f"{price_note} → `{path.relative_to(ROOT)}`")
        summary.append("")

    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    (DRAFT_DIR / "_summary.md").write_text("\n".join(summary), encoding="utf-8")

    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("\n".join(summary))
    print("\n".join(summary))


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        sys.exit(f"環境変数 {e} が未設定です（GitHub の Secrets を確認してください）")

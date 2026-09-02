#!/usr/bin/env python3
"""
抓取線上估價單，解析全部品項與價格，累積成價格歷史。

輸出：
  docs/data/items.json      品項索引（每筆一行，方便 git diff）
  docs/data/hist/XX.json    價格歷史，依 id 前兩碼分成 256 片
  docs/data/meta.json       更新時間與統計
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from facets import build as build_facets
import identity as identity_module
from identity import bare, item_id, redact, relink

# 抓取目標。放在環境變數裡，公開的程式碼就不必寫死是哪一家。
# 本機測試可以先 export SOURCE_URL=...，GitHub 上設成 repository secret。
URL = os.environ.get("SOURCE_URL", "")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
HIST = DATA / "hist"
TZ = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

# 這些符號開頭的 option 是說明、活動、贈品，不是可買的品項
SKIP_PREFIX = ("❤", "↪", "※", "◎", "★", "◆", "▲", "◇", "□", "●")
PRICE_RE = re.compile(r"\$\s*([\d,]+)")


def fetch_html() -> str:
    """抓頁面並處理 Big5 編碼。失敗時重試。"""
    # 檢查放在這裡而不是模組層級，這樣 rollback.py 之類的工具可以
    # 直接沿用本檔的讀寫函式，不必為了 import 而假造一個網址。
    if not URL:
        raise SystemExit(
            "沒有設定 SOURCE_URL。GitHub 上請到 Settings → Secrets and variables\n"
            "→ Actions → New repository secret，名稱填 SOURCE_URL，值填估價單網址。"
        )
    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=90)
            r.raise_for_status()
            if len(r.content) < 200_000:
                raise RuntimeError(f"頁面異常小（{len(r.content)} bytes），可能被擋")
            for enc in ("cp950", "big5-hkscs", "big5"):
                try:
                    return r.content.decode(enc)
                except UnicodeDecodeError:
                    continue
            return r.content.decode("cp950", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"第 {attempt} 次抓取失敗：{e}", file=sys.stderr)
            time.sleep(10 * attempt)
    raise SystemExit(f"連續三次抓取失敗，放棄。最後錯誤：{last_err}")


def tidy(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def category_of(select) -> str:
    """類別名在同一列 <tr> 中、select 之前的儲存格，例如「處理器 CPU」。"""
    tr = select.find_parent("tr")
    if tr is None:
        return "未分類"
    label = ""
    for td in tr.find_all("td"):
        if td.find("select") is not None:
            break
        t = tidy(td.get_text(" ", strip=True))
        if t and not t.isdigit():
            label = t
    return redact(label) or "未分類"


def clean_name(name: str) -> str:
    """去掉品名尾巴的促銷字樣，讓同一支商品在不同天算成同一筆。"""
    name = re.sub(r"↓\s*(酷幣|任搭)\s*[\d,]+\s*↓", "", name)
    name = re.sub(r"[▼↘↗]\s*(下殺|下砍|特價|降價)到[^$]*$", "", name)
    name = re.sub(r"\s*\*\s*(限時|特價|出清|優惠|活動|促銷|贈品)[^$]*$", "", name)
    name = re.sub(r"[▼↘↗◆★]+", " ", name)
    return tidy(name).strip(" -–—,、")


# 酷幣是結帳當下直接折抵的金額，標在價格後面，例如「, $7990 ◆ ★ 熱賣 ↓酷幣300↓」。
# 另一種「↓任搭300↓」要跟別的商品一起買才算，不是立即折扣，所以不收。
COIN_RE = re.compile(r"↓\s*酷幣\s*([\d,]+)\s*↓")


def parse_option(text: str):
    """把一行 option 文字拆成 (品名, 價格, 酷幣)。不是品項就回 None。"""
    text = tidy(text)
    if not text or text[0] in SKIP_PREFIX:
        return None
    prices = PRICE_RE.findall(text)
    if not prices:
        return None
    # 特價寫成「$40888↘$39990」，最後一個才是現在要付的錢
    price = int(prices[-1].replace(",", ""))
    if price < 10:  # $1 的登錄禮、活動項目
        return None
    m = COIN_RE.search(text)
    coin = int(m.group(1).replace(",", "")) if m else 0
    if coin >= price:      # 折抵不可能大於等於售價，這種八成是解析錯了
        coin = 0
    idx = text.rfind(", $")
    name = clean_name(text[:idx] if idx > 0 else PRICE_RE.sub("", text))
    if len(name) < 4:
        return None
    return name, price, coin


def parse(html: str):
    soup = BeautifulSoup(html, "lxml")
    rows, seen = [], set()
    cat_order = []          # 分類在頁面上的先後，供網頁的分類選單照原順序排
    for sel in soup.find_all("select"):
        options = sel.find_all("option")
        # 數量選單（1~10）沒有 optgroup 且全是數字，跳過
        if not sel.find("optgroup") and all(
            tidy(o.get_text()).isdigit() or not tidy(o.get_text()) for o in options
        ):
            continue
        category = category_of(sel)
        got = False
        for opt in options:
            parsed = parse_option(opt.get_text())
            if not parsed:
                continue
            name, price = parsed[0], parsed[1]
            coin = parsed[2]
            group = ""
            parent = opt.find_parent("optgroup")
            if parent is not None:
                group = redact(tidy(parent.get("label", "")))
            name = redact(name)
            iid = item_id(name)
            if iid in seen:  # 同一頁重複列出時只取第一次
                continue
            seen.add(iid)
            # o = 這筆在頁面上的第幾個。網頁的預設排序照它走，才會跟來源站看到的一樣。
            rows.append({"id": iid, "c": category, "g": group, "n": name,
                         "p": price, "k": coin, "o": len(rows)})
            got = True
        # 同一個分類可能拆成好幾個 select，只記第一次出現的位置；
        # 整個 select 都沒解析出品項的話不算數
        if got and category not in cat_order:
            cat_order.append(category)
    return rows, cat_order


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"{path} 讀取失敗，視為空白重建", file=sys.stderr)
        return default


def dump_lines(path: Path, header: dict, key: str, records) -> None:
    """每筆資料自成一行，git 只會記錄真正變動的那幾行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [json.dumps(header, ensure_ascii=False)[:-1]]
    parts.append(f', "{key}": [')
    body = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in records]
    parts.append(",\n".join(body))
    parts.append("]}\n")
    path.write_text("".join(parts), encoding="utf-8")


def main() -> None:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

    if identity_module._STORE is None:
        print("警告：沒有設定 REDACT_WORDS，來源站名稱不會被遮蔽", file=sys.stderr)
    rows, cat_order = parse(fetch_html())
    if len(rows) < 1000:
        raise SystemExit(f"只解析到 {len(rows)} 筆，明顯不對，中止以免污染歷史資料")
    print(f"解析到 {len(rows)} 筆品項，{len(cat_order)} 個分類")

    old = load_json(DATA / "items.json", {"items": []})
    known = {i["id"]: i for i in old.get("items", [])}

    # 品名（含曾用名）對應到編號。改名只需認一次，之後就靠這張表穩定對上。
    name_index = {}
    for iid, item in known.items():
        name_index[bare(item["n"])] = iid
        for alias in item.get("a", []):
            name_index[bare(alias)] = iid

    # 先用品名索引把這次抓到的資料對回既有品項
    resolved, first_seen = [], {}
    for row in rows:
        iid = name_index.get(bare(row["n"]))
        if iid is None:
            first_seen[row["id"]] = (row["n"], row["p"])
        row["_id"] = iid
        resolved.append(row)

    # 這次沒出現、而且之前還在架上的品項
    hit = {r["_id"] for r in resolved if r["_id"]}
    missing = {
        iid: (item["n"], item["p"])
        for iid, item in known.items()
        if iid not in hit and not item.get("x")
    }

    # 把「消失」與「新出現」配對，找出其實只是改了名的
    renamed = relink(missing, first_seen)
    rename_map = {new_id: old_id for old_id, new_id, _ in renamed}
    for old_id, new_id, ratio in renamed:
        print(f"  改名接續（{ratio:.2f}）：{known[old_id]['n'][:40]}"
              f" → {first_seen[new_id][0][:40]}")

    shards, touched = {}, set()

    def shard_of(item_id_: str):
        key = item_id_[:2]
        if key not in shards:
            shards[key] = load_json(HIST / f"{key}.json", {})
        return shards[key]

    added = changed = 0
    # 改名有兩種：配對接續換 id，以及品名微調但 id 不變。兩種都會寫入曾用名，
    # 都得記下來，否則之後要「當作沒抓過這次」時還原不回去。
    rename_log = []
    for row in resolved:
        iid = row["_id"] or rename_map.get(row["id"]) or row["id"]
        del row["_id"]
        prev = known.get(iid)
        row["id"] = iid
        hist = shard_of(iid)
        series = hist.setdefault(iid, [])

        if prev is None:
            added += 1
            row.update(pv=None, pd=today, f=today, l=today, x=0, a=[])
            series.append([today, row["p"]])
            touched.add(iid[:2])
        else:
            aliases = list(prev.get("a", []))
            if bare(prev["n"]) != bare(row["n"]) and prev["n"] not in aliases:
                aliases.append(prev["n"])      # 品名換了，把舊的留成曾用名
                rename_log.append([iid, prev["n"], row["n"]])
            row.update(
                pv=prev.get("pv"),
                pd=prev.get("pd", today),
                f=prev.get("f", today),
                l=today,
                x=0,
                a=aliases[-5:],
            )
            if prev["p"] != row["p"]:
                changed += 1
                row["pv"] = prev["p"]
                row["pd"] = today
                series.append([today, row["p"]])
                touched.add(iid[:2])

        # 歷史最低／最高價，讓列表不必載入歷史就能標出「目前是低點」
        seen_prices = [p for _, p in series]
        row["lo"] = min(seen_prices)
        row["hi"] = max(seen_prices)
        row["np"] = len(series)
        known[iid] = row

    # 這次沒出現的品項標記為已下架，最後出現日期保持不動
    live = {r["id"] for r in resolved}
    gone = 0
    for iid, item in known.items():
        if iid not in live and not item.get("x"):
            item["x"] = 1
            gone += 1

    items = sorted(known.values(), key=lambda i: (i["c"], i["g"], i["n"]))
    dump_lines(
        DATA / "items.json",
        {"schema": 1, "updated": now, "count": len(items), "cats": cat_order},
        "items",
        items,
    )

    for key in sorted(touched):
        path = HIST / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = shards[key]
        body = ",\n".join(
            f'"{k}":{json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
            for k, v in sorted(data.items())
        )
        path.write_text("{" + body + "}\n", encoding="utf-8")

    build_facets(items, DATA / "facets.json")

    # 抓取紀錄。要「當作沒抓過某一次」時，價格點自己帶日期刪得掉，
    # 當天新增的品項刪完點之後歷史會變空、認得出來，這兩項都不必記。
    # 下架標記也不必記：下次抓取會重新判定，還在架上的會自己改回來。
    # 只有改名接續非記不可——它把品名換掉了，沒有日期，
    # 而且下次抓取會用新品名再對回同一筆，錯誤的接續會一直黏著解不開。
    runs = load_json(DATA / "runs.json", {"schema": 1, "runs": []})
    runs["runs"] = [r for r in runs["runs"] if r["date"] != today]
    runs["runs"].append({
        "date": today, "at": now, "parsed": len(rows),
        "added": added, "changed": changed,
        "gone": gone, "renamed": rename_log,
    })
    runs["runs"] = runs["runs"][-90:]      # 只留最近 90 次，免得檔案無限長大
    (DATA / "runs.json").write_text(
        json.dumps(runs, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    (DATA / "meta.json").write_text(
        json.dumps(
            {
                "updated": now,
                "total": len(items),
                "onsale": len(rows),
                "added": added,
                "changed": changed,
                "renamed": len(renamed),
                "gone_today": gone,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"新增 {added} 筆、變價 {changed} 筆、改名接續 {len(renamed)} 筆、下架 {gone} 筆")


if __name__ == "__main__":
    main()

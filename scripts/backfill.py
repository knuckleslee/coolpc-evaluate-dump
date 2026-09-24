#!/usr/bin/env python3
"""
把手動存下的估價單快照 repo 匯入成價格資料。

每個 commit 對應一天的價格，匯入後即有該 repo 涵蓋期間的完整歷史，
不需從執行當日開始累積。

用法（在本專案根目錄）：
    python scripts/backfill.py /path/to/dumps-repo

dump 檔前後有三種格式，皆支援。
"""
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from facets import build as build_facets
from identity import (LOOKBACK_DAYS, RECENT_DAYS, bare, item_id,  # noqa: E402
                      redact, relink_staged)

DATA = ROOT / "docs" / "data"
HIST = DATA / "hist"

SKIP = ("❤", "↪", "※", "◎", "★", "◆", "▲", "◇", "□", "●", "　")
PRICE = re.compile(r"\$\s*([\d,]+)")


def clean(n: str) -> str:
    n = re.sub(r"[▼↘↗]\s*(下殺|下砍|特價|降價)到[^$]*$", "", n)
    n = re.sub(r"\s*\*\s*(限時|特價|出清|優惠|活動|促銷)[^$]*$", "", n)
    n = re.sub(r"[▼↘↗◆★]+", " ", n)
    n = re.sub(r"\s*(熱賣|限量|現貨|補貨中)\s*$", "", n)
    return re.sub(r"\s+", " ", n).strip(" -–—,、")


def parse_dump(text: str) -> dict:
    """
    三種 dump 格式通用。回傳 {品名: (價格, 主分類, 次類)}。

    格式差異：2025-05 之前是整頁表格貼上，有主分類沒次類；
    之後改成逐個 select 匯出，有次類（optgroup）沒主分類。
    兩邊都盡量抓，抓不到的留空，等第一次正式抓取時補齊。
    """
    out = {}
    cat = grp = ""
    for raw in text.split("\n"):
        ln = raw.replace("\xa0", " ").rstrip()
        if not ln.strip():
            continue

        m = re.match(r"^(\d+)\t([^\t]+)", ln)      # 舊格式的主分類行
        if m and "$" not in ln:
            cat = re.sub(r"\s+", " ", m.group(2)).strip()
            grp = ""
            continue
        m = re.match(r"^\s*\u25ce\s*(.+)", ln)     # 新格式的次類行
        if m:
            grp = re.sub(r"\s+", " ", m.group(1)).strip()
            continue
        if ln.startswith("\u25bc Select"):
            grp = ""
            continue

        body = re.sub(r"^\s*-\s+", "", ln).strip()
        if not body or body[0] in SKIP:
            continue
        if body.startswith("共有商品") or "品　名" in body or body.startswith("已【"):
            continue
        prices = PRICE.findall(body)
        if not prices:
            continue
        p = int(prices[-1].replace(",", ""))
        if p < 10:
            continue
        i = body.rfind(", $")
        name = clean(body[:i] if i > 0 else PRICE.sub("", body))
        if len(name) < 5:
            continue
        out.setdefault(redact(name), (p, redact(cat), redact(grp)))
    return out


def git(repo: Path, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法：python scripts/backfill.py <舊 repo 的路徑>")
    repo = Path(sys.argv[1]).expanduser().resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"{repo} 不是 git 儲存庫")
    if (DATA / "items.json").exists():
        raise SystemExit(
            "docs/data/items.json 已經存在。回填只能在乾淨的狀態下做一次，"
            "請先確認是否真的要重來，要的話手動刪掉 docs/data 再執行。"
        )

    log = git(repo, "log", "--reverse", "--format=%h\t%ad", "--date=short",
              "--", "evaluate.dump").strip()
    if not log:
        raise SystemExit("找不到 evaluate.dump 的歷史")
    versions = [l.split("\t") for l in log.split("\n")]
    print(f"找到 {len(versions)} 個版本：{versions[0][1]} ~ {versions[-1][1]}")

    known, hist, name_index = {}, {}, {}
    n_ren = n_skip = 0
    last_date = None

    for n, (sha, date_) in enumerate(versions, 1):
        snap = parse_dump(git(repo, "show", f"{sha}:evaluate.dump"))
        if len(snap) < 3000:
            n_skip += 1
            continue
        if date_ == last_date:      # 同一天有多個 commit，只取第一個
            continue
        last_date = date_

        rows = {}
        for name, (price, cat, grp) in snap.items():
            rows[item_id(name)] = (name, price, cat, grp)

        matched, fresh, meta = {}, {}, {}
        for iid, (name, price, cat, grp) in rows.items():
            meta[bare(name)] = (cat, grp)
            mapped = name_index.get(bare(name))
            if mapped:
                matched[mapped] = (name, price)
            else:
                fresh[iid] = (name, price)

        # 回頭找還沒接回的下架品項，不只看上一個版本
        today = date.fromisoformat(date_)
        recent, older = {}, {}
        for i, it in known.items():
            if i in matched:
                continue
            age = (today - date.fromisoformat(it["l"])).days
            if age > LOOKBACK_DAYS:
                continue
            (recent if age <= RECENT_DAYS else older)[i] = (it["n"], it["p"])
        for old_id, new_id, _ in relink_staged(recent, older, fresh):
            matched[old_id] = fresh.pop(new_id)
            n_ren += 1

        for iid, (name, price) in list(fresh.items()):
            matched[iid] = (name, price)

        for iid, (name, price) in matched.items():
            prev = known.get(iid)
            series = hist.setdefault(iid, [])
            cat, grp = meta.get(bare(name), ("", ""))
            if prev is None:
                known[iid] = {"id": iid, "c": cat, "g": grp, "n": name, "p": price,
                              "pv": None, "pd": date_, "f": date_, "l": date_,
                              "x": 0, "a": []}
                series.append([date_, price])
            else:
                aliases = list(prev.get("a", []))
                if bare(prev["n"]) != bare(name) and prev["n"] not in aliases:
                    aliases.append(prev["n"])
                prev.update(n=name, l=date_, x=0, a=aliases[-5:])
                if cat:
                    prev["c"] = cat
                if grp:
                    prev["g"] = grp
                if prev["p"] != price:
                    prev["pv"] = prev["p"]
                    prev["p"] = price
                    prev["pd"] = date_
                    series.append([date_, price])
            name_index[bare(name)] = iid

        for iid, it in known.items():
            if iid not in matched and not it.get("x"):
                it["x"] = 1

        if n % 25 == 0 or n == len(versions):
            print(f"  {n:>3}/{len(versions)}  {date_}  累積品項 {len(known)}")

    # 新版 dump 只有次類沒有主分類，舊版相反。用次類把主分類補回去：
    # 同一個次類（例如「27吋(2560*1440)(16:9)」）長期都掛在同一個主分類底下。
    from collections import Counter
    vote = {}
    for it in known.values():
        if it["g"] and it["c"]:
            vote.setdefault(it["g"], Counter())[it["c"]] += 1
    filled = 0
    for it in known.values():
        if not it["c"] and it["g"] in vote:
            it["c"] = vote[it["g"]].most_common(1)[0][0]
            filled += 1
    print(f"  用次類補回主分類 {filled} 筆")

    for iid, it in known.items():
        ps = [p for _, p in hist[iid]]
        it["lo"], it["hi"], it["np"] = min(ps), max(ps), len(ps)

    DATA.mkdir(parents=True, exist_ok=True)
    HIST.mkdir(parents=True, exist_ok=True)
    items = sorted(known.values(), key=lambda i: i["n"])
    header = json.dumps(
        {"schema": 1, "updated": f"{last_date} (歷史回填)", "count": len(items)},
        ensure_ascii=False)[:-1]
    body = ",\n".join(json.dumps(i, ensure_ascii=False, separators=(",", ":"))
                      for i in items)
    (DATA / "items.json").write_text(header + ', "items": [' + body + "]}\n",
                                     encoding="utf-8")

    shards = {}
    for iid, series in hist.items():
        shards.setdefault(iid[:2], {})[iid] = series
    for key, data in shards.items():
        line = ",\n".join(
            f'"{k}":{json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
            for k, v in sorted(data.items()))
        (HIST / f"{key}.json").write_text("{" + line + "}\n", encoding="utf-8")

    build_facets(items, DATA / "facets.json")

    alive = sum(1 for i in items if not i["x"])
    multi = sum(1 for i in items if i["np"] > 1)
    print(f"\n完成：品項 {len(items)}（在架 {alive}）")
    print(f"      有價格變動紀錄的 {multi} 筆，改名接續 {n_ren} 次，"
          f"跳過損毀版本 {n_skip} 個")


if __name__ == "__main__":
    main()

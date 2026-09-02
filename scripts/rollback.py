#!/usr/bin/env python3
"""把某一次抓取當作沒發生過。

    python scripts/rollback.py 2026-09-04
    python scripts/rollback.py 2026-09-04 --dry-run     # 只看會動到什麼，不寫檔

一次抓取會在三個地方留下痕跡，處理方式各不相同：

  價格點        自己帶日期，直接刪。
  當天新增的品項  歷史上只有那一個點，刪完點之後歷史變空，就認得出來。
  改名接續      沒有日期，靠 runs.json 的紀錄還原。沒有紀錄就還原不了，
                會印出警告——這種情況下那次的改名會留著。

下架標記刻意不處理。x=1 的意思是「上次抓取沒看到它」，下次抓取會重新
判定所有在架品項，還在架上的自己會改回 x=0，真的沒了的本來就該是 1。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape import DATA, HIST, load_json, dump_lines
from facets import build as build_facets


def load_shards():
    """把 hist/ 底下所有分片讀進來。分片是依 id 前兩碼切的，共 256 個。"""
    shards = {}
    for path in sorted(HIST.glob("*.json")):
        shards[path.stem] = load_json(path, {})
    return shards


def rollback(date: str, dry_run: bool = False) -> int:
    items_doc = load_json(DATA / "items.json", None)
    if items_doc is None:
        raise SystemExit("找不到 docs/data/items.json")
    known = {i["id"]: i for i in items_doc["items"]}

    runs_doc = load_json(DATA / "runs.json", {"schema": 1, "runs": []})
    rec = next((r for r in runs_doc["runs"] if r["date"] == date), None)
    prev = None
    for r in runs_doc["runs"]:
        if r["date"] < date and (prev is None or r["date"] > prev["date"]):
            prev = r

    shards = load_shards()

    # 1. 刪掉當天的價格點
    dropped_pts = 0
    touched = set()
    for key, shard in shards.items():
        for iid, series in list(shard.items()):
            kept = [p for p in series if p[0] != date]
            if len(kept) != len(series):
                dropped_pts += len(series) - len(kept)
                shard[iid] = kept
                touched.add(key)

    # 2. 歷史變空的品項就是當天才出現的，整筆刪掉
    removed = []
    for key, shard in shards.items():
        for iid, series in list(shard.items()):
            if series:
                continue
            del shard[iid]
            touched.add(key)
            if iid in known:
                removed.append(known.pop(iid))

    # 3. 還原當天的改名。反向處理，同一筆若被改過多次才不會錯位。
    renamed_back, rename_miss = 0, 0
    if rec:
        for iid, old_name, new_name in reversed(rec.get("renamed", [])):
            it = known.get(iid)
            if it is None or it["n"] != new_name:
                rename_miss += 1     # 之後又被改過，這裡不硬改回去
                continue
            it["n"] = old_name
            if old_name in it.get("a", []):
                it["a"].remove(old_name)
            renamed_back += 1

    # 4. 依剩下的歷史重算每一筆的現況
    for iid, it in known.items():
        series = shards.get(iid[:2], {}).get(iid)
        if not series:
            continue                 # 這次抓取沒碰到、也沒有歷史的，維持原狀
        prices = [p for _, p in series]
        it["p"] = prices[-1]
        it["pv"] = prices[-2] if len(prices) > 1 else None
        it["pd"] = series[-1][0]
        it["f"] = series[0][0]
        it["lo"], it["hi"], it["np"] = min(prices), max(prices), len(prices)
        if it.get("l") == date:
            # 最後出現日退回上一次抓取；沒有紀錄就退回最後一次變價日（近似值，
            # 下次抓取會蓋掉）
            it["l"] = prev["date"] if prev else series[-1][0]

    print(f"刪除價格點 {dropped_pts} 個")
    print(f"刪除品項 {len(removed)} 筆（歷史只剩那一天）")
    if rec:
        print(f"還原改名 {renamed_back} 筆" +
              (f"，{rename_miss} 筆之後又被改過、保持現狀" if rename_miss else ""))
    else:
        print("runs.json 裡沒有這一天的紀錄，改名接續無法還原。")
        print("  下架標記不受影響，下次抓取會自己修正。")
    for it in removed[:10]:
        print(f"  刪除：{it['n'][:50]}")
    if len(removed) > 10:
        print(f"  …另外 {len(removed) - 10} 筆")

    if dry_run:
        print("\n--dry-run，沒有寫入任何檔案。")
        return 0
    if dropped_pts == 0 and not removed and not rec:
        raise SystemExit(f"{date} 沒有留下任何可刪的痕跡，什麼都沒做。")

    items = sorted(known.values(), key=lambda i: (i["c"], i["g"], i["n"]))
    header = {"schema": 1,
              "updated": prev["at"] if prev else items_doc.get("updated", ""),
              "count": len(items)}
    if items_doc.get("cats"):
        header["cats"] = items_doc["cats"]
    dump_lines(DATA / "items.json", header, "items", items)

    for key in sorted(touched):
        body = ",\n".join(
            f'"{k}":{json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
            for k, v in sorted(shards[key].items()))
        (HIST / f"{key}.json").write_text("{" + body + "}\n", encoding="utf-8")

    build_facets(items, DATA / "facets.json")

    runs_doc["runs"] = [r for r in runs_doc["runs"] if r["date"] != date]
    (DATA / "runs.json").write_text(
        json.dumps(runs_doc, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8")

    print(f"\n完成，剩下 {len(items)} 筆品項。")
    return len(removed)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        raise SystemExit("用法：python scripts/rollback.py YYYY-MM-DD [--dry-run]")
    rollback(args[0], "--dry-run" in sys.argv)

#!/usr/bin/env python3
"""
自動探勘每個分類的篩選項。

篩選項分兩層產生：

  第一層（docs/index.html 的 attrsOf）
      綁品名樣式而非分類。品名裡的 `180Hz` 一律算更新率、
      `〈2H1P1C/…〉` 一律算連接介面。新增分類時自動適用，
      組名由人指定，畫面上顯示「面板」「更新率」等可讀名稱。

  第二層（本程式）
      每次抓取後重新探勘，找出第一層未涵蓋、在該分類具鑑別力的詞。
      例如機殼的「支援背插」、電源的「全日系」「ATX3.1」、
      主機板的「LAN 2.5Gb+無線」。

手寫清單會隨來源站改版失效，因此第二層不預先定義任何分類的欄位。

演算法：切詞 → 正規化 → 數值另外歸類 → 依「互斥」與「出現位置」分組。
同一規格維度的不同取值不會出現在同一個品名裡（螢幕不會同時是 IPS 和 VA），
且落在品名的相近位置，兩個訊號合併即可辨識維度。

分組不保證精確，可能把不相干的詞歸為一組，此時排版不理想但勾選行為仍正確。
"""

import json
import re
from collections import Counter, defaultdict

SPLIT = re.compile(r"[/／、,，|｜]|[〈〉<>【】\[\]()（）]")
NUM = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(Hz|W|吋|核|緒|年|mm|cm|ms|dpi|rpm|Gb|TB|GB|MB)$", re.I
)

# 同義寫法。來源站同一個意思會有好幾種寫法，不合併的話篩選項會重複列出。
SYNONYMS = [
    (r"^藍芽$", "藍牙"),
    (r"^註?冊?([一二三四五六七八九十兩])年保?$", r"\1年保"),
    (r"^([一二三四五六七八九十兩])年$", r"\1年保"),
    (r"^保固([一二三四五六七八九十])年$", r"\1年保"),
]

# 促銷字眼不是規格，不該變成篩選項。
STOPWORD = re.compile(
    r"(優惠|特價|限時|下殺|活動|贈|送|折|第二件|加購|買就|滿額|登錄|抽獎"
    r"|現貨|補貨|熱賣|限量|預購|客訂|來電|洽詢|同捆|組合價)"
)

# 第一層已經處理掉的東西，第二層不要重複列。
COVERED = re.compile(
    r"^("
    r"(IPS|VA|OLED|QD-?OLED|TN|Mini\s?LED)(曲面|平面)?"   # VA曲面 也算面板，第一層已處理
    r"|\d+(?:\.\d+)?\s*(Hz|TB|GB)"
    r"|\d{3,4}R"                                        # 曲率
    r"|\d*[HPCAD](\d*[HPCAD])+"
    r"|含喇叭|內建喇叭|可升降旋轉|可昇降|無亮點|零亮點|曲面|平面|觸控"
    r"|有線|無線|雙模|三模|藍[牙芽]|A?[-.\s]?RGB|熱?插拔軸?"
    r"|HDR\s?\d*|HDR10|RJ-?45|[一二三四五六七八九十兩]年保?|\d+\s*年保?"
    r"|.*麥克風|智慧螢幕.*|Thunderbolt\s?\d*|TB[45]|.*切換|.*雙模式"
    r"|黑色?|白色?|銀色?|灰色?|紅色?|藍色?|粉色?|綠色?|珍珠白|雪白|冰藍"
    r")$",
    re.I,
)

MIN_RATIO = 0.04      # 出現率低於這個就太零碎，做成篩選項也沒人點
MAX_RATIO = 0.70      # 高於這個代表幾乎人人都有，篩了也篩不掉什麼
MAX_GROUPS = 8
MAX_VALUES = 12


def normalize(token: str) -> str:
    t = token.strip(" 　.。:：*+~-")
    for pattern, repl in SYNONYMS:
        new = re.sub(pattern, repl, t)
        if new != t:
            return new
    return t


def segments(name: str):
    """把品名切成片段，同時記住每段的位置。"""
    out = []
    for idx, seg in enumerate(SPLIT.split(name)):
        seg = normalize(seg)
        if 1 < len(seg) <= 14:
            out.append((idx, seg))
    return out


# 多出來的字如果只是把話講完整（支援背插 vs 背插、全模組 vs 全模），兩者是同一件事；
# 如果是在型號前面加修飾（M-ATX vs ATX、Mini ITX vs ITX），那是不同的規格值。
FILLER = re.compile(r"^(支援|內建|附|含|可|有|具)$|^(組|款|版|型|保|裝)$")


def same_thing(long: str, short: str) -> bool:
    a, b = re.sub(r"\s+", "", long), re.sub(r"\s+", "", short)
    if len(a) < 2 or len(b) < 2 or a == b:
        return False
    if b not in a:
        return False
    extra = a.replace(b, "", 1)
    return len(extra) <= 2 and bool(FILLER.match(extra))


def merge_variants(cand, count, where):
    """把同一件事的不同寫法併起來，保留出現次數較多的那個當代表。"""
    order = sorted(cand, key=lambda t: -count[t])
    dropped = set()
    for i, long in enumerate(order):
        if long in dropped:
            continue
        for short in order[i + 1:]:
            if short in dropped:
                continue
            if not (same_thing(long, short) or same_thing(short, long)):
                continue
            if len(where[long] & where[short]) > 0.15 * min(count[long], count[short]):
                continue
            where[long] |= where[short]
            count[long] = len(where[long])
            dropped.add(short)
    return [t for t in cand if t not in dropped], {}


# 品牌已經是第一層的一組，自動探勘不該再列一次
_BRAND_HEAD = re.compile(r"^[【\[（(][^】\]）)]{1,6}[】\]）)]\s*|^[\s★◆▼※•]+")


def brands_in(pool):
    out = set()
    for it in pool:
        head = _BRAND_HEAD.sub("", it["n"]).split(" ")[0]
        zh = re.match(r"^[\u4e00-\u9fff]{2,4}", head)
        if zh:
            out.add(zh.group(0))
        en = re.match(r"^[A-Za-z][A-Za-z\-.]{1,}", head)
        if en:
            out.add(en.group(0).upper())
        out.add(head)
    return out


def mine(pool):
    """回傳這個分類自動找出來的篩選組：[(組內的值, 這組的總命中數)]。"""
    n = len(pool)
    if n < 25:
        return []

    count, where, pos = Counter(), defaultdict(set), defaultdict(list)
    for idx, item in enumerate(pool):
        for k, token in segments(item["n"]):
            if idx in where[token]:
                continue
            count[token] += 1
            where[token].add(idx)
            pos[token].append(k)
    avg_pos = {t: sum(v) / len(v) for t, v in pos.items()}

    brands = brands_in(pool)
    cand = [
        t for t, c in count.items()
        if MIN_RATIO * n <= c <= MAX_RATIO * n
        and not COVERED.match(t) and not STOPWORD.search(t)
        and t not in brands and t.upper() not in brands
    ]
    if not cand:
        return []
    cand, _ = merge_variants(cand, count, where)

    # 數值型自己成一組，用不著看互斥（144Hz 和 240Hz 本來就互斥，
    # 但 144Hz 和「HDMI 2.1」互斥純屬巧合，混在一起就亂了）
    by_unit = defaultdict(list)
    labels = []
    for t in cand:
        m = NUM.match(t)
        if m:
            by_unit[m.group(2).upper()].append(t)
        else:
            labels.append(t)

    groups = []
    for unit, vals in by_unit.items():
        if len(vals) >= 2:
            vals.sort(key=lambda x: float(NUM.match(x).group(1)))
            groups.append(vals[:MAX_VALUES])

    labels.sort(key=lambda t: -count[t])
    used = set()
    for t in labels:
        if t in used:
            continue
        group = [t]
        used.add(t)
        for u in labels:
            if u in used or abs(avg_pos[t] - avg_pos[u]) > 1.2:
                continue
            if any(len(where[g] & where[u]) > 0.05 * min(count[g], count[u])
                   for g in group):
                continue
            group.append(u)
            used.add(u)
        if len(group) >= 2:
            groups.append(sorted(group, key=lambda x: -count[x])[:MAX_VALUES])

    groups.sort(key=lambda g: -sum(count[t] for t in g))
    return groups[:MAX_GROUPS]


def build(items, path):
    """對每個主分類跑一次探勘，結果寫成 facets.json 給網頁讀。"""
    alive = [i for i in items if not i.get("x")]
    by_cat = defaultdict(list)
    for it in alive:
        if it.get("c"):
            by_cat[it["c"]].append(it)

    out = {}
    for cat, pool in by_cat.items():
        groups = mine(pool)
        if groups:
            out[cat] = groups

    path.parent.mkdir(parents=True, exist_ok=True)
    body = ",\n".join(
        f'{json.dumps(cat, ensure_ascii=False)}:'
        f'{json.dumps(groups, ensure_ascii=False)}'
        for cat, groups in sorted(out.items())
    )
    path.write_text("{" + body + "}\n", encoding="utf-8")
    total = sum(len(g) for g in out.values())
    print(f"  自動探勘：{len(out)} 個分類，共 {total} 組篩選項")
    return out

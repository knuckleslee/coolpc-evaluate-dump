#!/usr/bin/env python3
"""
品項識別規則。抓取與歷史回填共用，兩邊算出的編號才會一致。

來源站的品名不固定，同一支商品在不同日期可能寫法不同。直接以完整品名
作為識別碼會使走勢圖斷成多段——以 2023-12 至 2026-08 的 224 個版本實測，
「消失」的品項有一半以上實際上只是改了名。

識別分兩層：

第一層 item_id()
    抹除品名的空白、標點與大小寫差異後計算編號。
    涵蓋「WIN11 PRO → WIN11 Pro」「(白) → （白）」等純格式調整。

第二層 relink()
    處理描述文字被改寫的情況，例如：
        技嘉 RTX4090 GAMING OC 24G(...)任搭優惠到2/18
        技嘉 RTX4090 GAMING OC 24G(...)任搭優惠到2/29
    在同一批消失與新增的品項之間配對，接續歷史。

第二層的門檻設得嚴格，因為來源站大量存在僅差一字的不同商品：
    顏色不同    聯力 PC-O11 Dynamic 黑 ←→ 白
    容量不同    WD SN850X 1TB ←→ 2TB
    料號不同    ANV15-51-58L8 ←→ ANV15-51-54GD
漏接的代價是該品項被視為新品、走勢重新起算；誤接的代價是兩支不同商品
被合併，在圖上產生不存在的價格暴跌。因此偏向漏接。
"""

import hashlib
import os
import re
from urllib.parse import urlparse
from collections import defaultdict
from difflib import SequenceMatcher

_PUNCT = re.compile(r'[\s　【】\[\]()（）〈〉《》/、,.\-_+*:：!！?？"\'’｜|]')
_TOKEN = re.compile(r"[A-Za-z0-9]{4,}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]{4,}[A-Za-z0-9]")
_UNIT = re.compile(r"^\d+(?:\.\d+)?[A-Za-z]{1,3}$")
# 促銷尾巴。比對前移除，否則「送 XX 支撐架」這類活動說明會壓低相似度。
# 僅移除字串結尾、且前方有空白或星號者，以免誤傷「隨附Pulsar 無線接收器」等規格描述。
_PROMO = re.compile(r"[\s*＊]+(買就送|加贈|附贈|送|贈)[^,$]*$")
# 顏色字很常出現在不是顏色的詞裡：藍牙、白金牌、金士頓、銀幕、紅軸。
# 不排掉的話，「藍牙鍵盤」會被判成藍色，跨站比對時就配不上只寫「黑」的那一邊。
_COLOR = re.compile(
    r"(黑(?!標)|白(?!金|牌)|銀(?!幕|行)|灰(?!塵)|紅(?!軸|外)|藍(?!牙|芽|光|圖|寶)"
    r"|粉|綠(?!軸|能)|金(?!牌|士頓|屬|磚)|紫|橙|黃"
    r"|BLACK|WHITE|SILVER|GREY|GRAY|PINK|BLUE|RED)",
    re.I,
)
_CAP = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|G\b|T\b)", re.I)


# 來源站的店名會出現在次類與部分品名中（「【○○售出 2年快換】」、
# 「加贈○○【火】鍵盤」），解析時替換為「本店」。
# 必須在計算編號之前執行，否則遮蔽前後會產生兩組編號，導致歷史斷裂。
#
# 待遮蔽的詞不寫入原始碼：英文站名由 SOURCE_URL 的網域推得，
# 中文店名由 REDACT_WORDS 提供，逗號分隔。
def _redact_pattern():
    words = []
    host = urlparse(os.environ.get("SOURCE_URL", "")).hostname or ""
    for part in host.split("."):
        if part and part not in ("www", "com", "tw", "net", "org", "co"):
            words.append(part)
    words += [w.strip() for w in os.environ.get("REDACT_WORDS", "").split(",")]
    words = [w for w in words if len(w) >= 2]
    if not words:
        return None
    words.sort(key=len, reverse=True)
    return re.compile("|".join(re.escape(w) for w in words), re.I)


_STORE = _redact_pattern()


def redact(text: str) -> str:
    if not text or _STORE is None:
        return text
    t = _STORE.sub("本店", text)
    t = re.sub(r"(?:本店[\s　]*){2,}", "本店", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def bare(name: str) -> str:
    """抹掉空白與標點的品名骨架。"""
    return _PUNCT.sub("", name).upper()


def core(name: str) -> str:
    """去掉結尾的贈品與活動說明，留下商品本身。只用於比對，不影響存下來的品名。"""
    prev = None
    while prev != name:
        prev = name
        name = _PROMO.sub("", name).strip()
    return name


def item_id(name: str) -> str:
    return hashlib.sha1(bare(name).encode("utf-8")).hexdigest()[:10]


def tokens(s: str) -> frozenset:
    return frozenset(t.upper() for t in _TOKEN.findall(s))


def models(s: str) -> frozenset:
    """抽出型號與料號。長度至少 6、英數混合，排除 7400M、165Hz 這類規格單位。"""
    out = set()
    for m in _MODEL.finditer(s):
        t = m.group(0).upper().strip(".-")
        if len(t) < 6:
            continue
        if not (re.search(r"[A-Za-z]", t) and re.search(r"\d", t)):
            continue
        if _UNIT.match(t):
            continue
        out.add(t)
    return frozenset(out)


def colors(s: str) -> frozenset:
    return frozenset(m.group(0).upper() for m in _COLOR.finditer(s))


def caps(s: str) -> frozenset:
    return frozenset(f"{a}{b.upper().strip()}" for a, b in _CAP.findall(s))


def blocking_keys(name: str) -> set:
    """
    配對時用來縮小候選範圍的鍵。三種來源取聯集，任一命中就成為候選。

    只用英數 token 是不夠的。像「喬思伯 TF3-360 白色版…【WXZ】」這種
    以中文為主的品名，抽不出任何長度 4 以上的純英數字串，會整個漏掉。
    """
    keys = set(tokens(name))
    keys |= set(models(name))
    skeleton = bare(name)
    if len(skeleton) >= 6:
        keys.add("^" + skeleton[:8])      # 開頭骨架，改名多發生在尾巴
    return keys


def relink(gone: dict, fresh: dict, ratio_min=0.85, price_tol=0.40,
           require_model=False):
    """
    gone  {id: (品名, 價格)}   這次沒出現的品項
    fresh {id: (品名, 價格)}   這次才第一次看到的品項
    回傳  [(舊 id, 新 id, 相似度)]，代表這兩筆其實是同一支商品。
    """
    if not gone or not fresh:
        return []

    index = defaultdict(list)
    core_fresh = {fid: core(v[0]) for fid, v in fresh.items()}
    for fid, cname in core_fresh.items():
        for k in blocking_keys(cname):
            index[k].append(fid)

    pairs = []
    for gid, (gname_raw, gprice) in gone.items():
        gname = core(gname_raw)
        seen = set()
        for k in blocking_keys(gname):
            bucket = index.get(k)
            if bucket and len(bucket) <= 300:   # 太籠統的鍵直接略過
                seen.update(bucket)
        for fid in seen:
            fname, fprice = core_fresh[fid], fresh[fid][1]
            if colors(gname) != colors(fname):
                continue
            if caps(gname) != caps(fname):
                continue
            gm = models(gname)
            if gm != models(fname):
                continue
            if require_model and not gm:
                continue
            # 型號、顏色、容量三者吻合時，價格不作為判準。
            # 2025 至 2026 的記憶體漲價期間，同一支商品下架再上架可漲數倍
            # （創見 300S 32G 由 $158 變 $599），以價差過濾會排除掉正確的配對。
            tol = 3.0 if gm else price_tol
            if abs(fprice - gprice) / max(gprice, 1) > tol:
                continue
            r = SequenceMatcher(None, gname, fname).ratio()
            if r >= ratio_min:
                pairs.append((r, gid, fid))

    pairs.sort(reverse=True)
    used_g, used_f, out = set(), set(), []
    for r, gid, fid in pairs:
        if gid in used_g or fid in used_f:
            continue
        used_g.add(gid)
        used_f.add(fid)
        out.append((gid, fid, r))
    return out


# 下架後回頭配對的期限。實測改名後重新上架的間隔中位數為 28 天，
# 長者接近一年（顯卡、記憶體缺貨後回歸），故回溯一年。
LOOKBACK_DAYS = 365
RECENT_DAYS = 60


def relink_staged(recent: dict, older: dict, fresh: dict):
    """
    分兩批配對。剛消失者用一般門檻；下架已久者提高門檻，
    因為間隔越久，誤配到另一支相似新品的機率越高。
    """
    out = relink(recent, fresh, ratio_min=0.85, price_tol=0.40)
    taken = {fid for _, fid, _ in out}
    rest = {k: v for k, v in fresh.items() if k not in taken}
    # 間隔兩個月以上者，額外要求品名含可比對的型號料號；
    # 僅憑文字相似度配對一年前的品項，誤配率過高。
    out += relink(older, rest, ratio_min=0.90, price_tol=0.35, require_model=True)
    return out

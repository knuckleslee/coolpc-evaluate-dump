#!/usr/bin/env python3
"""把一次抓取的變化整理成清單，推送到 Google Chat。

    上架       新出現的品項，以及之前下架、這次又回來的
    下架       這次沒出現、之前還在架上的
    價格變化    跌價在前（跌最多的排最前面），漲價在後

Webhook 網址放在 repository secret「CHAT_WEBHOOK」。沒設定就只把訊息印在
Actions 的紀錄裡，不會報錯——所以先上線、之後再接 Chat 也可以。

一天 13 則訊息如果各自獨立會把 space 洗掉，所以同一天的訊息全部回在
同一個討論串裡（threadKey 用日期）。
"""

import os
import re

import requests

WEBHOOK = os.environ.get("CHAT_WEBHOOK", "")
PER_SECTION = 15        # 每一區最多列幾筆，超過的只報數量
NAME_LEN = 42           # 品名截斷長度，手機上一行大約就這麼寬


def site_url() -> str:
    """GitHub Pages 的網址，從 Actions 內建的環境變數推出來，不必寫死。"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    return f"https://{owner}.github.io/{name}/"


def short(name: str) -> str:
    # 來源站用全形大括號框型號，訊息裡拿掉比較好讀（跟網頁顯示同一套處理）
    n = re.sub(r"[｛｝{}]", " ", name)
    n = re.sub(r"\s{2,}", " ", n).strip()
    return n if len(n) <= NAME_LEN else n[:NAME_LEN - 1] + "…"


def nt(v: int) -> str:
    return f"${v:,}"


def section(title: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    out = [f"*{title}*"]
    out += lines[:PER_SECTION]
    if len(lines) > PER_SECTION:
        out.append(f"…另外 {len(lines) - PER_SECTION} 筆")
    return out + [""]


def build(at: str, added, gone, changed) -> str | None:
    """組出訊息文字。三區都沒東西就回 None，代表這次不必發。"""
    if not (added or gone or changed):
        return None

    up = []
    for n, p, info in added:
        # info：None＝全新品項；"back"＝重新上架價格沒變；數字＝重新上架且價格變了（原價）
        tag = ("" if info is None else "（重新上架）" if info == "back"
               else f"（重新上架，原 {nt(info)}）")
        up.append(f"• {short(n)}　{nt(p)}{tag}")

    down = [f"• {short(n)}　最後 {nt(p)}" for n, p in gone]

    # 跌價在前、跌最多的最前面；漲價在後、漲最多的最前面
    def pct(o, n):
        return (n - o) / o * 100 if o else 0
    drops = sorted((c for c in changed if c[2] < c[1]), key=lambda c: pct(c[1], c[2]))
    rises = sorted((c for c in changed if c[2] > c[1]), key=lambda c: -pct(c[1], c[2]))
    moves = [f"{'▼' if n < o else '▲'} {short(name)}　{nt(o)} → {nt(n)}（{pct(o, n):+.1f}%）"
             for name, o, n in drops + rises]

    head = [f"*價格更新 {at}*",
            f"上架 {len(added)}・下架 {len(gone)}・變價 {len(changed)}", ""]
    body = section("上架", up) + section("下架", down) + section("價格變化", moves)
    url = site_url()
    tail = [f"<{url}|看完整清單>"] if url else []
    return "\n".join(head + body + tail).rstrip()


def send(at: str, added, gone, changed) -> None:
    text = build(at, added, gone, changed)
    if text is None:
        print("這次沒有上架、下架或變價，不發通知")
        return
    if not WEBHOOK:
        print("沒有設定 CHAT_WEBHOOK，以下是會送出的內容：\n" + text)
        return
    # 同一天的訊息回在同一串。REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD：
    # 串還不存在時（當天第一則）自動開新串。
    sep = "&" if "?" in WEBHOOK else "?"
    url = WEBHOOK + sep + "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    body = {"text": text, "thread": {"threadKey": "price-" + at[:10]}}
    # 錯誤訊息一律不能帶到網址。公開 repo 的 Actions 紀錄任何人都看得到，
    # 而 requests 的連線錯誤會把網址拆成主機與路徑分開印出，GitHub 的自動遮罩
    # 只認得完整的 secret 字串，比對不到，key 與 token 就會原樣外流。
    # 所以只回報錯誤類型；from None 讓原本的例外（含網址）不會跟著印出來。
    try:
        r = requests.post(url, json=body, timeout=20)
    except requests.RequestException as e:
        raise RuntimeError(f"連不上 Chat（{type(e).__name__}）") from None
    if r.status_code >= 300:
        raise RuntimeError(f"Chat 回應 {r.status_code}：{scrub(r.text[:200])}")
    print(f"已推送到 Chat（上架 {len(added)}、下架 {len(gone)}、變價 {len(changed)}）")


def scrub(text: str) -> str:
    """保險：萬一回應內容夾帶 key=／token= 參數，把值遮掉再印。"""
    return re.sub(r"(key|token)=[^&\s'\"]+", r"\1=***", text)

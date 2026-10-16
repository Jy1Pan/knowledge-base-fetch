#!/usr/bin/env python3
"""
把 sources.json 里的源抓一遍，落成两样东西：
  latest/<slug>.<ext>          稳定路径，知识库按它做字节数与校验和的 diff
  manifest.jsonl               append-only，一行一次抓取，带四项 provenance

设计依据，每一条都对应知识库踩过的一个坑，改这个脚本之前先读一遍：

1. 稳定路径加 append-only 清单，两样都要。稳定路径让下游能比「这一版对上一版」，
   清单让下游能回答「这个读数是哪一时刻取的」。知识库 2026-10-01 定的那条
   provenance 四项（来源、取数时刻、字节数、校验和）就是 manifest 的字段。

2. 一个源失败不能让整次运行失败。失败要被记成一行带状态码的记录，
   因为「源拒绝了我」与「工作流根本没跑」在下游看起来必须不同。
   所以这里永远 exit 0，除了配置本身读不出来。

3. 校验和与字节数同时写，而且下游比的时候先比校验和。
   知识库 2026-10-16 在 PyPSA 上撞到一次等长替换：两版同为 172968 字节而内容不同，
   只有 sha256 报了出来。

4. 不写「预测」只写实测。每一行的 bytes 与 sha256 都是这次真的落盘之后算的。

5. 付费与需登录的源不在这里，它们在 sources.json 的 manual_only 里。
   这个脚本不碰付费墙，也不伪装登录。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
LATEST = ROOT / "latest"
HISTORY = ROOT / "history"
MANIFEST = ROOT / "manifest.jsonl"

UA = "jypan-kb-fetcher/1.0 (personal research; contact via repo issues)"
TIMEOUT = 45

EXT = {"api": "json", "rss": "xml", "page": "html"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def due(cadence: str) -> bool:
    """按 UTC 日期决定今天该不该抓。daily 每天；weekly 周一；monthly 每月 1 号。
    刻意用日期而不是用「上次抓过没有」，这样补跑一次不会乱掉节奏。"""
    d = datetime.now(timezone.utc)
    if cadence == "daily":
        return True
    if cadence == "weekly":
        return d.weekday() == 0
    if cadence == "monthly":
        return d.day == 1
    return True


def fetch_one(src: dict) -> dict:
    slug = src["slug"]
    url = src["url"]
    ext = EXT.get(src.get("kind", "page"), "txt")
    rec = {
        "slug": slug,
        "line": src.get("line", ""),
        "url": url,
        "kind": src.get("kind", "page"),
        "fetched_at_utc": now_iso(),
        "status": None,
        "bytes": None,
        "sha256": None,
        "path": None,
        "error": None,
    }
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
            rec["status"] = resp.status
    except urllib.error.HTTPError as e:
        # 一个 4xx/5xx 是一条读数，不是一次崩溃。状态码照记。
        rec["status"] = e.code
        rec["error"] = f"HTTPError {e.code} {e.reason}"
        _append(rec)
        return rec
    except Exception as e:  # 连接层失败，与 4xx 分开记
        rec["error"] = f"{type(e).__name__}: {e}"
        _append(rec)
        return rec

    rel = f"latest/{slug}.{ext}"
    (LATEST).mkdir(parents=True, exist_ok=True)
    (LATEST / f"{slug}.{ext}").write_bytes(body)

    # 历史副本按日期留一份，便于以后把某一天那一版原样取回重算
    hdir = HISTORY / slug
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / f"{today()}.{ext}").write_bytes(body)

    rec["bytes"] = len(body)
    rec["sha256"] = hashlib.sha256(body).hexdigest()
    rec["path"] = rel
    _append(rec)
    return rec


def _append(rec: dict) -> None:
    with MANIFEST.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> int:
    cfg = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    srcs = [s for s in cfg["sources"] if due(s.get("cadence", "daily"))]
    print(f"[{now_iso()}] 本次到期 {len(srcs)} / 全部 {len(cfg['sources'])}")

    ok = fail = 0
    for s in srcs:
        r = fetch_one(s)
        if r["sha256"]:
            ok += 1
            print(f"  OK   {r['slug']:<34} {r['status']} {r['bytes']:>9} {r['sha256'][:12]}")
        else:
            fail += 1
            print(f"  FAIL {r['slug']:<34} {r['status']} {r['error']}")
        time.sleep(2)  # 对每个站点客气一点

    print(f"[{now_iso()}] 成功 {ok} 失败 {fail}")
    # 刻意永远返回 0：失败已经被记成 manifest 里的行，让工作流红灯会掩盖掉那些行。
    return 0


if __name__ == "__main__":
    sys.exit(main())

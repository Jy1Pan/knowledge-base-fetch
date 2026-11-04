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

5. 付费与需登录的源不在这里。2026-10-16 起它们的清单只留在知识库 Project 里，不放进这个公开仓库。
   这个脚本不碰付费墙，也不伪装登录。

6. 「返回 200」不等于「取到了」。2026-10-16 第一次运行把两份 202 空响应、一份 3 字节的页面
   和三个只有前端壳的页面全部记成了成功，提交标题写 ok 15/16 而真正可用的只有 9 个。
   所以从第二版起每一行另记 content_ok：状态必须是 200，正文字节数不低于 min_bytes，
   HTML 面去掉脚本与样式后的可见文字不低于 min_text。不合格的那一版只进 history/
   以便诊断，不覆盖 latest/，于是 latest/ 里永远是最后一次合格的那一版。
   提交标题里的 ok 数从此按 content_ok 计。

7. 「今天该抓哪些源」按 cron 的计划日算，不按开跑那一刻的墙上时钟算（2026-11-04 起）。
   2026-11-02（UTC 周一）20:30 那一班被 GitHub 推迟了 6 小时 02 分，在 11-03（UTC 周二）
   02:31 才开跑，于是按周二判 cadence，把十个只在周一抓的每周源整批跳过。
   现在由工作流传入 KB_CRON_UTC（这一班 cron 的 HH:MM），计划日取「不晚于此刻的最近一次
   该 cron 时刻」所在的 UTC 日期，延迟只要不满 24 小时就判对。手动跑可直接传 KB_PLAN_DATE。
   另外每周源从 2026-11-04 起由单独一个更早的 cron（UTC 周一 08:17）抓，
   由 KB_RUN_SET 区分：daily 这一班抓 daily 与 monthly，weekly 这一班只抓 weekly。
   fetched_at_utc 仍写真实的墙上时刻，另加一列 plan_date 写计划日，下游按哪一列分组要写明。
"""

from __future__ import annotations

import hashlib
import re
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent
LATEST = ROOT / "latest"
HISTORY = ROOT / "history"
MANIFEST = ROOT / "manifest.jsonl"

UA = "jypan-kb-fetcher/2.0 (personal research; contact via repo issues)"
TIMEOUT = 45

EXT = {"api": "json", "rss": "xml", "page": "html"}

# 第二版的内容闸门，单个源可在 sources.json 里用 min_bytes / min_text 覆盖
MIN_BYTES = 512
MIN_TEXT = 200


def visible_chars(body: bytes) -> int:
    """HTML 去掉 script、style、标签与空白之后剩下的字符数。前端壳页面这个数只有几十。"""
    t = body.decode("utf-8", "replace")
    t = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = re.sub(r"&[#\w]+;", " ", t)
    return len("".join(t.split()))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def plan_date() -> date:
    """这一班 cron 的计划日（设计依据第 7 条）。优先级：KB_PLAN_DATE、KB_CRON_UTC、墙上时钟。"""
    s = os.environ.get("KB_PLAN_DATE", "").strip()
    if s:
        return date.fromisoformat(s)
    now = datetime.now(timezone.utc)
    hhmm = os.environ.get("KB_CRON_UTC", "").strip()
    if hhmm:
        h, m = (int(x) for x in hhmm.split(":"))
        sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < sched:
            sched -= timedelta(days=1)
        return sched.date()
    return now.date()


# 这一班抓哪几种 cadence（设计依据第 7 条）。没设时与 2026-11-04 之前的行为相同：三种都看。
RUN_SETS = {"daily": {"daily", "monthly"}, "weekly": {"weekly"}, "all": {"daily", "weekly", "monthly"}}


def due(cadence: str, d: date | None = None) -> bool:
    """按计划日决定该不该抓。daily 每天；weekly 周一；monthly 每月 1 号。
    刻意用日期而不是用「上次抓过没有」，这样补跑一次不会乱掉节奏。"""
    d = d or plan_date()
    if cadence not in RUN_SETS.get(os.environ.get("KB_RUN_SET", "all"), RUN_SETS["all"]):
        return False
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
    ext = src.get("ext") or EXT.get(src.get("kind", "page"), "txt")
    rec = {
        "slug": slug,
        "line": src.get("line", ""),
        "url": url,
        "kind": src.get("kind", "page"),
        "fetched_at_utc": now_iso(),
        "plan_date": PLAN.isoformat(),
        "run_set": os.environ.get("KB_RUN_SET", "all"),
        "status": None,
        "bytes": None,
        "sha256": None,
        "path": None,
        "content_ok": False,
        "visible_chars": None,
        "error": None,
    }
    headers = {"User-Agent": UA, "Accept": "*/*"}
    headers.update(src.get("headers", {}))
    req = urllib.request.Request(url, headers=headers)
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

    rec["bytes"] = len(body)
    rec["sha256"] = hashlib.sha256(body).hexdigest()

    # 内容闸门（设计依据第 6 条）
    why = []
    if rec["status"] != 200:
        why.append(f"status {rec['status']}")
    if len(body) < src.get("min_bytes", MIN_BYTES):
        why.append(f"bytes {len(body)} < {src.get('min_bytes', MIN_BYTES)}")
    if ext == "html":
        rec["visible_chars"] = visible_chars(body)
        if rec["visible_chars"] < src.get("min_text", MIN_TEXT):
            why.append(f"visible_chars {rec['visible_chars']} < {src.get('min_text', MIN_TEXT)}，疑似前端壳")
    rec["content_ok"] = not why
    if why:
        rec["error"] = "content_gate: " + "; ".join(why)

    # 历史副本：2026-10-16 起只在内容变了、或内容闸门判失败时才写一份。
    # 没变的那天 manifest 里照样有一行（带 sha256），要取回某一天那一版，
    # 就找 history/<slug>/ 里日期不晚于它、sha256 与那一行相同的最近一份。
    # 这样仓库文件数只随真实变化增长，而任何一天的读数仍能逐字节取回。
    prev_latest = LATEST / f"{slug}.{ext}"
    unchanged = (rec["content_ok"] and prev_latest.exists()
                 and hashlib.sha256(prev_latest.read_bytes()).hexdigest() == rec["sha256"])
    if not unchanged:
        hdir = HISTORY / slug
        hdir.mkdir(parents=True, exist_ok=True)
        (hdir / f"{today()}.{ext}").write_bytes(body)

    if rec["content_ok"]:
        rel = f"latest/{slug}.{ext}"
        LATEST.mkdir(parents=True, exist_ok=True)
        (LATEST / f"{slug}.{ext}").write_bytes(body)
        rec["path"] = rel
    else:
        rec["path"] = f"history/{slug}/{today()}.{ext}"
    _append(rec)
    return rec


def _append(rec: dict) -> None:
    with MANIFEST.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


PLAN = plan_date()


def main() -> int:
    cfg = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    srcs = [s for s in cfg["sources"] if due(s.get("cadence", "daily"), PLAN)]
    print(f"[{now_iso()}] 计划日 {PLAN.isoformat()}（星期{'一二三四五六日'[PLAN.weekday()]}），"
          f"本班 {os.environ.get('KB_RUN_SET', 'all')}，本次到期 {len(srcs)} / 全部 {len(cfg['sources'])}")

    ok = fail = 0
    for s in srcs:
        r = fetch_one(s)
        if r["content_ok"]:
            ok += 1
            print(f"  OK   {r['slug']:<34} {r['status']} {r['bytes']:>9} {r['sha256'][:12]}")
        else:
            fail += 1
            print(f"  FAIL {r['slug']:<34} {r['status']} {r['bytes']} {r['error']}")
        time.sleep(2)  # 对每个站点客气一点

    print(f"[{now_iso()}] 成功 {ok} 失败 {fail}")
    # 给工作流的提交标题与 digest 用：这一班的计划日、抓了哪几个源、成功几个。
    # 写在 RUNNER_TEMP 下，不进仓库。
    out = os.environ.get("KB_RUN_FILE")
    if out:
        pathlib.Path(out).write_text(json.dumps({
            "plan_date": PLAN.isoformat(), "run_set": os.environ.get("KB_RUN_SET", "all"),
            "slugs": [s["slug"] for s in srcs], "ok": ok, "total": ok + fail}, ensure_ascii=False),
            encoding="utf-8")
    # 刻意永远返回 0：失败已经被记成 manifest 里的行，让工作流红灯会掩盖掉那些行。
    return 0


if __name__ == "__main__":
    sys.exit(main())

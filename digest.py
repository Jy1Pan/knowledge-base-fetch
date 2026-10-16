#!/usr/bin/env python3
"""
三方分工里 API 那一环：在 fetch.py 抓完之后，对「今天变了的源」各调一次模型，
从新增文字里挑出候选，写进 digest/。知识库的定时任务再读 digest/，逐条回原文核对后决定收不收。

它刻意只做预读，不做判断：
  一，只看新增的行。没变的源、第一次抓到的源（没有上一版可比）一律不调模型，零花费。
  二，模型必须逐字引用原文。本脚本拿引用去新版全文里逐字找，找不到的候选标 quote_found=false，
      知识库那边对这类候选一律不收。这是零成本的编造报警器，与 battery-extract 的证据核对同一个道理。
  三，日期与链接同样要在原文里找得到，否则标 false。
  四，模型 ID 不写死。优先用仓库变量 KB_MODEL；没设就从 /v1/models 的实时列表里挑最新的 haiku，
      并把「怎么选出来的」记进 run 记录。
  五，没有密钥、或者到了每次调用上限，记一行状态然后正常退出，永远 exit 0，不让工作流亮红灯
      掩盖掉 fetch 那边已经记下的行。
  六，每次运行记模型、温度、三份提示词文件的 sha256、每次调用的 token 用量，
      所以两天的结果可以比，花了多少也可以从 run 记录里直接加出来。

环境变量：
  ANTHROPIC_API_KEY   必需（GitHub 仓库 Secrets 里设，不要写进任何文件）
  KB_MODEL            可选，固定模型 ID
  KB_MAX_CALLS        可选，单次运行最多调用几次，默认 25
  KB_MAX_INPUT_CHARS  可选，单个源送进模型的新增文字上限，默认 30000
  KB_DIGEST_MOCK      可选，测试用：一个 JSON 文件，{slug: tool_input}，有它就不联网
  KB_TODAY            可选，测试用：覆盖 UTC 日期 YYYY-MM-DD
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import pathlib
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parent
MANIFEST = ROOT / "manifest.jsonl"
HISTORY = ROOT / "history"
LATEST = ROOT / "latest"
DIGEST = ROOT / "digest"
PROMPT = ROOT / "prompt"

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"
TEMPERATURE = 0
MAX_TOKENS = 2048
EXT = {"api": "json", "rss": "xml", "page": "html"}


def today() -> str:
    return os.environ.get("KB_TODAY") or datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def norm(s) -> str:
    """只用于比对：NFKC 后去空白，并把 HTML 实体还原。"""
    if s is None:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", html.unescape(str(s))))


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}
    BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "section", "article", "item", "entry", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a" and not self.skip:
            href = dict(attrs).get("href")
            if href and href.startswith("http"):
                self.out.append(f" <{href}> ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def to_text(raw: bytes, kind: str) -> str:
    t = raw.decode("utf-8", errors="replace")
    if kind == "api" and t.lstrip()[:1] in "{[":
        try:
            return json.dumps(json.loads(t), ensure_ascii=False, indent=1)
        except ValueError:
            pass
    p = _Text()
    p.feed(t)
    lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in "".join(p.out).splitlines()]
    return "\n".join(ln for ln in lines if ln)


def added_lines(old: str, new: str) -> list[str]:
    """新版里有、旧版里没有的行，保持新版顺序。按行集合求差，不受整段挪位影响。"""
    seen = set(old.splitlines())
    out, dup = [], set()
    for ln in new.splitlines():
        if ln not in seen and ln not in dup:
            out.append(ln)
            dup.add(ln)
    return out


def find_history(slug: str, ext: str, want_sha: str, not_after: str):
    """2026-10-16 起 fetch.py 只在内容变了时写 history/，所以上一版那天未必有文件。
    在 history/<slug>/ 里找日期不晚于 not_after、sha256 等于 want_sha 的最近一份。"""
    d = HISTORY / slug
    if not d.is_dir():
        return None
    for f in sorted(d.glob(f"*.{ext}"), reverse=True):
        if f.stem <= not_after and sha(f.read_bytes()) == want_sha:
            return f
    return None


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    return [json.loads(l) for l in MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()]


def pick_changed(rows: list[dict], d: str) -> list[dict]:
    """对每个 slug：今天最后一次成功的抓取，对比它之前最后一次成功且日期不同的抓取。"""
    by = {}
    for r in rows:
        # 2026-10-16 修：内容闸门判失败的那一版（前端壳、空响应）不能当 prev 也不能当 cur。
        # 默认 True 是为了让第二版之前、还没有 content_ok 字段的旧行仍然可用。
        if r.get("sha256") and r.get("content_ok", True):
            by.setdefault(r["slug"], []).append(r)
    out = []
    for slug, rs in by.items():
        cur = [r for r in rs if r["fetched_at_utc"].startswith(d)]
        if not cur:
            continue
        cur = cur[-1]
        prev = [r for r in rs if not r["fetched_at_utc"].startswith(d)]
        if not prev:
            out.append({"slug": slug, "cur": cur, "prev": None, "state": "baseline"})
        elif prev[-1]["sha256"] == cur["sha256"]:
            out.append({"slug": slug, "cur": cur, "prev": prev[-1], "state": "unchanged"})
        else:
            out.append({"slug": slug, "cur": cur, "prev": prev[-1], "state": "changed"})
    return out


def _req(method: str, path: str, key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "x-api-key": key, "anthropic-version": VERSION, "content-type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {e.read()[:300]!r}") from None
    raise RuntimeError("unreachable")


def choose_model(key: str) -> tuple[str, str]:
    m = os.environ.get("KB_MODEL", "").strip()
    if m:
        return m, "仓库变量 KB_MODEL"
    data = _req("GET", "/models?limit=100", key).get("data", [])
    hk = sorted([x for x in data if "haiku" in x["id"]], key=lambda x: x.get("created_at", ""), reverse=True)
    if not hk:
        raise RuntimeError("实时模型列表里没有 haiku，请在仓库变量里设 KB_MODEL")
    return hk[0]["id"], f"/v1/models 实时列表里 created_at 最新的 haiku（共 {len(data)} 个模型）"


def call(key, model, system, tool, user, mock, slug):
    if mock is not None:
        return {"input": mock.get(slug, {"candidates": []}), "usage": {"input_tokens": 0, "output_tokens": 0}, "mock": True}
    r = _req("POST", "/messages", key, {
        "model": model, "max_tokens": MAX_TOKENS, "temperature": TEMPERATURE,
        "system": system, "tools": [tool], "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [{"role": "user", "content": user}]})
    blk = next((b for b in r.get("content", []) if b.get("type") == "tool_use"), None)
    return {"input": blk["input"] if blk else {"candidates": []}, "usage": r.get("usage", {}),
            "stop_reason": r.get("stop_reason")}


def main() -> int:
    d = today()
    DIGEST.mkdir(exist_ok=True)
    out_path = DIGEST / f"{d}.jsonl"
    sys_txt = (PROMPT / "digest_system.md").read_text(encoding="utf-8")
    tool = json.loads((PROMPT / "digest_schema.json").read_text(encoding="utf-8"))
    lines_raw = (PROMPT / "lines.json").read_bytes()
    lines = json.loads(lines_raw)
    srcs = {s["slug"]: s for s in json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]}
    max_calls = int(os.environ.get("KB_MAX_CALLS", "25"))
    max_chars = int(os.environ.get("KB_MAX_INPUT_CHARS", "30000"))

    mock = None
    if os.environ.get("KB_DIGEST_MOCK"):
        mock = json.loads(pathlib.Path(os.environ["KB_DIGEST_MOCK"]).read_text(encoding="utf-8"))
    key = os.environ.get("ANTHROPIC_API_KEY", "")

    run = {"type": "run", "date": d, "started_utc": now_iso(), "temperature": TEMPERATURE,
           "prompt_sha256": sha(sys_txt.encode()), "schema_sha256": sha(json.dumps(tool, sort_keys=True).encode()),
           "lines_sha256": sha(lines_raw), "mock": mock is not None,
           "model": None, "model_selected_by": None, "calls": 0,
           "input_tokens": 0, "output_tokens": 0, "status": "ok", "error": None}
    recs = []

    todo = pick_changed(load_manifest(), d)
    changed = [t for t in todo if t["state"] == "changed"]
    for t in todo:
        if t["state"] != "changed":
            recs.append({"type": "source", "slug": t["slug"], "state": t["state"], "called": False})

    if changed and not key and mock is None:
        run["status"] = "skipped_no_key"
    elif changed:
        try:
            run["model"], run["model_selected_by"] = ("mock", "KB_DIGEST_MOCK") if mock is not None else choose_model(key)
        except Exception as e:
            run["status"], run["error"] = "model_select_failed", str(e)[:300]

    for t in changed:
        s = srcs.get(t["slug"], {})
        kind = t["cur"].get("kind", "page")
        # 2026-10-16 修：与 fetch.py 第 95 行同一条逻辑，sources.json 里设了 ext 就以它为准，
        # 否则按 kind 推。两边不一致时 prev 找不到，而且是每天安静地跳过。
        ext = s.get("ext") or EXT.get(kind, "txt")
        new_raw = (ROOT / t["cur"]["path"]).read_bytes() if (ROOT / t["cur"]["path"]).exists() else b""
        prev_file = find_history(t["slug"], ext, t["prev"]["sha256"], t["prev"]["fetched_at_utc"][:10])
        rec = {"type": "source", "slug": t["slug"], "line": t["cur"].get("line"), "state": "changed",
               "prev_sha256": t["prev"]["sha256"], "cur_sha256": t["cur"]["sha256"],
               "cur_path": t["cur"]["path"], "called": False}
        # 防相位错：latest/ 的字节必须就是今天 manifest 记的那一版
        if not new_raw or sha(new_raw) != t["cur"]["sha256"]:
            rec["skip"] = "latest_mismatch_manifest"
            recs.append(rec); continue
        if prev_file is None or sha(prev_file.read_bytes()) != t["prev"]["sha256"]:
            rec["skip"] = "prev_history_missing_or_mismatch"
            recs.append(rec); continue
        new_txt = to_text(new_raw, kind)
        add = added_lines(to_text(prev_file.read_bytes(), kind), new_txt)
        rec["added_lines"] = len(add)
        if not add:
            rec["skip"] = "no_added_text"   # 只删不增，或只改了标签与脚本
            recs.append(rec); continue
        if run["status"] != "ok":
            rec["skip"] = run["status"]
            recs.append(rec); continue
        if run["calls"] >= max_calls:
            rec["skip"] = "over_max_calls"
            recs.append(rec); continue

        body = "\n".join(add)
        rec["truncated"] = len(body) > max_chars
        body = body[:max_chars]
        lab = (t["cur"].get("line") or "").split("-")[0]
        user = (f"源：{t['slug']}\n网址：{s.get('url', t['cur'].get('url'))}\n"
                f"知识库线：{t['cur'].get('line')}\n这条线关心：{lines.get(lab, '（未登记）')}\n"
                f"源的说明：{s.get('note', '')}\n\n=== 今天比上一版新增的文字行（共 {len(add)} 行"
                f"{'，已截断' if rec['truncated'] else ''}）===\n{body}")
        try:
            r = call(key, run["model"], sys_txt, tool, user, mock, t["slug"])
        except Exception as e:
            rec["skip"] = "api_error"; rec["error"] = str(e)[:300]
            recs.append(rec); continue
        run["calls"] += 1
        u = r.get("usage", {})
        run["input_tokens"] += u.get("input_tokens", 0)
        run["output_tokens"] += u.get("output_tokens", 0)
        rec.update({"called": True, "usage": u, "stop_reason": r.get("stop_reason")})
        recs.append(rec)

        nt = norm(new_txt)
        nraw = norm(new_raw.decode("utf-8", errors="replace"))
        for c in (r["input"].get("candidates") or [])[:10]:
            q = c.get("quote") or ""
            nq = norm(q)
            recs.append({"type": "candidate", "slug": t["slug"], "line": t["cur"].get("line"),
                         "cur_sha256": t["cur"]["sha256"], "cur_path": t["cur"]["path"], **c,
                         "quote_found": bool(nq) and len(nq) >= 8 and nq in nt,
                         "date_found": None if not c.get("date") else norm(c["date"]) in nq,
                         "link_found": None if not c.get("link") else (norm(c["link"]) in nt or norm(c["link"]) in nraw)})

    run["finished_utc"] = now_iso()
    with out_path.open("a", encoding="utf-8") as f:
        for r in [run] + recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    cands = [r for r in recs if r["type"] == "candidate"]
    good = [c for c in cands if c["quote_found"] and c.get("date_found") is not False and c.get("link_found") is not False]
    summary = {"date": d, "run": run, "sources_changed": len(changed),
               "sources_unchanged": sum(1 for t in todo if t["state"] == "unchanged"),
               "sources_baseline": sum(1 for t in todo if t["state"] == "baseline"),
               "candidates": len(cands), "candidates_verified": len(good),
               "candidates_rejected": len(cands) - len(good), "file": f"digest/{d}.jsonl"}
    (DIGEST / "latest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

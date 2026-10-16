# kb-fetch

把知识库取不到的那批源，在 GitHub 的出口上抓下来，commit 进本仓库，知识库再从 `raw.githubusercontent.com` 读。
于是那批源从「第二层，只有 JyPan 在场时可取」变成「第一层，无人值守可取」。

## 它解的是什么问题

知识库跑在一个出口受限的容器里。2026-10-16 实测了二十二个域，**二十二个全部取不到**，两种签名：

- 十九个是 `curl: (56) CONNECT tunnel failed, response 403`，出口代理拒绝 CONNECT。
  工信部、生态环境部、发改委、EUR-Lex、Have Your Say、JRC EPLCA、上海有色网、Fastmarkets、
  electrive、battery-news、pv magazine、Batteriepasswerk、ScienceDirect、Springer，
  连 Crossref API 与 DOAJ API 这两条本该最通的机读入口也在内。
- 三个连 DNS 都解析不出来：`www.nea.gov.cn`、`www.news.cn`、`www.cnenergynews.cn`。
  同一时刻 `raw.githubusercontent.com` 正常解析并返回 200，所以不是网络坏了。

GitHub Actions 的 runner 没有这个限制。

## 装起来，四步

1. 新建一个仓库（公开或私有都行，私有也能从 raw 域读，但要带 token，所以**建议公开**，里面只有公开资料）。
2. 把本目录的四个文件放进去：`fetch.py`、`sources.json`、`README.md`、`.github/workflows/fetch.yml`。
3. 仓库的 Settings → Actions → General，把 Workflow permissions 设成 **Read and write**（工作流要 commit）。
4. Actions 页面手动跑一次 `fetch`，看 `latest/` 与 `manifest.jsonl` 有没有出来。

跑通之后把仓库名告诉知识库，它会把 `https://raw.githubusercontent.com/<owner>/<repo>/main/latest/<slug>.<ext>`
写进 `kb/03-sources-d.md` 的轮转表，并给每一个面留第一份基线。

## 它产出什么

```
latest/<slug>.json|xml|html     稳定路径。知识库按它比字节数与 sha256。
history/<slug>/<YYYY-MM-DD>.*   当天那一版的副本。用来把某一天的读数原样取回重算。
manifest.jsonl                  append-only，一行一次抓取。
```

`manifest.jsonl` 每行的字段：`slug`、`line`、`url`、`kind`、`fetched_at_utc`、`status`、`bytes`、`sha256`、`path`、`error`。
前四项是身份，`fetched_at_utc` 加 `bytes` 加 `sha256` 是知识库 2026-10-01 定的 provenance 四项，
`status` 与 `error` 分开记是为了让「源拒绝了我」和「工作流没跑」在下游看起来不一样。

## 几条刻意的设计，改之前先读

**一个源失败不让整次运行失败。** `fetch.py` 永远 exit 0。失败已经是 manifest 里的一行带状态码的记录，
再让工作流亮红灯只会掩盖那些行。判「这次跑成了没有」看提交标题里的 `ok 18/21`。

**时间排在知识库之前半小时。** 工作流 cron 是 UTC 21:30，知识库的定时任务是 UTC 22:00。
知识库此前在三个源上各记过一次「相位差」的坑（读到的是二十小时前那一版），这里一次解掉。

**稳定路径与历史副本都留。** 只留稳定路径，就没法把某一天那一版取回来用新算法重算；
只留历史副本，就没法做「这一版对上一版」的 diff。知识库两种需求都有过。

**付费与需登录的源不在这里。** 它们在 `sources.json` 的 `manual_only` 段里，带不抓的理由：
Fastmarkets 是付费商业数据；ScienceDirect 与 Springer 的全文付费，题录走 Crossref 公开 API，
全文标 `library_only` 由你在 Chrome 里用墨大账号打开；猎聘与 BOSS 要登录且反自动化，
只能你自己在 Chrome 里跑，仓库里只放关键词与城市清单。
**这个脚本不碰付费墙，也不伪装登录。**

**`sleep(2)`。** 对每个站点客气一点，二十来个源一次跑完也就一分钟。

## 顺手解掉的一件旧账

`sources.json` 里有一条 `arxiv-kb-lines`。arXiv 在知识库那个容器里 API、列表页、RSS 三条路全部 403，
这个缺口从 2026-09-16 挂到今天三十天。它在 Actions 上是一条开放 API，所以一并收进来。

## 加一个新源的时候

往 `sources.json` 的 `sources` 数组里加一个对象，四个必填字段是 `slug`、`kind`、`cadence`、`url`，
`note` 里写清**为什么用这条入口而不是另一条**。最后这一项不是客套：
知识库吃过的最大一类亏就是入口选错了而返回码正常（取到的是旧的、是旁支、是落地页），
而当初选它的理由没人写下来。

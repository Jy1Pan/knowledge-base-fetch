# kb-fetch

> 2026-10-16 晚起，本仓库是知识库每日生长「三方分工」里的前两环：**Actions 抓取（fetch.py，不调模型）→ API 预读（digest.py，只对变了的源调模型）→ Cowork 定时任务判断与写入**（读本仓库的 raw 地址，逐条回原文核对后决定收不收）。

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

## 装起来

下面这四步之后，还有「API 预读」一节的三步。两节都做完才是完整的三方分工；只做前四步，抓取照常跑，digest 那一步会记 `skipped_no_key` 然后正常结束。

### 抓取，四步

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
再让工作流亮红灯只会掩盖那些行。判「这次跑成了没有」看提交标题里的 `ok 18/21`。**第二版（2026-10-16 晚）起这个数按 `content_ok` 计**：状态 200、正文不低于 `min_bytes`、HTML 面去掉脚本后的可见文字不低于 `min_text`，三条都过才算。不合格的那一版只进 `history/`，不覆盖 `latest/`。起因是第一次运行标题写 ok 15/16，而按内容判只有 9 个可用。**2026-10-16 再修一处计数**：同一天手动跑几次时，每个源只算当天最后一行，否则分母会是几次运行的行数之和（当日出现过 ok 11/48，实为 11/16）。

**时间排在知识库之前九十分钟。** 工作流 cron 是 UTC 20:30，知识库的定时任务是 UTC 22:00。2026-10-16 从 21:30 提前：GitHub 的计划任务高峰时会被推迟，半小时余量不够。
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

## API 预读（digest.py）

### 它做什么

fetch.py 抓完之后，digest.py 对**今天内容变了的源**各调一次模型，只把「比上一版新增的文字行」送进去，让模型挑候选并逐字引用原文。结果写进：

```
digest/<YYYY-MM-DD>.jsonl   第一行是本次运行记录（模型、温度、三份提示词的 sha256、token 用量），
                            然后每个源一行状态，每个候选一行
digest/latest.json          当日汇总：变了几个源、几个候选、几个通过逐字核对
```

每个候选带三个机器核对的标记：`quote_found`（引用在新版全文里逐字找到）、`date_found`、`link_found`。**知识库只看三项都不为 false 的候选，而且还要自己回 `latest/` 原文再核一次才写。** 模型在这里只是预读员，不是作者。

### 什么时候不花钱

没变的源、第一次抓到的源（没有上一版可比）、只改了脚本或标签而没有新增文字的源，一律不调模型。实际每天调用几次取决于那天有几个源真的更新了，上限是仓库变量 `KB_MAX_CALLS`，默认 25。

### 三步，需要你自己动手的只有这里

1. **在 Console 设一个月度花费上限**（Console 里的 Limits 设置，按账户或按 Workspace 都可以）。设多少你定，这是防意外的最后一道闸。
2. **建一把只给这个仓库用的密钥**：Console → API Keys → Create Key，名字写 `kb-fetch-actions`。
3. **把密钥粘进仓库 Secrets**：仓库 Settings → Secrets and variables → Actions → New repository secret，Name 填 `ANTHROPIC_API_KEY`，Secret 粘密钥。**只粘在这一处。** 不要粘进任何对话、任何文件、任何提交。

可选：同一页的 Variables 标签里加 `KB_MODEL`（固定模型 ID）与 `KB_MAX_CALLS`。不设 `KB_MODEL` 时，脚本从 `/v1/models` 的实时列表里挑 created_at 最新的 haiku，并把选择依据记进运行记录，不靠猜。

### 怎么确认它跑通了

提交标题会变成 `fetch 2026-10-17: ok 18/21; digest 3/4 ok`，分号后面是「通过核对的候选 / 全部候选」与运行状态。状态的几种取值：`ok`、`skipped_no_key`（没设密钥）、`model_select_failed`（密钥错或无权限，`error` 字段有原文）。第一天全部源都是 baseline，所以 digest 会是 `0/0 ok`，这是对的，**第二天才开始有候选**。

### 改提示词之前

`prompt/digest_system.md`、`prompt/digest_schema.json`、`prompt/lines.json` 三份的 sha256 记在每次运行里。改了任何一份，改之前与改之后的结果就不能直接比。`lines.json` 摘自知识库协议第二节，协议改了它要跟着改。

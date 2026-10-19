# AI 治理与安全日报（免 API 版）

每天自动抓取全球 AI 立法、政策标准、国际治理与安全事件的新闻，按规则筛选、分类、合并同一事件的报道，发布为一个手机和电脑都能看的网站。

整套系统跑在 GitHub 上，**完全免费**：不需要服务器、不需要域名，也不需要任何 AI 接口或密钥。

## 它是怎么工作的

```
每天北京时间 7:00
  GitHub Actions 运行 scripts/update.py
    ├─ 抓取：从官方机构、权威媒体、专业媒体、Google 新闻检索和 arXiv 抓取最近 36 小时的报道
    ├─ 去重：跳过以前处理过的（记录在 data/seen.json）
    ├─ 筛选：按 config/rules.json 的关键词打分，过滤融资、股价、新品这类无关报道
    ├─ 合并：标题相似的报道视为同一事件，合并成一条，记下共有几家来源
    ├─ 分类：按关键词判断类别和地区
    ├─ 重要度：报道来源越多、出现「签署成法、生效、安理会」等高信号词、
    │          有政府官网来源，重要度越高
    ├─ 摘要：读取原文网页自带的简介
    ├─ 今日要点：根据当天的统计自动生成
    └─ 写入 site/data/，自动提交到仓库
  同一个工作流把 site/ 发布到 GitHub Pages
```

数据就是仓库里的 JSON 文件，每次更新都是一次提交，Git 历史就是完整的备份。超过 120 天的资讯按月移到 `site/data/archive/`。

## 和 AI 整理相比的取舍

- **外文报道保留原文标题和摘要。** 没有 AI 就没法翻译。网页上有「中文 / 外文」筛选，只想看中文报道时可以切换。
- **合并同一事件靠标题相似度。** 同一种语言、措辞接近的报道能合并；中英文报道同一件事、或者标题差别很大时，会分成两条。
- **分类和重要度靠关键词。** 大多数情况下准确，偶尔会有偏差。可以调 `config/rules.json` 改善，见下文。
- **今日要点是统计式的。** 内容是「收录几条、各类别几条、哪条报道最多」，不是对新闻内容的概括。你也可以自己写，见下文。

## 信息来源与质量控制

来源分三档，每条资讯前会标出档次，网页上也可以按来源筛选：

- **官方**：政府和监管机构（英国 AI 安全研究所、美国 NIST 和 FTC、欧盟、OECD、联合国、中国网信办等），以及 OpenAI、Google DeepMind 等实验室的官方发布。
- **权威媒体**：Reuters、AP、Bloomberg、FT、WSJ、纽约时报、华盛顿邮报、Politico、Axios、Semafor、The Hill、BBC、卫报、经济学人等；中文有财新、澎湃、第一财经、21 世纪经济报道、界面、南方周末、联合早报、新华社、人民日报、央视等。
- **专业媒体**：MIT Technology Review、Wired、Ars Technica、The Verge、Tech Policy Press、Lawfare、IAPP、Euractiv、Rest of World 等科技与政策媒体；Transformer、CAIS 的 AI Safety Newsletter、EU AI Act Newsletter、Import AI、ChinAI、安远 AI 的 AI Safety in China 等专业通讯；Brookings、CSIS、Carnegie、RAND、CSET、Ada Lovelace Institute 等智库；36 氪、虎嗅；以及 arXiv 论文。

质量控制规则：

- 官方、权威、专业来源在打分时有加分，同一事件有多家报道时优先选它们的链接；两家以上权威或专业来源都报道了的事件，重要度会提高。
- 不在名单里的来源，入选门槛更高，每次最多入选 8 条，重要度一般不超过「值得关注」。
- 财经聚合站、股票社区、内容农场等在黑名单里，直接排除。
- Google 新闻的跳转链接会尽量还原成原文地址，这样能读到摘要，在中国大陆也能打开。还原失败时保留原链接。

来源名单在 `config/rules.json` 的 `source_tiers` 里，新闻来源在 `config/feeds.json` 里，都可以自己增删。

**来源体检**：每次运行后，在 Actions 里点开这次运行，页面底部的摘要里有一张表，列出每个来源读到几条、新候选几条、入选几条。❌ 表示连不上，⚠️ 表示连上了但没有内容，连续失败的会标出次数。长期失败的来源可以在 `config/feeds.json` 里设 `"enabled": false` 停用。

**屏蔽来源或单条资讯**：编辑 `manual.json`，把来源名称或域名加进 `block_sources`，把单条资讯的链接加进 `block_urls`。下次运行时会生效，已收录的相应资讯也会撤下。

## 搭建步骤（约 10 分钟）

### 1. 新建仓库

登录 GitHub，右上角 **+** → **New repository**。

- 名字随意，比如 `ai-governance-daily`
- 选 **Public**（免费账号的 GitHub Pages 只支持公开仓库）
- 其余保持默认，点 **Create repository**

### 2. 上传文件

在新仓库页面点 **uploading an existing file**，把解压后文件夹里的**全部内容**拖进去，然后点 **Commit changes**。

> **注意隐藏文件夹。** `.github` 是以点开头的隐藏文件夹，里面放着定时任务，漏传就不会自动更新。
> Mac 上在访达里按 `Cmd + Shift + .` 显示隐藏文件；Windows 在资源管理器的「查看」里勾选「隐藏的项目」。
> 上传后在仓库首页确认能看到 `.github` 文件夹。

如果你会用 git 命令行：

```bash
cd ai-governance-daily
git init && git add -A && git commit -m "初始化"
git branch -M main
git remote add origin https://github.com/你的用户名/ai-governance-daily.git
git push -u origin main
```

### 3. 打开 GitHub Pages

仓库 **Settings** → 左侧 **Pages** → **Build and deployment** 下的 **Source** 选 **GitHub Actions**。

### 4. 手动跑第一次

仓库 **Actions** 标签页 → 左侧选 **每日更新并发布** → 右侧 **Run workflow** → **Run workflow**。

一两分钟后跑完。点进这次运行，页面底部的摘要会列出新增了哪些资讯；日志里能看到每个来源抓到几条。

### 5. 打开网站

```
https://你的用户名.github.io/ai-governance-daily/
```

也可以在 **Settings** → **Pages** 顶部看到。

**加到 iPhone 主屏幕：** 用 Safari 打开网址 → 点底部「分享」→「添加到主屏幕」。

之后每天早上 7 点会自动更新，不需要再做任何事。

## 调整筛选效果

所有规则都在 `config/rules.json`，在 GitHub 网页上点铅笔图标就能编辑。

| 字段 | 作用 |
| --- | --- |
| `ai_terms` | AI 相关词。报道里至少要出现一个，否则直接排除 |
| `strong_terms` | 强相关词，每个加 3 分（出现在标题里算两倍） |
| `medium_terms` | 一般相关词，每个加 1 分（出现在标题里算两倍） |
| `negative_terms` | 排除词，每个扣 4 分 |
| `min_score` | 入选门槛，默认 4 分 |
| `high_signal_terms` | 出现时重要度加一级 |
| `source_tiers` | 来源分档：`official` 官方、`tier_a` 权威媒体、`tier_b` 专业媒体、`block` 黑名单。含「.」的条目按域名匹配，其余按来源名称匹配 |
| `tier_bonus` | 各档来源的加分 |
| `unknown_extra_score` / `unknown_max_per_run` | 名单外来源要多达到几分才入选、每次最多入选几条 |
| `categories` / `regions` | 每个类别和地区的判断词，命中最多的类别胜出 |
| `max_items_per_run` | 每次最多新增几条 |

匹配规则：英文按词首匹配、不分大小写（`regulat` 能匹配 regulation、regulator）；3 个字母以内的英文词（如 `AI`、`EU`、`UN`）要求完整匹配且区分大小写；中文按包含匹配。

几个常见的调整：

- **无关新闻太多**：把反复出现的无关词加进 `negative_terms`，或把 `min_score` 调高到 5 或 6。
- **漏掉了相关新闻**：把那类新闻常用的词加进 `strong_terms`，或把 `min_score` 降到 3。
- **某个类别总是分错**：给正确的类别多加几个特征词。

## 日常使用

**手动收录一条资讯**：编辑 `manual.json`，按下面的格式添加后提交。下次自动更新时会写进网站（想马上生效，就去 Actions 手动运行一次）。

```json
{
  "items": [
    {
      "date": "2026-10-12",
      "title": "某某发布 AI 安全评估指南",
      "summary": "一两句话摘要。",
      "url": "https://……",
      "source": "来源名称",
      "category": "政策与标准",
      "regions": ["中国"],
      "importance": 2
    }
  ]
}
```

`category` 可选：立法与监管、政策与标准、国际治理、安全事件、安全研究、行业动态。
`regions` 可选：中国、美国、欧洲、全球/多边、其他。
`importance`：1 一般，2 值得关注，3 重大。

**自己写当天要点**：编辑 `site/data/days.json`，按下面的格式写，并加上 `"manual": true`，自动更新就不会覆盖它。

```json
"2026-10-12": { "brief": "你写的要点。", "manual": true }
```

**删除或修改某条资讯**：编辑 `site/data/items.json`，找到那一条删掉或修改后提交，网站一分钟左右会自动重新发布。

**调整新闻来源**：编辑 `config/feeds.json`。每个来源可以设置搜索词（`q`）、语言（`lang`：`zh` 或 `en`）、最多读几条（`max`）、每天最多入选几条（`max_selected`），`"enabled": false` 可以临时停用。也可以用 `"type": "rss"` 加任何 RSS 或 Atom 地址，比如你常看的媒体或机构的订阅源。

**调整更新时间**：编辑 `.github/workflows/update.yml` 里的 `cron`。时间用的是 UTC，比北京时间早 8 小时，比如 `"0 23 * * *"` 就是北京时间 7:00。GitHub 的定时任务高峰期可能延迟十几分钟到一小时，属于正常现象。

## 可调参数

在 `update.yml` 的「抓取并整理资讯」步骤下加 `env:`：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `LOOKBACK_HOURS` | 36 | 只收录多少小时内发布的报道 |
| `KEEP_DAYS` | 120 | 网站上保留几天，更早的移入 archive |
| `FETCH_SUMMARIES` | 1 | 设为 0 则不去原文网页读简介 |

## 常见问题

**Actions 运行失败，提示 permission denied 或 push 失败**
**Settings** → **Actions** → **General** → **Workflow permissions** 选 **Read and write permissions** 后保存，再运行一次。

**部署步骤报错，提示 environment 或 Pages 没有启用**
确认第 3 步的 Source 选的是 **GitHub Actions**，不是 Deploy from a branch。

**某个来源一直抓到 0 条**
看运行日志里每个来源的「解析 N 条」。如果长期是 0，可能是对方改了格式或屏蔽了抓取，在 `config/feeds.json` 里停用它或换一个来源。

**有些条目没有摘要**
摘要来自原文网页的简介。有些网站不提供简介或屏蔽了抓取；Google 新闻的链接是跳转地址，读不到原文简介。点标题可以看原文。

**在中国大陆访问**
`github.io` 的访问可能不稳定；网页使用的 Google 字体加载不了时会自动改用系统字体，不影响阅读；Google 新闻的链接会尽量还原成原文地址，个别还原失败的仍需要能访问 Google 才能打开。

**定时任务停了**
GitHub 会暂停 60 天内没有任何活动的公开仓库的定时任务。这个项目每天都会自动提交，一般不会触发；如果真的停了，在 Actions 页面点 **Enable workflow** 即可。

## 本地预览

```bash
python3 scripts/update.py        # 在本地抓取一次（可选）
cd site && python3 -m http.server 8000
# 浏览器打开 http://localhost:8000
```

直接双击 `index.html` 读不到数据，必须通过网址访问。

## 文件说明

```
.github/workflows/update.yml   定时任务：抓取、保存、发布
scripts/update.py              抓取与整理脚本（只用 Python 标准库）
config/feeds.json              新闻来源
config/rules.json              筛选、分类、重要度规则
manual.json                    手动收录
data/seen.json                 已处理过的报道记录（不发布到网站）
site/index.html                网页
site/data/items.json           资讯库
site/data/days.json            每日要点
site/data/archive/             按月归档的旧资讯
```

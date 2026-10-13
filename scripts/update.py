#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日抓取 AI 治理与 AI 安全资讯，整理后写入 site/data/。

不依赖任何 AI 接口，全部用规则完成：
- 筛选：按 config/rules.json 里的关键词打分，过滤无关报道
- 合并：标题相似的报道视为同一事件，合并成一条并记录有几家来源
- 重要度：根据来源数量、高信号词、官方来源来判断
- 摘要：读取原文网页自带的简介（og:description）
- 今日要点：根据当天的统计数据自动生成

只用 Python 标准库，不需要安装任何依赖。
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_DATA = ROOT / "site" / "data"
ITEMS_FILE = SITE_DATA / "items.json"
DAYS_FILE = SITE_DATA / "days.json"
ARCHIVE_DIR = SITE_DATA / "archive"
SEEN_FILE = ROOT / "data" / "seen.json"
FEEDS_FILE = Path(os.getenv("FEEDS_CONFIG") or ROOT / "config" / "feeds.json")
RULES_FILE = Path(os.getenv("RULES_CONFIG") or ROOT / "config" / "rules.json")
MANUAL_FILE = ROOT / "manual.json"

TZ = dt.timezone(dt.timedelta(hours=float(os.getenv("TZ_OFFSET_HOURS") or 8)))
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS") or 36)
KEEP_DAYS = int(os.getenv("KEEP_DAYS") or 120)
SEEN_DAYS = 200
FETCH_SUMMARIES = (os.getenv("FETCH_SUMMARIES") or "1") != "0"
UA = "Mozilla/5.0 (compatible; ai-governance-daily/1.0)"

CATS = ["立法与监管", "政策与标准", "国际治理", "安全事件", "安全研究", "行业动态"]
REGIONS = ["中国", "美国", "欧洲", "全球/多边", "其他"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def now():
    return dt.datetime.now(TZ)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        log(f"警告：{path} 不是有效的 JSON（{e}），按空文件处理")
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")
    tmp.replace(path)


# ================================================================ 关键词匹配

class Terms:
    """一组关键词。英文按词首匹配；3 个字母以内的英文要求完整匹配且区分大小写；中文按包含匹配。"""

    def __init__(self, terms):
        self.terms = [t for t in terms if t]
        self.patterns = []
        for t in self.terms:
            if re.fullmatch(r"[\x00-\x7f]+", t):
                esc = re.escape(t)
                if len(t) <= 3:
                    pat = re.compile(rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])")
                else:
                    pat = re.compile(rf"(?<![A-Za-z0-9]){esc}", re.I)
            else:
                pat = re.compile(re.escape(t))
            self.patterns.append((t, pat))

    def hits(self, text):
        return [t for t, p in self.patterns if p.search(text)]

    def any(self, text):
        return any(p.search(text) for _, p in self.patterns)


class Rules:
    def __init__(self, cfg):
        self.ai = Terms(cfg.get("ai_terms", []))
        self.strong = Terms(cfg.get("strong_terms", []))
        self.medium = Terms(cfg.get("medium_terms", []))
        self.negative = Terms(cfg.get("negative_terms", []))
        self.high = Terms(cfg.get("high_signal_terms", []))
        self.auth_domains = [d.lower() for d in cfg.get("authority_domains", [])]
        self.auth_sources = [s.lower() for s in cfg.get("authority_sources", [])]
        self.categories = [(c, Terms(t)) for c, t in cfg.get("categories", []) if c in CATS]
        self.regions = [(r, Terms(t)) for r, t in cfg.get("regions", []) if r in REGIONS]
        self.min_score = float(cfg.get("min_score", 4))
        self.max_items = int(os.getenv("MAX_ITEMS_PER_RUN") or cfg.get("max_items_per_run", 30))

    def score(self, c):
        """相关度：标题里的词算两倍。没有 AI 相关词直接为 0。"""
        title, body = c["title"], c["snippet"]
        if not (self.ai.any(title) or self.ai.any(body)):
            return 0.0
        s = 0.0
        for terms, w in ((self.strong, 3), (self.medium, 1)):
            th, bh = set(terms.hits(title)), set(terms.hits(body))
            s += w * (2 * len(th) + 0.5 * len(bh - th))
        s -= 4 * len(set(self.negative.hits(title + " " + body)))
        return s

    def is_authority(self, source, url):
        host = urllib.parse.urlsplit(url or "").netloc.lower()
        if any(host.endswith(d) or (d.startswith(".") and d[1:] in host.split(".")) for d in self.auth_domains):
            return True
        s = (source or "").lower()
        return any(a in s for a in self.auth_sources)

    def is_official(self, url):
        host = urllib.parse.urlsplit(url or "").netloc.lower()
        return any(host.endswith(d) for d in self.auth_domains)

    def category(self, text, forced=None):
        if forced in CATS:
            return forced
        best, best_n = None, 0
        for cat, terms in self.categories:  # 按配置顺序，命中最多的优先，平局取靠前的
            n = len(terms.hits(text))
            if n > best_n:
                best, best_n = cat, n
        return best or "行业动态"

    def regions_of(self, text):
        found = [r for r, terms in self.regions if terms.any(text)]
        if "全球/多边" in found and len(found) > 2:
            found.remove("全球/多边")
        return found[:2] or ["其他"]


# ================================================================ 抓取

def feed_url(f):
    t = f.get("type", "rss")
    lang = f.get("lang", "en")
    if t == "bing":
        return "https://www.bing.com/news/search?" + urllib.parse.urlencode(
            {"q": f["q"], "format": "rss", "setlang": "zh-cn" if lang == "zh" else "en-us",
             "cc": "CN" if lang == "zh" else "US"})
    if t == "gnews":
        hl, gl, ceid = ("zh-CN", "CN", "CN:zh-Hans") if lang == "zh" else ("en-US", "US", "US:en")
        return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": f'{f["q"]} when:{f.get("when", "2d")}', "hl": hl, "gl": gl, "ceid": ceid})
    return f["url"]


def http_get(url, tries=3, timeout=30, limit=None):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(limit) if limit else r.read()
                return data, r.headers.get_content_charset()
        except Exception as e:  # noqa: BLE001 — 任何网络错误都只影响这一个来源
            if tries > 1:
                log(f"  抓取失败（第 {i + 1} 次）：{e}")
                time.sleep(2 * (i + 1))
    return None, None


TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s):
    s = html.unescape(TAG_RE.sub(" ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child_text(node, name):
    for c in node:
        if local(c.tag).lower() == name.lower():
            return "".join(c.itertext()).strip()
    return ""


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    d = None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        pass
    if d is None:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").lower())


def real_url(link):
    """Bing 新闻的链接是跳转地址，原文地址在 url 参数里，取出来方便直接打开。"""
    try:
        p = urllib.parse.urlsplit(link)
        if p.netloc.endswith("bing.com"):
            q = urllib.parse.parse_qs(p.query)
            if q.get("url"):
                return q["url"][0]
    except ValueError:
        pass
    return link


def parse_feed(raw, feed):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log(f"  解析失败：{e}")
        return []
    out = []
    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        title = strip_html(child_text(node, "title"))
        link, source = "", ""
        for c in node:
            n = local(c.tag).lower()
            if n == "link":
                href = c.get("href")
                if href and c.get("rel") in (None, "alternate") and not link:
                    link = href.strip()
                elif (c.text or "").strip() and not link:
                    link = c.text.strip()
            elif n == "source":
                source = (c.text or "").strip() or child_text(c, "title") or source
        pub = parse_date(child_text(node, "pubDate") or child_text(node, "published")
                         or child_text(node, "updated") or child_text(node, "date"))
        desc = strip_html(child_text(node, "description") or child_text(node, "summary")
                          or child_text(node, "content"))
        m = re.search(r"\s[-–—|]\s([^-–—|]{2,60})$", title)  # Google 新闻标题末尾带「 - 媒体名」
        if m and (not source or m.group(1).strip() == source):
            source = source or m.group(1).strip()
            title = title[: m.start()].strip()
        if desc and norm(title) and norm(title)[:40] in norm(desc):
            desc = ""  # 简介只是标题的重复
        if not title or not link:
            continue
        out.append({"title": title, "url": real_url(link), "source": source or feed.get("name", ""),
                    "published": pub, "snippet": desc[:600], "feed": feed})
    return out


def key_of(text):
    return hashlib.sha1(norm(text)[:80].encode()).hexdigest()[:16]


def url_key(u):
    p = urllib.parse.urlsplit(u or "")
    extra = "?" + p.query if "news.google" in p.netloc else ""
    return hashlib.sha1((p.netloc.lower() + p.path.rstrip("/") + extra).encode()).hexdigest()[:16]


def lang_of(text):
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    return "zh" if cjk >= 2 and cjk >= 0.2 * len(re.sub(r"\s", "", text or "")) else "en"


def collect(feeds, seen):
    cands, keys = [], set()
    for f in feeds:
        if f.get("enabled") is False:
            continue
        cutoff = now() - dt.timedelta(hours=int(f.get("lookback_hours") or LOOKBACK_HOURS))
        log(f"抓取：{f.get('name') or feed_url(f)}")
        raw, _ = http_get(feed_url(f))
        if not raw:
            continue
        got = parse_feed(raw, f)
        n = 0
        for c in got[: int(f.get("max", 40))]:
            if c["published"] and c["published"] < cutoff:
                continue
            c["published"] = c["published"] or now()
            tk, uk = key_of(c["title"]), url_key(c["url"])
            if tk in seen or uk in seen or tk in keys or uk in keys:
                continue
            keys.update((tk, uk))
            c["tk"], c["uk"] = tk, uk
            c["date"] = c["published"].astimezone(TZ).date().isoformat()
            cands.append(c)
            n += 1
        log(f"  解析 {len(got)} 条，新候选 {n} 条")
        time.sleep(1)
    return cands


# ================================================================ 合并同一事件

STOP = set("""a an the of to in on for and or with by at from as is are was were be been after over new says said say
will would could can may its it this that than into about amid more how why what who when not no up out via vs just
report reports news update latest""".split())


def tokens(title):
    t = (title or "").lower()
    words = set()
    for w in re.findall(r"[a-z0-9]+", t):
        if w in STOP or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.add(w)
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        words.update(seg[i:i + 2] for i in range(len(seg) - 1))
    words.discard("ai")
    return words


def similar(a, b):
    if not a or not b:
        return False
    inter = len(a & b)
    if inter / len(a | b) >= 0.45:
        return True
    return inter >= 4 and inter / min(len(a), len(b)) >= 0.7


def cluster(cands):
    groups = []  # 每组：{"members": [...], "tok": set}
    for c in sorted(cands, key=lambda x: -x["score"]):
        c["tok"] = tokens(c["title"])
        for g in groups:
            if similar(c["tok"], g["tok"]):
                g["members"].append(c)
                break
        else:
            groups.append({"members": [c], "tok": c["tok"]})
    return groups


# ================================================================ 原文简介

META_RE = re.compile(r"<meta\b[^>]*>", re.I)
ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", re.I)


def page_description(url, title):
    host = urllib.parse.urlsplit(url).netloc
    if not url.startswith("http") or "news.google." in host or "bing.com" in host:
        return ""
    raw, charset = http_get(url, tries=1, timeout=12, limit=400_000)
    if not raw:
        return ""
    if not charset:
        m = CHARSET_RE.search(raw[:5000])
        charset = m.group(1).decode() if m else "utf-8"
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    head = text[: text.lower().find("</head>")] if "</head>" in text.lower() else text[:60000]
    found = {}
    for tag in META_RE.findall(head):
        attrs = {k.lower(): (v1 or v2) for k, v1, v2 in ATTR_RE.findall(tag)}
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        if name in ("og:description", "description", "twitter:description") and attrs.get("content"):
            found.setdefault(name, attrs["content"])
    for name in ("og:description", "description", "twitter:description"):
        d = re.sub(r"\s+", " ", html.unescape(found.get(name, ""))).strip()
        if len(d) >= 20 and norm(d)[:40] != norm(title)[:40]:
            return trim(d, 220)
    return ""


def trim(s, n):
    if len(s) <= n:
        return s
    cut = s[:n]
    m = re.search(r"^(.*[。！？.!?])", cut)
    return m.group(1) if m and len(m.group(1)) > n * 0.5 else cut.rstrip() + "…"


# ================================================================ 生成条目

def importance(group, rules):
    members = group["members"]
    sources = {m["source"] for m in members if m["source"]}
    text = " ".join(m["title"] + " " + m["snippet"] for m in members)
    imp = 1
    if len(sources) >= 3:
        imp += 1
    if len(sources) >= 5:
        imp += 1
    if rules.high.any(text):
        imp += 1
    if any(rules.is_official(m["url"]) for m in members):
        imp += 1
    return min(3, imp)


def pick_primary(group, rules):
    members = group["members"]
    return max(members, key=lambda m: (rules.is_authority(m["source"], m["url"]),
                                       "news.google." not in m["url"],
                                       bool(m["snippet"]), m["score"]))


def build_item(group, rules):
    p = pick_primary(group, rules)
    others, seen_src = [], {p["source"]}
    for m in sorted(group["members"], key=lambda m: -m["score"]):
        if m is p or not m["source"] or m["source"] in seen_src:
            continue
        seen_src.add(m["source"])
        others.append({"source": m["source"], "url": m["url"]})
    text = " ".join(m["title"] + " " + m["snippet"] for m in group["members"])
    forced = p["feed"].get("category")
    return {
        "id": f'{p["date"]}-{p["tk"]}', "date": p["date"], "title": p["title"],
        "summary": trim(p["snippet"], 220) if p["snippet"] else "",
        "url": p["url"], "source": p["source"], "also": others[:8], "coverage": len(seen_src),
        "category": rules.category(text, forced), "regions": rules.regions_of(text),
        "importance": importance(group, rules), "lang": lang_of(p["title"]),
        "addedBy": "auto", "createdAt": now().isoformat(timespec="seconds"),
        "keys": sorted({k for m in group["members"] for k in (m["tk"], m["uk"])}),
        "_score": max(m["score"] for m in group["members"]),
        "_feed": p["feed"].get("name", ""), "_cap": p["feed"].get("max_selected"),
    }


def attach_to_existing(groups, items, rules):
    """新报道如果和最近 3 天已收录的事件相同，并入那一条，不再单独收录。"""
    since = (now().date() - dt.timedelta(days=3)).isoformat()
    recent = [(it, tokens(it["title"])) for it in items if it["date"] >= since and it.get("addedBy") == "auto"]
    rest, merged = [], 0
    for g in groups:
        target = next((it for it, tok in recent if similar(g["tok"], tok)), None)
        if not target:
            rest.append(g)
            continue
        merged += 1
        srcs = {target.get("source")} | {a["source"] for a in target.get("also", [])}
        for m in g["members"]:
            if m["source"] and m["source"] not in srcs:
                srcs.add(m["source"])
                target.setdefault("also", []).append({"source": m["source"], "url": m["url"]})
        target["also"] = target.get("also", [])[:8]
        target["coverage"] = len(srcs)
        if target["coverage"] >= 3:
            target["importance"] = max(target.get("importance", 1), 2)
        if target["coverage"] >= 5:
            target["importance"] = 3
        target["keys"] = sorted(set(target.get("keys", [])) | {k for m in g["members"] for k in (m["tk"], m["uk"])})
    return rest, merged


def select(cands, items, rules):
    for c in cands:
        c["score"] = rules.score(c)
    kept = [c for c in cands if c["score"] >= rules.min_score
            or (c["feed"].get("category") == "安全研究" and c["score"] >= rules.min_score / 2)]
    log(f"相关度达标 {len(kept)} 条（门槛 {rules.min_score:g} 分）")
    groups = cluster(kept)
    groups, merged = attach_to_existing(groups, items, rules)
    if merged:
        log(f"{merged} 组报道并入了已收录的事件")
    new = [build_item(g, rules) for g in groups]
    new.sort(key=lambda it: (it["importance"], it["coverage"], it["_score"]), reverse=True)
    per_feed, out = Counter(), []
    for it in new:
        if it["_cap"] and per_feed[it["_feed"]] >= int(it["_cap"]):
            continue
        per_feed[it["_feed"]] += 1
        out.append(it)
        if len(out) >= rules.max_items:
            break
    return out


def fill_summaries(new):
    todo = [it for it in new if len(it["summary"]) < 120]
    if not FETCH_SUMMARIES or not todo:
        return
    log(f"读取 {len(todo)} 篇原文的简介")
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(page_description, it["url"], it["title"]): it for it in todo}
        got = 0
        for f in cf.as_completed(futs):
            try:
                d = f.result()
            except Exception:  # noqa: BLE001
                d = ""
            if d and len(d) > len(futs[f]["summary"]):
                futs[f]["summary"] = d
                got += 1
    log(f"  取到 {got} 篇")


# ================================================================ 今日要点

def make_brief(day_items):
    n = len(day_items)
    hi = sum(1 for it in day_items if it.get("importance") == 3)
    cats = Counter(it.get("category") for it in day_items).most_common()
    cat_text = "、".join(f"{c} {k} 条" for c, k in cats if c)
    zh = sum(1 for it in day_items if (it.get("lang") or lang_of(it.get("title", ""))) == "zh")
    parts = [f"当天收录 {n} 条资讯" + (f"，其中重大 {hi} 条" if hi else "") + "。"]
    if cat_text:
        parts.append(f"按类别分：{cat_text}。")
    top = max(day_items, key=lambda it: (it.get("coverage", 1), it.get("importance", 1)))
    if top.get("coverage", 1) >= 2:
        parts.append(f"报道来源最多的是「{top['title']}」，共 {top['coverage']} 家。")
    if 0 < zh < n:
        parts.append(f"中文报道 {zh} 条，外文报道 {n - zh} 条。")
    return "".join(parts)


# ================================================================ 存储

def merge_manual(items):
    manual = load_json(MANUAL_FILE, {}).get("items", [])
    by_id = {it["id"]: i for i, it in enumerate(items)}
    n = 0
    for m in manual:
        if not isinstance(m, dict) or not m.get("title") or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(m.get("date", ""))):
            continue
        try:
            imp = min(3, max(1, int(m.get("importance") or 2)))
        except (TypeError, ValueError):
            imp = 2
        item = {"id": "m-" + key_of(m["date"] + m["title"]), "date": m["date"], "title": m["title"],
                "summary": m.get("summary", ""), "url": m.get("url", ""), "source": m.get("source", ""), "also": [],
                "category": m.get("category") if m.get("category") in CATS else "行业动态",
                "regions": [g for g in (m.get("regions") or []) if g in REGIONS][:2] or ["其他"],
                "importance": imp, "lang": lang_of(m["title"]), "addedBy": "manual"}
        if item["id"] in by_id:
            items[by_id[item["id"]]].update(item)
        else:
            item["createdAt"] = now().isoformat(timespec="seconds")
            items.append(item)
            n += 1
    return n


def archive_old(items):
    cutoff = (now().date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    keep, old = [], {}
    for it in items:
        (keep if it["date"] >= cutoff else old.setdefault(it["date"][:7], [])).append(it)
    for month, lst in old.items():
        path = ARCHIVE_DIR / f"{month}.json"
        arc = load_json(path, {"items": []})
        ids = {x["id"] for x in arc["items"]}
        arc["items"].extend(x for x in lst if x["id"] not in ids)
        arc["items"].sort(key=lambda x: (x["date"], x.get("importance", 1)), reverse=True)
        save_json(path, arc)
    if old:
        log(f"归档 {sum(len(v) for v in old.values())} 条早于 {cutoff} 的资讯")
    return keep


def main():
    rules = Rules(load_json(RULES_FILE, {}))
    feeds = load_json(FEEDS_FILE, {}).get("feeds", [])
    store = load_json(ITEMS_FILE, {"items": []})
    items = [it for it in store.get("items", []) if isinstance(it, dict) and it.get("id")]
    days = load_json(DAYS_FILE, {})
    seen = load_json(SEEN_FILE, {})
    today = now().date().isoformat()

    for it in items:
        for k in it.get("keys") or []:
            seen.setdefault(k, it["date"])
        if it.get("url"):
            seen.setdefault(url_key(it["url"]), it["date"])

    cands = collect(feeds, seen)
    log(f"共 {len(cands)} 条新候选")
    new = select(cands, items, rules) if cands else []
    fill_summaries(new)
    for c in cands:  # 看过的候选不再重复处理，包括没被选中的
        seen[c["tk"]] = seen[c["uk"]] = today

    existing = {it["id"] for it in items}
    new = [{k: v for k, v in it.items() if not k.startswith("_")} for it in new if it["id"] not in existing]
    items = new + items
    n_manual = merge_manual(items)

    touched = sorted({it["date"] for it in new})
    for date in touched:
        if isinstance(days.get(date), dict) and days[date].get("manual"):
            continue  # 你手写的要点不覆盖
        day_items = [it for it in items if it["date"] == date]
        if len(day_items) >= 2:
            days[date] = {"brief": make_brief(day_items), "auto": True}

    items.sort(key=lambda x: (x["date"], x.get("importance", 1)), reverse=True)
    items = archive_old(items)
    cutoff = (now().date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    days = {d: v for d, v in days.items() if d >= cutoff}
    seen_cut = (now().date() - dt.timedelta(days=SEEN_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= seen_cut}

    save_json(ITEMS_FILE, {"updated": now().isoformat(timespec="seconds"), "items": items})
    save_json(DAYS_FILE, days)
    save_json(SEEN_FILE, seen)

    summary = f"新增 {len(new)} 条资讯，手动收录 {n_manual} 条，资讯库现有 {len(items)} 条。"
    log(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### 资讯更新\n\n{summary}\n\n")
            for it in new[:40]:
                fh.write(f"- {'★' * it['importance']} [{it['category']}] {it['title']}（{it['source']}"
                         + (f" 等 {it['coverage']} 家" if it.get("coverage", 1) > 1 else "") + "）\n")


if __name__ == "__main__":
    main()

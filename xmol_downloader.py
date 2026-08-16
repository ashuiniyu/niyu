#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X-MOL 订阅期刊更新下载器
========================

功能：
  - 使用浏览器 Cookie 登录 X-MOL (www.x-mol.com)
  - 抓取你订阅/关注的期刊最新文章目录 (TOC)
  - 提取每篇文章的元数据 (标题/作者/DOI/摘要/链接/日期)，保存为 JSON
  - 尝试下载文章 PDF (优先文章页直链，回退 Unpaywall 开放获取)
  - 用 SQLite 记录已下载文章，支持增量/定时重复运行，自动跳过已抓取内容

依赖：
  pip install requests beautifulsoup4 lxml
  (可选，当 X-MOL 登录后页面为纯 JS 渲染时需要):
  pip install playwright && playwright install chromium

使用示例：
  # 1) 首次：用 --save-html 抓取并保存原始页面，便于核对选择器
  python3 xmol_downloader.py --cookie "XMOL_COOKIE_..." --journals 50,134 --save-html

  # 2) 日常增量运行（可放入 cron）
  python3 xmol_downloader.py --cookie-file cookies.txt --journals 50,134 --days 7

  # 3) 若 requests 拿不到文章内容（JS 渲染），改用 playwright 引擎
  python3 xmol_downloader.py --cookie-file cookies.txt --journals 50 --engine playwright

获取 Cookie：
  浏览器登录 www.x-mol.com 后，F12 -> Network -> 刷新页面 -> 任选请求 ->
  复制 Request Header 中整行 Cookie 的值，写入 --cookie 或保存到 --cookie-file。

注意：
  X-MOL 页面结构可能随版本变化。若解析不到文章，请加 --save-html 查看实际
  HTML，并按需调整下方 SELECTORS 区域的选择器。PDF 受出版社版权限制，非 OA
  文章可能无法直接下载，脚本会记录 status=no_pdf。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote

# ----------------------------- 依赖检测 -----------------------------
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as e:
    sys.stderr.write(
        "缺少依赖：%s\n请运行：pip install requests beautifulsoup4 lxml\n" % e.name
    )
    sys.exit(2)


# ----------------------------- 站点常量 -----------------------------
BASE = "https://www.x-mol.com"
LOGIN_MARK = ("/login", "用户登录", "login-page")  # 用于判定 Cookie 是否失效

# 期刊 TOC URL 模板：{BASE}/paper/journal/{journal_id}
JOURNAL_TOC = BASE + "/paper/journal/{jid}"

# 文章详情 URL 正则：/paper/<纯数字> （排除 /paper/journal/... /paper/search 等）
ARTICLE_URL_RE = re.compile(r"^/paper/(\d+)(?:[/?#]|$)")

# DOI 正则
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+", re.I)

# Unpaywall 开放获取查询
UNPAYWALL_API = "https://api.unpaywall.org/v2/{doi}"

# 默认 UA
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ------------------- HTML 选择器（按实际页面调整） -------------------
# X-MOL 登录后的期刊 TOC 页面中，文章卡片大致结构。若抓不到内容，
# 用 --save-html 查看实际 HTML，再调整下面的选择器。
SELECTORS = {
    # 文章条目容器（每篇文章一个）
    "article_item": "div.paper-list-item, li.paper-item, div.qa-item, article",
    # 文章标题链接（href 指向 /paper/<id>）
    "title_link": "a[href*='/paper/']",
    # 期刊名
    "journal_name": ".journal-name, .paper-journal",
    # 出版日期文本（含 Pub Date 字样）
    "pub_date": ".pub-date, .paper-date",
}


# ============================ 工具函数 ============================
def setup_logger(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("xmol")


def load_cookie(args) -> str:
    """从 --cookie 或 --cookie-file 读取 Cookie 字符串。"""
    if args.cookie:
        return args.cookie.strip()
    if args.cookie_file:
        path = Path(args.cookie_file)
        if not path.exists():
            raise FileNotFoundError(f"Cookie 文件不存在: {path}")
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        # 支持 Netscape cookies.txt：取每行第6列以后拼接，或直接整行当 Cookie 头
        if text.startswith("#") or "\t" in text:
            pairs = []
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    pairs.append(f"{parts[5]}={parts[6]}")
            if pairs:
                return "; ".join(pairs)
        return text
    # 从环境变量
    env = os.environ.get("XMOL_COOKIE")
    if env:
        return env.strip()
    raise ValueError("未提供 Cookie，请用 --cookie / --cookie-file 或设置 XMOL_COOKIE")


def safe_slug(s: str, maxlen: int = 80) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fa5.-]", "_", s).strip("._") or "untitled"
    return s[:maxlen]


# ============================ HTTP 引擎 ============================
class RequestsEngine:
    """基于 requests 的抓取引擎（默认）。"""

    def __init__(self, cookie: str, logger: logging.Logger, sleep: float):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Cookie": cookie,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": BASE + "/",
            }
        )
        self.logger = logger
        self.sleep = sleep

    def _nap(self):
        if self.sleep:
            time.sleep(self.sleep)

    def get(self, url: str, params=None) -> str:
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=30, allow_redirects=True)
                resp.encoding = resp.apparent_encoding or "utf-8"
                self._nap()
                if self._is_login_page(resp):
                    raise RuntimeError(
                        "Cookie 已失效或未登录（被重定向到登录页）。请重新获取 Cookie。"
                    )
                return resp.text
            except requests.RequestException as e:
                self.logger.warning("请求失败(%d/3) %s: %s", attempt + 1, url, e)
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"多次重试仍失败: {url}")

    def download(self, url: str, dest: Path) -> bool:
        try:
            with self.session.get(url, stream=True, timeout=60, allow_redirects=True) as r:
                if r.status_code != 200 or "text/html" in r.headers.get("Content-Type", ""):
                    self.logger.debug("下载失败 status=%s ct=%s", r.status_code, r.headers.get("Content-Type"))
                    return False
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(8192):
                        if chunk:
                            f.write(chunk)
                return True
        except requests.RequestException as e:
            self.logger.warning("下载异常 %s: %s", url, e)
            return False

    @staticmethod
    def _is_login_page(resp) -> bool:
        url = resp.url
        body = resp.text[:4000]
        return any(m in url for m in ("/login",)) or "用户登录" in body and "login-page" in body


class PlaywrightEngine:
    """基于 Playwright 的抓取引擎（可选，用于纯 JS 渲染页面）。"""

    def __init__(self, cookie: str, logger: logging.Logger, sleep: float):
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "未安装 playwright，请运行: pip install playwright && playwright install chromium"
            )
        self.cookie = cookie
        self.logger = logger
        self.sleep = sleep

    def _cookies(self):
        # 将 Cookie 头解析为 playwright cookie 列表
        out = []
        for pair in self.cookie.split(";"):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            out.append(
                {"name": k.strip(), "value": v.strip(), "domain": ".x-mol.com", "path": "/"}
            )
        return out

    def get(self, url: str, params=None) -> str:
        from playwright.sync_api import sync_playwright

        if params:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(params)}"
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA)
            ctx.add_cookies(self._cookies())
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=60000)
            # 滚动加载懒加载内容
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            html = page.content()
            browser.close()
            if self.sleep:
                time.sleep(self.sleep)
            if "用户登录" in html[:4000] and "login-page" in html[:4000]:
                raise RuntimeError("Cookie 已失效或未登录（页面为登录页）。")
            return html

    def download(self, url: str, dest: Path) -> bool:
        # PDF 用 requests 引擎兜底下载更简单；此处复用 requests
        eng = RequestsEngine(self.cookie, self.logger, self.sleep)
        return eng.download(url, dest)


def make_engine(args, cookie, logger):
    if args.engine == "playwright":
        return PlaywrightEngine(cookie, logger, args.sleep)
    return RequestsEngine(cookie, logger, args.sleep)


# ============================ 数据库 ============================
def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,          -- XMOL 文章 URL 末段数字 id
            url TEXT,
            doi TEXT,
            title TEXT,
            journal_id TEXT,
            published TEXT,
            saved_at TEXT,
            pdf_path TEXT,
            status TEXT
        )
        """
    )
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, aid: str) -> bool:
    cur = conn.execute("SELECT 1 FROM articles WHERE id=?", (aid,))
    return cur.fetchone() is not None


def record(conn: sqlite3.Connection, **kw):
    conn.execute(
        "INSERT OR REPLACE INTO articles "
        "(id,url,doi,title,journal_id,published,saved_at,pdf_path,status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            kw["id"],
            kw.get("url"),
            kw.get("doi"),
            kw.get("title"),
            kw.get("journal_id"),
            kw.get("published"),
            kw.get("saved_at"),
            kw.get("pdf_path"),
            kw.get("status"),
        ),
    )
    conn.commit()


# ============================ 解析逻辑 ============================
def extract_article_id(href: str) -> str | None:
    m = ARTICLE_URL_RE.match(urlparse(href).path)
    return m.group(1) if m else None


def parse_toc(html: str, journal_id: str, logger: logging.Logger) -> list[dict]:
    """从期刊 TOC 页解析文章列表。返回 [{id,url,title,...}]"""
    soup = BeautifulSoup(html, "lxml")
    items = []

    # 策略：找所有指向 /paper/<数字> 的链接，去重，作为文章入口
    seen_ids = set()
    for a in soup.select(SELECTORS["title_link"]):
        href = a.get("href", "")
        aid = extract_article_id(href)
        if not aid or aid in seen_ids:
            continue
        seen_ids.add(aid)
        title = a.get_text(strip=True) or "(无标题)"
        items.append(
            {
                "id": aid,
                "url": urljoin(BASE, href),
                "title": title,
                "journal_id": journal_id,
            }
        )

    # 兜底：若选择器没命中，扫描全部 <a>
    if not items:
        for a in soup.find_all("a", href=True):
            aid = extract_article_id(a["href"])
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                items.append(
                    {
                        "id": aid,
                        "url": urljoin(BASE, a["href"]),
                        "title": a.get_text(strip=True) or "(无标题)",
                        "journal_id": journal_id,
                    }
                )

    logger.info("期刊 %s 解析到 %d 篇文章", journal_id, len(items))
    return items


def parse_article_detail(html: str, base_meta: dict) -> dict:
    """从文章详情页提取完整元数据。"""
    soup = BeautifulSoup(html, "lxml")
    meta = dict(base_meta)

    # 标题：优先 <h1> 或 og:title
    if not meta.get("title") or meta["title"] == "(无标题)":
        h1 = soup.find("h1")
        if h1:
            meta["title"] = h1.get_text(strip=True)
        og = soup.find("meta", property="og:title")
        if og:
            meta["title"] = og.get("content", meta["title"])

    # DOI：正则扫描全文
    text = soup.get_text(" ", strip=True)
    m = DOI_RE.search(text)
    if m:
        meta["doi"] = m.group(0).rstrip(".,;)")

    # 摘要：常见容器
    abstract = ""
    for sel in (
        ".abstract",
        "#abstract",
        "[class*=abstract]",
        "[class*=Abstract]",
    ):
        node = soup.select_one(sel)
        if node:
            abstract = node.get_text(" ", strip=True)
            break
    meta["abstract"] = abstract

    # 作者
    authors = []
    for sel in (".author", "[class*=author]", "[itemprop='author']"):
        nodes = soup.select(sel)
        if nodes:
            authors = [n.get_text(strip=True) for n in nodes if n.get_text(strip=True)]
            break
    meta["authors"] = authors

    # 出版日期
    if not meta.get("published"):
        for sel in ("[class*=date]", "time", "[datetime]"):
            node = soup.select_one(sel)
            if node:
                meta["published"] = node.get("datetime") or node.get_text(strip=True)
                break

    # PDF 直链：找 href 含 .pdf 或 pdf 按钮
    pdf_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.pdf($|\?)", href, re.I) or "pdf" in a.get_text(strip=True).lower():
            pdf_url = urljoin(BASE, href)
            break
    meta["pdf_url"] = pdf_url

    # 期刊名
    jnode = soup.select_one(SELECTORS["journal_name"])
    meta["journal_name"] = jnode.get_text(strip=True) if jnode else ""

    return meta


def fetch_oa_pdf(doi: str, email: str, engine, logger: logging.Logger) -> str | None:
    """通过 Unpaywall 查询开放获取 PDF 地址。"""
    if not doi or not email:
        return None
    try:
        url = UNPAYWALL_API.format(doi=quote(doi, safe="/"))
        resp = requests.get(url, params={"email": email}, timeout=20)
        data = resp.json()
        loc = data.get("best_oa_location") or {}
        return loc.get("url_for_pdf") or loc.get("url")
    except Exception as e:
        logger.debug("Unpaywall 查询失败 %s: %s", doi, e)
        return None


# ============================ 主流程 ============================
def process_journal(jid: str, args, engine, conn, logger: logging.Logger) -> int:
    url = JOURNAL_TOC.format(jid=jid)
    logger.info("抓取期刊 TOC: %s", url)
    html = engine.get(url)

    if args.save_html:
        dbg = Path(args.out) / "_debug" / f"journal_{jid}.html"
        dbg.parent.mkdir(parents=True, exist_ok=True)
        dbg.write_text(html, encoding="utf-8")
        logger.info("已保存调试 HTML: %s", dbg)

    articles = parse_toc(html, jid, logger)
    if not articles:
        logger.warning(
            "期刊 %s 未解析到文章。可能原因：Cookie 失效 / 页面为 JS 渲染(改用 --engine playwright) / 选择器需调整(用 --save-html 查看)。",
            jid,
        )
        return 0

    if args.limit:
        articles = articles[: args.limit]

    n_done = 0
    for art in articles:
        aid = art["id"]
        if is_seen(conn, aid):
            logger.debug("跳过已下载: %s", art["url"])
            continue
        try:
            detail_html = engine.get(art["url"])
            if args.save_html:
                dbg = Path(args.out) / "_debug" / f"article_{aid}.html"
                dbg.parent.mkdir(parents=True, exist_ok=True)
                dbg.write_text(detail_html, encoding="utf-8")
            meta = parse_article_detail(detail_html, art)
        except Exception as e:
            logger.warning("抓取文章详情失败 %s: %s", art["url"], e)
            record(conn, id=aid, url=art["url"], title=art.get("title", ""),
                   journal_id=jid, saved_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                   status="detail_failed")
            continue

        # 落盘元数据 JSON
        ym = (meta.get("published") or time.strftime("%Y-%m"))[:7]
        out_dir = Path(args.out) / f"journal_{jid}" / ym
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = safe_slug(meta.get("doi") or meta.get("title") or aid)
        json_path = out_dir / f"{slug}.json"
        json_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 下载 PDF
        status = "meta_only"
        pdf_path = None
        if not args.no_pdf:
            pdf_url = meta.get("pdf_url") or fetch_oa_pdf(
                meta.get("doi", ""), args.unpaywall_email, engine, logger
            )
            if pdf_url:
                pdf_path = out_dir / f"{slug}.pdf"
                if engine.download(pdf_url, pdf_path):
                    status = "ok"
                    logger.info("已下载 PDF: %s", pdf_path.name)
                else:
                    status = "pdf_failed"
                    pdf_path = None
            else:
                status = "no_pdf"

        record(
            conn,
            id=aid,
            url=meta["url"],
            doi=meta.get("doi"),
            title=meta.get("title"),
            journal_id=jid,
            published=meta.get("published"),
            saved_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            pdf_path=str(pdf_path) if pdf_path else None,
            status=status,
        )
        logger.info("[%s] %s — %s", status, meta.get("doi") or aid, meta.get("title", "")[:50])
        n_done += 1

    return n_done


def main():
    ap = argparse.ArgumentParser(
        description="X-MOL 订阅期刊更新下载器（Cookie 登录 + 元数据 + PDF + 增量去重）"
    )
    ap.add_argument("--cookie", help="登录后的 Cookie 字符串（或用 --cookie-file / 环境变量 XMOL_COOKIE）")
    ap.add_argument("--cookie-file", help="Cookie 文件路径（纯文本 Cookie 头 或 Netscape cookies.txt）")
    ap.add_argument(
        "--journals",
        help="期刊 ID 列表（逗号分隔），对应 /paper/journal/<id>；或设置环境变量 XMOL_JOURNALS",
    )
    ap.add_argument("--out", default="./xmol_data", help="输出目录（默认 ./xmol_data）")
    ap.add_argument("--db", default=None, help="SQLite 路径（默认 <out>/xmol.sqlite）")
    ap.add_argument("--limit", type=int, default=0, help="每个期刊最多处理文章数（0=不限）")
    ap.add_argument("--no-pdf", action="store_true", help="不下载 PDF，仅抓元数据")
    ap.add_argument("--unpaywall-email", default="", help="Unpaywall 查询邮箱（用于 OA PDF 回退）")
    ap.add_argument("--engine", choices=["requests", "playwright"], default="requests",
                    help="抓取引擎（默认 requests；JS 渲染严重时用 playwright）")
    ap.add_argument("--sleep", type=float, default=1.0, help="每次请求间隔秒数（限速）")
    ap.add_argument("--save-html", action="store_true", help="保存原始 HTML 供调试选择器")
    ap.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    args = ap.parse_args()

    logger = setup_logger(args.verbose)

    cookie = load_cookie(args)
    jids_str = args.journals or os.environ.get("XMOL_JOURNALS", "")
    if not jids_str:
        ap.error("请通过 --journals 或环境变量 XMOL_JOURNALS 提供期刊 ID")
    jids = [j.strip() for j in jids_str.split(",") if j.strip()]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db) if args.db else out_dir / "xmol.sqlite"
    conn = init_db(db_path)

    engine = make_engine(args, cookie, logger)

    logger.info("开始抓取 %d 个期刊，输出目录: %s", len(jids), out_dir)
    total = 0
    for jid in jids:
        try:
            total += process_journal(jid, args, engine, conn, logger)
        except RuntimeError as e:
            logger.error("期刊 %s 失败: %s", jid, e)
            if "Cookie" in str(e):
                break
        except Exception as e:
            logger.exception("期刊 %s 异常: %s", jid, e)

    conn.close()
    logger.info("完成。本次新增 %d 篇。", total)


if __name__ == "__main__":
    main()

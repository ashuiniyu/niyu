#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XMOL 期刊新文献监控 + 开放获取(OA)下载 + 本地 PDF 整理

三个功能：
  1. fetch     通过 XMOL 检索接口抓取指定期刊的最新文献（标题/作者/DOI/摘要链接）
  2. download  在 fetch 基础上，按 DOI 查询 Unpaywall，仅下载开放获取 PDF
  3. organize  整理本地已合法下载的 PDF（按期刊/年份归类并规范化命名）

依赖：见 requirements.txt
用法：
  python xmol_monitor.py fetch
  python xmol_monitor.py download
  python xmol_monitor.py organize --src /path/to/pdfs --index papers_index.json

说明：
  - XMOL 检索接口需要登录 Cookie，请填在 config.json 的 "cookie" 字段（不要发到聊天里）。
  - Cookie 获取方式：浏览器登录 https://www.x-mol.com 后，F12 -> Network 任意请求 ->
    复制 Request Headers 里完整的 Cookie 值。
  - XMOL 页面结构 / 接口可能调整，如抓不到数据请用 --debug 保存 HTML 自行核对选择器。
"""

import argparse
import json
import random
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.x-mol.com/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.x-mol.com/",
}

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def load_config(path="config.json"):
    path = Path(path)
    if not path.exists():
        print(f"[错误] 找不到配置文件 {path}，请先复制并填写 config.json")
        sys.exit(1)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def cookie_to_dict(cookie_str):
    """把 'k1=v1; k2=v2' 形式的 Cookie 字符串转成字典。"""
    d = {}
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def safe_filename(name):
    name = re.sub(r'[\\/*?:"<>|]', "_", str(name))
    name = re.sub(r"\s+", " ", name).strip()
    return name[:160] if name else "paper"


def unique_path(path: Path) -> Path:
    """目标已存在时自动追加序号，避免覆盖。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    return path


# --------------------------------------------------------------------------- #
# 1. XMOL 检索（抓标题 / 作者 / DOI / 摘要链接）
# --------------------------------------------------------------------------- #
def parse_search_result(html: str):
    """解析 XMOL 检索结果页，返回文献列表。"""
    soup = BeautifulSoup(html, "lxml")
    container = soup.find("div", {"class": "magazine-senior-search-results-list"})
    if not container:
        return []

    papers = []
    for li in container.find_all("li"):
        title_el = li.find("div", {"class": "it-bold space-bottom-m10"})
        title = title_el.get_text(" ", strip=True) if title_el else ""

        info_divs = li.find_all("div", {"class": "div-text-line-one it-new-gary"})
        info_text = info_divs[0].get_text(" ", strip=True) if info_divs else ""

        journal = ""
        if info_divs:
            journal_el = info_divs[0].find("em", {"class": "it-blue"})
            if journal_el:
                journal = journal_el.get_text(strip=True)

        doi = ""
        m = re.search(r"DOI\s*:\s*(\S+)", info_text, re.IGNORECASE)
        if m:
            doi = m.group(1).rstrip(".,;")

        pub_date = ""
        m2 = re.search(r"Pub Date\s*:\s*(\d{4}-\d{2}-\d{2})", info_text, re.IGNORECASE)
        if m2:
            pub_date = m2.group(1)

        # 作者一般位于第二个同类 div
        authors = ""
        if len(info_divs) > 1:
            authors = info_divs[1].get_text(" ", strip=True)

        abstract_link = ""
        for a in li.find_all("a"):
            href = a.get("href") or ""
            if "/paper/" in href:
                abstract_link = href
                break
        if not abstract_link:
            anchors = li.find_all("a")
            if len(anchors) > 3:
                abstract_link = anchors[3].get("href") or ""
        if abstract_link.startswith("/"):
            abstract_link = BASE.rstrip("/") + abstract_link

        if title or doi:
            papers.append({
                "title": title,
                "authors": authors,
                "journal": journal,
                "doi": doi,
                "pub_date": pub_date,
                "abstract_link": abstract_link,
            })
    return papers


def fetch_xmol_papers(cfg, journal, keyword="", days=7, pages=3, debug=False):
    """通过 XMOL 检索接口抓取指定期刊近 N 天的新文献。"""
    cookies = cookie_to_dict(cfg.get("cookie", ""))
    session = requests.Session()
    session.headers.update(HEADERS)

    end = datetime.now().date()
    start = end - timedelta(days=days)

    post_data = {
        "keywordsRange": "2",
        "keywordList[0].operator": "",
        "keywordList[0].option": keyword,
        "authorList[0].option": "",
        "affiliation": "",
        "journals[0]": journal,
        "journals[1]": "",
        "journals[2]": "",
        "journals[3]": "",
        "journals[4]": "",
        "publishDateStart": start.isoformat(),
        "publishDateEnd": end.isoformat(),
        "impactFactorStart": "",
        "impactFactorEnd": "",
    }

    search_url = f"{BASE}paper/search/searchPaper?date={random.randint(333, 999)}"
    resp = session.post(search_url, data=post_data, cookies=cookies)
    m = re.search(r"searchLogId=([^&]+)", resp.url)
    if not m:
        return [], f"无法从响应 URL 提取 searchLogId：{resp.url}（请检查 Cookie 是否有效）"

    log_id = m.group(1)
    papers = []
    for page in range(1, pages + 1):
        result_url = (
            f"{BASE}paper/search/result?searchLogId={log_id}"
            f"&readMode=en&searchSort=publishDate&pageIndex={page}"
        )
        r = session.get(result_url, cookies=cookies)
        r.encoding = "utf-8"
        if debug:
            Path(f"debug_page{page}.html").write_text(r.text, encoding="utf-8")
        if r.status_code != 200:
            break
        batch = parse_search_result(r.text)
        if not batch:
            break
        papers.extend(batch)
        time.sleep(1.5)

    # 去重（按 DOI，其次按标题）
    seen, uniq = set(), []
    for p in papers:
        key = p["doi"] or p["title"].lower()
        if key and key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq, None


# --------------------------------------------------------------------------- #
# 2. 开放获取查询与下载（Unpaywall）
# --------------------------------------------------------------------------- #
def query_unpaywall(doi, email):
    url = f"https://api.unpaywall.org/v2/{doi}"
    params = {"email": email}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def pick_pdf_url(data):
    best = data.get("best_oa_location") or {}
    if best.get("url_for_pdf"):
        return best["url_for_pdf"]
    for loc in data.get("oa_locations", []):
        if loc.get("url_for_pdf"):
            return loc["url_for_pdf"]
    return None


def download_pdf(url, save_path: Path) -> bool:
    try:
        with requests.get(url, headers=HEADERS, timeout=60, stream=True,
                          allow_redirects=True) as r:
            if r.status_code != 200:
                return False
            ctype = r.headers.get("Content-Type", "").lower()
            if "pdf" not in ctype and not str(r.url).lower().endswith(".pdf"):
                return False
            with open(save_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except requests.RequestException:
        return False


# --------------------------------------------------------------------------- #
# 3. 本地 PDF 整理
# --------------------------------------------------------------------------- #
def extract_doi_from_pdf(path: Path):
    """优先从文件名提取 DOI，失败则读取前两页文本。"""
    m = DOI_RE.search(path.name)
    if m:
        return m.group(0).rstrip(".,;")

    reader_cls = None
    try:
        from pypdf import PdfReader
        reader_cls = PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
            reader_cls = PdfReader
        except ImportError:
            return None

    try:
        reader = reader_cls(str(path))
        for page in reader.pages[:2]:
            text = page.extract_text() or ""
            m = DOI_RE.search(text)
            if m:
                return m.group(0).rstrip(".,;")
    except Exception:
        return None
    return None


def organize_pdfs(src, dest, index=None, mode="move"):
    """扫描 PDF，按 期刊/年份 归类并规范化命名。

    index: 可选，DOI -> {title, journal, pub_date} 的映射（通常来自 download 步骤的 papers_index.json）。
    """
    src, dest = Path(src), Path(dest)
    if not src.exists():
        print(f"[错误] 源目录不存在：{src}")
        return

    meta = {}
    if index:
        ip = Path(index)
        if ip.exists():
            with ip.open("r", encoding="utf-8") as f:
                meta = json.load(f)

    pdfs = sorted(src.rglob("*.pdf"))
    print(f"发现 {len(pdfs)} 个 PDF")
    for pdf in pdfs:
        doi = extract_doi_from_pdf(pdf)
        info = meta.get(doi) if doi else None

        if info:
            journal = safe_filename(info.get("journal") or "unknown")
            year = (info.get("pub_date") or "unknown")[:4]
            title = safe_filename(info.get("title") or pdf.stem)
            subdir = dest / journal / year
            target = subdir / f"{title}.pdf"
        else:
            subdir = dest / "未识别"
            target = subdir / safe_filename(pdf.name)

        target = unique_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "move":
            shutil.move(str(pdf), str(target))
        else:
            shutil.copy2(str(pdf), str(target))
        print(f"  {pdf.name} -> {target.relative_to(dest)}")
    print("整理完成，输出目录：", dest)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def cmd_fetch(args):
    cfg = load_config(args.config)
    all_papers = []
    for journal in cfg.get("journals", []):
        print(f"[fetch] 期刊：{journal}")
        papers, err = fetch_xmol_papers(
            cfg, journal,
            keyword=cfg.get("keyword", ""),
            days=cfg.get("days", 7),
            pages=cfg.get("pages", 3),
            debug=args.debug,
        )
        if err:
            print(f"  [错误] {err}")
            continue
        print(f"  抓取到 {len(papers)} 篇")
        all_papers.extend(papers)

    out = Path(args.output)
    out.write_text(json.dumps(all_papers, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存到 {out}")


def cmd_download(args):
    cfg = load_config(args.config)
    email = cfg.get("unpaywall_email", "")
    dl_dir = Path(cfg.get("download_dir", "downloads"))
    dl_dir.mkdir(parents=True, exist_ok=True)

    all_papers = []
    for journal in cfg.get("journals", []):
        print(f"[download] 期刊：{journal}")
        papers, err = fetch_xmol_papers(
            cfg, journal,
            keyword=cfg.get("keyword", ""),
            days=cfg.get("days", 7),
            pages=cfg.get("pages", 3),
            debug=args.debug,
        )
        if err:
            print(f"  [错误] {err}")
            continue
        all_papers.extend(papers)

    index = {}
    results = []
    for i, p in enumerate(all_papers, 1):
        doi = p["doi"]
        print(f"[{i}/{len(all_papers)}] {p['title'][:50]}")
        item = dict(p)
        item.update({"is_oa": False, "pdf_url": None, "downloaded": False, "file": None})

        if doi:
            data = query_unpaywall(doi, email)
            if data:
                pdf_url = pick_pdf_url(data)
                item["is_oa"] = bool(pdf_url)
                item["pdf_url"] = pdf_url
                if pdf_url and cfg.get("download_oa", True):
                    target = dl_dir / f"{safe_filename(p['title'])}.pdf"
                    target = unique_path(target)
                    ok = download_pdf(pdf_url, target)
                    item["downloaded"] = ok
                    if ok:
                        item["file"] = str(target)
                        print(f"    -> 已下载 {target}")
                    else:
                        print("    -> 发现 OA 链接但下载失败")
                elif pdf_url:
                    print(f"    -> 开放获取：{pdf_url}")
            else:
                print("    -> Unpaywall 查询失败")
            index[doi] = {
                "title": p["title"],
                "journal": p["journal"],
                "pub_date": p["pub_date"],
            }
            time.sleep(1)
        else:
            print("    -> 无 DOI，跳过")
        results.append(item)

    Path("papers_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    n_ok = sum(1 for r in results if r["downloaded"])
    print(f"\n完成：共 {len(results)} 篇，其中下载成功 {n_ok} 篇。")
    print("索引已写入 papers_index.json，结果写入 results.json")


def cmd_organize(args):
    organize_pdfs(args.src, args.dest, index=args.index, mode=args.mode)


def main():
    parser = argparse.ArgumentParser(description="XMOL 期刊监控 + OA 下载 + PDF 整理")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="只抓取文献元数据")
    p_fetch.add_argument("--config", default="config.json")
    p_fetch.add_argument("--output", default="papers.json")
    p_fetch.add_argument("--debug", action="store_true", help="保存返回 HTML 以便调试")

    p_dl = sub.add_parser("download", help="抓取并下载开放获取 PDF")
    p_dl.add_argument("--config", default="config.json")
    p_dl.add_argument("--debug", action="store_true")

    p_org = sub.add_parser("organize", help="整理本地 PDF")
    p_org.add_argument("--src", required=True, help="PDF 所在目录")
    p_org.add_argument("--dest", default="organized")
    p_org.add_argument("--index", default="papers_index.json", help="DOI 索引文件（可选）")
    p_org.add_argument("--mode", choices=["move", "copy"], default="move")

    args = parser.parse_args()
    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "download":
        cmd_download(args)
    elif args.command == "organize":
        cmd_organize(args)


if __name__ == "__main__":
    main()

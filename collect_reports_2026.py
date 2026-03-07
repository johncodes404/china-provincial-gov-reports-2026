#!/usr/bin/env python3
"""
2026 省级政府工作报告阶段一采集器

能力范围：
1) 首次人工登录并保存 Playwright 会话
2) 人民数据优先抓取
3) 抓取失败时自动回退到官方站检索补齐
4) 输出 31 个分省 CSV（每省 1 行）
5) 生成失败清单与运行摘要
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Page, sync_playwright


DEFAULT_LIST_URL = (
    "https://s.fjlib.net:6443/interlibSSO/goto/385/+c-s-9odnokd9bnl9bm/"
    "pd/gzbg/list.html?qs={%22cId%22:36,%22cds%22:[{%22cdr%22:%22AND%22,"
    "%22fld%22:%22class1%22,%22val%22:%22%E5%90%84%E5%9C%B0%E6%94%BF%E5%BA%9C"
    "%E5%B7%A5%E4%BD%9C%E6%8A%A5%E5%91%8A%22}]}&id=326&index=9"
)
DEFAULT_LOGIN_URL = "https://s.fjlib.net:6443/interlibSSO/main"
DEFAULT_ENTRY_URL = "https://www.fjlib.net/"
DEFAULT_READER_URL = "https://s.fjlib.net:6443/interlibSSO/main/main.jsp"
CSV_FIELDS = ["province", "year", "title", "source_url", "publish_date", "full_text"]

NEGATIVE_TITLE_WORDS = (
    "解读",
    "摘要",
    "图解",
    "新闻发布会",
    "说明",
    "直播",
    "快讯",
    "审议",
    "答记者问",
    "评论",
    "短评",
    "讲话",
    "通知",
    "会议纪要",
)

LOGIN_PAGE_HINTS = ("请输入证号", "OnecardLoginServlet", "电子资源馆外访问系统", "验证码")


@dataclass(frozen=True)
class ProvinceSpec:
    province: str
    short_name: str
    aliases: List[str]


@dataclass
class CandidateLink:
    title: str
    url: str
    publish_date: str
    context: str


@dataclass
class ReportRecord:
    province: str
    year: int
    title: str
    source_url: str
    publish_date: str
    full_text: str
    source_type: str


RecordCallback = Optional[Callable[[ReportRecord], None]]
PRIORITY_CONTENT_SELECTORS = (
    ".article-content",
    ".DocHtmlCon",
    ".TRS_UEDITOR",
    ".trs_editor_view",
    ".view",
    ".conm",
    ".m-cente-detail",
    ".m-new-main-content",
    "td.zhengwen",
    ".content",
    ".detail-content",
    ".news_content",
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="2026 省级政府工作报告采集器")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-session", help="初始化并保存登录会话")
    _add_common_args(init_parser)
    init_parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式打开浏览器（初始化会话时一般不要开启）",
    )
    init_parser.add_argument(
        "--wait-seconds",
        type=int,
        default=900,
        help="等待人工登录的最长秒数",
    )

    run_parser = subparsers.add_parser("run", help="执行采集并输出 CSV")
    _add_common_args(run_parser)
    run_parser.add_argument("--headless", action="store_true", help="运行抓取时启用无头模式")
    run_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output") / "2026_raw_text",
        help="分省 CSV 输出目录",
    )
    run_parser.add_argument(
        "--manual-fallback-csv",
        type=Path,
        default=Path("manual_fallback_urls.csv"),
        help="可选的人工补齐 URL 映射（列名：province,url）",
    )
    run_parser.add_argument(
        "--max-pages-per-province",
        type=int,
        default=6,
        help="人民数据每省最多翻页数",
    )
    run_parser.add_argument(
        "--min-text-length",
        type=int,
        default=2000,
        help="正文最小长度阈值（低于该值视为异常）",
    )
    run_parser.add_argument(
        "--disable-official-fallback",
        action="store_true",
        help="禁用官方站补齐",
    )
    run_parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="忽略现有分省 CSV，强制重新抓取全部省份",
    )

    check_parser = subparsers.add_parser("validate", help="校验输出目录完整性")
    check_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output") / "2026_raw_text",
        help="待校验的输出目录",
    )
    check_parser.add_argument(
        "--provinces-file",
        type=Path,
        default=Path("provinces_31.json"),
        help="省份配置 JSON 文件",
    )
    check_parser.add_argument(
        "--min-text-length",
        type=int,
        default=2000,
        help="正文最小长度阈值",
    )

    return parser.parse_args()


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--list-url",
        default=DEFAULT_LIST_URL,
        help="人民数据省级报告列表页 URL",
    )
    parser.add_argument(
        "--login-url",
        default=DEFAULT_LOGIN_URL,
        help="SSO 登录页 URL",
    )
    parser.add_argument(
        "--entry-url",
        default=DEFAULT_ENTRY_URL,
        help="福建省图书馆官网入口 URL",
    )
    parser.add_argument(
        "--reader-url",
        default=DEFAULT_READER_URL,
        help="官网数字资源入口 URL",
    )
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=Path("state") / "peopledata_state.json",
        help="Playwright 会话文件路径",
    )
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        default=Path("state") / "persistent_chromium",
        help="持久化浏览器用户目录",
    )
    parser.add_argument(
        "--provinces-file",
        type=Path,
        default=Path("provinces_31.json"),
        help="省份配置 JSON 文件",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path("logs") / "collect_2026.log",
        help="日志文件路径",
    )


def setup_logger(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("collect_reports_2026")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    )

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    return logger


def load_provinces(path: Path) -> List[ProvinceSpec]:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    provinces: List[ProvinceSpec] = []
    for item in raw:
        provinces.append(
            ProvinceSpec(
                province=item["province"],
                short_name=item["short_name"],
                aliases=list(dict.fromkeys(item["aliases"])),
            )
        )
    return provinces


def normalize_date(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    text = text.replace("年", "-").replace("月", "-").replace("日", "")
    text = text.replace("/", "-").replace(".", "-")
    text = re.sub(r"\s+", "", text)
    m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
    if not m:
        return ""
    year, month, day = m.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def extract_first_date(text: str) -> str:
    if not text:
        return ""
    patterns = [
        r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}",
        r"20\d{2}年\d{1,2}月\d{1,2}日",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return normalize_date(m.group(0))
    return ""


def clean_paragraph_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    compact = [line for line in lines if line]
    cleaned = "\n".join(compact)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def decode_response_html(resp: requests.Response) -> str:
    encoding = (resp.encoding or "").lower()
    apparent = (resp.apparent_encoding or "").lower()
    chosen = apparent or encoding or "utf-8"
    if encoding and encoding not in ("iso-8859-1", "latin-1"):
        chosen = encoding
    try:
        return resp.content.decode(chosen, errors="ignore")
    except Exception:
        return resp.text


def trim_report_text(text: str) -> str:
    if not text:
        return ""
    for marker in ("政府工作报告", "各位代表："):
        idx = text.find(marker)
        if idx != -1 and idx <= 300:
            text = text[idx:]
            break
    text = re.sub(r"^\s*发布日期[:：]?\s*\d{4}-\d{2}-\d{2}.*?\n", "", text)
    return clean_paragraph_text(text)


def extract_main_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(
        ["script", "style", "noscript", "iframe", "svg", "form", "nav", "footer", "header", "aside"]
    ):
        tag.decompose()

    for selector in PRIORITY_CONTENT_SELECTORS:
        nodes = soup.select(selector)
        if not nodes:
            continue
        texts = []
        for node in nodes:
            for br in node.find_all("br"):
                br.replace_with("\n")
            text = node.get_text("\n", strip=True)
            if len(text) >= 200:
                texts.append(text)
        if texts:
            longest = max(texts, key=len)
            return trim_report_text(longest)

    body = soup.body or soup
    candidates = []
    marker = re.compile(r"(content|article|main|text|detail|正文|article-content)", re.IGNORECASE)
    for node in body.find_all(True):
        marker_text = " ".join(node.get("class", [])) + " " + str(node.get("id", ""))
        if marker.search(marker_text):
            text = node.get_text("\n", strip=True)
            if len(text) >= 200:
                candidates.append((len(text), node))

    # 关键逻辑：优先选择“正文容器”最长块，降低导航噪声混入。
    target = max(candidates, key=lambda x: x[0])[1] if candidates else body
    for br in target.find_all("br"):
        br.replace_with("\n")

    text = target.get_text("\n", strip=True)
    return trim_report_text(text)


def extract_page_title_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for selector in (
        "h1",
        ".article-title",
        ".detail-title",
        ".arti_title",
        ".news-title",
        ".bt",
        "h2",
        ".title",
    ):
        for node in soup.select(selector):
            value = clean_paragraph_text(node.get_text(" ", strip=True))
            if not value:
                continue
            if "首页 >" in value or value.startswith("首页 >"):
                continue
            if "政府工作报告" in value or len(value) >= 10:
                return value
    if soup.title and soup.title.string:
        title = clean_paragraph_text(soup.title.string)
        title = re.split(r"[_|\-—]", title)[0].strip()
        return title
    return ""


def title_has_negative_words(title: str) -> bool:
    return any(word in title for word in NEGATIVE_TITLE_WORDS)


def title_matches_province(title: str, province: ProvinceSpec, year: int = 2026) -> bool:
    if not title:
        return False
    if str(year) not in title or "政府工作报告" not in title:
        return False
    if title_has_negative_words(title):
        return False
    return any(alias in title for alias in province.aliases)


def candidate_text(candidate: CandidateLink) -> str:
    return clean_paragraph_text(f"{candidate.title}\n{candidate.context}")


def candidate_matches_province(candidate: CandidateLink, province: ProvinceSpec, year: int = 2026) -> bool:
    return title_matches_province(candidate_text(candidate), province, year)


def score_title(title: str, province: ProvinceSpec, year: int = 2026) -> int:
    score = 0
    if str(year) in title:
        score += 20
    if "政府工作报告" in title:
        score += 30
    if title_has_negative_words(title):
        score -= 60
    if province.province in title:
        score += 40
    if province.short_name in title:
        score += 20
    if title.startswith(f"{year}年{province.province}政府工作报告"):
        score += 40
    return score


def score_candidate(candidate: CandidateLink, province: ProvinceSpec, year: int = 2026) -> int:
    return score_title(candidate_text(candidate), province, year)


def is_login_page_html(html: str) -> bool:
    return any(hint in html for hint in LOGIN_PAGE_HINTS)


def is_authenticated_page(html: str, url: str) -> bool:
    if is_login_page_html(html):
        return False
    markers = ("欢迎您", "退出登录", "个人中心", "搜索中心", "人民数据")
    if any(marker in html for marker in markers):
        return True
    if "s.fjlib.net:6443" in url and ("/goto/" in url or "/pd/" in url or "/main/main.jsp" in url):
        return True
    return False


def score_record(record: ReportRecord, province: ProvinceSpec, min_text_length: int, year: int = 2026) -> int:
    score = score_title(record.title, province, year)
    if record.publish_date:
        score += 15
    if len(record.full_text) >= min_text_length:
        score += 40
    else:
        score -= 30
    return score


def normalize_report_title(title: str, province: ProvinceSpec, year: int = 2026) -> str:
    value = clean_paragraph_text(title)
    if not value:
        return f"{year}年{province.province}政府工作报告"
    value = re.split(r"\s*[-|_]\s*", value)[0].strip()
    value = value.replace("（全文）", "").replace("(全文)", "")
    value = re.sub(r"[，,]?\s*全文来了！？?$", "", value)
    if "政府工作报告" not in value:
        return f"{year}年{province.province}政府工作报告"
    if not any(alias in value for alias in province.aliases):
        return f"{year}年{province.province}政府工作报告"
    if not value.startswith(f"{year}年"):
        return f"{year}年{province.province}政府工作报告"
    return value


def read_manual_fallback_urls(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    result: Dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            province = (row.get("province") or "").strip()
            url = (row.get("url") or "").strip()
            if province and url:
                result[province] = url
    return result


def load_existing_records(output_dir: Path, provinces: Sequence[ProvinceSpec]) -> Dict[str, ReportRecord]:
    records: Dict[str, ReportRecord] = {}
    if not output_dir.exists():
        return records

    for province in provinces:
        file_path = output_dir / f"2026_{province.province}_政府工作报告.csv"
        if not file_path.exists():
            continue
        try:
            with file_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except Exception:
            continue
        if reader.fieldnames != CSV_FIELDS or len(rows) != 1:
            continue
        row = rows[0]
        full_text = row.get("full_text", "")
        if not full_text:
            continue
        records[province.province] = ReportRecord(
            province=row.get("province", province.province),
            year=int(row.get("year", "2026") or 2026),
            title=row.get("title", ""),
            source_url=row.get("source_url", ""),
            publish_date=row.get("publish_date", ""),
            full_text=full_text,
            source_type="existing_output",
        )
    return records


class PeopleDataCollector:
    def __init__(
        self,
        list_url: str,
        login_url: str,
        entry_url: str,
        reader_url: str,
        storage_state: Path,
        user_data_dir: Path,
        max_pages_per_province: int,
        min_text_length: int,
        logger: logging.Logger,
        timeout_ms: int = 45000,
    ):
        self.list_url = list_url
        self.login_url = login_url
        self.entry_url = entry_url
        self.reader_url = reader_url
        self.storage_state = storage_state
        self.user_data_dir = user_data_dir
        self.max_pages_per_province = max_pages_per_province
        self.min_text_length = min_text_length
        self.timeout_ms = timeout_ms
        self.logger = logger

    def init_session(self, headless: bool = False, wait_seconds: int = 900) -> None:
        self.storage_state.parent.mkdir(parents=True, exist_ok=True)
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=headless,
            )
            page = context.pages[0] if context.pages else context.new_page()
            # 关键逻辑：先从福建省图书馆官网进入，再跳转到数字资源入口。
            page.goto(self.entry_url, wait_until="commit", timeout=self.timeout_ms)
            page.wait_for_timeout(3000)
            page.goto(self.reader_url, wait_until="commit", timeout=self.timeout_ms)
            print("请在浏览器中完成登录（含验证码），脚本会自动检测并保存会话...")

            # 关键逻辑：轮询页面状态，检测从登录页进入目标列表页后自动持久化会话。
            deadline = time.time() + wait_seconds
            saved = False
            while time.time() < deadline:
                page.wait_for_timeout(3000)

                pages = [pg for pg in context.pages if not pg.is_closed()]
                if not pages:
                    page = context.new_page()
                    try:
                        page.goto(self.reader_url, wait_until="commit", timeout=self.timeout_ms)
                    except Exception:
                        continue
                    pages = [page]

                # 关键逻辑：扫描所有打开标签页，只要任一页已完成认证就保存会话。
                for candidate_page in reversed(pages):
                    try:
                        html = candidate_page.content()
                        url = candidate_page.url
                    except Exception:
                        continue
                    if is_authenticated_page(html, url):
                        context.storage_state(path=str(self.storage_state))
                        saved = True
                        break
                if saved:
                    break

            context.close()
        if not saved:
            raise TimeoutError(f"等待登录超时（{wait_seconds}秒），未保存会话。")
        self.logger.info("会话已保存：%s", self.storage_state)
        self.logger.info("持久化目录：%s", self.user_data_dir)

    def collect(
        self,
        provinces: Sequence[ProvinceSpec],
        headless: bool = True,
        on_record: RecordCallback = None,
    ) -> Tuple[Dict[str, ReportRecord], Dict[str, str]]:
        if not self.storage_state.exists() and not self.user_data_dir.exists():
            raise RuntimeError(
                f"未找到会话文件：{self.storage_state}，且持久化目录不存在。请先执行 init-session。"
            )

        records: Dict[str, ReportRecord] = {}
        failures: Dict[str, str] = {}
        with sync_playwright() as p:
            if self.storage_state.exists():
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context(storage_state=str(self.storage_state))
                close_browser = True
            else:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(self.user_data_dir),
                    headless=headless,
                )
                browser = None
                close_browser = False
            page = context.new_page()
            page.goto(self.list_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            page.wait_for_timeout(1500)
            html = page.content()
            if is_login_page_html(html):
                context.close()
                if close_browser and browser:
                    browser.close()
                raise RuntimeError("会话已失效，请重新执行 init-session。")

            # 关键逻辑：先全站扫一轮候选，作为逐省筛选失败时的兜底池。
            global_candidates = self._collect_candidates_paginated(
                page, max_pages=max(10, self.max_pages_per_province * 2)
            )
            self.logger.info("全局候选链接数：%d", len(global_candidates))
            page.close()

            for idx, province in enumerate(provinces, start=1):
                self.logger.info("[%d/%d] 开始抓取 %s", idx, len(provinces), province.province)
                record, reason = self._collect_one_province(context, province, global_candidates)
                if record:
                    records[province.province] = record
                    if on_record:
                        on_record(record)
                    self.logger.info(
                        "完成 %s（source=%s, len=%d）",
                        province.province,
                        record.source_type,
                        len(record.full_text),
                    )
                else:
                    failures[province.province] = reason or "人民数据未命中候选"
                    self.logger.warning("未完成 %s：%s", province.province, failures[province.province])

            context.close()
            if close_browser and browser:
                browser.close()
        return records, failures

    def _collect_one_province(
        self,
        context: BrowserContext,
        province: ProvinceSpec,
        global_candidates: Sequence[CandidateLink],
    ) -> Tuple[Optional[ReportRecord], str]:
        page = context.new_page()
        try:
            page.goto(self.list_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            page.wait_for_timeout(1200)
            if is_login_page_html(page.content()):
                return None, "会话失效"

            local_candidates: List[CandidateLink] = []
            if self._apply_province_filter(page, province):
                local_candidates = self._collect_candidates_paginated(
                    page, max_pages=self.max_pages_per_province
                )

            merged = self._merge_candidates(
                local_candidates
                + [c for c in global_candidates if candidate_matches_province(c, province)]
            )
            if not merged:
                return None, "无候选链接"

            merged.sort(
                key=lambda c: (score_candidate(c, province), len(c.context)),
                reverse=True,
            )

            # 关键逻辑：候选逐条落详情页校验，优先取高分且正文长度达标的版本。
            best: Optional[Tuple[int, ReportRecord]] = None
            for candidate in merged[:8]:
                record = self._extract_record_from_detail(context, province, candidate)
                if not record:
                    continue
                score = score_record(record, province, self.min_text_length)
                if best is None or score > best[0]:
                    best = (score, record)
                if score >= 100:
                    return record, ""

            if best and best[1].full_text:
                return best[1], ""
            return None, "详情页正文提取失败"
        finally:
            page.close()

    def _merge_candidates(self, candidates: Sequence[CandidateLink]) -> List[CandidateLink]:
        dedup: Dict[str, CandidateLink] = {}
        for item in candidates:
            if not item.url:
                continue
            old = dedup.get(item.url)
            if old is None or len(item.context) > len(old.context):
                dedup[item.url] = item
        return list(dedup.values())

    def _collect_candidates_paginated(self, page: Page, max_pages: int) -> List[CandidateLink]:
        all_candidates: Dict[str, CandidateLink] = {}
        visited_signatures = set()

        for _ in range(max_pages):
            page.wait_for_timeout(1000)
            current = self._collect_candidates_current_page(page)
            for cand in current:
                if cand.url not in all_candidates:
                    all_candidates[cand.url] = cand

            signature = tuple(sorted(c.url for c in current)[:30])
            if signature in visited_signatures:
                break
            visited_signatures.add(signature)

            if not self._click_next_page(page):
                break
        return list(all_candidates.values())

    def _collect_candidates_current_page(self, page: Page) -> List[CandidateLink]:
        results: List[CandidateLink] = []
        raw = page.eval_on_selector_all(
            "a",
            """(elements) => elements.map((e) => {
                const text = (e.textContent || "").trim();
                let href = e.getAttribute("href") || "";
                if (href && !href.startsWith("http")) {
                    try { href = new URL(href, window.location.href).href; } catch (_) {}
                }
                let context = "";
                let node = e;
                for (let i = 0; i < 3 && node; i++) {
                    context += " " + (node.textContent || "");
                    node = node.parentElement;
                }
                return {text, href, context: context.trim()};
            })""",
        )

        for item in raw:
            text = clean_paragraph_text(item.get("text", ""))
            href = (item.get("href") or "").strip()
            context = clean_paragraph_text(item.get("context", ""))
            if not href or href.lower().startswith("javascript"):
                continue
            combined = f"{text} {context}"
            if "政府工作报告" not in combined or "2026" not in combined:
                continue
            title = text or context.split("\n")[0]
            if not title:
                continue
            publish_date = extract_first_date(combined)
            results.append(
                CandidateLink(
                    title=title,
                    url=urljoin(page.url, href),
                    publish_date=publish_date,
                    context=context,
                )
            )

        # JS 抽取失败时，退回到 HTML 解析兜底。
        if results:
            return self._merge_candidates(results)

        soup = BeautifulSoup(page.content(), "lxml")
        for a in soup.find_all("a", href=True):
            text = clean_paragraph_text(a.get_text(" ", strip=True))
            if "政府工作报告" not in text or "2026" not in text:
                continue
            href = urljoin(page.url, a["href"])
            context = clean_paragraph_text(a.parent.get_text(" ", strip=True)) if a.parent else text
            publish_date = extract_first_date(context)
            results.append(CandidateLink(title=text, url=href, publish_date=publish_date, context=context))
        return self._merge_candidates(results)

    def _apply_province_filter(self, page: Page, province: ProvinceSpec) -> bool:
        # 关键逻辑：优先点击“省份短名”，减少“全列表翻页漏采”风险。
        tokens = [province.short_name] + [a for a in province.aliases if a != province.short_name]
        self._click_if_exists(page, "label:has-text('不限')")
        page.wait_for_timeout(600)
        for token in tokens:
            selectors = [
                f"label:has-text('{token}')",
                f"span:has-text('{token}')",
                f"li:has-text('{token}')",
            ]
            for selector in selectors:
                if self._click_if_exists(page, selector):
                    page.wait_for_timeout(1200)
                    return True
        return False

    def _click_if_exists(self, page: Page, selector: str) -> bool:
        try:
            locator = page.locator(selector)
            if locator.count() > 0:
                locator.first.click(timeout=2000)
                return True
        except Exception:
            return False
        return False

    def _click_next_page(self, page: Page) -> bool:
        selectors = [
            "a:has-text('下一页')",
            "a:has-text('下页')",
            "li.next a",
            ".next a",
            ".pagination-next",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    continue
                target = locator.first
                cls = (target.get_attribute("class") or "").lower()
                text = (target.inner_text(timeout=1000) or "").strip()
                if "disabled" in cls or "末页" in text:
                    continue
                target.click(timeout=3000)
                page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
                page.wait_for_timeout(800)
                return True
            except Exception:
                continue
        return False

    def _extract_record_from_detail(
        self,
        context: BrowserContext,
        province: ProvinceSpec,
        candidate: CandidateLink,
    ) -> Optional[ReportRecord]:
        detail_page = context.new_page()
        try:
            detail_page.goto(candidate.url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            detail_page.wait_for_timeout(900)
            html = detail_page.content()
            if is_login_page_html(html):
                return None

            title = extract_page_title_from_html(html) or candidate.title
            publish_date = extract_first_date(html) or candidate.publish_date
            full_text = extract_main_text_from_html(html)
            if len(full_text) < self.min_text_length:
                try:
                    body_text = clean_paragraph_text(detail_page.inner_text("body"))
                    if len(body_text) > len(full_text):
                        full_text = body_text
                except Exception:
                    pass
            if not full_text:
                return None

            record = ReportRecord(
                province=province.province,
                year=2026,
                title=title,
                source_url=detail_page.url,
                publish_date=publish_date,
                full_text=full_text,
                source_type="peopledata",
            )
            return record
        except Exception:
            return None
        finally:
            detail_page.close()


class OfficialFallbackCollector:
    def __init__(
        self,
        manual_urls: Dict[str, str],
        min_text_length: int,
        logger: logging.Logger,
        timeout_sec: int = 20,
    ):
        self.manual_urls = manual_urls
        self.min_text_length = min_text_length
        self.logger = logger
        self.timeout_sec = timeout_sec
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            }
        )

    def fetch(self, province: ProvinceSpec) -> Optional[ReportRecord]:
        urls = []
        manual_url = self.manual_urls.get(province.province)
        if manual_url:
            urls.append(manual_url)

        urls.extend(self._search_candidates(province))
        dedup_urls = []
        seen = set()
        for url in urls:
            if url not in seen:
                dedup_urls.append(url)
                seen.add(url)

        best: Optional[Tuple[int, ReportRecord]] = None
        for url in dedup_urls[:20]:
            record = self._fetch_record_from_url(url, province)
            if not record:
                continue
            score = score_record(record, province, self.min_text_length)
            if best is None or score > best[0]:
                best = (score, record)
            if score >= 100:
                return record
        return best[1] if best else None

    def _search_candidates(self, province: ProvinceSpec) -> List[str]:
        queries = [
            f"2026年 {province.province} 政府工作报告 site:.gov.cn",
            f"{province.short_name} 人民政府 2026 政府工作报告",
        ]
        links: List[str] = []
        for query in queries:
            links.extend(self._search_bing(query))
            links.extend(self._search_duckduckgo(query))
        filtered = []
        for link in links:
            host = urlparse(link).netloc.lower()
            if ".gov.cn" in host:
                filtered.append(link)
        return filtered

    def _search_bing(self, query: str) -> List[str]:
        url = f"https://cn.bing.com/search?q={quote_plus(query)}&setlang=zh-cn"
        try:
            resp = self.session.get(url, timeout=self.timeout_sec)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            links = [a["href"] for a in soup.select("li.b_algo h2 a[href]")]
            if links:
                return links
        except Exception:
            return []
        return []

    def _search_duckduckgo(self, query: str) -> List[str]:
        url = "https://duckduckgo.com/html/"
        try:
            resp = self.session.get(url, params={"q": query}, timeout=self.timeout_sec)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            links = [a["href"] for a in soup.select("a.result__a[href]")]
            return links
        except Exception:
            return []

    def _fetch_record_from_url(self, url: str, province: ProvinceSpec) -> Optional[ReportRecord]:
        try:
            resp = self.session.get(url, timeout=self.timeout_sec, allow_redirects=True, verify=False)
            resp.raise_for_status()
        except Exception:
            return None

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            return None

        html = decode_response_html(resp)
        title = normalize_report_title(extract_page_title_from_html(html), province)
        if not title_matches_province(title, province):
            body_text = clean_paragraph_text(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
            if not title_matches_province(body_text[:120], province):
                return None

        full_text = extract_main_text_from_html(html)
        if len(full_text) < self.min_text_length:
            return None

        publish_date = extract_first_date(html)
        return ReportRecord(
            province=province.province,
            year=2026,
            title=title or f"2026年{province.province}政府工作报告",
            source_url=resp.url,
            publish_date=publish_date,
            full_text=full_text,
            source_type="official_fallback",
        )


def write_province_csv(output_dir: Path, record: ReportRecord) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / f"2026_{record.province}_政府工作报告.csv"
    with file_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerow(
            {
                "province": record.province,
                "year": record.year,
                "title": record.title,
                "source_url": record.source_url,
                "publish_date": record.publish_date,
                "full_text": record.full_text,
            }
        )
    return file_path


def write_failures(path: Path, failures: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["province", "reason"])
        writer.writeheader()
        for province, reason in failures.items():
            writer.writerow({"province": province, "reason": reason})


def write_run_manifest(path: Path, records: Iterable[ReportRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["province", "source_type", "publish_date", "source_url", "text_length"]
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "province": record.province,
                    "source_type": record.source_type,
                    "publish_date": record.publish_date,
                    "source_url": record.source_url,
                    "text_length": len(record.full_text),
                }
            )


def run_pipeline(args: argparse.Namespace) -> int:
    logger = setup_logger(args.log_file)
    provinces = load_provinces(args.provinces_file)
    manual_fallback = read_manual_fallback_urls(args.manual_fallback_csv)
    existing_records = {} if args.overwrite_existing else load_existing_records(args.output_dir, provinces)
    pending_provinces = [p for p in provinces if p.province not in existing_records]
    logger.info("省份数量：%d", len(provinces))
    logger.info("人工补齐映射：%d", len(manual_fallback))
    logger.info("已存在输出：%d", len(existing_records))
    logger.info("待抓取省份：%d", len(pending_provinces))

    people_collector = PeopleDataCollector(
        list_url=args.list_url,
        login_url=args.login_url,
        entry_url=args.entry_url,
        reader_url=args.reader_url,
        storage_state=args.storage_state,
        user_data_dir=args.user_data_dir,
        max_pages_per_province=args.max_pages_per_province,
        min_text_length=args.min_text_length,
        logger=logger,
    )
    official_collector = OfficialFallbackCollector(
        manual_urls=manual_fallback,
        min_text_length=args.min_text_length,
        logger=logger,
    )

    start = time.time()
    people_records, failures = people_collector.collect(
        pending_provinces,
        headless=args.headless,
        on_record=lambda record: write_province_csv(args.output_dir, record),
    )
    logger.info("人民数据完成：%d，待补齐：%d", len(people_records), len(failures))

    final_records: Dict[str, ReportRecord] = dict(existing_records)
    final_records.update(people_records)
    if not args.disable_official_fallback:
        for province in pending_provinces:
            if province.province in final_records:
                continue
            record = official_collector.fetch(province)
            if record:
                final_records[province.province] = record
                failures.pop(province.province, None)
                write_province_csv(args.output_dir, record)
                logger.info("官方补齐成功：%s", province.province)
            else:
                failures[province.province] = failures.get(province.province, "官方补齐失败")
    else:
        logger.info("已禁用官方补齐")

    write_failures(args.output_dir / "failures_2026.csv", failures)
    write_run_manifest(args.output_dir / "run_manifest_2026.csv", final_records.values())
    elapsed = time.time() - start
    written = len(final_records)
    logger.info("输出完成：%d 省成功，%d 省失败，耗时 %.1f 秒", written, len(failures), elapsed)

    # 关键逻辑：阶段一“采全”要求 31 省全部成功，否则返回非 0 便于流水线阻断。
    return 0 if written == len(provinces) and not failures else 2


def validate_outputs(output_dir: Path, provinces_file: Path, min_text_length: int) -> int:
    provinces = load_provinces(provinces_file)
    missing_files = []
    bad_schema = []
    bad_rows = []

    for province in provinces:
        file_path = output_dir / f"2026_{province.province}_政府工作报告.csv"
        if not file_path.exists():
            missing_files.append(province.province)
            continue

        with file_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != CSV_FIELDS:
                bad_schema.append(province.province)
                continue
            rows = list(reader)
            if len(rows) != 1:
                bad_rows.append((province.province, "行数不是1"))
                continue
            row = rows[0]
            if row.get("province") != province.province:
                bad_rows.append((province.province, "province 字段不匹配"))
            if row.get("year") != "2026":
                bad_rows.append((province.province, "year 不是 2026"))
            if not row.get("title"):
                bad_rows.append((province.province, "title 为空"))
            if not row.get("source_url"):
                bad_rows.append((province.province, "source_url 为空"))
            if row.get("publish_date") and not re.match(r"20\d{2}-\d{2}-\d{2}", row["publish_date"]):
                bad_rows.append((province.province, "publish_date 格式非法"))
            if len(row.get("full_text", "")) < min_text_length:
                bad_rows.append((province.province, "full_text 过短"))

    print(f"输出目录: {output_dir}")
    print(f"缺失文件: {len(missing_files)}")
    print(f"Schema异常: {len(bad_schema)}")
    print(f"数据异常: {len(bad_rows)}")
    if missing_files:
        print("缺失省份:", "、".join(missing_files))
    if bad_schema:
        print("Schema异常省份:", "、".join(bad_schema))
    if bad_rows:
        for province, reason in bad_rows:
            print(f"{province}: {reason}")
    return 0 if not missing_files and not bad_schema and not bad_rows else 2


def main() -> int:
    args = parse_args()
    if args.command == "init-session":
        logger = setup_logger(args.log_file)
        provinces = load_provinces(args.provinces_file)
        logger.info("已加载省份配置：%d 条", len(provinces))
        collector = PeopleDataCollector(
            list_url=args.list_url,
            login_url=args.login_url,
            entry_url=args.entry_url,
            reader_url=args.reader_url,
            storage_state=args.storage_state,
            user_data_dir=args.user_data_dir,
            max_pages_per_province=6,
            min_text_length=2000,
            logger=logger,
        )
        collector.init_session(headless=args.headless, wait_seconds=args.wait_seconds)
        return 0
    if args.command == "run":
        return run_pipeline(args)
    if args.command == "validate":
        return validate_outputs(args.output_dir, args.provinces_file, args.min_text_length)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

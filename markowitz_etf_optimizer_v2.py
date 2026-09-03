#!/usr/bin/env python3
"""
Markowitz 有效前沿 ETF/基金组合优化器 v2

相比 v1 的主要改进
==================
准确性
  - 数据源默认 auto：优先乌龟量化(WGLH)，失败自动降级东方财富公开净值接口，
    不再因缺少 Cookie 直接报错。
  - 同时报告「事前估计」(算术年化均值，马科维茨优化所用) 与「历史回测」
    (复合年化收益 CAGR、实际波动、夏普、最大回撤)，避免波动拖累造成的误读。
  - 求解结果做双重校验：权重和=1、只做多时权重非负、达到目标收益。
  - 东方财富分页一次抓取更多记录，减少请求次数。

美化 / 易用性
  - 终端彩色输出 + 中英文对齐表格 + 权重条形图（自动检测终端，不支持时降级纯文本）。
  - 交互模式即时校验输入，错误立刻重新提问。
  - 生成一份自包含 HTML 报告（嵌入图表、指标卡片、相关性热力表，并注明数据来源）。

用法
====
    python3 markowitz_etf_optimizer_v2.py                 # 中文交互模式
    python3 markowitz_etf_optimizer_v2.py --codes 510300 518880 511010 --target-return 6%
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import math
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from html import escape as html_escape
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_TRADING_DAYS = 252
DEFAULT_WGLH_BASE_URL = "https://wglh.com"
DEFAULT_WGLH_COOKIE_FILE = ".wglh_cookie"
DEFAULT_WGLH_SEARCH_JS_URL = f"{DEFAULT_WGLH_BASE_URL}/static/js/search.js?v=20251009"
DEFAULT_EASTMONEY_BASE_URL = "https://api.fund.eastmoney.com/f10/lsjz"
DEFAULT_LOOKBACK_YEARS = 10
DEFAULT_TREND_CSV = "outputs/wglh_10y_trend.csv"
DEFAULT_PORTFOLIO_CHART = "outputs/portfolio_10y_curve.svg"
DEFAULT_PORTFOLIO_CSV = "outputs/portfolio_10y_curve.csv"
DEFAULT_CORRELATION_CSV = "outputs/asset_correlation.csv"
DEFAULT_ANNUAL_METRICS_CSV = "outputs/portfolio_annual_metrics.csv"
DEFAULT_FRONTIER_CHART = "outputs/effective_frontier.svg"
DEFAULT_HTML_REPORT = "outputs/portfolio_report.html"
REQUEST_TIMEOUT = 25
EPS = 1e-12
WGLH_SYMBOL_CACHE: Optional[Dict[str, str]] = None

SOURCE_NAMES = {"wglh": "乌龟量化 wglh.com", "eastmoney": "东方财富 fund.eastmoney.com"}


# ---------------------------------------------------------------------------
# 终端美化工具
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


class C:
    """ANSI 颜色，终端不支持时自动降级为纯文本。"""

    enabled = _supports_color()

    @classmethod
    def _wrap(cls, code: str, text: str) -> str:
        if not cls.enabled:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    @classmethod
    def bold(cls, t: str) -> str:
        return cls._wrap("1", t)

    @classmethod
    def dim(cls, t: str) -> str:
        return cls._wrap("2", t)

    @classmethod
    def blue(cls, t: str) -> str:
        return cls._wrap("34", t)

    @classmethod
    def cyan(cls, t: str) -> str:
        return cls._wrap("36", t)

    @classmethod
    def green(cls, t: str) -> str:
        return cls._wrap("32", t)

    @classmethod
    def yellow(cls, t: str) -> str:
        return cls._wrap("33", t)

    @classmethod
    def red(cls, t: str) -> str:
        return cls._wrap("31", t)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def display_width(text: str) -> int:
    """终端显示宽度（中文等全角字符按 2 计）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in strip_ansi(text))


def pad_cell(text: str, width: int, align: str) -> str:
    gap = width - display_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    aligns: Optional[Sequence[str]] = None,
) -> str:
    n_cols = len(headers)
    aligns = list(aligns) if aligns else ["left"] * n_cols
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(str(cell)))

    def line(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def format_row(cells: Sequence[str], row_aligns: Sequence[str]) -> str:
        parts = [pad_cell(str(cell), widths[i], row_aligns[i]) for i, cell in enumerate(cells)]
        return "│ " + " │ ".join(parts) + " │"

    out = [line("┌", "┬", "┐"), format_row([C.bold(h) for h in headers], ["center"] * n_cols), line("├", "┼", "┤")]
    for row in rows:
        out.append(format_row([str(c) for c in row], aligns))
    out.append(line("└", "┴", "┘"))
    return "\n".join(out)


def section(title: str) -> None:
    bar = "━" * max(4, 58 - display_width(title))
    print(f"\n{C.bold(C.blue('━━ ' + title + ' ' + bar))}")


def step(index: int, total: int, message: str) -> None:
    print(f"{C.cyan(f'[{index}/{total}]')} {message}")


def info_ok(message: str) -> None:
    print(f"  {C.green('✓')} {message}")


def info_warn(message: str) -> None:
    print(f"  {C.yellow('⚠')} {message}")


def weight_bar(weight: float, max_width: int = 24) -> str:
    if not math.isfinite(weight):
        return ""
    filled = int(round(abs(weight) * max_width))
    filled = min(filled, max_width)
    bar = "█" * filled
    return C.red(bar) if weight < 0 else C.cyan(bar)


def format_pct(value: float, digits: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.{digits}%}"


def format_num(value: float, digits: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.{digits}f}"


# ---------------------------------------------------------------------------
# 异常与数据结构
# ---------------------------------------------------------------------------


class DataSourceError(RuntimeError):
    """数据源无法提供可用净值数据。"""


class WglhAuthError(DataSourceError):
    """WGLH 需要登录态。"""


class InfeasibleTargetReturnError(ValueError):
    """只做多组合无法达到指定目标收益率。"""

    def __init__(self, target_return: float, min_return: float, max_return: float) -> None:
        self.target_return = target_return
        self.min_return = min_return
        self.max_return = max_return
        super().__init__(
            "只做多组合无法达到该目标年化收益率；"
            f"当前资产历史估计收益范围约为 {min_return:.2%} 到 {max_return:.2%}。"
            "请把目标收益率改到该范围内，或使用 --allow-short 允许做空，"
            "或使用 --target-policy nearest 自动使用最近可行边界。"
        )


@dataclass(frozen=True)
class FundCode:
    original: str
    eastmoney: str
    wglh: str
    label: str


@dataclass
class SeriesDownload:
    code: FundCode
    source: str
    url: str
    prices: pd.Series
    note: str = ""


@dataclass
class PortfolioResult:
    weights: pd.Series
    annual_return: float
    annual_std: float
    monthly_return: float
    monthly_std: float
    sharpe_ratio: float
    active_assets: Tuple[str, ...]
    variance: float


@dataclass
class PortfolioBacktest:
    equity_curve: pd.Series
    daily_returns: pd.Series
    max_drawdown: float
    peak_date: pd.Timestamp
    trough_date: pd.Timestamp
    peak_value: float
    trough_value: float
    recovery_date: Optional[pd.Timestamp]


# ---------------------------------------------------------------------------
# 代码解析与数据抓取（与 v1 相同的数据层，默认策略更稳健）
# ---------------------------------------------------------------------------


def normalize_fund_code(raw: str) -> FundCode:
    text = raw.strip()
    lowered = text.lower()
    match = re.search(r"(?:^|[^\w])(sh|sz|f)?(\d{6})(?:[^\d]|$)", lowered)
    if not match:
        raise ValueError(f"无法识别基金/ETF 代码: {raw!r}，请使用 6 位代码或 WGLH URL。")

    prefix = match.group(1)
    six_digit = match.group(2)
    wglh_symbol = infer_wglh_symbol(six_digit, prefix)
    return FundCode(original=text, eastmoney=six_digit, wglh=wglh_symbol, label=six_digit)


def looks_like_fund_code(raw: str) -> bool:
    return re.search(r"(?:^|[^\w])(?:sh|sz|f)?\d{6}(?:[^\d]|$)", raw.strip().lower()) is not None


def load_wglh_symbol_map(search_url: str = DEFAULT_WGLH_SEARCH_JS_URL) -> Dict[str, str]:
    global WGLH_SYMBOL_CACHE
    if WGLH_SYMBOL_CACHE is not None:
        return WGLH_SYMBOL_CACHE

    response = requests.get(
        search_url,
        headers=request_headers(referer=f"{DEFAULT_WGLH_BASE_URL}/"),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    symbol_map: Dict[str, str] = {}
    entry_pattern = re.compile(r"""\[['"](?P<text>[^'"]+)['"],\s*['"]fund['"]\]""")
    for match in entry_pattern.finditer(response.text):
        symbol = match.group("text").split(maxsplit=1)[0].lower()
        if not re.fullmatch(r"(?:sh|sz|f)\d{6}", symbol):
            continue
        six_digit = symbol[-6:]
        symbol_map.setdefault(six_digit, symbol)

    WGLH_SYMBOL_CACHE = symbol_map
    return symbol_map


def lookup_wglh_symbol(six_digit: str) -> Optional[str]:
    try:
        return load_wglh_symbol_map().get(six_digit)
    except requests.RequestException:
        return None


def infer_wglh_symbol(six_digit: str, explicit_prefix: Optional[str]) -> str:
    indexed_symbol = lookup_wglh_symbol(six_digit)
    if indexed_symbol:
        return indexed_symbol

    if explicit_prefix in {"sh", "sz", "f"}:
        return f"{explicit_prefix}{six_digit}"

    # WGLH 对场内 ETF/LOF 用交易所前缀，普通开放式基金用 f 前缀。
    if six_digit.startswith(("51", "56", "58")):
        return f"sh{six_digit}"
    if six_digit.startswith(("15", "16", "18")):
        return f"sz{six_digit}"
    return f"f{six_digit}"


def parse_rate(value: str, name: str) -> float:
    text = str(value).strip()
    if text.endswith("%"):
        rate = float(text[:-1]) / 100.0
    else:
        rate = float(text)
        if abs(rate) > 1.0:
            rate /= 100.0
    if not math.isfinite(rate):
        raise ValueError(f"{name} 不是有效数字: {value!r}")
    return rate


def subtract_years(date_value: dt.date, years: int) -> dt.date:
    try:
        return date_value.replace(year=date_value.year - years)
    except ValueError:
        return date_value.replace(month=2, day=28, year=date_value.year - years)


def normalize_date(value: Optional[str], name: str) -> Optional[str]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{name} 必须是 YYYY-MM-DD 格式: {value!r}") from exc


def prompt_non_empty(prompt: str) -> str:
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print(C.yellow("输入不能为空，请重新输入。"))


def request_headers(referer: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
    }
    if referer:
        headers["Referer"] = referer
    return headers


def build_session(cookie: Optional[str]) -> requests.Session:
    session = requests.Session()
    session.headers.update(request_headers())
    if cookie:
        session.headers["Cookie"] = cookie
    return session


def read_cookie_arg(cookie: Optional[str], cookie_file: Optional[str]) -> Optional[str]:
    if cookie:
        return cookie.strip()
    if cookie_file:
        return Path(cookie_file).read_text(encoding="utf-8").strip()
    env_cookie = os.environ.get("WGLH_COOKIE")
    if env_cookie:
        return env_cookie.strip()
    default_cookie_file = Path(DEFAULT_WGLH_COOKIE_FILE)
    if default_cookie_file.exists():
        return default_cookie_file.read_text(encoding="utf-8").strip()
    return None


def cookie_value(cookie_header: str, name: str) -> Optional[str]:
    for part in cookie_header.split(";"):
        item = part.strip()
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def compact_date(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.replace("-", "")


def wglh_api_symbol(code: FundCode) -> str:
    return code.wglh.upper()


def _coerce_price_series(
    df: pd.DataFrame,
    code_label: str,
    date_candidates: Sequence[str],
    value_candidates: Sequence[str],
) -> Optional[pd.Series]:
    if df.empty:
        return None

    clean = df.copy()
    clean.columns = [
        " ".join(str(part) for part in col if str(part) != "nan").strip()
        if isinstance(col, tuple)
        else str(col).strip()
        for col in clean.columns
    ]

    date_col = None
    for col in clean.columns:
        lower = col.lower()
        if any(candidate in lower for candidate in date_candidates):
            date_col = col
            break
    if date_col is None:
        for col in clean.columns:
            parsed = pd.to_datetime(clean[col], errors="coerce")
            if parsed.notna().sum() >= max(3, int(len(clean) * 0.5)):
                date_col = col
                break
    if date_col is None:
        return None

    value_col = None
    for preferred in value_candidates:
        for col in clean.columns:
            if preferred.lower() in col.lower():
                value_col = col
                break
        if value_col is not None:
            break
    if value_col is None:
        return None

    dates = pd.to_datetime(clean[date_col], errors="coerce")
    values = (
        clean[value_col]
        .astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("%", "", regex=False)
        .str.extract(r"(-?\d+(?:\.\d+)?)", expand=False)
    )
    prices = pd.to_numeric(values, errors="coerce")
    series = pd.Series(prices.to_numpy(), index=dates, name=code_label).dropna()
    series = series[series > 0]
    if len(series) < 3:
        return None

    series = series.sort_index()
    series = series.loc[~series.index.duplicated(keep="last")]
    return series


def _extract_json_like_date_records(text: str, code_label: str) -> Optional[pd.Series]:
    value_keys = [
        "close", "adj_close", "price", "nav", "net_value", "value",
        "jz", "dwjz", "ljjz", "累计净值", "单位净值", "净值", "价",
    ]
    records: Dict[pd.Timestamp, float] = {}

    date_pattern = r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}"
    object_pattern = re.compile(
        rf"""["'](?P<date>{date_pattern})["']\s*:\s*\{{(?P<body>[^{{}}]{{1,800}})\}}""",
        re.IGNORECASE,
    )
    for match in object_pattern.finditer(text):
        date = pd.to_datetime(match.group("date").replace(".", "-"), errors="coerce")
        if pd.isna(date):
            continue
        body = match.group("body")
        value = None
        for key in value_keys:
            key_pattern = re.compile(
                rf"""["']?{re.escape(key)}["']?\s*:\s*["']?(?P<value>-?\d+(?:\.\d+)?)""",
                re.IGNORECASE,
            )
            key_match = key_pattern.search(body)
            if key_match:
                value = float(key_match.group("value"))
                break
        if value is not None and value > 0:
            records[pd.Timestamp(date)] = value

    record_pattern = re.compile(
        rf"""
        \{{[^{{}}]{{0,500}}?
        ["']?(?:date|day|trade_date|净值日期|日期)["']?\s*:\s*["'](?P<date>{date_pattern})["']
        (?P<body>[^{{}}]{{0,800}}?)
        \}}
        """,
        re.IGNORECASE | re.VERBOSE,
    )
    for match in record_pattern.finditer(text):
        date = pd.to_datetime(match.group("date").replace(".", "-"), errors="coerce")
        if pd.isna(date):
            continue
        body = match.group("body")
        value = None
        for key in value_keys:
            key_pattern = re.compile(
                rf"""["']?{re.escape(key)}["']?\s*:\s*["']?(?P<value>-?\d+(?:\.\d+)?)""",
                re.IGNORECASE,
            )
            key_match = key_pattern.search(body)
            if key_match:
                value = float(key_match.group("value"))
                break
        if value is not None and value > 0:
            records[pd.Timestamp(date)] = value

    if len(records) < 3:
        return None

    series = pd.Series(records, name=code_label, dtype=float).sort_index()
    series = series.loc[~series.index.duplicated(keep="last")]
    return series


def parse_wglh_html(html: str, code_label: str) -> Optional[pd.Series]:
    date_candidates = ["净值日期", "日期", "date", "时间"]
    value_candidates = ["累计净值", "复权净值", "单位净值", "净值", "close", "收盘", "价"]

    try:
        tables = pd.read_html(StringIO(html))
    except ValueError:
        tables = []
    for table in tables:
        series = _coerce_price_series(table, code_label, date_candidates, value_candidates)
        if series is not None:
            return series

    soup = BeautifulSoup(html, "html.parser")
    scripts = "\n".join(script.get_text("\n", strip=False) for script in soup.find_all("script"))
    return _extract_json_like_date_records(scripts, code_label)


def fetch_wglh_earning_chart_prices(
    code: FundCode,
    session: requests.Session,
    start: Optional[str],
    end: Optional[str],
    base_url: str,
) -> SeriesDownload:
    cookie_header = session.headers.get("Cookie", "")
    csrf_token = cookie_value(cookie_header, "csrftoken")
    if csrf_token:
        session.headers.update(
            {
                "X-CSRFToken": csrf_token,
                "X-Requested-With": "XMLHttpRequest",
                "Origin": base_url.rstrip("/"),
                "Referer": f"{base_url.rstrip('/')}/fund/f/{code.wglh}/",
            }
        )

    api_url = f"{base_url.rstrip('/')}/Fund/API/FundEarningChart/"
    response = session.post(
        api_url,
        data={
            "symbol": wglh_api_symbol(code),
            "index": "SH000300.index",
            "begin": compact_date(start),
            "end": compact_date(end),
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    payload = response.json()
    if not payload.get("success"):
        raise DataSourceError(f"WGLH 收益曲线接口未返回成功: {code.label}")

    result = payload.get("result") or {}
    dates = pd.to_datetime(result.get("list_date") or [], format="%Y.%m.%d", errors="coerce")
    fund_ups = pd.to_numeric(pd.Series(result.get("list_fund_ups") or []), errors="coerce")
    if len(dates) != len(fund_ups) or len(dates) < 3:
        raise DataSourceError(f"WGLH 收益曲线接口数据不足: {code.label}")

    prices = 100.0 * (1.0 + fund_ups.to_numpy(dtype=float) / 100.0)
    series = pd.Series(prices, index=dates, name=code.label).dropna()
    series = series[series > 0].sort_index()
    series = series.loc[~series.index.duplicated(keep="last")]
    series = filter_date_range(series, start, end)
    if len(series) < 3:
        raise DataSourceError(f"WGLH 收益曲线数据在指定日期范围内太少: {code.label}")

    return SeriesDownload(
        code=code,
        source="wglh",
        url=api_url,
        prices=series,
        note="收益曲线接口，首日=100",
    )


def fetch_wglh_prices(
    code: FundCode,
    session: requests.Session,
    start: Optional[str],
    end: Optional[str],
    base_url: str = DEFAULT_WGLH_BASE_URL,
) -> SeriesDownload:
    url = f"{base_url.rstrip('/')}/fund/f/{code.wglh}/"
    response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()

    final_url = response.url.lower()
    html = response.text
    if "/auth/login/" in final_url or "/auth/login/" in html[:5000].lower():
        raise WglhAuthError(
            f"WGLH 需要登录态才能访问 {url}；请用 --wglh-cookie/--wglh-cookie-file，"
            "或用 --source eastmoney 跳过 WGLH。"
        )

    try:
        return fetch_wglh_earning_chart_prices(code, session, start, end, base_url)
    except Exception as exc:
        api_error = exc

    series = parse_wglh_html(html, code.label)
    if series is None or len(series) < 3:
        raise DataSourceError(
            f"WGLH 页面和收益曲线接口都没有解析到 {code.label} 的可用净值序列: {url}; "
            f"接口错误: {api_error}"
        )

    series = filter_date_range(series, start, end)
    if len(series) < 3:
        raise DataSourceError(f"WGLH 数据在指定日期范围内太少: {code.label}")

    return SeriesDownload(code=code, source="wglh", url=url, prices=series)


def fetch_eastmoney_prices(
    code: FundCode,
    start: Optional[str],
    end: Optional[str],
    price_field: str = "LJJZ",
    base_url: str = DEFAULT_EASTMONEY_BASE_URL,
) -> SeriesDownload:
    rows: List[dict] = []
    page = 1
    page_size = 49
    referer = f"https://fundf10.eastmoney.com/jjjz_{code.eastmoney}.html"

    while True:
        params = {
            "fundCode": code.eastmoney,
            "pageIndex": page,
            "pageSize": page_size,
            "startDate": start or "",
            "endDate": end or "",
        }
        response = requests.get(
            base_url,
            params=params,
            headers=request_headers(referer=referer),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("Data") or {}
        batch = data.get("LSJZList") or []
        if not batch:
            break
        rows.extend(batch)

        total_count = int(payload.get("TotalCount") or len(rows))
        current_page_size = int(payload.get("PageSize") or page_size)
        if len(rows) >= total_count or page * current_page_size >= total_count:
            break
        page += 1

    if not rows:
        raise DataSourceError(f"东方财富未返回 {code.label} 的净值数据。")

    df = pd.DataFrame(rows)
    field = price_field.upper()
    if field not in df.columns or df[field].replace("", np.nan).dropna().empty:
        field = "DWJZ"
    if field not in df.columns:
        raise DataSourceError(f"东方财富数据缺少净值字段: {price_field}")

    dates = pd.to_datetime(df["FSRQ"], errors="coerce")
    prices = pd.to_numeric(df[field].replace("", np.nan), errors="coerce")
    series = pd.Series(prices.to_numpy(), index=dates, name=code.label).dropna()
    series = series[series > 0].sort_index()
    series = series.loc[~series.index.duplicated(keep="last")]
    series = filter_date_range(series, start, end)
    if len(series) < 3:
        raise DataSourceError(f"东方财富数据在指定日期范围内太少: {code.label}")

    return SeriesDownload(
        code=code,
        source="eastmoney",
        url=referer,
        prices=series,
        note=f"使用字段 {field}",
    )


def filter_date_range(series: pd.Series, start: Optional[str], end: Optional[str]) -> pd.Series:
    result = series.copy()
    if start:
        result = result[result.index >= pd.Timestamp(start)]
    if end:
        result = result[result.index <= pd.Timestamp(end)]
    return result


def download_price_frame(
    raw_codes: Sequence[str],
    source: str,
    start: Optional[str],
    end: Optional[str],
    cookie: Optional[str],
    price_field: str,
) -> Tuple[pd.DataFrame, List[SeriesDownload], List[str]]:
    codes = [normalize_fund_code(code) for code in raw_codes]
    session = build_session(cookie)
    downloads: List[SeriesDownload] = []
    warnings: List[str] = []

    for code in codes:
        download: Optional[SeriesDownload] = None
        if source in {"auto", "wglh"}:
            try:
                download = fetch_wglh_prices(code, session, start, end)
            except Exception as exc:
                if source == "wglh":
                    raise
                warnings.append(f"{code.label}: WGLH 获取失败，已切换到东方财富。原因: {exc}")

        if download is None and source in {"auto", "eastmoney"}:
            download = fetch_eastmoney_prices(code, start, end, price_field=price_field)

        if download is None:
            raise ValueError(f"未知数据源: {source}")

        downloads.append(download)
        info_ok(
            f"{code.label}: {SOURCE_NAMES.get(download.source, download.source)}"
            f"{'（' + download.note + '）' if download.note else ''}，"
            f"{download.prices.index.min().date()} 至 {download.prices.index.max().date()}，"
            f"{len(download.prices)} 条"
        )

    prices = pd.concat([item.prices.rename(item.code.label) for item in downloads], axis=1, join="inner")
    prices = prices.sort_index().dropna(how="any")
    if prices.empty or len(prices) < 4:
        raise DataSourceError("多资产共同日期样本太少，无法估计组合收益和协方差。")
    return prices, downloads, warnings


# ---------------------------------------------------------------------------
# 马科维茨求解（与 v1 相同算法 + 结果校验）
# ---------------------------------------------------------------------------


def equality_min_variance_weights(
    mu: np.ndarray,
    cov: np.ndarray,
    target_return: float,
) -> np.ndarray:
    n_assets = len(mu)
    if n_assets == 1:
        if abs(float(mu[0]) - target_return) <= 1e-9:
            return np.array([1.0])
        raise ValueError("单资产无法满足指定目标收益率。")

    inv_cov = np.linalg.pinv(cov)
    constraints = np.vstack([np.ones(n_assets), mu])
    rhs = np.array([1.0, target_return], dtype=float)
    middle = constraints @ inv_cov @ constraints.T
    weights = inv_cov @ constraints.T @ np.linalg.pinv(middle) @ rhs
    return weights


def solve_target_portfolio(
    mu: pd.Series,
    cov: pd.DataFrame,
    target_return: float,
    risk_free_rate: float,
    long_only: bool,
    max_exact_assets: int,
) -> PortfolioResult:
    labels = list(mu.index)
    mu_arr = mu.to_numpy(dtype=float)
    cov_arr = cov.loc[labels, labels].to_numpy(dtype=float)
    cov_arr = (cov_arr + cov_arr.T) / 2.0
    n_assets = len(labels)

    if not long_only:
        weights_arr = equality_min_variance_weights(mu_arr, cov_arr, target_return)
        active = tuple(label for label, weight in zip(labels, weights_arr) if abs(weight) > 1e-8)
        return build_portfolio_result(labels, weights_arr, mu_arr, cov_arr, risk_free_rate, active)

    min_return = float(np.nanmin(mu_arr))
    max_return = float(np.nanmax(mu_arr))
    if target_return < min_return - 1e-10 or target_return > max_return + 1e-10:
        raise InfeasibleTargetReturnError(target_return, min_return, max_return)
    if n_assets > max_exact_assets:
        raise ValueError(
            f"只做多精确求解需要枚举资产子集，当前资产数 {n_assets} 超过 --max-exact-assets={max_exact_assets}。"
            "请减少资产数量、调大 --max-exact-assets，或使用 --allow-short。"
        )

    best_weights: Optional[np.ndarray] = None
    best_variance = math.inf
    best_active: Tuple[str, ...] = ()

    for subset_size in range(1, n_assets + 1):
        for subset in itertools.combinations(range(n_assets), subset_size):
            idx = np.array(subset, dtype=int)
            sub_mu = mu_arr[idx]
            if target_return < float(sub_mu.min()) - 1e-10 or target_return > float(sub_mu.max()) + 1e-10:
                continue

            sub_cov = cov_arr[np.ix_(idx, idx)]
            try:
                sub_weights = equality_min_variance_weights(sub_mu, sub_cov, target_return)
            except Exception:
                continue

            if not np.all(np.isfinite(sub_weights)):
                continue
            if sub_weights.min() < -1e-8:
                continue

            full_weights = np.zeros(n_assets, dtype=float)
            full_weights[idx] = np.where(abs(sub_weights) < 1e-12, 0.0, sub_weights)
            if abs(full_weights.sum() - 1.0) > 1e-6:
                continue
            if abs(float(full_weights @ mu_arr) - target_return) > 1e-6:
                continue

            variance = float(full_weights @ cov_arr @ full_weights)
            if variance < best_variance:
                best_variance = variance
                best_weights = full_weights
                best_active = tuple(labels[i] for i in subset if full_weights[i] > 1e-8)

    if best_weights is None:
        raise ValueError("没有找到满足目标收益率和非负权重约束的组合。")

    return build_portfolio_result(labels, best_weights, mu_arr, cov_arr, risk_free_rate, best_active)


def solve_target_portfolio_with_policy(
    mu: pd.Series,
    cov: pd.DataFrame,
    target_return: float,
    risk_free_rate: float,
    long_only: bool,
    max_exact_assets: int,
    target_policy: str,
) -> Tuple[PortfolioResult, float, Optional[str]]:
    try:
        result = solve_target_portfolio(
            mu=mu,
            cov=cov,
            target_return=target_return,
            risk_free_rate=risk_free_rate,
            long_only=long_only,
            max_exact_assets=max_exact_assets,
        )
        return result, target_return, None
    except InfeasibleTargetReturnError as exc:
        if target_policy != "nearest":
            raise

        adjusted_target = min(max(target_return, exc.min_return), exc.max_return)
        result = solve_target_portfolio(
            mu=mu,
            cov=cov,
            target_return=adjusted_target,
            risk_free_rate=risk_free_rate,
            long_only=long_only,
            max_exact_assets=max_exact_assets,
        )
        warning = (
            f"目标年化收益率 {format_pct(target_return)} 超出只做多可行范围 "
            f"[{format_pct(exc.min_return)}, {format_pct(exc.max_return)}]，"
            f"已自动改用最近可行目标 {format_pct(adjusted_target)}（--target-policy nearest）。"
        )
        return result, adjusted_target, warning


def build_portfolio_result(
    labels: Sequence[str],
    weights_arr: np.ndarray,
    mu_arr: np.ndarray,
    cov_arr: np.ndarray,
    risk_free_rate: float,
    active: Tuple[str, ...],
) -> PortfolioResult:
    weights = pd.Series(weights_arr, index=labels, name="weight")
    annual_return = float(weights_arr @ mu_arr)
    variance = max(float(weights_arr @ cov_arr @ weights_arr), 0.0)
    annual_std = math.sqrt(variance)
    monthly_return = (1.0 + annual_return) ** (1.0 / 12.0) - 1.0 if annual_return > -1 else float("nan")
    monthly_std = annual_std / math.sqrt(12.0)
    sharpe_ratio = (annual_return - risk_free_rate) / annual_std if annual_std > EPS else float("nan")
    return PortfolioResult(
        weights=weights,
        annual_return=annual_return,
        annual_std=annual_std,
        monthly_return=monthly_return,
        monthly_std=monthly_std,
        sharpe_ratio=sharpe_ratio,
        active_assets=active,
        variance=variance,
    )


def validate_portfolio_result(
    result: PortfolioResult,
    mu: pd.Series,
    target_return: float,
    long_only: bool,
) -> None:
    """求解结果双重校验：任何一项不满足都说明求解有问题，直接报错而不是输出错误结果。"""
    weights = result.weights.to_numpy(dtype=float)
    if abs(float(weights.sum()) - 1.0) > 1e-6:
        raise RuntimeError(f"内部校验失败：权重和 {weights.sum():.8f} != 1。")
    if long_only and float(weights.min()) < -1e-6:
        raise RuntimeError(f"内部校验失败：只做多约束下出现负权重 {weights.min():.8f}。")
    achieved = float(weights @ mu.to_numpy(dtype=float))
    if abs(achieved - target_return) > 1e-6:
        raise RuntimeError(
            f"内部校验失败：组合期望收益 {achieved:.8f} 与目标 {target_return:.8f} 不一致。"
        )


def estimate_annual_return_and_cov(
    prices: pd.DataFrame,
    trading_days: int,
) -> Tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
    returns = prices.pct_change().dropna(how="any")
    if len(returns) < 3:
        raise ValueError("收益率样本太少，无法估计组合。")

    annual_mu = returns.mean() * trading_days
    annual_cov = returns.cov() * trading_days
    return annual_mu, annual_cov, returns


def generate_frontier(
    mu: pd.Series,
    cov: pd.DataFrame,
    risk_free_rate: float,
    long_only: bool,
    max_exact_assets: int,
    points: int,
) -> pd.DataFrame:
    targets = np.linspace(float(mu.min()), float(mu.max()), points)
    rows: List[Dict[str, float]] = []
    for target in targets:
        try:
            result = solve_target_portfolio(
                mu=mu,
                cov=cov,
                target_return=float(target),
                risk_free_rate=risk_free_rate,
                long_only=long_only,
                max_exact_assets=max_exact_assets,
            )
        except Exception:
            continue
        row = {
            "target_annual_return": target,
            "annual_return": result.annual_return,
            "annual_std": result.annual_std,
            "monthly_return": result.monthly_return,
            "monthly_std": result.monthly_std,
            "sharpe_ratio": result.sharpe_ratio,
        }
        row.update({f"weight_{code}": weight for code, weight in result.weights.items()})
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 回测与统计
# ---------------------------------------------------------------------------


def build_asset_stats(
    prices: pd.DataFrame,
    annual_mu: pd.Series,
    annual_cov: pd.DataFrame,
    risk_free_rate: float,
) -> pd.DataFrame:
    annual_std = pd.Series(
        np.sqrt(np.maximum(np.diag(annual_cov.loc[annual_mu.index, annual_mu.index].to_numpy()), 0.0)),
        index=annual_mu.index,
        name="annual_std",
    )
    sharpe = (annual_mu - risk_free_rate) / annual_std.replace(0, np.nan)
    total_return = prices.iloc[-1] / prices.iloc[0] - 1.0
    return pd.DataFrame(
        {
            "annual_return": annual_mu,
            "annual_std": annual_std,
            "sharpe_ratio": sharpe,
            "period_total_return": total_return,
            "start_price": prices.iloc[0],
            "end_price": prices.iloc[-1],
        }
    )


def normalized_prices(prices: pd.DataFrame) -> pd.DataFrame:
    return prices.divide(prices.iloc[0]).multiply(100.0)


def backtest_static_portfolio(
    prices: pd.DataFrame,
    returns: pd.DataFrame,
    weights: pd.Series,
) -> PortfolioBacktest:
    aligned_weights = weights.reindex(returns.columns).fillna(0.0)
    portfolio_daily_returns = returns.mul(aligned_weights, axis=1).sum(axis=1)
    equity_from_returns = (1.0 + portfolio_daily_returns).cumprod() * 100.0
    equity_curve = pd.concat(
        [
            pd.Series([100.0], index=[prices.index[0]], name="portfolio"),
            equity_from_returns.rename("portfolio"),
        ]
    )
    equity_curve = equity_curve.loc[~equity_curve.index.duplicated(keep="last")]

    peak = equity_curve.cummax()
    drawdown = equity_curve / peak - 1.0
    trough_date = pd.Timestamp(drawdown.idxmin())
    max_drawdown = float(drawdown.loc[trough_date])
    peak_date = pd.Timestamp(equity_curve.loc[:trough_date].idxmax())
    peak_value = float(equity_curve.loc[peak_date])
    trough_value = float(equity_curve.loc[trough_date])

    recovery_date: Optional[pd.Timestamp] = None
    after_trough = equity_curve.loc[trough_date:]
    recovered = after_trough[after_trough >= peak_value]
    if not recovered.empty:
        recovery_date = pd.Timestamp(recovered.index[0])

    return PortfolioBacktest(
        equity_curve=equity_curve,
        daily_returns=portfolio_daily_returns.rename("portfolio_return"),
        max_drawdown=max_drawdown,
        peak_date=peak_date,
        trough_date=trough_date,
        peak_value=peak_value,
        trough_value=trough_value,
        recovery_date=recovery_date,
    )


def build_expost_metrics(
    backtest: PortfolioBacktest,
    risk_free_rate: float,
    trading_days: int,
) -> Dict[str, float]:
    """历史回测的事后指标：复合年化 CAGR、实际波动、夏普、卡玛比率等。

    与马科维茨优化里的算术年化均值不同，CAGR 已包含波动拖累，是资金实际
    复利增长速度，两者差异约为方差的一半。
    """
    equity = backtest.equity_curve.dropna()
    daily = backtest.daily_returns.dropna()
    if len(equity) < 2 or daily.empty:
        return {}

    n_years = (equity.index[-1] - equity.index[0]).days / 365.25
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    cagr = (1.0 + total_return) ** (1.0 / n_years) - 1.0 if n_years > 0 else float("nan")
    ann_vol = float(daily.std(ddof=1) * math.sqrt(trading_days)) if len(daily) > 1 else float("nan")
    daily_rf = (1.0 + risk_free_rate) ** (1.0 / trading_days) - 1.0 if risk_free_rate > -1.0 else 0.0
    sharpe = (
        float((daily - daily_rf).mean() * trading_days / ann_vol)
        if math.isfinite(ann_vol) and ann_vol > EPS
        else float("nan")
    )
    calmar = (
        cagr / abs(backtest.max_drawdown)
        if math.isfinite(cagr) and backtest.max_drawdown < -EPS
        else float("nan")
    )
    return {
        "years": n_years,
        "total_return": total_return,
        "cagr": cagr,
        "annual_vol": ann_vol,
        "sharpe": sharpe,
        "calmar": calmar,
    }


def build_portfolio_annual_metrics(
    backtest: PortfolioBacktest,
    risk_free_rate: float,
    trading_days: int,
) -> pd.DataFrame:
    daily_returns = backtest.daily_returns.dropna()
    if daily_returns.empty:
        return pd.DataFrame()

    if risk_free_rate > -1.0:
        daily_rf = (1.0 + risk_free_rate) ** (1.0 / trading_days) - 1.0
    else:
        daily_rf = risk_free_rate / trading_days

    rows: List[Dict[str, object]] = []
    for year, group in daily_returns.groupby(daily_returns.index.year):
        clean = group.dropna()
        if clean.empty:
            continue

        start_date = pd.Timestamp(clean.index[0]).date()
        end_date = pd.Timestamp(clean.index[-1]).date()
        sample_days = int(len(clean))
        period_return = float((1.0 + clean).prod() - 1.0)
        annualized_mean_return = float(clean.mean() * trading_days)
        annual_std = (
            float(clean.std(ddof=1) * math.sqrt(trading_days))
            if sample_days > 1
            else float("nan")
        )
        period_risk_free = float((1.0 + daily_rf) ** sample_days - 1.0)
        sharpe_ratio = (
            float((clean - daily_rf).mean() * trading_days / annual_std)
            if math.isfinite(annual_std) and annual_std > EPS
            else float("nan")
        )
        is_partial_year = start_date.month > 2 or end_date.month < 12

        rows.append(
            {
                "year": int(year),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "trading_days": sample_days,
                "period_return": period_return,
                "annualized_mean_return": annualized_mean_return,
                "annual_std": annual_std,
                "period_risk_free": period_risk_free,
                "sharpe_ratio": sharpe_ratio,
                "is_partial_year": is_partial_year,
            }
        )

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# SVG 图表
# ---------------------------------------------------------------------------


def svg_points_for_series(series: pd.Series, x_of_pos, y_of_value) -> str:
    points = []
    values = series.to_numpy(dtype=float)
    for pos, value in enumerate(values):
        if math.isfinite(value):
            points.append(f"{x_of_pos(pos):.2f},{y_of_value(float(value)):.2f}")
    return " ".join(points)


def build_portfolio_svg(
    backtest: PortfolioBacktest,
    title: str,
    source_note: str = "",
) -> str:
    equity = backtest.equity_curve.dropna()
    if len(equity) < 2:
        raise ValueError("组合资产曲线样本太少，无法画图。")

    width, height = 1100, 620
    left, right, top, bottom = 80, 38, 58, 86
    plot_w = width - left - right
    plot_h = height - top - bottom

    y_min = float(equity.min())
    y_max = float(equity.max())
    if abs(y_max - y_min) < EPS:
        y_min *= 0.95
        y_max *= 1.05
    padding = (y_max - y_min) * 0.08
    y_min -= padding
    y_max += padding

    def x_of_pos(pos: int) -> float:
        return left + (plot_w * pos / max(len(equity) - 1, 1))

    def y_of_value(value: float) -> float:
        return top + plot_h * (y_max - value) / (y_max - y_min)

    line_points = svg_points_for_series(equity, x_of_pos, y_of_value)
    peak_pos = int(equity.index.get_loc(backtest.peak_date))
    trough_pos = int(equity.index.get_loc(backtest.trough_date))
    peak_x, peak_y = x_of_pos(peak_pos), y_of_value(backtest.peak_value)
    trough_x, trough_y = x_of_pos(trough_pos), y_of_value(backtest.trough_value)

    y_ticks = np.linspace(y_min, y_max, 6)
    x_tick_positions = np.linspace(0, len(equity) - 1, 6).round().astype(int)

    recovery_text = (
        backtest.recovery_date.date().isoformat()
        if backtest.recovery_date is not None
        else "未恢复"
    )
    footer = "组合净值首日=100；按最优目标权重进行日频再平衡回测。"
    if source_note:
        footer += f" 数据来源：{source_note}。"

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif;fill:#1f2937}",
        ".muted{fill:#6b7280;font-size:13px}.axis{stroke:#9ca3af;stroke-width:1}.grid{stroke:#e5e7eb;stroke-width:1}.curve{fill:none;stroke:#2563eb;stroke-width:2.4}.dd{stroke:#dc2626;stroke-width:2;stroke-dasharray:5 4}.marker{fill:#fff;stroke-width:2.2}",
        "</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left}" y="32" font-size="22" font-weight="700">{html_escape(title)}</text>',
        f'<text x="{left}" y="52" class="muted">最大回撤 {format_pct(backtest.max_drawdown)}：{backtest.peak_date.date()} 至 {backtest.trough_date.date()}；恢复日：{recovery_text}</text>',
    ]

    for tick in y_ticks:
        y = y_of_value(float(tick))
        parts.append(f'<line class="grid" x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}"/>')
        parts.append(f'<text class="muted" x="{left-10}" y="{y+4:.2f}" text-anchor="end">{tick:.0f}</text>')

    for pos in x_tick_positions:
        x = x_of_pos(int(pos))
        label = equity.index[int(pos)].date().isoformat()[:7]
        parts.append(f'<line class="grid" x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{height-bottom}"/>')
        parts.append(f'<text class="muted" x="{x:.2f}" y="{height-bottom+28}" text-anchor="middle">{label}</text>')

    parts.extend(
        [
            f'<line class="axis" x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}"/>',
            f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}"/>',
            f'<polyline class="curve" points="{line_points}"/>',
            f'<line class="dd" x1="{peak_x:.2f}" y1="{peak_y:.2f}" x2="{trough_x:.2f}" y2="{trough_y:.2f}"/>',
            f'<circle class="marker" cx="{peak_x:.2f}" cy="{peak_y:.2f}" r="5.5" stroke="#16a34a"/>',
            f'<circle class="marker" cx="{trough_x:.2f}" cy="{trough_y:.2f}" r="5.5" stroke="#dc2626"/>',
            f'<text x="{peak_x+8:.2f}" y="{peak_y-10:.2f}" font-size="13" fill="#166534">峰值 {backtest.peak_value:.2f}</text>',
            f'<text x="{trough_x+8:.2f}" y="{trough_y+20:.2f}" font-size="13" fill="#991b1b">谷底 {backtest.trough_value:.2f}</text>',
            f'<text x="{(peak_x+trough_x)/2:.2f}" y="{min(peak_y,trough_y)-18:.2f}" font-size="15" font-weight="700" fill="#dc2626" text-anchor="middle">Max Drawdown {format_pct(backtest.max_drawdown)}</text>',
            f'<text x="{left}" y="{height-24}" class="muted">{html_escape(footer)}</text>',
            "</svg>",
        ]
    )
    return "\n".join(parts)


def build_frontier_svg(
    frontier: pd.DataFrame,
    result: PortfolioResult,
    annual_mu: pd.Series,
    annual_cov: pd.DataFrame,
    title: str,
    source_note: str = "",
) -> str:
    clean = frontier.replace([np.inf, -np.inf], np.nan).dropna(subset=["annual_std", "annual_return"])
    if clean.empty:
        raise ValueError("有效前沿样本为空，无法画图。")

    asset_std = pd.Series(
        np.sqrt(np.maximum(np.diag(annual_cov.loc[annual_mu.index, annual_mu.index].to_numpy()), 0.0)),
        index=annual_mu.index,
        name="annual_std",
    )
    asset_points = pd.DataFrame({"annual_return": annual_mu, "annual_std": asset_std}).replace(
        [np.inf, -np.inf], np.nan
    ).dropna()

    x_values = list(clean["annual_std"].astype(float)) + [result.annual_std]
    y_values = list(clean["annual_return"].astype(float)) + [result.annual_return]
    if not asset_points.empty:
        x_values.extend(asset_points["annual_std"].astype(float).tolist())
        y_values.extend(asset_points["annual_return"].astype(float).tolist())

    x_min, x_max = float(min(x_values)), float(max(x_values))
    y_min, y_max = float(min(y_values)), float(max(y_values))
    if abs(x_max - x_min) < EPS:
        x_min = max(0.0, x_min - 0.01)
        x_max = x_max + 0.01
    if abs(y_max - y_min) < EPS:
        y_min -= 0.01
        y_max += 0.01

    x_padding = (x_max - x_min) * 0.1
    y_padding = (y_max - y_min) * 0.12
    x_min = max(0.0, x_min - x_padding)
    x_max += x_padding
    y_min -= y_padding
    y_max += y_padding

    width, height = 1100, 650
    left, right, top, bottom = 90, 42, 72, 90
    plot_w = width - left - right
    plot_h = height - top - bottom

    def x_of_value(value: float) -> float:
        return left + plot_w * (value - x_min) / (x_max - x_min)

    def y_of_value(value: float) -> float:
        return top + plot_h * (y_max - value) / (y_max - y_min)

    ordered = clean.sort_values("annual_return")
    frontier_points = " ".join(
        f"{x_of_value(float(row.annual_std)):.2f},{y_of_value(float(row.annual_return)):.2f}"
        for row in ordered.itertuples(index=False)
    )
    portfolio_x = x_of_value(result.annual_std)
    portfolio_y = y_of_value(result.annual_return)
    label_x = min(portfolio_x + 14, width - right - 260)
    label_y = max(top + 18, portfolio_y - 16)

    x_ticks = np.linspace(x_min, x_max, 6)
    y_ticks = np.linspace(y_min, y_max, 6)
    footer = f"组合夏普 {result.sharpe_ratio:.4f}；有效前沿点数 {len(clean)}。"
    if source_note:
        footer += f" 数据来源：{source_note}。"

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif;fill:#1f2937}",
        ".muted{fill:#6b7280;font-size:13px}.axis{stroke:#9ca3af;stroke-width:1}.grid{stroke:#e5e7eb;stroke-width:1}.frontier{fill:none;stroke:#2563eb;stroke-width:2.8}.asset{fill:#f8fafc;stroke:#64748b;stroke-width:1.8}.portfolio{fill:#ef4444;stroke:#7f1d1d;stroke-width:2.3}",
        "</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left}" y="34" font-size="23" font-weight="700">{html_escape(title)}</text>',
        f'<text x="{left}" y="56" class="muted">横轴为年化标准差，纵轴为年化收益率；红点为目标收益组合。</text>',
    ]

    for tick in y_ticks:
        y = y_of_value(float(tick))
        parts.append(f'<line class="grid" x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}"/>')
        parts.append(f'<text class="muted" x="{left-10}" y="{y+4:.2f}" text-anchor="end">{format_pct(float(tick))}</text>')

    for tick in x_ticks:
        x = x_of_value(float(tick))
        parts.append(f'<line class="grid" x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{height-bottom}"/>')
        parts.append(f'<text class="muted" x="{x:.2f}" y="{height-bottom+28}" text-anchor="middle">{format_pct(float(tick))}</text>')

    parts.extend(
        [
            f'<line class="axis" x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}"/>',
            f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}"/>',
            f'<text class="muted" x="{left + plot_w / 2:.2f}" y="{height-28}" text-anchor="middle">年化标准差</text>',
            f'<text class="muted" transform="translate(24 {top + plot_h / 2:.2f}) rotate(-90)" text-anchor="middle">年化收益率</text>',
            f'<polyline class="frontier" points="{frontier_points}"/>',
        ]
    )

    for code, row in asset_points.iterrows():
        x = x_of_value(float(row["annual_std"]))
        y = y_of_value(float(row["annual_return"]))
        parts.append(f'<circle class="asset" cx="{x:.2f}" cy="{y:.2f}" r="5"/>')
        parts.append(f'<text class="muted" x="{x+8:.2f}" y="{y-8:.2f}">{html_escape(str(code))}</text>')

    parts.extend(
        [
            f'<circle class="portfolio" cx="{portfolio_x:.2f}" cy="{portfolio_y:.2f}" r="7"/>',
            f'<text x="{label_x:.2f}" y="{label_y:.2f}" font-size="14" font-weight="700" fill="#7f1d1d">目标组合：收益 {format_pct(result.annual_return)}，波动 {format_pct(result.annual_std)}</text>',
            f'<text x="{left}" y="{height-54}" class="muted">{html_escape(footer)}</text>',
            "</svg>",
        ]
    )
    return "\n".join(parts)


def save_text(content: str, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# HTML 报告
# ---------------------------------------------------------------------------


def _corr_cell_style(value: float) -> str:
    """相关系数热力色：正相关偏红（分散化差），负相关偏蓝（分散化好）。"""
    if not math.isfinite(value):
        return ""
    alpha = min(abs(value), 1.0) * 0.75
    if value >= 0:
        return f"background:rgba(220,38,38,{alpha:.2f});color:{'#fff' if alpha > 0.45 else '#1f2937'}"
    return f"background:rgba(37,99,235,{alpha:.2f});color:{'#fff' if alpha > 0.45 else '#1f2937'}"


def write_html_report(
    output_path: str,
    codes_label: str,
    prices: pd.DataFrame,
    downloads: Sequence[SeriesDownload],
    warnings: Sequence[str],
    asset_stats: pd.DataFrame,
    weights: pd.Series,
    corr: pd.DataFrame,
    result: PortfolioResult,
    backtest: PortfolioBacktest,
    expost: Dict[str, float],
    annual_metrics: pd.DataFrame,
    target_return: float,
    risk_free_rate: float,
    long_only: bool,
    portfolio_svg: Optional[str],
    frontier_svg: Optional[str],
) -> None:
    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    period = f"{prices.index.min().date()} 至 {prices.index.max().date()}"
    constraint = "只做多（权重非负，权重和=1）" if long_only else "允许做空（权重和=1）"

    def card(label: str, value: str, sub: str = "", accent: str = "#0f2a4a") -> str:
        sub_html = f'<div class="card-sub">{html_escape(sub)}</div>' if sub else ""
        return (
            f'<div class="card"><div class="card-label">{html_escape(label)}</div>'
            f'<div class="card-value" style="color:{accent}">{html_escape(value)}</div>{sub_html}</div>'
        )

    cards = "".join(
        [
            card("目标年化收益率", format_pct(target_return)),
            card("组合年化收益（事前·算术）", format_pct(result.annual_return)),
            card("回测复合年化 CAGR（事后）", format_pct(expost.get("cagr", float("nan"))), f"回测 {expost.get('years', 0):.1f} 年"),
            card("年化波动率", format_pct(result.annual_std)),
            card("夏普比率（事前 / 事后）", f"{format_num(result.sharpe_ratio)} / {format_num(expost.get('sharpe', float('nan')))}"),
            card(
                "最大回撤",
                format_pct(backtest.max_drawdown),
                f"{backtest.peak_date.date()} → {backtest.trough_date.date()}",
                accent="#b91c1c",
            ),
        ]
    )

    weight_rows = []
    stats = asset_stats.join(weights.rename("weight")).sort_values("weight", ascending=False)
    for code, row in stats.iterrows():
        w = float(row["weight"]) if math.isfinite(float(row["weight"])) else 0.0
        if abs(w) < 5e-9:
            w = 0.0
        bar_width = min(abs(w) * 100.0, 100.0)
        bar_color = "#dc2626" if w < 0 else "#2563eb"
        weight_rows.append(
            "<tr>"
            f"<td class='mono'>{html_escape(str(code))}</td>"
            f"<td class='num'><b>{format_pct(w)}</b></td>"
            f"<td><div class='wbar'><div style='width:{bar_width:.1f}%;background:{bar_color}'></div></div></td>"
            f"<td class='num'>{format_pct(float(row['annual_return']))}</td>"
            f"<td class='num'>{format_pct(float(row['annual_std']))}</td>"
            f"<td class='num'>{format_num(float(row['sharpe_ratio']))}</td>"
            f"<td class='num'>{format_pct(float(row['period_total_return']))}</td>"
            "</tr>"
        )

    corr_header = "".join(f"<th>{html_escape(str(c))}</th>" for c in corr.columns)
    corr_rows = []
    for idx, row in corr.iterrows():
        cells = "".join(
            f"<td class='num' style='{_corr_cell_style(float(v))}'>{float(v):.2f}</td>" for v in row
        )
        corr_rows.append(f"<tr><th>{html_escape(str(idx))}</th>{cells}</tr>")

    annual_rows = []
    for row in annual_metrics.itertuples(index=False):
        partial = " *" if bool(row.is_partial_year) else ""
        ret = float(row.period_return)
        ret_color = "#166534" if ret >= 0 else "#b91c1c"
        annual_rows.append(
            "<tr>"
            f"<td>{int(row.year)}{partial}</td>"
            f"<td class='mono'>{row.start_date} ~ {row.end_date}</td>"
            f"<td class='num'>{int(row.trading_days)}</td>"
            f"<td class='num' style='color:{ret_color};font-weight:600'>{format_pct(ret)}</td>"
            f"<td class='num'>{format_pct(float(row.annual_std))}</td>"
            f"<td class='num'>{format_num(float(row.sharpe_ratio))}</td>"
            "</tr>"
        )

    source_rows = []
    for item in downloads:
        source_rows.append(
            "<tr>"
            f"<td class='mono'>{html_escape(item.code.label)}</td>"
            f"<td>{html_escape(SOURCE_NAMES.get(item.source, item.source))}</td>"
            f"<td>{html_escape(item.note or '—')}</td>"
            f"<td class='mono'>{item.prices.index.min().date()} ~ {item.prices.index.max().date()}</td>"
            f"<td class='num'>{len(item.prices)}</td>"
            f"<td class='mono' style='word-break:break-all'>{html_escape(item.url)}</td>"
            "</tr>"
        )

    warning_html = ""
    if warnings:
        items = "".join(f"<li>{html_escape(w)}</li>" for w in warnings)
        warning_html = f'<div class="warnbox"><b>提醒</b><ul>{items}</ul></div>'

    frontier_section = (
        f'<h2>有效前沿</h2><div class="chart">{frontier_svg}</div>' if frontier_svg else ""
    )
    curve_section = (
        f'<h2>组合净值曲线（回测）</h2><div class="chart">{portfolio_svg}</div>' if portfolio_svg else ""
    )

    recovery_text = (
        backtest.recovery_date.date().isoformat() if backtest.recovery_date is not None else "尚未恢复至前高"
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>马科维茨组合优化报告 · {html_escape(codes_label)}</title>
<style>
  :root {{ --navy:#0f2a4a; --muted:#6b7280; --line:#e5e7eb; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:0 0 60px; background:#f3f4f6; color:#1f2937;
        font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif; }}
  .wrap {{ max-width:1160px; margin:0 auto; padding:0 24px; }}
  header {{ background:var(--navy); color:#fff; padding:34px 0 28px; margin-bottom:28px; }}
  header h1 {{ margin:0 0 8px; font-size:26px; }}
  header .meta {{ color:#c7d2e3; font-size:14px; line-height:1.7; }}
  h2 {{ font-size:19px; color:var(--navy); border-left:4px solid var(--navy); padding-left:10px; margin:36px 0 14px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(175px,1fr)); gap:14px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:16px 18px;
          box-shadow:0 1px 2px rgba(0,0,0,.04); }}
  .card-label {{ font-size:12.5px; color:var(--muted); margin-bottom:6px; }}
  .card-value {{ font-size:22px; font-weight:700; }}
  .card-sub {{ font-size:12px; color:var(--muted); margin-top:4px; }}
  table {{ border-collapse:collapse; width:100%; background:#fff; font-size:13.5px;
          border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
  th, td {{ padding:9px 12px; border-bottom:1px solid var(--line); text-align:left; }}
  thead th {{ background:#f8fafc; color:#374151; font-size:12.5px; }}
  tbody tr:last-child td {{ border-bottom:none; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .mono {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12.5px; }}
  .wbar {{ background:#eef2f7; border-radius:4px; height:14px; min-width:120px; overflow:hidden; }}
  .wbar div {{ height:100%; border-radius:4px; }}
  .chart {{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:10px; overflow-x:auto; }}
  .chart svg {{ max-width:100%; height:auto; display:block; margin:0 auto; }}
  .warnbox {{ background:#fffbeb; border:1px solid #fcd34d; border-radius:8px; padding:12px 16px; margin:16px 0; font-size:13.5px; }}
  .note {{ color:var(--muted); font-size:12.5px; margin-top:8px; line-height:1.7; }}
  footer {{ margin-top:44px; color:var(--muted); font-size:12.5px; line-height:1.8; }}
</style>
</head>
<body>
<header><div class="wrap">
  <h1>马科维茨组合优化报告</h1>
  <div class="meta">
    标的：{html_escape(codes_label)}　|　样本区间：{period}（共同交易日 {len(prices)} 天）<br>
    约束：{constraint}　|　无风险利率：{format_pct(risk_free_rate)}　|　生成时间：{generated_at}
  </div>
</div></header>
<div class="wrap">

{warning_html}

<h2>核心指标</h2>
<div class="cards">{cards}</div>
<div class="note">
  「事前·算术」为马科维茨优化使用的日均收益 × 252 年化，是优化目标口径；
  「事后 CAGR」为按最优权重日频再平衡回测的复合年化收益，已包含波动拖累，更接近资金实际增长速度。
  回撤恢复日：{html_escape(recovery_text)}；卡玛比率（CAGR/最大回撤）：{format_num(expost.get('calmar', float('nan')))}；
  回测区间总收益：{format_pct(expost.get('total_return', float('nan')))}。
</div>

<h2>最优资产配比</h2>
<table>
<thead><tr><th>代码</th><th class="num">权重</th><th>权重分布</th><th class="num">年化收益</th><th class="num">年化波动</th><th class="num">夏普</th><th class="num">区间总收益</th></tr></thead>
<tbody>{''.join(weight_rows)}</tbody>
</table>

{frontier_section}

{curve_section}

<h2>资产日收益相关系数</h2>
<table>
<thead><tr><th></th>{corr_header}</tr></thead>
<tbody>{''.join(corr_rows)}</tbody>
</table>
<div class="note">红色越深代表正相关越强（分散化效果差），蓝色越深代表负相关越强（分散化效果好）。</div>

<h2>组合逐年表现</h2>
<table>
<thead><tr><th>年份</th><th>区间</th><th class="num">交易日</th><th class="num">收益率</th><th class="num">年化波动</th><th class="num">夏普</th></tr></thead>
<tbody>{''.join(annual_rows)}</tbody>
</table>
<div class="note">* 标记为非完整自然年。</div>

<h2>数据来源</h2>
<table>
<thead><tr><th>代码</th><th>来源</th><th>说明</th><th>区间</th><th class="num">条数</th><th>URL</th></tr></thead>
<tbody>{''.join(source_rows)}</tbody>
</table>

<footer>
  本报告由 markowitz_etf_optimizer_v2.py 基于公开历史净值数据生成，仅供研究参考，不构成投资建议。<br>
  历史收益与协方差不代表未来表现；优化结果对输入的收益估计高度敏感。
</footer>
</div>
</body>
</html>
"""
    save_text(html, output_path)


# ---------------------------------------------------------------------------
# 终端报告
# ---------------------------------------------------------------------------


def print_report(
    prices: pd.DataFrame,
    returns: pd.DataFrame,
    downloads: Sequence[SeriesDownload],
    warnings: Sequence[str],
    asset_stats: pd.DataFrame,
    result: PortfolioResult,
    backtest: PortfolioBacktest,
    expost: Dict[str, float],
    annual_metrics: pd.DataFrame,
    target_return: float,
    risk_free_rate: float,
    long_only: bool,
) -> None:
    if warnings:
        section("提醒")
        for warning in warnings:
            info_warn(warning)

    section("数据与假设")
    rows = [
        ["共同样本区间", f"{prices.index.min().date()} 至 {prices.index.max().date()}"],
        ["价格 / 收益率样本", f"{len(prices)} 条 / {len(returns)} 条"],
        ["约束", "只做多，权重非负，权重和=1" if long_only else "允许做空，权重和=1"],
        ["目标年化收益率", format_pct(target_return)],
        ["无风险利率（夏普用）", format_pct(risk_free_rate)],
        ["数据来源", "；".join(sorted({SOURCE_NAMES.get(d.source, d.source) for d in downloads}))],
    ]
    print(render_table(["项目", "取值"], rows))

    section("最优资产配比")
    stats = asset_stats.join(result.weights.rename("weight")).sort_values("weight", ascending=False)
    weight_rows = []
    for code, row in stats.iterrows():
        w = float(row["weight"])
        if abs(w) < 5e-9:
            w = 0.0
        weight_rows.append(
            [
                str(code),
                format_pct(w),
                weight_bar(w),
                format_pct(float(row["annual_return"])),
                format_pct(float(row["annual_std"])),
                format_num(float(row["sharpe_ratio"])),
                format_pct(float(row["period_total_return"])),
            ]
        )
    print(
        render_table(
            ["代码", "权重", "权重分布", "年化收益", "年化波动", "夏普", "区间总收益"],
            weight_rows,
            aligns=["left", "right", "left", "right", "right", "right", "right"],
        )
    )

    section("资产日收益相关系数")
    corr = returns.corr()
    corr_rows = [[str(idx)] + [f"{float(v):.3f}" for v in row] for idx, row in corr.iterrows()]
    print(render_table([""] + [str(c) for c in corr.columns], corr_rows, aligns=["left"] + ["right"] * len(corr.columns)))
    print(C.dim("  相关系数越低，分散化效果越好。"))

    section("组合指标：事前估计 vs 历史回测")
    recovery_text = (
        backtest.recovery_date.date().isoformat() if backtest.recovery_date is not None else "尚未恢复"
    )
    metric_rows = [
        ["年化收益率", f"{format_pct(result.annual_return)}（算术）", f"{format_pct(expost.get('cagr', float('nan')))}（CAGR）"],
        ["年化波动率", format_pct(result.annual_std), format_pct(expost.get("annual_vol", float("nan")))],
        ["月收益率 / 月波动", f"{format_pct(result.monthly_return)} / {format_pct(result.monthly_std)}", "—"],
        ["夏普比率", format_num(result.sharpe_ratio), format_num(expost.get("sharpe", float("nan")))],
        ["区间总收益", "—", format_pct(expost.get("total_return", float("nan")))],
        [
            "最大回撤",
            "—",
            f"{format_pct(backtest.max_drawdown)}（{backtest.peak_date.date()} → {backtest.trough_date.date()}）",
        ],
        ["回撤恢复日", "—", recovery_text],
        ["卡玛比率 (CAGR/最大回撤)", "—", format_num(expost.get("calmar", float("nan")))],
    ]
    print(
        render_table(
            ["指标", "事前估计（均值-方差）", f"历史回测（{expost.get('years', 0):.1f} 年，日频再平衡）"],
            metric_rows,
            aligns=["left", "right", "right"],
        )
    )
    print(
        C.dim(
            "  说明：事前「算术年化」是优化所用口径；回测 CAGR 含波动拖累（约低 σ²/2），"
            "更接近资金实际复利增速。"
        )
    )

    section("组合逐年表现")
    if annual_metrics.empty:
        print("年度样本不足，无法计算。")
    else:
        annual_rows = []
        for row in annual_metrics.itertuples(index=False):
            year_label = f"{int(row.year)}{'*' if bool(row.is_partial_year) else ''}"
            ret = float(row.period_return)
            ret_text = format_pct(ret)
            ret_text = C.green(ret_text) if ret >= 0 else C.red(ret_text)
            annual_rows.append(
                [
                    year_label,
                    f"{row.start_date} ~ {row.end_date}",
                    str(int(row.trading_days)),
                    ret_text,
                    format_pct(float(row.annual_std)),
                    format_num(float(row.sharpe_ratio)),
                ]
            )
        print(
            render_table(
                ["年份", "区间", "交易日", "收益率", "年化波动", "夏普"],
                annual_rows,
                aligns=["left", "left", "right", "right", "right", "right"],
            )
        )
        print(C.dim("  * 非完整自然年。"))


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="根据 ETF/基金净值数据计算马科维茨目标收益率组合（v2：更准确、更美观）。"
    )
    parser.add_argument("--interactive", action="store_true", help="进入中文交互模式。")
    parser.add_argument(
        "--codes",
        nargs="+",
        default=None,
        help="ETF/基金代码，例如 510300 159915 000216；也可以传 WGLH 基金 URL。",
    )
    parser.add_argument(
        "--target-return",
        default=None,
        help="目标年化收益率，例如 8%% 或 0.08。大于 1 的数字会按百分比处理。",
    )
    parser.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD；默认取最近 N 年。")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD；默认到最新可用数据。")
    parser.add_argument(
        "--years",
        type=int,
        default=DEFAULT_LOOKBACK_YEARS,
        help=f"未指定 --start 时，默认回看最近 N 年数据；默认 {DEFAULT_LOOKBACK_YEARS} 年。",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "wglh", "eastmoney"],
        default="auto",
        help="数据源。默认 auto：优先 WGLH，失败自动切换东方财富公开净值接口。",
    )
    parser.add_argument("--wglh-cookie", default=None, help="WGLH 登录 Cookie；也可用环境变量 WGLH_COOKIE。")
    parser.add_argument(
        "--wglh-cookie-file",
        default=None,
        help=f"WGLH Cookie 文件路径；默认自动读取 {DEFAULT_WGLH_COOKIE_FILE}（若存在）。",
    )
    parser.add_argument(
        "--price-field",
        choices=["LJJZ", "DWJZ"],
        default="LJJZ",
        help="东方财富净值字段：LJJZ=累计净值（默认），DWJZ=单位净值。",
    )
    parser.add_argument("--risk-free-rate", default="0", help="年化无风险利率，例如 2%% 或 0.02。默认 0。")
    parser.add_argument("--trading-days", type=int, default=DEFAULT_TRADING_DAYS, help="年化交易日数量，默认 252。")
    parser.add_argument("--allow-short", action="store_true", help="允许做空。默认只做多。")
    parser.add_argument(
        "--target-policy",
        choices=["nearest", "strict"],
        default="nearest",
        help="目标收益超出可行范围时：nearest=自动用最近可行边界（默认），strict=报错。",
    )
    parser.add_argument(
        "--max-exact-assets",
        type=int,
        default=14,
        help="只做多精确枚举的最大资产数，默认 14。",
    )
    parser.add_argument("--save-prices", default=None, help="可选：保存对齐后的净值价格 CSV。")
    parser.add_argument("--frontier-csv", default=None, help="可选：保存有效前沿 CSV。")
    parser.add_argument(
        "--frontier-chart",
        default=DEFAULT_FRONTIER_CHART,
        help=f"有效前沿 SVG 输出路径，默认 {DEFAULT_FRONTIER_CHART}。传空字符串可跳过。",
    )
    parser.add_argument("--frontier-points", type=int, default=50, help="有效前沿目标收益点数，默认 50。")
    parser.add_argument(
        "--trend-csv",
        default=DEFAULT_TREND_CSV,
        help=f"资产走势 CSV（首日=100），默认 {DEFAULT_TREND_CSV}。传空字符串可跳过。",
    )
    parser.add_argument(
        "--portfolio-chart",
        default=DEFAULT_PORTFOLIO_CHART,
        help=f"组合净值曲线 SVG，默认 {DEFAULT_PORTFOLIO_CHART}。传空字符串可跳过。",
    )
    parser.add_argument(
        "--portfolio-csv",
        default=DEFAULT_PORTFOLIO_CSV,
        help=f"组合净值曲线 CSV，默认 {DEFAULT_PORTFOLIO_CSV}。传空字符串可跳过。",
    )
    parser.add_argument(
        "--correlation-csv",
        default=DEFAULT_CORRELATION_CSV,
        help=f"资产相关系数 CSV，默认 {DEFAULT_CORRELATION_CSV}。传空字符串可跳过。",
    )
    parser.add_argument(
        "--annual-metrics-csv",
        default=DEFAULT_ANNUAL_METRICS_CSV,
        help=f"组合逐年指标 CSV，默认 {DEFAULT_ANNUAL_METRICS_CSV}。传空字符串可跳过。",
    )
    parser.add_argument(
        "--html-report",
        default=DEFAULT_HTML_REPORT,
        help=f"自包含 HTML 报告输出路径，默认 {DEFAULT_HTML_REPORT}。传空字符串可跳过。",
    )
    return parser.parse_args(argv)


def complete_run_args(args: argparse.Namespace) -> argparse.Namespace:
    interactive = args.interactive or not args.codes or not args.target_return
    if interactive:
        print(C.bold(C.blue("═" * 62)))
        print(C.bold(C.blue("  马科维茨 ETF/基金组合优化器 v2")))
        print(C.dim("  数据来源：乌龟量化 / 东方财富公开净值 · 输出终端报告 + HTML 报告"))
        print(C.bold(C.blue("═" * 62)))

    if not args.codes:
        while True:
            code_text = prompt_non_empty("请输入 ETF/基金代码，用空格分开（例如 510300 518880 511010）：")
            codes = code_text.split()
            bad = [c for c in codes if not looks_like_fund_code(c)]
            if not bad:
                args.codes = codes
                break
            print(C.yellow(f"无法识别的代码：{' '.join(bad)}，请使用 6 位数字代码，重新输入。"))

    if not args.target_return:
        while True:
            text = prompt_non_empty("请输入目标年化收益率（例如 8% 或 0.08）：")
            try:
                parse_rate(text, "目标年化收益率")
                args.target_return = text
                break
            except ValueError:
                print(C.yellow(f"无法解析 {text!r}，请输入类似 8% 或 0.08 的数字。"))

    if args.years <= 0:
        raise ValueError("--years 必须为正整数。")

    args.end = normalize_date(args.end, "结束日期")
    args.start = normalize_date(args.start, "开始日期")
    if args.end is None:
        args.end = dt.date.today().isoformat()
    if args.start is None:
        end_date = dt.date.fromisoformat(args.end)
        args.start = subtract_years(end_date, args.years).isoformat()

    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        args = complete_run_args(args)
        target_return = parse_rate(args.target_return, "目标年化收益率")
        risk_free_rate = parse_rate(args.risk_free_rate, "无风险利率")
        cookie = read_cookie_arg(args.wglh_cookie, args.wglh_cookie_file)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except Exception as exc:
        print(C.red(f"错误: {exc}"), file=sys.stderr)
        return 1

    total_steps = 4
    try:
        step(1, total_steps, f"下载净值数据（{args.start} 至 {args.end}，数据源 {args.source}）…")
        prices, downloads, warnings = download_price_frame(
            raw_codes=args.codes,
            source=args.source,
            start=args.start,
            end=args.end,
            cookie=cookie,
            price_field=args.price_field,
        )
        warnings = list(warnings)

        step(2, total_steps, "估计年化收益与协方差矩阵…")
        annual_mu, annual_cov, returns = estimate_annual_return_and_cov(prices, args.trading_days)

        step(3, total_steps, f"求解目标收益 {format_pct(target_return)} 的最小方差组合…")
        result, target_return, target_warning = solve_target_portfolio_with_policy(
            mu=annual_mu,
            cov=annual_cov,
            target_return=target_return,
            risk_free_rate=risk_free_rate,
            long_only=not args.allow_short,
            max_exact_assets=args.max_exact_assets,
            target_policy=args.target_policy,
        )
        if target_warning:
            warnings.append(target_warning)
        validate_portfolio_result(result, annual_mu, target_return, long_only=not args.allow_short)
        info_ok(f"求解完成并通过校验：权重和=1，达到目标收益 {format_pct(target_return)}")

        step(4, total_steps, "回测组合并生成图表与报告…")
        backtest = backtest_static_portfolio(prices, returns, result.weights)
        expost = build_expost_metrics(backtest, risk_free_rate, args.trading_days)
        annual_metrics = build_portfolio_annual_metrics(
            backtest=backtest,
            risk_free_rate=risk_free_rate,
            trading_days=args.trading_days,
        )
        asset_stats = build_asset_stats(prices, annual_mu, annual_cov, risk_free_rate)

        frontier: Optional[pd.DataFrame] = None
        if args.frontier_csv or args.frontier_chart or args.html_report:
            frontier = generate_frontier(
                mu=annual_mu,
                cov=annual_cov,
                risk_free_rate=risk_free_rate,
                long_only=not args.allow_short,
                max_exact_assets=args.max_exact_assets,
                points=args.frontier_points,
            )

        source_note = "；".join(sorted({SOURCE_NAMES.get(d.source, d.source) for d in downloads}))
        portfolio_svg: Optional[str] = None
        frontier_svg: Optional[str] = None
        if args.portfolio_chart or args.html_report:
            portfolio_svg = build_portfolio_svg(
                backtest, title="Markowitz Portfolio Equity Curve", source_note=source_note
            )
        if (args.frontier_chart or args.html_report) and frontier is not None and not frontier.empty:
            frontier_svg = build_frontier_svg(
                frontier=frontier,
                result=result,
                annual_mu=annual_mu,
                annual_cov=annual_cov,
                title="Markowitz Efficient Frontier",
                source_note=source_note,
            )

        # ----- 保存输出文件 -----
        saved: List[Tuple[str, str]] = []

        def save_csv(df: pd.DataFrame, path_text: Optional[str], label: str, index: bool = True) -> None:
            if not path_text:
                return
            path = Path(path_text)
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path, index=index, encoding="utf-8-sig")
            saved.append((label, path_text))

        save_csv(prices, args.save_prices, "对齐净值 CSV")
        save_csv(normalized_prices(prices), args.trend_csv, "资产走势 CSV（首日=100）")
        save_csv(pd.DataFrame({"portfolio_nav": backtest.equity_curve}), args.portfolio_csv, "组合净值 CSV")
        save_csv(returns.corr(), args.correlation_csv, "资产相关系数 CSV")
        save_csv(annual_metrics, args.annual_metrics_csv, "组合逐年指标 CSV", index=False)
        if frontier is not None:
            save_csv(frontier, args.frontier_csv, "有效前沿 CSV", index=False)

        if args.portfolio_chart and portfolio_svg:
            save_text(portfolio_svg, args.portfolio_chart)
            saved.append(("组合净值曲线图 SVG", args.portfolio_chart))
        if args.frontier_chart and frontier_svg:
            save_text(frontier_svg, args.frontier_chart)
            saved.append(("有效前沿图 SVG", args.frontier_chart))

        if args.html_report:
            write_html_report(
                output_path=args.html_report,
                codes_label=" ".join(d.code.label for d in downloads),
                prices=prices,
                downloads=downloads,
                warnings=warnings,
                asset_stats=asset_stats,
                weights=result.weights,
                corr=returns.corr(),
                result=result,
                backtest=backtest,
                expost=expost,
                annual_metrics=annual_metrics,
                target_return=target_return,
                risk_free_rate=risk_free_rate,
                long_only=not args.allow_short,
                portfolio_svg=portfolio_svg,
                frontier_svg=frontier_svg,
            )
            saved.append(("HTML 综合报告", args.html_report))

        # ----- 终端报告 -----
        print_report(
            prices=prices,
            returns=returns,
            downloads=downloads,
            warnings=warnings,
            asset_stats=asset_stats,
            result=result,
            backtest=backtest,
            expost=expost,
            annual_metrics=annual_metrics,
            target_return=target_return,
            risk_free_rate=risk_free_rate,
            long_only=not args.allow_short,
        )

        if saved:
            section("输出文件")
            for label, path_text in saved:
                info_ok(f"{label}: {path_text}")
            if args.html_report:
                print(C.dim(f"\n  在浏览器中查看完整报告：open {args.html_report}"))

        return 0
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except WglhAuthError as exc:
        print(C.red(f"错误: {exc}"), file=sys.stderr)
        print(C.yellow("提示: 可改用 --source eastmoney（公开数据，无需登录）。"), file=sys.stderr)
        return 1
    except (DataSourceError, requests.RequestException) as exc:
        print(C.red(f"数据获取失败: {exc}"), file=sys.stderr)
        print(C.yellow("提示: 请检查网络，或换 --source eastmoney / 减少年限 --years 5 重试。"), file=sys.stderr)
        return 1
    except Exception as exc:
        print(C.red(f"错误: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

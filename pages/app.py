"""浏览器端胶水层。

只替换数据下载这一层：把 eastmoney 静态 CDN 文件里的净值序列拼成和命令行版本
完全相同的 prices DataFrame 与 SeriesDownload 列表。之后的估计、求解、校验、
回测、前沿、出图、报告全部直接调用 markowitz_etf_optimizer_v2 里的原函数，
不做任何重写，因此结果与命令行版本一致。
"""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd

import markowitz_etf_optimizer_v2 as core

MAX_CODES = 12


def validate_inputs(codes_raw, target_return_raw, risk_free_raw, years_raw):
    codes = [c for c in str(codes_raw).replace(",", " ").split() if c]
    if not codes:
        raise ValueError("请输入至少 1 个基金/ETF 代码。")
    if len(codes) > MAX_CODES:
        raise ValueError(f"最多支持 {MAX_CODES} 个代码（只做多精确求解需要枚举子集）。")
    bad = [c for c in codes if not core.looks_like_fund_code(c)]
    if bad:
        raise ValueError(f"无法识别的代码：{' '.join(bad)}，请使用 6 位数字代码。")

    target_return = core.parse_rate(str(target_return_raw), "目标年化收益率")
    risk_free_rate = core.parse_rate(str(risk_free_raw or "0"), "无风险利率")

    years = int(years_raw)
    if not 1 <= years <= 20:
        raise ValueError("回看年数需在 1-20 之间。")

    end = dt.date.today().isoformat()
    start = core.subtract_years(dt.date.fromisoformat(end), years).isoformat()
    return codes, target_return, risk_free_rate, years, start, end


def build_frame(payload_json, start, end):
    """payload: [{code, name, points: [[epoch_ms, nav], ...]}, ...]"""
    payload = json.loads(payload_json)
    downloads = []
    display_labels = {}
    for item in payload:
        code = core.normalize_fund_code(item["code"])
        points = item["points"]
        if not points:
            raise core.DataSourceError(f"{code.label}: 数据源没有返回净值序列。")
        index = pd.to_datetime([p[0] for p in points], unit="ms")
        series = pd.Series([float(p[1]) for p in points], index=index).sort_index()
        series = series[~series.index.duplicated(keep="last")]
        series = series.loc[str(start):str(end)]
        if len(series) < 5:
            raise core.DataSourceError(f"{code.label}: 指定日期范围内的净值样本太少。")
        downloads.append(core.SeriesDownload(
            code=code,
            source="eastmoney",
            url=f"https://fundf10.eastmoney.com/jjjz_{code.eastmoney}.html",
            prices=series,
            note=item.get("field_note", ""),
        ))
        name = (item.get("name") or "").strip()
        display_labels[code.label] = f"{code.label} {name}" if name else code.label

    prices = pd.concat(
        [d.prices.rename(d.code.label) for d in downloads], axis=1, join="inner"
    ).sort_index().dropna(how="any")
    if prices.empty or len(prices) < 4:
        raise core.DataSourceError("多资产共同日期样本太少，无法估计组合收益和协方差。")
    return prices, downloads, display_labels


def run(prices, downloads, display_labels, target_return, risk_free_rate, allow_short):
    """与 markowitz_web.py 中的任务流程逐行对应，调用的是同一批 core 函数。"""
    from dataclasses import replace

    warnings = []
    trading_days = core.DEFAULT_TRADING_DAYS

    annual_mu, annual_cov, returns = core.estimate_annual_return_and_cov(prices, trading_days)

    result, target_return, target_warning = core.solve_target_portfolio_with_policy(
        mu=annual_mu, cov=annual_cov, target_return=target_return,
        risk_free_rate=risk_free_rate, long_only=not allow_short,
        max_exact_assets=14, target_policy="nearest",
    )
    if target_warning:
        warnings.append(target_warning)
    core.validate_portfolio_result(result, annual_mu, target_return, long_only=not allow_short)

    backtest = core.backtest_static_portfolio(prices, returns, result.weights)
    expost = core.build_expost_metrics(backtest, risk_free_rate, trading_days)
    annual_metrics = core.build_portfolio_annual_metrics(
        backtest=backtest, risk_free_rate=risk_free_rate, trading_days=trading_days,
    )
    asset_stats = core.build_asset_stats(prices, annual_mu, annual_cov, risk_free_rate)
    frontier = core.generate_frontier(
        mu=annual_mu, cov=annual_cov, risk_free_rate=risk_free_rate,
        long_only=not allow_short, max_exact_assets=14, points=50,
    )

    source_note = "；".join(sorted({core.SOURCE_NAMES.get(d.source, d.source) for d in downloads}))
    portfolio_svg = core.build_portfolio_svg(
        backtest, title="Markowitz Portfolio Equity Curve", source_note=source_note,
    )
    frontier_svg = None
    if frontier is not None and not frontier.empty:
        display_mu = annual_mu.rename(index=display_labels)
        display_cov = annual_cov.rename(index=display_labels, columns=display_labels)
        frontier_svg = core.build_frontier_svg(
            frontier=frontier, result=result, annual_mu=display_mu,
            annual_cov=display_cov, title="Markowitz Efficient Frontier",
            source_note=source_note,
        )

    report_downloads = [
        replace(d, code=replace(d.code, label=display_labels[d.code.label])) for d in downloads
    ]
    core.write_html_report(
        output_path="/report.html",
        codes_label=" ".join(display_labels[d.code.label] for d in downloads),
        prices=prices, downloads=report_downloads, warnings=warnings,
        asset_stats=asset_stats.rename(index=display_labels),
        weights=result.weights.rename(index=display_labels),
        corr=returns.corr().rename(index=display_labels, columns=display_labels),
        result=result, backtest=backtest, expost=expost,
        annual_metrics=annual_metrics, target_return=target_return,
        risk_free_rate=risk_free_rate, long_only=not allow_short,
        portfolio_svg=portfolio_svg, frontier_svg=frontier_svg,
    )

    weights = result.weights.rename(index=display_labels)
    return json.dumps({
        "warnings": warnings,
        "target_return": float(target_return),
        "annual_return": float(result.annual_return),
        "annual_std": float(result.annual_std),
        "sharpe": float(result.sharpe_ratio),
        "max_drawdown": float(backtest.max_drawdown),
        "peak_date": str(backtest.peak_date.date()),
        "trough_date": str(backtest.trough_date.date()),
        "weights": [[str(k), float(v)] for k, v in weights.items() if abs(float(v)) > 1e-9],
        "start": str(prices.index.min().date()),
        "end": str(prices.index.max().date()),
        "rows": int(len(prices)),
        "portfolio_svg": portfolio_svg,
        "frontier_svg": frontier_svg or "",
        "annual_metrics": annual_metrics.to_dict(orient="records"),
        "correlation": {
            "labels": [str(x) for x in returns.corr().rename(
                index=display_labels, columns=display_labels).columns],
            "matrix": returns.corr().rename(
                index=display_labels, columns=display_labels).round(3).values.tolist(),
        },
        "report_html": open("/report.html", encoding="utf-8").read(),
    })

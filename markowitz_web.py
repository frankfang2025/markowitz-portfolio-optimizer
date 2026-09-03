#!/usr/bin/env python3
"""
马科维茨 ETF/基金组合优化器 · 网页版

准确性保证：本文件不重写任何计算逻辑，所有下载、求解、校验、回测均直接调用
markowitz_etf_optimizer_v2.py 中经过验证的同一套函数（含 validate_portfolio_result
双重校验），因此网页结果与命令行 v2 的结果完全一致。

运行:
    ./myenv/bin/python markowitz_web.py
    然后浏览器打开 http://localhost:8801
"""

from __future__ import annotations

import math
import os
import re
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory

import markowitz_etf_optimizer_v2 as core

# Hosts (Render, Fly, Hugging Face Spaces, Cloud Run) inject the port to bind and
# require binding 0.0.0.0. Locally both default to the original behaviour.
PORT = int(os.environ.get("PORT", "8801"))
HOST = os.environ.get("HOST", "127.0.0.1")
WEB_OUTPUT_ROOT = Path("outputs/web")
MAX_CODES = 12

app = Flask(__name__)

# job_id -> {"status": queued|running|done|error, "step": int, "message": str,
#            "result": dict|None, "error": str|None}
JOBS: Dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def fetch_fund_short_names(codes: List[str]) -> Dict[str, str]:
    """读取基金简称；查询失败时保留原代码，不影响优化流程。"""
    names: Dict[str, str] = {}
    pattern = re.compile(r"var\s+fS_name\s*=\s*['\"]([^'\"]+)['\"]")
    for code in codes:
        try:
            response = core.requests.get(
                f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                headers=core.request_headers(referer=f"https://fund.eastmoney.com/{code}.html"),
                timeout=core.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            match = pattern.search(response.text)
            if match and match.group(1).strip():
                names[code] = match.group(1).strip()
        except Exception:  # 简称只是展示信息，失败不能中断组合计算
            continue
    return names


def _set_job(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        JOBS[job_id].update(fields)


def safe_float(value) -> Optional[float]:
    """numpy/NaN 安全地转成可 JSON 序列化的值（NaN/inf -> None）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def run_pipeline(job_id: str, params: dict) -> None:
    """与 markowitz_etf_optimizer_v2.main() 相同的计算流程，逐步更新任务状态。"""
    try:
        codes: List[str] = params["codes"]
        target_return: float = params["target_return"]
        risk_free_rate: float = params["risk_free_rate"]
        years: int = params["years"]
        source: str = params["source"]
        allow_short: bool = params["allow_short"]
        price_field: str = params["price_field"]
        trading_days = core.DEFAULT_TRADING_DAYS

        import datetime as dt

        end = dt.date.today().isoformat()
        start = core.subtract_years(dt.date.fromisoformat(end), years).isoformat()
        cookie = core.read_cookie_arg(None, None)

        _set_job(job_id, status="running", step=1,
                 message=f"正在下载 {len(codes)} 只基金 {start} 至 {end} 的净值数据（约需 1-2 分钟）…")
        prices, downloads, warnings = core.download_price_frame(
            raw_codes=codes, source=source, start=start, end=end,
            cookie=cookie, price_field=price_field,
        )
        warnings = list(warnings)
        short_names = fetch_fund_short_names([d.code.label for d in downloads])
        display_labels = {
            d.code.label: f"{d.code.label} {short_names[d.code.label]}"
            if d.code.label in short_names else d.code.label
            for d in downloads
        }

        _set_job(job_id, step=2, message="正在估计年化收益与协方差矩阵…")
        annual_mu, annual_cov, returns = core.estimate_annual_return_and_cov(prices, trading_days)

        _set_job(job_id, step=3, message=f"正在求解目标收益 {target_return:.2%} 的最小方差组合…")
        result, target_return, target_warning = core.solve_target_portfolio_with_policy(
            mu=annual_mu, cov=annual_cov, target_return=target_return,
            risk_free_rate=risk_free_rate, long_only=not allow_short,
            max_exact_assets=14, target_policy="nearest",
        )
        if target_warning:
            warnings.append(target_warning)
        core.validate_portfolio_result(result, annual_mu, target_return, long_only=not allow_short)

        _set_job(job_id, step=4, message="正在回测组合并生成图表…")
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

        # 保存本次任务的全部输出文件（含经典 HTML 报告），供页面下载
        job_dir = WEB_OUTPUT_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        prices.to_csv(job_dir / "aligned_prices.csv", encoding="utf-8-sig")
        core.normalized_prices(prices).to_csv(job_dir / "trend.csv", encoding="utf-8-sig")
        pd.DataFrame({"portfolio_nav": backtest.equity_curve}).to_csv(
            job_dir / "portfolio_curve.csv", encoding="utf-8-sig")
        returns.corr().to_csv(job_dir / "correlation.csv", encoding="utf-8-sig")
        annual_metrics.to_csv(job_dir / "annual_metrics.csv", index=False, encoding="utf-8-sig")
        frontier.to_csv(job_dir / "frontier.csv", index=False, encoding="utf-8-sig")
        core.save_text(portfolio_svg, str(job_dir / "portfolio_curve.svg"))
        if frontier_svg:
            core.save_text(frontier_svg, str(job_dir / "effective_frontier.svg"))
        report_downloads = [
            replace(d, code=replace(d.code, label=display_labels[d.code.label]))
            for d in downloads
        ]
        core.write_html_report(
            output_path=str(job_dir / "portfolio_report.html"),
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

        # 组装前端 JSON
        stats = asset_stats.join(result.weights.rename("weight")).sort_values("weight", ascending=False)
        weights_rows = [
            {
                "code": str(code),
                "label": display_labels.get(str(code), str(code)),
                "weight": safe_float(0.0 if abs(float(row["weight"])) < 5e-9 else float(row["weight"])),
                "annual_return": safe_float(row["annual_return"]),
                "annual_std": safe_float(row["annual_std"]),
                "sharpe": safe_float(row["sharpe_ratio"]),
                "total_return": safe_float(row["period_total_return"]),
            }
            for code, row in stats.iterrows()
        ]
        corr = returns.corr()
        annual_rows = [
            {
                "year": int(r.year),
                "range": f"{r.start_date} ~ {r.end_date}",
                "days": int(r.trading_days),
                "ret": safe_float(r.period_return),
                "std": safe_float(r.annual_std),
                "sharpe": safe_float(r.sharpe_ratio),
                "partial": bool(r.is_partial_year),
            }
            for r in annual_metrics.itertuples(index=False)
        ]
        downloads_rows = [
            {
                "code": d.code.label,
                "label": display_labels[d.code.label],
                "source": core.SOURCE_NAMES.get(d.source, d.source),
                "note": d.note or "—",
                "range": f"{d.prices.index.min().date()} ~ {d.prices.index.max().date()}",
                "count": int(len(d.prices)),
                "url": d.url,
            }
            for d in downloads
        ]

        result_payload = {
            "validated": True,
            "warnings": warnings,
            "assumptions": {
                "period": f"{prices.index.min().date()} 至 {prices.index.max().date()}",
                "samples": f"{len(prices)} 条价格 / {len(returns)} 条收益率",
                "constraint": "允许做空，权重和=1" if allow_short else "只做多，权重非负，权重和=1",
                "target_return": safe_float(target_return),
                "risk_free_rate": safe_float(risk_free_rate),
                "source": source_note,
            },
            "weights": weights_rows,
            "correlation": {
                "labels": [display_labels.get(str(c), str(c)) for c in corr.columns],
                "matrix": [[safe_float(v) for v in row] for _, row in corr.iterrows()],
            },
            "metrics": {
                "exante_return": safe_float(result.annual_return),
                "exante_std": safe_float(result.annual_std),
                "exante_sharpe": safe_float(result.sharpe_ratio),
                "monthly_return": safe_float(result.monthly_return),
                "monthly_std": safe_float(result.monthly_std),
                "cagr": safe_float(expost.get("cagr")),
                "expost_vol": safe_float(expost.get("annual_vol")),
                "expost_sharpe": safe_float(expost.get("sharpe")),
                "total_return": safe_float(expost.get("total_return")),
                "calmar": safe_float(expost.get("calmar")),
                "years": safe_float(expost.get("years")),
                "max_drawdown": safe_float(backtest.max_drawdown),
                "dd_peak": backtest.peak_date.date().isoformat(),
                "dd_trough": backtest.trough_date.date().isoformat(),
                "dd_recovery": backtest.recovery_date.date().isoformat() if backtest.recovery_date is not None else None,
            },
            "annual": annual_rows,
            "downloads": downloads_rows,
            "charts": {"frontier": frontier_svg, "curve": portfolio_svg},
            "files": [
                {"label": "完整 HTML 报告", "name": "portfolio_report.html"},
                {"label": "对齐净值 CSV", "name": "aligned_prices.csv"},
                {"label": "组合净值 CSV", "name": "portfolio_curve.csv"},
                {"label": "有效前沿 CSV", "name": "frontier.csv"},
                {"label": "逐年指标 CSV", "name": "annual_metrics.csv"},
                {"label": "相关系数 CSV", "name": "correlation.csv"},
            ],
        }
        _set_job(job_id, status="done", step=4, message="完成", result=result_payload)
    except core.WglhAuthError as exc:
        _set_job(job_id, status="error", error=f"{exc} 建议：数据源选择「东方财富」重试。")
    except (core.DataSourceError, Exception) as exc:  # noqa: BLE001
        _set_job(job_id, status="error", error=str(exc))


@app.post("/api/optimize")
def api_optimize():
    data = request.get_json(force=True, silent=True) or {}
    raw_codes = str(data.get("codes", "")).replace(",", " ").split()
    codes = [c for c in raw_codes if c]

    if not codes:
        return jsonify({"error": "请输入至少 1 个基金/ETF 代码。"}), 400
    if len(codes) > MAX_CODES:
        return jsonify({"error": f"最多支持 {MAX_CODES} 个代码（只做多精确求解需要枚举子集）。"}), 400
    bad = [c for c in codes if not core.looks_like_fund_code(c)]
    if bad:
        return jsonify({"error": f"无法识别的代码：{' '.join(bad)}，请使用 6 位数字代码。"}), 400

    try:
        target_return = core.parse_rate(str(data.get("target_return", "")), "目标年化收益率")
        risk_free_rate = core.parse_rate(str(data.get("risk_free_rate", "0") or "0"), "无风险利率")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        years = int(data.get("years", core.DEFAULT_LOOKBACK_YEARS))
    except (TypeError, ValueError):
        return jsonify({"error": "回看年数必须是整数。"}), 400
    if not 1 <= years <= 20:
        return jsonify({"error": "回看年数需在 1-20 之间。"}), 400

    source = str(data.get("source", "auto"))
    if source not in {"auto", "wglh", "eastmoney"}:
        return jsonify({"error": f"未知数据源: {source}"}), 400
    price_field = str(data.get("price_field", "LJJZ"))
    if price_field not in {"LJJZ", "DWJZ"}:
        return jsonify({"error": f"未知净值字段: {price_field}"}), 400

    params = {
        "codes": codes,
        "target_return": target_return,
        "risk_free_rate": risk_free_rate,
        "years": years,
        "source": source,
        "allow_short": bool(data.get("allow_short", False)),
        "price_field": price_field,
    }
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "step": 0, "message": "排队中…", "result": None, "error": None}
    threading.Thread(target=run_pipeline, args=(job_id, params), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/status/<job_id>")
def api_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify({
            "status": job["status"],
            "step": job["step"],
            "message": job["message"],
            "error": job["error"],
            "result": job["result"] if job["status"] == "done" else None,
        })


@app.get("/files/<job_id>/<path:filename>")
def api_files(job_id: str, filename: str):
    job_dir = (WEB_OUTPUT_ROOT / job_id).resolve()
    root = WEB_OUTPUT_ROOT.resolve()
    if not str(job_dir).startswith(str(root)):
        return "非法路径", 403
    return send_from_directory(job_dir, filename)


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>马科维茨组合优化器</title>
<style>
  :root { --navy:#0f2a4a; --blue:#2563eb; --muted:#6b7280; --line:#e5e7eb; }
  * { box-sizing:border-box; }
  body { margin:0; background:#f3f4f6; color:#1f2937;
         font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif; }
  .wrap { max-width:1160px; margin:0 auto; padding:0 24px 70px; }
  header { background:var(--navy); color:#fff; padding:30px 0 26px; margin-bottom:26px; }
  header h1 { margin:0 0 6px; font-size:25px; }
  header .meta { color:#c7d2e3; font-size:13.5px; }
  h2 { font-size:18px; color:var(--navy); border-left:4px solid var(--navy); padding-left:10px; margin:34px 0 14px; }
  .panel { background:#fff; border:1px solid var(--line); border-radius:12px; padding:22px 24px;
           box-shadow:0 1px 3px rgba(0,0,0,.05); }
  label { display:block; font-size:13px; color:#374151; font-weight:600; margin:14px 0 6px; }
  input[type=text], select { width:100%; padding:10px 12px; font-size:15px; border:1px solid #d1d5db;
           border-radius:8px; background:#fff; }
  input[type=text]:focus, select:focus { outline:2px solid #93c5fd; border-color:var(--blue); }
  .chips { margin-top:8px; display:flex; flex-wrap:wrap; gap:8px; }
  .chip { font-size:12.5px; padding:5px 12px; border-radius:999px; border:1px solid #cbd5e1;
          background:#f8fafc; cursor:pointer; user-select:none; }
  .chip:hover { background:#e0e7ff; border-color:var(--blue); }
  .grid2 { display:grid; grid-template-columns:2fr 1fr; gap:18px; }
  .grid4 { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px; }
  details { margin-top:16px; }
  summary { cursor:pointer; font-size:13.5px; color:var(--blue); font-weight:600; }
  .runbtn { margin-top:22px; width:100%; padding:13px; font-size:16px; font-weight:700; color:#fff;
            background:var(--navy); border:none; border-radius:9px; cursor:pointer; }
  .runbtn:hover { background:#16375e; }
  .runbtn:disabled { background:#9ca3af; cursor:not-allowed; }
  .checkline { display:flex; align-items:center; gap:8px; font-size:13.5px; margin-top:14px; color:#374151; }
  #status { display:none; margin-top:22px; }
  .steps { display:flex; flex-direction:column; gap:9px; margin-top:12px; }
  .stepline { display:flex; align-items:center; gap:10px; font-size:14px; color:var(--muted); }
  .stepline .dot { width:20px; height:20px; border-radius:50%; border:2px solid #d1d5db; flex:0 0 20px;
                   display:flex; align-items:center; justify-content:center; font-size:11px; color:#fff; }
  .stepline.active { color:var(--navy); font-weight:600; }
  .stepline.active .dot { border-color:var(--blue); background:var(--blue); animation:pulse 1.2s infinite; }
  .stepline.done { color:#166534; }
  .stepline.done .dot { border-color:#16a34a; background:#16a34a; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.45} }
  .errbox { background:#fef2f2; border:1px solid #fca5a5; color:#991b1b; border-radius:8px;
            padding:12px 16px; margin-top:16px; font-size:14px; display:none; }
  .warnbox { background:#fffbeb; border:1px solid #fcd34d; border-radius:8px; padding:12px 16px;
             margin:14px 0; font-size:13.5px; }
  .okbadge { display:inline-block; background:#ecfdf5; color:#065f46; border:1px solid #6ee7b7;
             border-radius:999px; padding:4px 14px; font-size:12.5px; font-weight:600; margin-bottom:10px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:10px; padding:15px 17px; }
  .card-label { font-size:12.5px; color:var(--muted); margin-bottom:5px; }
  .card-value { font-size:21px; font-weight:700; color:var(--navy); }
  .card-sub { font-size:12px; color:var(--muted); margin-top:3px; }
  table { border-collapse:collapse; width:100%; background:#fff; font-size:13.5px;
          border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  th, td { padding:9px 12px; border-bottom:1px solid var(--line); text-align:left; }
  thead th { background:#f8fafc; color:#374151; font-size:12.5px; }
  tbody tr:last-child td { border-bottom:none; }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  .mono { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12.5px; }
  .wbar { background:#eef2f7; border-radius:4px; height:14px; min-width:110px; overflow:hidden; }
  .wbar div { height:100%; border-radius:4px; background:var(--blue); }
  .chart { background:#fff; border:1px solid var(--line); border-radius:10px; padding:10px; overflow-x:auto; }
  .chart svg { max-width:100%; height:auto; display:block; margin:0 auto; }
  .filelinks { display:flex; flex-wrap:wrap; gap:10px; }
  .filelinks a { font-size:13px; padding:7px 14px; border:1px solid #cbd5e1; border-radius:8px;
                 background:#f8fafc; color:var(--navy); text-decoration:none; font-weight:600; }
  .filelinks a:hover { background:#e0e7ff; }
  .note { color:var(--muted); font-size:12.5px; margin-top:8px; line-height:1.7; }
  #results { display:none; }
  footer { margin-top:44px; color:var(--muted); font-size:12.5px; line-height:1.8; }
</style>
</head>
<body>
<header><div class="wrap">
  <h1>马科维茨 ETF/基金组合优化器</h1>
  <div class="meta">输入基金代码和目标年化收益率，计算最小方差最优配比 · 数据来源：乌龟量化 / 东方财富公开净值</div>
</div></header>
<div class="wrap">

<div class="panel">
  <div class="grid2">
    <div>
      <label>ETF / 基金代码（空格分开，最多 12 个）</label>
      <input type="text" id="codes" placeholder="例如 510300 518880 511010" value="">
      <div class="chips" id="chips"></div>
    </div>
    <div>
      <label>目标年化收益率</label>
      <input type="text" id="target" placeholder="例如 6% 或 0.06">
    </div>
  </div>
  <details>
    <summary>高级选项（回看年数 / 无风险利率 / 数据源 / 做空）</summary>
    <div class="grid4" style="margin-top:12px">
      <div><label>回看年数</label>
        <select id="years">
          <option value="3">3 年</option><option value="5">5 年</option>
          <option value="10" selected>10 年</option><option value="15">15 年</option>
        </select></div>
      <div><label>无风险利率（夏普用）</label><input type="text" id="rf" value="1.8%"></div>
      <div><label>数据源</label>
        <select id="source">
          <option value="auto" selected>自动（乌龟量化优先）</option>
          <option value="wglh">乌龟量化</option>
          <option value="eastmoney">东方财富</option>
        </select></div>
      <div><label>净值字段（东方财富）</label>
        <select id="pricefield">
          <option value="LJJZ" selected>累计净值</option>
          <option value="DWJZ">单位净值</option>
        </select></div>
    </div>
    <div class="checkline"><input type="checkbox" id="allowshort"><span>允许做空（默认只做多）</span></div>
  </details>
  <button class="runbtn" id="run">开始优化</button>
  <div class="errbox" id="errbox"></div>
  <div id="status">
    <div class="steps">
      <div class="stepline" id="s1"><span class="dot">1</span><span>下载净值数据</span></div>
      <div class="stepline" id="s2"><span class="dot">2</span><span>估计年化收益与协方差</span></div>
      <div class="stepline" id="s3"><span class="dot">3</span><span>求解最小方差组合并校验</span></div>
      <div class="stepline" id="s4"><span class="dot">4</span><span>回测并生成图表</span></div>
    </div>
    <div class="note" id="statusmsg"></div>
  </div>
</div>

<div id="results">
  <h2>核心指标</h2>
  <div class="okbadge" id="badge">✓ 已通过双重校验：权重和 = 1 · 非负约束 · 精确达到目标收益</div>
  <div id="warnings"></div>
  <div class="grid4" id="cards"></div>
  <div class="note" id="metricnote"></div>

  <h2>最优资产配比</h2>
  <div style="overflow-x:auto"><table id="wtable">
    <thead><tr><th>代码</th><th class="num">权重</th><th>权重分布</th><th class="num">年化收益</th>
    <th class="num">年化波动</th><th class="num">夏普</th><th class="num">区间总收益</th></tr></thead>
    <tbody></tbody></table></div>

  <h2>有效前沿</h2>
  <div class="chart" id="frontierchart"></div>

  <h2>组合净值曲线（回测）</h2>
  <div class="chart" id="curvechart"></div>

  <h2>资产日收益相关系数</h2>
  <div style="overflow-x:auto"><table id="ctable"><thead></thead><tbody></tbody></table></div>
  <div class="note">红色越深＝正相关越强（分散化差）；蓝色越深＝负相关越强（分散化好）。</div>

  <h2>组合逐年表现</h2>
  <div style="overflow-x:auto"><table id="atable">
    <thead><tr><th>年份</th><th>区间</th><th class="num">交易日</th><th class="num">收益率</th>
    <th class="num">年化波动</th><th class="num">夏普</th></tr></thead><tbody></tbody></table></div>
  <div class="note">* 非完整自然年。</div>

  <h2>数据来源</h2>
  <div style="overflow-x:auto"><table id="dtable">
    <thead><tr><th>代码</th><th>来源</th><th>说明</th><th>区间</th><th class="num">条数</th></tr></thead>
    <tbody></tbody></table></div>

  <h2>下载</h2>
  <div class="filelinks" id="files"></div>

  <footer>本工具基于公开历史净值数据，仅供研究参考，不构成投资建议。历史收益与协方差不代表未来表现。</footer>
</div>
</div>

<script>
const PRESETS = [
  ["510300","沪深300"],["510500","中证500"],["159915","创业板"],["513100","纳指100"],
  ["518880","黄金"],["511010","国债"],["511260","十年国债"],["512890","红利低波"]
];
const chips = document.getElementById("chips");
PRESETS.forEach(([code,name])=>{
  const b=document.createElement("span"); b.className="chip"; b.textContent=name+" "+code;
  b.onclick=()=>{ const el=document.getElementById("codes");
    const cur=el.value.trim().split(/\\s+/).filter(Boolean);
    if(!cur.includes(code)){ cur.push(code); el.value=cur.join(" "); } };
  chips.appendChild(b);
});

const pct=(v,d=2)=> v==null ? "—" : (v*100).toFixed(d)+"%";
const num=(v,d=2)=> v==null ? "—" : v.toFixed(d);
let poller=null;

function setError(msg){ const e=document.getElementById("errbox");
  e.textContent=msg; e.style.display= msg ? "block":"none"; }

function setSteps(step,status){
  for(let i=1;i<=4;i++){
    const el=document.getElementById("s"+i); el.className="stepline";
    if(status==="done"||i<step) el.classList.add("done");
    else if(i===step) el.classList.add("active");
    el.querySelector(".dot").textContent = (status==="done"||i<step) ? "✓" : i;
  }
}

document.getElementById("run").onclick=async ()=>{
  setError(""); document.getElementById("results").style.display="none";
  const body={
    codes: document.getElementById("codes").value,
    target_return: document.getElementById("target").value,
    years: document.getElementById("years").value,
    risk_free_rate: document.getElementById("rf").value,
    source: document.getElementById("source").value,
    price_field: document.getElementById("pricefield").value,
    allow_short: document.getElementById("allowshort").checked,
  };
  if(!body.codes.trim()){ setError("请输入至少 1 个基金/ETF 代码。"); return; }
  if(!body.target_return.trim()){ setError("请输入目标年化收益率，例如 6% 或 0.06。"); return; }

  const btn=document.getElementById("run"); btn.disabled=true; btn.textContent="计算中…";
  document.getElementById("status").style.display="block"; setSteps(1,"running");
  try{
    const res=await fetch("/api/optimize",{method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    const data=await res.json();
    if(!res.ok){ throw new Error(data.error||"请求失败"); }
    poller=setInterval(()=>poll(data.job_id), 1000);
  }catch(err){ finish(); setError(err.message); }
};

async function poll(jobId){
  try{
    const res=await fetch("/api/status/"+jobId);
    const data=await res.json();
    if(data.status==="error"){ finish(); setError(data.error||"计算失败"); return; }
    setSteps(data.step, data.status);
    document.getElementById("statusmsg").textContent=data.message||"";
    if(data.status==="done"){ finish(); render(data.result, jobId); }
  }catch(err){ /* 网络抖动，下一轮重试 */ }
}

function finish(){ if(poller){clearInterval(poller); poller=null;}
  const btn=document.getElementById("run"); btn.disabled=false; btn.textContent="开始优化"; }

function card(label,value,sub,color){
  return `<div class="card"><div class="card-label">${label}</div>
    <div class="card-value" style="color:${color||'var(--navy)'}">${value}</div>
    ${sub?`<div class="card-sub">${sub}</div>`:""}</div>`;
}

function corrStyle(v){
  if(v==null) return "";
  const a=Math.min(Math.abs(v),1)*0.75;
  const color = v>=0 ? `rgba(220,38,38,${a.toFixed(2)})` : `rgba(37,99,235,${a.toFixed(2)})`;
  return `background:${color};color:${a>0.45?"#fff":"#1f2937"}`;
}

function render(r, jobId){
  document.getElementById("results").style.display="block";
  const m=r.metrics, a=r.assumptions;

  document.getElementById("warnings").innerHTML = r.warnings.length
    ? `<div class="warnbox"><b>提醒</b><ul>${r.warnings.map(w=>`<li>${w}</li>`).join("")}</ul></div>` : "";

  document.getElementById("cards").innerHTML =
    card("目标年化收益率", pct(a.target_return)) +
    card("组合年化收益（事前·算术）", pct(m.exante_return)) +
    card("回测复合年化 CAGR（事后）", pct(m.cagr), `回测 ${num(m.years,1)} 年`) +
    card("年化波动率", pct(m.exante_std)) +
    card("夏普比率（事前 / 事后）", `${num(m.exante_sharpe)} / ${num(m.expost_sharpe)}`) +
    card("最大回撤", pct(m.max_drawdown), `${m.dd_peak} → ${m.dd_trough}`, "#b91c1c");

  document.getElementById("metricnote").textContent =
    `样本区间 ${a.period}（${a.samples}）· ${a.constraint} · 无风险利率 ${pct(a.risk_free_rate)} · `+
    `回测区间总收益 ${pct(m.total_return)} · 卡玛比率 ${num(m.calmar)} · `+
    `回撤恢复日 ${m.dd_recovery||"尚未恢复"} · 数据来源 ${a.source}`;

  document.querySelector("#wtable tbody").innerHTML = r.weights.map(w=>`<tr>
    <td class="mono">${w.label}</td><td class="num"><b>${pct(w.weight)}</b></td>
    <td><div class="wbar"><div style="width:${Math.min(Math.abs(w.weight||0)*100,100).toFixed(1)}%"></div></div></td>
    <td class="num">${pct(w.annual_return)}</td><td class="num">${pct(w.annual_std)}</td>
    <td class="num">${num(w.sharpe)}</td><td class="num">${pct(w.total_return)}</td></tr>`).join("");

  document.getElementById("frontierchart").innerHTML = r.charts.frontier || "<div class='note'>无有效前沿数据</div>";
  document.getElementById("curvechart").innerHTML = r.charts.curve || "";

  const labels=r.correlation.labels;
  document.querySelector("#ctable thead").innerHTML =
    "<tr><th></th>"+labels.map(l=>`<th>${l}</th>`).join("")+"</tr>";
  document.querySelector("#ctable tbody").innerHTML = r.correlation.matrix.map((row,i)=>
    `<tr><th>${labels[i]}</th>`+row.map(v=>
      `<td class="num" style="${corrStyle(v)}">${v==null?"—":v.toFixed(2)}</td>`).join("")+"</tr>").join("");

  document.querySelector("#atable tbody").innerHTML = r.annual.map(y=>`<tr>
    <td>${y.year}${y.partial?" *":""}</td><td class="mono">${y.range}</td>
    <td class="num">${y.days}</td>
    <td class="num" style="color:${(y.ret??0)>=0?"#166534":"#b91c1c"};font-weight:600">${pct(y.ret)}</td>
    <td class="num">${pct(y.std)}</td><td class="num">${num(y.sharpe)}</td></tr>`).join("");

  document.querySelector("#dtable tbody").innerHTML = r.downloads.map(d=>`<tr>
    <td class="mono">${d.label}</td><td>${d.source}</td><td>${d.note}</td>
    <td class="mono">${d.range}</td><td class="num">${d.count}</td></tr>`).join("");

  document.getElementById("files").innerHTML = r.files.map(f=>
    `<a href="/files/${jobId}/${f.name}" target="_blank">⬇ ${f.label}</a>`).join("");

  document.getElementById("results").scrollIntoView({behavior:"smooth"});
}
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return PAGE


if __name__ == "__main__":
    print(f"马科维茨组合优化器网页版已启动：http://localhost:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)

# 马科维茨 ETF/基金组合优化器

脚本：`markowitz_etf_optimizer.py`

功能：

- 直接运行后，依次询问用户 ETF/基金代码和目标年化收益率；
- 默认从乌龟量化 `https://wglh.com/fund/f/f000216/` 这类基金页抓取过去 10 年数据；
- 按马科维茨理论，在给定目标年化收益率下求最小方差组合；
- 输出每个资产配比、每个资产过去 10 年走势、年化收益率、年化标准差、夏普比率等；
- 回测组合过去 10 年资产曲线，计算最大回撤，并在 SVG 图上标注最大回撤；
- 输出每个资产之间的日收益相关系数矩阵；
- 输出组合过去 10 年逐年收益率、年化标准差和夏普比率；
- 画出马科维茨有效前沿，并标出目标投资组合在前沿上的位置；
- 默认保存资产走势、组合净值、组合曲线图、年度指标、有效前沿图和相关系数 CSV。

## 设置乌龟量化 Cookie

先在浏览器登录乌龟量化：

```bash
python3 setup_wglh_cookie.py --open-login
```

登录完成后，从本机 Chrome 只读取 `wglh.com` 的 Cookie 并保存到 `.wglh_cookie`：

```bash
python3 setup_wglh_cookie.py --from-chrome
```

之后就可以强制使用乌龟量化：

```bash
python3 markowitz_etf_optimizer.py \
  --codes 510300 159915 000216 \
  --target-return 8% \
  --source wglh
```

## 快速运行

直接进入交互模式：

```bash
python3 markowitz_etf_optimizer.py
```

程序会先问：

```text
请输入要抓取的 ETF/基金代码（用空格分开）：
请输入目标年化收益率（例如 8% 或 0.08）：
```

也可以用命令行一次性传参：

```bash
python3 markowitz_etf_optimizer.py \
  --codes 510300 159915 000216 \
  --target-return 8% \
  --risk-free-rate 2% \
  --frontier-csv outputs/markowitz_frontier.csv \
  --save-prices outputs/markowitz_prices.csv
```

默认输出文件：

- `outputs/wglh_10y_trend.csv`：每个资产首日归一为 100 的走势。
- `outputs/portfolio_10y_curve.csv`：投资组合回测净值曲线。
- `outputs/portfolio_10y_curve.svg`：投资组合过去 10 年资产曲线，并标出最大回撤。
- `outputs/asset_correlation.csv`：资产日收益相关系数矩阵。
- `outputs/portfolio_annual_metrics.csv`：组合逐年收益率、年化标准差和夏普比率。
- `outputs/effective_frontier.svg`：有效前沿图，并标出目标组合位置。

## 常用参数

- `--codes`：ETF/基金代码，支持多个 6 位代码，也支持 WGLH URL。
- `--target-return`：目标年化收益率，例如 `8%` 或 `0.08`。
- `--source`：`wglh`、`auto`、`eastmoney`。默认 `wglh`。
- `--years`：没有指定 `--start` 时默认回看年数，默认 `10`。
- `--wglh-cookie` / `--wglh-cookie-file`：如果要强制使用乌龟量化登录后的数据，可传登录 Cookie；不传时会自动读取 `.wglh_cookie`。
- `--price-field`：东方财富净值字段，`LJJZ` 为累计净值，`DWJZ` 为单位净值。默认 `LJJZ`。
- `--allow-short`：允许做空；默认只做多，权重非负且权重和为 1。
- `--target-policy`：目标收益率超出只做多可行范围时的处理方式；默认 `nearest` 会自动使用最近可行边界，`strict` 会直接报错。
- `--trend-csv`：保存资产走势 CSV（首日=100），默认 `outputs/wglh_10y_trend.csv`。
- `--portfolio-chart`：保存组合资产曲线 SVG，默认 `outputs/portfolio_10y_curve.svg`。
- `--portfolio-csv`：保存组合净值曲线 CSV，默认 `outputs/portfolio_10y_curve.csv`。
- `--correlation-csv`：保存相关系数 CSV，默认 `outputs/asset_correlation.csv`。
- `--annual-metrics-csv`：保存组合逐年指标 CSV，默认 `outputs/portfolio_annual_metrics.csv`。
- `--frontier-chart`：保存有效前沿 SVG，默认 `outputs/effective_frontier.svg`。
- `--frontier-csv`：可选保存有效前沿明细 CSV。

说明：只做多时，如果目标年化收益率不在输入资产历史估计年化收益率范围内，程序默认会自动使用最近的可行边界，并在报告里提示；如需保持严格约束，使用 `--target-policy strict`。

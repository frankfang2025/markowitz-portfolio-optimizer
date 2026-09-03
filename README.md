# 马科维茨 ETF/基金组合优化器 · 网页版

输入若干只 ETF/基金代码和目标年化收益率，程序下载历史净值，按马科维茨均值-方差
理论求解组合权重，并输出回测净值曲线、最大回撤、相关系数矩阵、逐年指标和有效前沿。

网页版不重写任何计算逻辑：`markowitz_web.py` 直接调用 `markowitz_etf_optimizer_v2.py`
中同一套已验证的函数（含 `validate_portfolio_result` 双重校验），因此网页结果与命令行
版本完全一致。

## 在线运行（无需安装）

**https://frankfang2025.github.io/markowitz-portfolio-optimizer/**

打开即用。GitHub Pages 只能托管静态文件、跑不了 Flask，所以这个版本用 Pyodide 把真实的
CPython + numpy + pandas 加载到访问者的浏览器里运行。**只替换了数据下载这一层**，
`estimate_annual_return_and_cov`、`solve_target_portfolio_with_policy`、
`validate_portfolio_result`、回测、有效前沿、SVG 出图和 HTML 报告全部直接调用
`markowitz_etf_optimizer_v2.py` 中的原函数，未做任何重写。

首次打开需下载约 25MB 的 Python 运行时（之后浏览器缓存），一次优化约 1-3 分钟。

### 为什么换数据源

天天基金的 `api.fund.eastmoney.com/f10/lsjz` 接口有 Referer 校验，而浏览器脚本被禁止
设置 `Referer`，实测直接请求会返回 `ErrCode:-999`。因此浏览器版改用同站的静态净值文件
`fund.eastmoney.com/pingzhongdata/<code>.js`（无 Referer 校验），经公共 CORS 代理获取。

两个数据源已验证等价：510300 近一年，按北京时间对齐后 **242/243 个交易日重合，
最大绝对差 0.0000000000**。

注意该文件的时间戳是北京时间零点的 epoch 毫秒，按 UTC 解析会整体早一天（实测只有
190/243 天对得上，最大差 0.0615），代码里固定 +8 小时后再 normalize。

## 本地运行

```bash
pip install -r requirements.txt
python markowitz_web.py
```

打开 http://localhost:8801 。

## 部署

镜像已经写好，任何支持 Docker 的平台都能直接跑：

```bash
docker build -t markowitz .
docker run -p 8801:8801 markowitz
```

`render.yaml` 是 Render 的 Blueprint，连上仓库即可一键部署。

**部署时必须单进程运行。** 任务状态存在进程内的 `JOBS` 字典里（由 `threading.Lock`
保护），每次优化跑在后台线程上。如果用多个 worker，提交任务的进程和轮询状态的进程会
不是同一个，前端会一直查不到结果。Dockerfile 里已经固定成：

```
gunicorn --workers 1 --threads 8 --timeout 600 markowitz_web:app
```

单次优化约需 1-2 分钟，`--timeout 600` 是为此留的余量。

## 数据源

- `auto`（默认）：先试乌龟量化，失败则自动回退到东方财富，并在报告里给出提示。
- `eastmoney`：东方财富基金净值接口。
- `wglh`：强制使用乌龟量化。

乌龟量化的登录数据需要 Cookie。程序按 `--wglh-cookie` → `--wglh-cookie-file` →
环境变量 `WGLH_COOKIE` → 本地 `.wglh_cookie` 文件的顺序查找，都没有就跳过。

**不要把 `.wglh_cookie` 提交到仓库或放进镜像**——它是一份可用的登录会话（`csrftoken`
和 `sessionid`），泄露等于把账号交出去。`.gitignore` 已经排除了它。公开部署时不要设置
`WGLH_COOKIE`：默认的 `auto` 会自动走东方财富，功能不受影响。

## 公开部署的注意事项

这个服务没有鉴权。任何拿到网址的人都能提交计算任务，每次会向数据源发起下载请求
（单次上限 `MAX_CODES = 12` 只基金）。如果不希望被陌生人使用，请在平台层加访问控制，
或者不要公开分享网址。

## 命令行版本

命令行用法和全部参数见 [README_markowitz_etf_optimizer.md](./README_markowitz_etf_optimizer.md)。

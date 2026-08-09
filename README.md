# 2026 省级政府工作报告采集（阶段一）

> [!NOTE]
> 这是一次早期的 **AI + 政策研究工作流** 探索：资料采集完成，但研究阶段因问题不够明确、缺少分析框架与合适的数据结构而没有跑通。**核心提醒：先定义研究问题和分析框架，再决定采集什么数据。** 详见 [PROJECT_REVIEW.md](./PROJECT_REVIEW.md)。

目标：采集 2026 年 31 个省级政府工作报告，统一输出分省 CSV（每省 1 行，字段固定）。

## 文件说明

- `collect_reports_2026.py`: 主脚本（会话初始化、抓取、补齐、校验）
- `provinces_31.json`: 31 省级单位清单
- `manual_fallback_urls.csv`: 人工补齐 URL 映射模板（可选）
- `requirements.txt`: 依赖清单

## 依赖安装

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## 1) 初始化会话（首次）

```bash
python collect_reports_2026.py init-session
```

执行后会打开浏览器，请手动登录人民数据（含验证码），登录成功后回终端按回车，脚本会保存会话到：

- `state/peopledata_state.json`

当前默认从福建省图书馆数字资源入口进入：

- `https://s.fjlib.net:6443/interlibSSO/main/main.jsp`

## 2) 执行采集

```bash
python collect_reports_2026.py run --headless
```

默认行为：

- 抓到一个省份就立即写入一个 CSV
- 重跑时自动跳过已有分省 CSV，只补抓缺口
- 如需全量重跑，追加 `--overwrite-existing`

常用参数：

- `--output-dir output/2026_raw_text`
- `--manual-fallback-csv manual_fallback_urls.csv`
- `--max-pages-per-province 6`
- `--min-text-length 2000`
- `--disable-official-fallback`（禁用官方补齐）
- `--overwrite-existing`（忽略已有输出，强制重抓）

输出结果：

- 分省文件：`output/2026_raw_text/2026_<省份>_政府工作报告.csv`
- 失败清单：`output/2026_raw_text/failures_2026.csv`
- 运行摘要：`output/2026_raw_text/run_manifest_2026.csv`
- 运行日志：`logs/collect_2026.log`

## 3) 输出校验

```bash
python collect_reports_2026.py validate --output-dir output/2026_raw_text
```

校验项：

- 31 个分省 CSV 是否齐全
- 字段顺序是否为 `province,year,title,source_url,publish_date,full_text`
- 每省是否仅 1 行
- `year` 是否为 `2026`
- `publish_date` 格式是否 `YYYY-MM-DD`
- `full_text` 长度是否达到阈值

## 手工补齐映射（可选）

如果某省自动补齐稳定性不足，可在 `manual_fallback_urls.csv` 写入：

```csv
province,url
黑龙江省,https://www.hlj.gov.cn/...
```

脚本会优先尝试该链接，再进行搜索引擎检索。

# 2026 中国省级政府工作报告数据集与采集工具

收集中国 31 个省级行政区的 2026 年政府工作报告，并保留对应的采集、补齐与校验工具，用于政策比较与 AI 辅助研究。

> [!NOTE]
> 这是一次早期的 **AI + 政策研究工作流** 探索。**核心提醒：先定义研究问题和分析框架，再决定采集什么数据。** 完整复盘见 [PROJECT_REVIEW.md](./PROJECT_REVIEW.md)。

## 项目状态

| 项目 | 状态 |
| --- | --- |
| 31 省级政府工作报告采集 | 已完成 |
| 数据整理与校验 | 已完成 |
| 跨省政策比较与研究分析 | 未完成 |
| 维护状态 | 停止继续开发，作为实践记录保留 |

## 这个仓库包含什么

- `output/2026_raw_text/`：2026 年各省级政府工作报告分省 CSV 数据
- `collect_reports_2026.py`：采集、补齐与校验主脚本
- `provinces_31.json`：31 个省级行政区清单
- `manual_fallback_urls.csv`：自动抓取失败时的人工补齐 URL 映射
- `PROJECT_REVIEW.md`：项目背景、局限与复盘

当前数据结构采用“每省一个 CSV、每省一行全文”的方式，字段统一为：

`province, year, title, source_url, publish_date, full_text`

> 这一结构完成了资料采集，但并不是理想的研究型数据结构。若重新设计，应先明确研究问题和分析维度，再决定文本拆分、标注和字段组织方式。

## 可以用来做什么

这个仓库更适合作为一份原始政策文本集合和工作流样本，可用于：

- 比较不同省份的产业政策与政策重点
- 对政府工作报告进行关键词、主题或政策工具分析
- 尝试 LLM / AI 辅助的批量政策文本分析
- 复现或改造省级政府工作报告的自动采集流程

它不是一个已经完成的政策研究成果，而是一次从“资料采集”走向“政策分析”的阶段性实践。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. 初始化会话

```bash
python collect_reports_2026.py init-session
```

执行后会打开浏览器，请手动登录人民数据（含验证码）。登录成功后回到终端按回车，脚本会保存会话到：

```text
state/peopledata_state.json
```

当前默认从福建省图书馆数字资源入口进入：

```text
https://s.fjlib.net:6443/interlibSSO/main/main.jsp
```

### 3. 执行采集

```bash
python collect_reports_2026.py run --headless
```

默认行为：

- 抓到一个省份后立即写入对应 CSV
- 重跑时自动跳过已有分省 CSV，只补抓缺口
- 如需全量重跑，追加 `--overwrite-existing`

常用参数：

- `--output-dir output/2026_raw_text`
- `--manual-fallback-csv manual_fallback_urls.csv`
- `--max-pages-per-province 6`
- `--min-text-length 2000`
- `--disable-official-fallback`：禁用官方补齐
- `--overwrite-existing`：忽略已有输出，强制重抓

输出结果：

- 分省文件：`output/2026_raw_text/2026_<省份>_政府工作报告.csv`
- 失败清单：`output/2026_raw_text/failures_2026.csv`
- 运行摘要：`output/2026_raw_text/run_manifest_2026.csv`
- 运行日志：`logs/collect_2026.log`

## 输出校验

```bash
python collect_reports_2026.py validate --output-dir output/2026_raw_text
```

校验内容包括：

- 31 个分省 CSV 是否齐全
- 字段顺序是否正确
- 每省是否仅 1 行
- `year` 是否为 `2026`
- `publish_date` 是否符合 `YYYY-MM-DD`
- `full_text` 长度是否达到阈值

## 手工补齐

如果某省自动补齐稳定性不足，可在 `manual_fallback_urls.csv` 中写入：

```csv
province,url
黑龙江省,https://www.hlj.gov.cn/...
```

脚本会优先尝试该链接，再进行搜索引擎检索。

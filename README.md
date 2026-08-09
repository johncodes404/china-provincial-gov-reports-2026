# 2026 省级政府工作报告采集（阶段一）

> [!NOTE]
> ## 项目复盘
>
> 这是一次把 AI 融入政策研究工作流的小探索。最初的设想，是批量收集 2026 年 31 个省级政府工作报告，对比不同省份的产业政策，并为以后选择省份、城市和研究方向提供参考。
>
> 资料采集阶段基本完成，但研究阶段没有真正跑通。原因主要有三点：**文本量过大、缺少稳定的分析流程与框架、研究问题本身不够具体。** “我应该去哪个省、哪个城市”虽然是真实问题，但还不足以直接转化成可操作的政策比较指标。
>
> 现在回看，数据结构本身也并不理想，例如“一个省份一行 CSV”不利于后续拆分、标注和比较。如果重新设计，我会先明确研究问题和分析维度，再决定抓取哪些字段、采用什么数据结构，而不是先把所有全文抓下来再思考如何分析。
>
> **这个项目最重要的提醒：政策研究不能从“我已经收集了很多资料”开始，而应该从“我要回答什么问题”开始。采集能力解决的是输入问题，真正决定研究质量的是问题意识、分析框架和输出机制。**
>
> 本仓库保留并计划公开，主要作为我早期“AI + 政策研究工作流”的实践记录，而不是一个已经完成的研究成果。

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

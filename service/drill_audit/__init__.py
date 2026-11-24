"""应急演练记录合规审查（演练 PDF × 知识库 EOP）。

对外入口：
- `pipeline.run_audit` —— 定位演练周 → 下载 4 份班组 PDF → OCR → 检索 EOP →
  LLM 比对 → 结构化合规结论（原始 OCR 文本不进模型上下文）。
- `cli` —— 本机核对脚本（`python -m service.drill_audit.cli --help`）。
"""

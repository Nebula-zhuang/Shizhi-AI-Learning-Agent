"""文件解析层（P1 已实现）。

  base.py         - 统一文档结构契约（ParsedDocument / Block / Page）
  router.py       - 类型校验、落盘、按类型分派
  pdf_parser.py   - PyMuPDF：逐页文本 + 页眉页脚剔除 + 标题识别 + 图片提取
  text_parser.py  - TXT / MD：编码探测 + 虚拟分页
  image_parser.py - 独立图片：识别尺寸并保留资源
  chunker.py      - 语义分块
  storage.py      - 落盘、hash、路径解析与清理

规则与取值理由见 skills/doc-ingestion/SKILL.md。
"""

"""core 包：Data Agent 确定性引擎（零 LLM）。

分层：
- 语义层（OAG/翻译共用）：
  * Ontology —— 本体加载与关系图（对象/指标/属性/关系/黑话索引）
  * MqlValidator —— MQL 校验（schema + 物理渗入检测）
  * Translator —— MQL → 可执行 SQL（表选择/JOIN/权限注入/多方言/默认 t-1）
  * metadata —— 元数据检索（表/指标口径/血缘/就绪时间）
- 执行层：
  * Executor —— 只读执行（SQLite 本地 / mysql/doris/hive/sparksql 远程驱动）
- 路径 D（建模/ETL）：
  * ddl_gen —— Spark SQL 建表草稿
  * etl_gen —— Spark SQL INSERT OVERWRITE 聚合脚本
  * modeling_plan —— 变更清单 → 配对 DDL+ETL（N 表 = N 对）
- 安全治理：
  * query_token —— 查询令牌（semantic_translate 签发 / execute_sql 校验）
  * confirm_token —— 确认令牌（mql_explain 签发 / semantic_translate 校验）
  * audit —— 审计日志（jsonl + 轮转）
  * runtime_log —— 运行日志（jsonl + 轮转）
- 运维：
  * startup_check —— 启动自检（ontology/介质/日志目录）
  * config —— 运行时配置（backend/.env，DATA_AGENT_* 前缀）
"""
from .executor import Executor
from .mql_validator import MqlValidator
from .ontology_loader import Ontology
from .translator import TranslateError, Translator

__all__ = ["Ontology", "MqlValidator", "Translator", "TranslateError", "Executor"]

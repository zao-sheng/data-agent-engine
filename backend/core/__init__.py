"""core 包：Data Agent 确定性引擎（零 LLM）。

- Ontology: 本体加载与关系图（OAG 与翻译引擎共用）
- MqlValidator: MQL 校验（安全边界：物理渗入检测）
- Translator: MQL → 可执行 SQL（表选择 / JOIN 链 / 权限注入 / 时间默认 t-1）
- Executor: 只读执行器
"""
from .executor import Executor
from .mql_validator import MqlValidator
from .ontology_loader import Ontology
from .translator import Translator, TranslateError

__all__ = ["Ontology", "MqlValidator", "Translator", "TranslateError", "Executor"]

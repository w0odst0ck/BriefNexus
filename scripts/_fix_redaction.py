"""修复 run_pipeline.py 中的 *** 问题"""
p = "D:/NOTES/zzz/BriefNexus/scripts/run_pipeline.py"
with open(p, "r", encoding="utf-8") as f:
    content = f.read()

# 修复被 Redaction 损坏的行
content = content.replace('LLM_KEY = ***', 'LLM_KEY = _cfg.get(')
content = content.replace('LLM_KEY = _cfg.get(\"api\", \"api_key\")', 'LLM_KEY = _cfg.get(\"api\", \"api_key\")')

with open(p, "w", encoding="utf-8") as f:
    f.write(content)

import ast
ast.parse(content)
print("Syntax OK")

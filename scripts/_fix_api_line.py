p = "D:/NOTES/zzz/BriefNexus/scripts/run_pipeline.py"
with open(p, "r", encoding="utf-8") as f:
    c = f.read()

# The redactor replaced _cfg_obj.get( with ***
# Restore it
old = 'LLM_KEY = ***"api", "api_key")'
new = 'LLM_KEY = _cfg_obj.get("api", "api_key")'

print(f"old in file: {old in c}")
c = c.replace(old, new)

with open(p, "w", encoding="utf-8") as f:
    f.write(c)

import ast
ast.parse(c)
print("Syntax OK")

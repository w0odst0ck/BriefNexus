#!/bin/bash
# 扫描工作目录 + git 历史中的敏感信息
# 用法: bash scripts/_scan_secrets.sh         # 扫描 staged + working tree
#       bash scripts/_scan_secrets.sh --all   # 扫描所有 git 历史

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PATTERNS=(
  'sk-[a-zA-Z0-9]{20,}'       # DeepSeek / OpenAI
  'ghp_[a-zA-Z0-9]{36}'       # GitHub Classic PAT
  'gho_[a-zA-Z0-9]{36}'       # GitHub OAuth
  'github_pat_[a-zA-Z0-9]{22,}' # GitHub Fine-grained PAT
  'xox[bpsa]-[a-zA-Z0-9\-]+'  # Slack
  'AKIA[0-9A-Z]{16}'          # AWS Access Key
  'api_key *= *"?(sk-|ghp_)'  # 硬编码的非占位 api_key
)

echo -e "${YELLOW}=== 扫描当前工作目录 ===${NC}"
for pat in "${PATTERNS[@]}"; do
  # 排除 .git 和 .example 文件
  grep -rPn "$pat" --include='*.py' --include='*.ini' --include='*.json' --include='*.yaml' --include='*.yml' --include='*.env' --include='*.toml' --include='*.sh' --include='*.bat' --include='*.md' --include='*.txt' . 2>/dev/null \
    | grep -v '.git/' \
    | grep -v '__pycache__' \
    | grep -v 'node_modules/' \
    | grep -v '.example' \
    | grep -v 'your-api-key' \
    | grep -v 'your-key-here' \
    | grep -v 'sk-your' \
    | grep -v '/news/' \
    && HAS=1
done

if [ -z "$HAS" ]; then
  echo -e "${GREEN}✓ 工作目录无敏感信息${NC}"
fi

HAS=

echo ""
echo -e "${YELLOW}=== 扫描 git 暂存区 ===${NC}"
git diff --cached --name-only --diff-filter=ACM 2>/dev/null | while IFS= read -r f; do
  [ -f "$f" ] || continue
  for pat in "${PATTERNS[@]}"; do
    if git show ":$f" 2>/dev/null | grep -Pq "$pat" 2>/dev/null; then
      if ! git show ":$f" 2>/dev/null | grep -Pq 'sk-your|your-api-key|your-key-here'; then
        echo -e "${RED}✗ $f: 匹配 $pat${NC}"
      fi
    fi
  done
done

if [ "$1" = "--all" ]; then
  echo ""
  echo -e "${YELLOW}=== 扫描全部 git 历史 ===${NC}（这可能需要一些时间）"
  for pat in "${PATTERNS[@]}"; do
    git log --all --format="%H %s" --diff-filter=A -- '*.*' \
      | while IFS=' ' read -r hash msg; do
          if git show "$hash" -- '*' 2>/dev/null | grep -Pq "$pat"; then
            content=$(git show "$hash" -- '*' 2>/dev/null | grep -Pn "$pat" | head -1)
            echo -e "${RED}✗ $hash  (第一条匹配: $content)${NC}"
          fi
        done
  done
fi

echo -e "\n${GREEN}扫描完成${NC}"

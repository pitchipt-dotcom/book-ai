#!/usr/bin/env bash
# 公開前チェック: コミット対象(追跡 + 未追跡で .gitignore 対象外)のファイルを検査する。
# 使い方: scripts/check_secrets.sh   (終了コード 0 = 問題なし, 1 = 要確認あり)
set -u
cd "$(git rev-parse --show-toplevel)"
fail=0
files=$(git ls-files --cached --others --exclude-standard)

echo "== 対象ファイル数: $(printf '%s\n' "$files" | wc -l)"

echo "== 1. 公開禁止の拡張子・大容量ファイル"
while IFS= read -r f; do
  [ -f "$f" ] || continue
  case "$f" in *.pdf|*.sqlite|*.db|*.log|*.pem|*.key|.env*) echo "NG 禁止拡張子: $f"; fail=1;; esac
  if [ "$(stat -c %s "$f")" -gt 5000000 ] && [[ "$f" != search/pdfjs/* ]]; then
    echo "NG 5MB超: $f"; fail=1
  fi
done <<< "$files"

echo "== 2. 秘密情報パターン"
printf '%s\n' "$files" | tr '\n' '\0' | xargs -0 grep -InE \
  '(api[_-]?key|secret|passwd|password|bearer |sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY)' \
  2>/dev/null | grep -v -e "search/pdfjs/" -e "pages_fts" -e "scripts/check_secrets.sh" && fail=1

echo "== 3. 内部IP・個人パス"
printf '%s\n' "$files" | tr '\n' '\0' | xargs -0 grep -InE \
  '(\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+|\b192\.168\.|\b10\.[0-9]+\.[0-9]+\.[0-9]+|/home/[a-z]+/)' \
  2>/dev/null | grep -v -e "search/pdfjs/" -e "scripts/check_secrets.sh" && fail=1

[ $fail -eq 0 ] && echo "OK: 問題は検出されませんでした" || echo "要確認: 上記を修正してください"
exit $fail

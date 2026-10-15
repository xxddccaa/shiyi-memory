#!/bin/bash
cd "$(dirname "$0")" || exit 1
bash scripts/manage.sh
result=$?
if [[ $result -ne 0 ]]; then
  printf '\n操作未完成，请根据上方说明处理后重试。\n'
  read -r -p '按回车关闭窗口…' _
fi
exit "$result"

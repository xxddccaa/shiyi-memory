#!/bin/bash
cd "$(dirname "$0")" || exit 1
printf '拾忆权限修复\n自动清除本应用旧授权，引导添加当前版本，并核验实际权限。\n不需要 sudo；系统如要求密码，请只在 macOS 弹窗内输入。\n\n'
bash scripts/manage.sh permissions --guided
result=$?
if [[ $result -eq 0 ]]; then
  printf '\n权限修复引导已完成。\n'
else
  printf '\n尚未完成，请根据上方原因处理。已保留数据与采集选择。\n'
fi
read -r -p '按回车关闭窗口…' _
exit "$result"

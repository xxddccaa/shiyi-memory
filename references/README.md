# 拾忆参考仓库

用于本项目的源码对照与开发规划。2026-10-09 完成七个仓库的浅克隆；每个仓库均为单分支、深度 1、无 tags，验证时可达提交数为 1、Git 工作区干净。这里保留的是本次分析快照，不代表以后访问 GitHub 时的最新版本。

- [源码对照与改进建议](../docs/reference-analysis.md)：本项目现状、各家方法与适配取舍。
- [分阶段开发规划](../docs/development-plan.md)：13 个任务、数据模型/API 草案、验收与迁移顺序。
- [机器可读快照清单](repos.lock.json)：远端地址、完整提交 SHA、提交时间、分支、浅克隆状态与根许可文件 SHA-256。

| 仓库 / 本地源码 | 分析提交 / 分支 | 快照根许可 | 主要阅读方向 |
|---|---|---|---|
| [Screenpipe](screenpipe/) | `2670901fe575` / `main` | [Screenpipe Commercial License](screenpipe/LICENSE.md) | 采集状态、精确缓存、电源策略、工具范围 |
| [MineContext](MineContext/) | `171c7a9ea809` / `main` | [Apache-2.0](MineContext/LICENSE) | 类型化事实、来源、分层报告与待办归并 |
| [Dayflow](Dayflow/) | `a45c7be14d11` / `main` | [MIT](Dayflow/LICENSE) | 工作段、滚动时间线、日报与周报 |
| [Pensieve](pensieve/) | `3e55c66edc87` / `master` | [Apache-2.0](pensieve/LICENSE) | 中文混合检索、筛选、前后文与后台调度 |
| [OpenRecall](openrecall/) | `62303e09bfd2` / `main` | [GNU AGPL v3](openrecall/LICENSE) | 简单采集与检索流程的对照基线 |
| [Petex](petex/) | `a0a5d2ed7ba0` / `master` | [Apache-2.0](petex/LICENSE) | 桌宠窗口、皮肤协议、轻量渲染 |
| [vscode-pets](vscode-pets/) | `6554662a53ef` / `main` | [MIT（代码）](vscode-pets/LICENSE) | 动画状态与交互；素材另行核对 |

**Screenpipe 的大型 LFS 模型/媒体文件保留为指针**，源码已检出；未完成全部 LFS 对象下载，不能将此目录视为带齐运行资源的安装包。当前根许可为 Screenpipe Commercial License，分析中只作为产品与实现对照，未将其代码接入拾忆。

本次没有安装参考项目的依赖，也没有运行应用或进行性能横评。其他仓库同样是源码快照，不保证本机开箱即用。

## 目录与版本管理

```text
references/
  README.md
  repos.lock.json
  screenpipe/
  MineContext/
  Dayflow/
  pensieve/
  openrecall/
  petex/
  vscode-pets/
```

根 `.gitignore` 的 `/references/*/` 排除克隆子目录，清单及本文仍可纳入拾忆的 Git。克隆各有独立 `.git`，没有添加为子模块，也没有把上游源码混入业务目录。不要把参考仓库中的自动化规则当作拾忆的开发约定。

## 在另一份工作区恢复快照

下面的 Python 片段在**拾忆项目根目录**执行，读取本次清单，在缺失目录中建立仓库并只取对应提交。`GIT_LFS_SKIP_SMUDGE=1` 避免在源码研究时额外下载模型和媒体；HTTP/1.1 仅作用于该 Git 命令。已有目录会跳过，不执行 reset 或自动更新。

```bash
python3 - <<'PY_RESTORE'
import json
import os
import subprocess
from pathlib import Path

root = Path('references')
manifest = json.loads((root / 'repos.lock.json').read_text())
env = dict(os.environ, GIT_LFS_SKIP_SMUDGE='1')
for repo in manifest['repositories']:
    target = root / repo['directory']
    if target.exists():
        print(f'已存在，跳过：{target}')
        continue
    subprocess.run(['git', 'init', str(target)], check=True, env=env)
    git = ['git', '-C', str(target), '-c', 'http.version=HTTP/1.1']
    subprocess.run(git + ['remote', 'add', 'origin', repo['origin']], check=True, env=env)
    subprocess.run(git + ['fetch', '--depth', '1', '--no-tags', 'origin', repo['commit']], check=True, env=env)
    subprocess.run(git + ['checkout', '--detach', 'FETCH_HEAD'], check=True, env=env)
    print(f"已恢复：{target} @ {repo['commit']}")
PY_RESTORE
```

这是恢复指定提交的说明，本文中的恢复片段未在第二个目录重复下载验证。若上游移除提交或禁止按 SHA 获取，命令会失败；届时应使用合法保留的快照，或选择新的提交并重新核对分析，不静默换成分支最新提交。

本次原始克隆使用 `git clone --depth 1 --single-branch --no-tags`。上面的恢复流程刻意按清单取 SHA，因此为 detached HEAD；提交内容与清单一致即可，不要求分支名相同。

## 本次核对

七个目录均验证了 `git rev-parse --is-shallow-repository`、`git rev-list --count HEAD`、origin、完整 HEAD 与 `git status --porcelain`。已检查清单与现有仓库一致、分析/规划中的相对链接存在，以及新增文档没有尾随空白。检查只覆盖这次源码研究交付；开发规划里的质量和性能指标仍待实施后测量。

桌宠使用拾忆自行绘制的原生矢量猫咪，没有复制这些项目的图片、模型或实现代码。导入自己的 PNG 图集时使用拾忆声明式皮肤协议，说明见 [桌宠与个人版实施说明](../docs/personal-companion.md)。

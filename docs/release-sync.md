# REP 播放器与技能染色工坊自动接入

工具业务源码与成品 EXE 不改变。两个项目发布稳定版后，项目内的 `toolbox-release.yml`
调用本仓库 `package-tool.yml`，下载正式成品、保留全部原始文件字节，添加启动适配脚本，
上传 `<tool_id>-<version>-win-x64-toolbox.zip` 到同一个 Release。

Registry 在每小时第 7、22、37、52 分钟检查两个项目的最新稳定版，验证完整附件的
大小、SHA-256、工具 ID、版本与入口，然后追加版本并更新 `latest.stable`。
GitHub 定时任务可能排队延迟；可手动运行 `Sync toolbox release registry`。
新工具首次登记增加 index 入口，后续不改 index；旧版本与封锁记录保留。
相同版本、相同附件重复执行不会产生提交；同版本不同附件拒绝覆盖。
草稿、预发布或尚未上传适配包的版本不会登记。
已登记版本只核对 Release 元数据，不会每 15 分钟重新下载整个安装包。

每个工作流只使用当前仓库的 `GITHUB_TOKEN`，不需要跨仓库 Token。
工具仓库只增加调用工作流；全部通用打包和同步代码在本仓库维护。
如果以后由另一个 Actions 使用 `GITHUB_TOKEN` 发布 Release，GitHub 不会再触发
`release.published` 链路，发布任务应显式调用这个打包工作流或触发 `workflow_dispatch`。
修改已经发布版本的原附件，不会自动覆盖同版本工具箱附件；应发布新版本。
GitHub 可能停用长期无活动的公开仓库定时工作流，可在 Actions 页面恢复。

## 用户数据

工具箱升级保留工具根目录的 `config` 与 `data`；程序本身仍从 `bin/app/runtime`
读写设置、预设、教程进度，从 `bin/app/exports` 读写导出。
`bin/run.cmd` 调用 PowerShell 启动脚本，启动前恢复 `config/runtime` 与 `data/exports`，
等待程序退出后同步回这两个持久目录。WebView 缓存排除，不进入适配包。
启动适配脚本按工具目录持有单实例锁，重复打开不会恢复旧备份或启动第二个实例，
锁保持到程序退出并完成保存，避免同时运行时覆盖未保存数据。
请通过工具箱或 `bin/run.cmd` 启动；直接双击内部 EXE 会绕过持久化适配。
更新工具前应关闭正在运行的工具。

旧 REP 1.1.3 工具箱包直接启动 EXE，未持久化 `bin/app` 中的用户文件。
首次升级前必须迁移，不能等旧 bin 被替换后再恢复。当前本机的安装过程会先执行
`toolbox_release.py migrate --tool-directory <旧工具目录>`，只复制、不删除原文件，
遇到已有持久目录的同名不同内容文件会停止以便核对。
其他旧版用户首次升级前，需要将旧 `bin/app/runtime` 与 `bin/app/exports` 分别备份到
工具根的 `config/runtime` 与 `data/exports` 后，再在工具箱更新。
完成首次迁移后，新适配包的后续升级沿用自动保存流程。
旧 1.1.3 包没有 `bin/run.cmd`，而 Registry 的入口字段对所有版本共用，
所以其历史条目保留，但加入 `blockedVersions`，不再提供工具箱内回退。
原版 GitHub Release 保留；已有封锁记录也不会被自动同步覆盖或重新推广。

## 手动补跑与本地校验

两个工具仓库的 `Publish toolbox attachment` 支持手动填写已发布标签补跑。
初次接入使用 `v1.1.2`（dnf_skill_prism）和 `v1.1.5`（rep_player）。

本地测试、打包与校验使用 `DNFtoolall/.build-venvs/toolbox/Scripts/python.exe`：

```text
python -m pytest tests/test_toolbox_release.py -q
python scripts/toolbox_release.py package --tool-id dnf_skill_prism --tag v1.1.2 --output-dir <新目录>
python scripts/toolbox_release.py package --tool-id rep_player --tag v1.1.5 --output-dir <新目录>
```

上面的 python 指向该专用 venv，不使用系统 Python。`package` 默认只生成本地包与
manifest 草稿；`--upload` 才会增加 Release 附件。`sync` 只修改本地 Registry，
远端提交由工作流完成。本地推送与首次配置仍遵守项目发布确认规则。

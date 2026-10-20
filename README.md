# IRIDIS6 maintenance — Home archiving and safe cleanup

本仓库管理 IRIDIS6 Home 目录维护的可复用脚本。**GitHub 只保存维护代码和说明；研究数据、模型输出、日志、归档包和校验清单必须存放在 Southampton Research Filestore，不得提交到公开仓库。**

## 已实现的功能

- **`audit`（只读）**：分类检查 Home 顶层非隐藏文件；列出旧日志、Slurm 预览、历史压缩包及未清理的项目目录。
- **`backup`（备份）**：直接写入 `/research/xclimatetbm/ly3n24/Home_Declutter/`，按 UTC 时间创建 `home_snapshot_YYYYMMDDTHHMMSSZ/`，生成 `home_selected.tar.gz`、`manifest.json` 和 `SHA256SUMS`；逐文件 SHA-256 校验压缩包，再对比源文件，源文件变化即中止，不清理。
- **`verify`（只读）**：再次校验完整归档的 SHA-256 与每个 TAR 成员。
- **`clean`（有条件清理）**：明确授权后，只考虑已完整归档并逐文件校验、至少 **48 小时**未修改的 Home 根目录普通文件：`*.log`、`*.out`、`*.err`、`*.bak`、`*.zip`、`*_preview_run.txt` 和 `*.before_*`。再次核对哈希和 mtime，并检查 Slurm 作业是否明确引用。将逐项结果写入归档 session 的 `cleanup_actions.tsv`。默认不删除任何目录、Python/Shell 源代码、CSV 或 Git 项目。
- **保护活跃目录**：例如 `CARDAMOM`、`CLM-FATES-single-site-workflows`、`FLUXNET_MultiSite_11`、`CLM_FATES_13_checkpoint`、`TCCAS-workflows` 和此 `maintenance` 仓库等。隐藏目录（`.ssh`、`.conda`、`.git` 等）完全跳过。
- **挂载检查**：先确认 Research Filestore 为 NFS，不会意外把归档写到未挂载的本地路径。符合 IRIDIS6 Python 3.9。

## 在 IRIDIS6 上运行

```bash
cd "$HOME"
git clone https://github.com/lyanyeung/maintenance.git maintenance
cd "$HOME/maintenance"

# 1. 查看文件分类，不修改
python3 scripts/archive_home.py audit

# 2. 完整备份，不删除
python3 scripts/archive_home.py backup
```

备份成功后会打印 `VERIFIED_BACKUP <SESSION>` 和下一步完整的 `clean` 命令。**只有看到 `VERIFIED_BACKUP` 才继续**，且先查看将清理的文件是否被其他项目手动引用：

```bash
# 3. 将 <SESSION> 替换为上一步真实输出，先单独复核
python3 scripts/archive_home.py verify --session "<SESSION>"

# 4. 经过审查后执行有条件清理
python3 scripts/archive_home.py clean --session "<SESSION>" \
  --confirm DELETE_OLD_VERIFIED_FILES

# 5. 查看清理记录与配额
cat "<SESSION>/cleanup_actions.tsv"
myfiles
```

如已有同名 clone，则先 `cd "$HOME/maintenance" && git pull --ff-only`，不要重复 `git clone`。

**一键执行“备份+验证”而不删除：**

```bash
bash scripts/run_home_maintenance.sh
```

清理是一个独立的明确授权步骤；不支持跳过校验直接删除。

## 恢复归档

先将压缩包解压到**全新的审查目录**，不要覆盖现有 Home：

```bash
SESSION="/research/xclimatetbm/ly3n24/Home_Declutter/home_snapshot_实际时间"
mkdir -p "$HOME/home_restore_review"
tar -xzf "$SESSION/home_selected.tar.gz" -C "$HOME/home_restore_review"
# 提取路径：$HOME/home_restore_review/home/
```

注意：`home_restore_review` 这样的审查目录如果位于 Home 且再次运行 `backup`，会成为下一次归档候选；完成检查后请手动处理，不要让归档反复嵌套。

## 归档目录与清理范围

| 路径 | 作用 | 是否自动删除 |
|---|---|---|
| `/iridisfs/home/ly3n24` 顶层旧日志等 | 主要整理目标 | 仅 `clean` 且通过全部检查 |
| `/research/xclimatetbm/ly3n24/Home_Declutter/` | 研究备份目的地 | 否 |
| `/scratch/ly3n24/CTSM_FATES_OUTPUTS/` | 活跃 CLM-FATES 模型 | **绝不触碰** |
| `/iridisfs/scratch/ly3n24/CLM_FATES/cases/` | 活跃 CIME cases | **绝不触碰** |
| GitHub `maintenance` | 本维护工具源码 | 否 |

**关键区别：本仓库目前仅整理 Home，不释放 Scratch inode。** 用户最近的 Scratch 文件数约 56 万/61.44 万，释放 Scratch 配额需另行制定按站点 checkpoint 与归档核验的专用流程，不得对正在运行的 CLM-FATES 文件夹执行批量删除。

## 安全边界

- 不把“GitHub 上有同名脚本”等同于“IRIDIS 本地文件可删”。未逐字节比对 GitHub 版本的 `.py`/`.sh` 一律保留。
- 不能凭未完成的 `backup`、`ls` 或 `df -i` 声称数据安全迁移；以日志的 `VERIFIED_BACKUP`、`VERIFIED_SESSION` 和校验收据为准。
- 即使 Slurm 队列未出现文件路径，也不能排除 cron、手动 shell、外部工作流的引用。自动清理前应检查这些潜在依赖。
- 不运行 `scancel`、`rm -rf` 或任何 Scratch/CIME 删除逻辑。只对筛选过的单个 Home 普通文件使用 unlink。
- 本仓库**不包含**用户真实归档或清理执行结果；本次 GitHub 提交不代表 IRIDIS 文件已经移动或删除。

## FLUXNET 原始数据审核和 DD/MM/YY 清理（独立工具）

[完整操作说明](docs/FLUXNET_RAW_CLEANUP.md) · [Python 脚本](scripts/fluxnet_raw_cleanup.py)

不需要在 IRIDIS6 克隆仓库、保存脚本或生成报告。直接通过 GitHub 流式执行只读审计：

```bash
curl -fsSL https://raw.githubusercontent.com/lyanyeung/maintenance/main/scripts/fluxnet_raw_cleanup.py | python3 -B - --details summary
```

审计会统计同一原始 release 目录中的 HH / BIF / DD / MM / YY CSV 与原始 ZIP。默认仅向终端打印结果，不创建任何文件或删除数据。删除需要显式的 `--apply --confirm DELETE_DD_MM_YY`，且仅作用于有同名 HH 对应文件的 DD/MM/YY。**ZIP 不会被删除**，DATM、CIME case、restart、模型历史 NC 均不触碰。操作前应确认官方聚合产品的归档需要，以及数据有独立备份。


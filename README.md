# BRT5

BRT4 从 issue、iCoRe 已生成的源码/测试检索结果和 buggy 仓库出发，生成单个完整 Bug Reproduction Test，并在独立阶段进行真实补丁 F2P 评测。

## 新机器复现

完整的安全导出、自动安装、密钥配置和全量实验说明见
[README_REPRODUCE.md](README_REPRODUCE.md)。真实 API key 不保存在 Git 中。

```bash
bash scripts/bootstrap_fresh_swt_server.sh
bash scripts/run_swt_experiment.sh \
  --behavior-target on \
  --behavior-target-cache data/behavior_targets/swt/full_method_f2p_47_46_20260717
```

## 快速运行

完整命令、恢复方式、tmux 和日志说明见 [README_RUN.md](README_RUN.md)。

## 项目结构

目录、代码文件和主调用链见 [README_STRUCTURE.md](README_STRUCTURE.md)。

## 最近结果

历史生成/评测结果通过 `scripts/clean_old_results.sh` 管理：普通旧结果只保留最近 3 个，带 `best`、`bestsofar`、`complete` 标记的结果会进入 `results/archive/legacy_best/`。

## 新实验输出

所有新实验统一写入 `results/runs/run_YYYYMMDD_HHMMSS/`，不再写到项目根目录。

## 核心导出

每次 run 的完整逐实例结果位于 `exports/all_outputs.json`；另有 JSONL、测试代码简表和总指标摘要。

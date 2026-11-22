# 数字文化遗产资料工作台

本目录是一个可复用的**数字资料接收与编目工作台**，用于持续导入虚构的藏品清单、测绘/照片元数据、研究说明、许可记录和展览条目，并回答三个问题：

1. 这个素材描述的是哪件藏品，由谁、何时、以何种方式采集？
2. 它能否用于某个传播渠道，在某个日期是否仍然有效？
3. 展览当前引用的是哪一版资料，哪些条目因归属或许可变化需要重新检查？

所有藏品、机构、人员和影像均为合成虚构素材，不含真实机构档案或版权作品。项目只依赖 Python 3.11+ 标准库，无需联网。

## 快速开始

```sh
python3 -m unittest discover -s tests          # 运行全部测试

python3 -m digital_intake ingest       fixtures/scenario.json
python3 -m digital_intake completeness fixtures/scenario.json
python3 -m digital_intake resolve      fixtures/scenario.json --file-name object_front.jpg
python3 -m digital_intake provenance   fixtures/scenario.json ASSET-108-C
python3 -m digital_intake release      fixtures/scenario.json ASSET-108-A online-exhibition --on 2026-11-01
python3 -m digital_intake rights       fixtures/scenario.json ASSET-108-A --on 2026-11-01
python3 -m digital_intake recheck      fixtures/scenario.json EX-SYN-9 --on 2026-11-01
```

也可以作为库使用：

```python
from digital_intake import Workbench

wb = Workbench()
wb.ingest_file("fixtures/scenario.json")
wb.check_release("ASSET-108-A", "online-exhibition", "2026-11-01")
wb.set_exhibition_state("EX-SYN-9", "ASSET-108-A", "published",
                        channel="online-exhibition", on="2026-11-01")
wb.published_snapshot("EX-SYN-9", "ASSET-108-A")
```

### 持续导入

用 `--store` 指定一个工作台文件，导入会跨会话累积，后续查询可省略来源文件：

```sh
python3 -m digital_intake ingest fixtures/sources.json --store workbench.json
python3 -m digital_intake ingest fixtures/scenario.json --store workbench.json
python3 -m digital_intake completeness --store workbench.json
```

库接口对应 `Workbench.load(path)` / `workbench.save(path)`。导出文件只保存已接收的信封，发布快照与补录合并结果在加载时重建，因此不会出现导出状态与来源不一致。

## 来源记录的格式

`fixtures/sources.json` 与 `fixtures/scenario.json` 都是同一格式：每条记录有稳定的信封字段 `source_id`、`record_kind`、`collected_at`（必须带时区）和 `payload`。

| `record_kind` | 关键字段 | 说明 |
| --- | --- | --- |
| `collection_item` | `item_ref`, `catalogue_revision`, `title`, `institution_ref`, `replaces_item_ref` | 藏品目录，可多次修订 |
| `digital_asset` | `asset_ref`, `item_ref`, `capture_kind`, `source_team`, `sha256`, `file_name`, `angle`, `precision_note`, `derived_from`, `revision` | 一次数字采集，一个 `asset_ref` 即一件载体 |
| `research_note` | `note_ref`, `item_ref`, `note_kind`(`observation`/`interpretation`), `revision`, `review_state`, `body`, `supersedes` | 研究记录 |
| `license` | `license_ref`, `asset_ref`, `allowed_use`(渠道列表), `valid_from`, `valid_until`, `revision`, `supersedes`, `revoked` | 权利许可 |
| `exhibition_item` | `exhibition_ref`, `asset_ref`, `state`, `channel`, `license_ref`, `note_refs`, `published_at`, `catalogue_revision` | 展览中的一个素材条目 |
| `provenance_amendment` | `asset_ref`, `item_ref`, `source_team`, `capture_kind`, `sha256`, `note`, `amended_by` | 编目人员补录来源（仅补缺，不改写历史） |

## 工作台规则

### 素材身份：只认 `asset_ref`

- **不按文件名或校验值合并。** 同一藏品的正面、背面、RTI 高精度扫描是不同载体，各自保留 `asset_ref`。
- `resolve_asset(file_name=...)` 会返回**所有**匹配载体，状态为 `ambiguous`，要求调用方改用 `asset_ref`。校验值相同也不视为同一素材。
- `revision` 更高的同一个 `asset_ref` 会替代旧信封，但旧信封仍保留可查。

### 来源链

- `provenance(asset_ref)` 返回采集团队、采集方式、校验值、所关联的藏品及 `derived_from` 派生链；派生链断裂时明确标为 `missing`。
- **缺失来源按素材逐条判定。** 同一藏品下其他素材完整，不会让来源不清的素材通过。
- 编目人员可用 `amend_provenance(...)` 或导入 `provenance_amendment` 补录缺失字段；补录只叠加在当前视图上，原始信封不变，且会出现在 `amendments` 记录中。

### 权利与渠道

- 许可按 `allowed_use` **精确匹配渠道名**：`research` 不隐含 `online-exhibition`。
- `valid_from` / `valid_until` 含端点；评估日期必须明确。到期、未生效、渠道未覆盖、已撤销都会拒绝。
- 同一素材的许可可发布新版本（`revision` + `supersedes`），新版本替代旧版本。许可更新只影响**新**的评估与发布动作。

### 展览生命周期

- 展览条目状态：`draft` → `preparing` → `ready` → `publishing` → `published`，另有 `withdrawn`。
- `set_exhibition_state(..., "published")` 会先做完整检查（来源完整、藏品存在、许可覆盖且有效），**不通过则不发布**。
- 发布成功时写入不可变快照：当时的藏品归属、目录修订号、许可判定、引用的研究说明。
- 归属重新确认或许可更新后：
  - 已发布展览继续显示**发布时的快照**，`recheck_exhibition` 标记为 `frozen`；
  - 待发布（`draft`/`preparing`/`ready`/`publishing`）条目会被标出 `attribution_drift` 并要求重新检查。
- 已发布条目不能在原条目上改状态，需建立新的展览版本。

### 研究说明

- `note_kind` 区分 `observation`（观察记录）与 `interpretation`（解释性文字），报告里以 `interpretation` 布尔值显式标注。
- 更正通过新 `revision` 与 `supersedes` 表达；查询只返回当前有效版本，历史仍保留。

## 目录结构

```
digital_intake/
  models.py     # 常量、日期与字段解析
  records.py    # 信封校验与类型化记录
  rights.py     # 渠道/有效期许可判定
  store.py      # 工作台：导入、归属、来源链、展览生命周期
  cli.py        # 命令行入口
fixtures/
  sources.json  # 最小基线样例
  scenario.json # 覆盖多角度、同名文件、缺来源、许可范围等情形
tests/
  test_fixture_contract.py  # 原始信封契约
  test_workbench.py         # 领域规则测试
```

## 设计取舍

- **追加式导入。** 同一 `source_id` 重复提交会被拒绝，更正必须使用新编号；这样任何已发布内容都能回溯到确定的来源信封。
- **快照而非引用。** 已发布展览保存发布当时的事实副本，而不是指向可变记录，避免“旧展览被新资料静默改写”。
- **猜错不如报错。** 文件名冲突、来源缺失、许可不明时一律显式拒绝或标记，而不是选一个最像的候选。

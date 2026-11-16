# 002 链式比较（冻结口径的逐月分解）

在 001 的批次/目标/运行表之外新增四张表，支持“一组批次冻结共同目标与样本版本、逐月校准、相邻月份分解、按链段增量重算、整组原子发布”。仍由 SQLAlchemy metadata `create_all` 首次建表；本文件记录结构约束，后续列变更不得继续依赖 create_all。

## 设计要点

1. **共同口径冻结**：`comparison_groups` 持有全组唯一的目标快照（JSON）与 `target_checksum`、`target_epoch`；`group_batches` 每个链节点持有各自的样本快照（JSON）、`sample_checksum`、`sample_epoch`。快照写入后不可变，实时批次表的后续改动不影响已冻结链，显式“更正”才会推进 epoch。
2. **版本即链**：`comparison_groups.version` 是当前工作链版本（epoch）。每次样本/目标更正 +1，并为受影响链段写入新版本的 `chain_segments` 行；未受影响链段从上一版本原样结转（同 `input_checksum`，不重算）。
3. **增量重算**：节点校准结果缓存于 `group_batches.calibration`（含 assignments）与 `calibration_checksum`（即 raking 输入 SHA256）。样本更正只使该节点及其相邻链段失效；目标更正使全部节点与链段失效。worker 只处理 `queued/failed/running`（崩溃残留）行，已完成且 checksum 未变的节点/链段跳过。确定性算法保证恢复后结果与一次完成逐字节一致。
4. **版本隔离**：队列任务负载为 `group_id:expected_version`。worker 取行用 `SELECT ... FOR UPDATE`，版本不符或组不在 `recomputing` 直接丢弃；更正接口在组处于 `recomputing` 时返回 409，杜绝并发更正与重算混用版本。
5. **原子发布**：`chain_publications` 只增不改。发布在单事务内校验全链 `completed` 且全部节点收敛，写入完整快照 JSON 与快照 SHA256；同版本重复发布幂等返回原行。新链未齐备前，已发布行继续可读。

## 表结构

### comparison_groups
| 列 | 类型 | 约束/说明 |
|---|---|---|
| id | int PK | |
| name | varchar(100) | unique |
| description | varchar(500) | default '' |
| tolerance / max_iterations / cap_ratio | float/int/float | 同 weight_runs 参数范围，创建后冻结 |
| target_snapshot | JSON | 全组共同边际目标快照（dimension/category/population，按自然键排序） |
| target_checksum | varchar(64) | 目标快照 SHA256 |
| target_epoch | int | default 1，目标更正 +1 |
| version | int | default 0，工作链版本 |
| published_version | int nullable | 最近一次成功发布的版本 |
| status | varchar(20) | draft/recomputing/ready/failed |
| last_error | varchar(500) nullable | |
| created_by | int FK users.id | |
| created_at / updated_at | datetime | updated_at ON UPDATE utcnow |

### group_batches（链节点）
| 列 | 类型 | 约束/说明 |
|---|---|---|
| id | int PK | |
| group_id | int FK comparison_groups.id | |
| batch_id | int FK sample_batches.id | |
| position | int | 组内月份次序，从 0 开始 |
| month_label | varchar(20) | |
| sample_snapshot | JSON | 冻结样本（code/zone/age_band/mode/trip_minutes/base_weight，按 code 排序） |
| sample_checksum | varchar(64) | |
| sample_epoch | int | default 1，样本更正 +1 |
| status | varchar(20) | queued/running/completed/failed |
| calibration | JSON nullable | 裁剪后的 raking 结果（含 assignments，不含 trace） |
| calibration_checksum | varchar(64) nullable | raking 输入 SHA256，同时是校准复用键 |
| error | varchar(500) nullable | |
| 唯一约束 | | uq_group_batch(group_id,batch_id)、uq_group_position(group_id,position) |

### chain_segments（相邻月份链段）
| 列 | 类型 | 约束/说明 |
|---|---|---|
| id | int PK | |
| group_id | int FK comparison_groups.id | |
| from_node_id / to_node_id | int FK group_batches.id | |
| position | int | 0 = 第一对相邻月份 |
| version | int | 所属链版本 |
| status | varchar(20) | queued/running/completed/failed |
| input_checksum | varchar(64) | 两端 calibration_checksum + 分解方法版本的 SHA256；复用/幂等键 |
| result | JSON nullable | 逐月均值、双效应、逐格分解、对账残差、覆盖情况 |
| error | varchar(500) nullable | |
| 唯一约束 | | uq_segment_version(group_id,position,version) |

### chain_publications（不可变发布）
| 列 | 类型 | 约束/说明 |
|---|---|---|
| id | int PK | |
| group_id | int FK comparison_groups.id | |
| version | int | |
| note | varchar(500) | |
| snapshot | JSON | 发布时刻整组完整快照（目标、节点、链段分解、全部 checksum） |
| checksum | varchar(64) | 快照规范化 JSON 的 SHA256 |
| published_by | int FK users.id | |
| created_at | datetime | |
| 唯一约束 | | uq_publication_version(group_id,version) |

## 分解口径（对称 Kitagawa/Oaxaca–Blinder）

格键 = (zone, age_band, mode)，格内权重由该月冻结样本在共同目标下 raking 得到。格份额 p=格权重/总权重，格时长 y=格内加权平均出行分钟数，月加权均值 m=Σp·y。

- 样本构成效应 = Σ 0.5·(p₁−p₀)·(y₀+y₁)
- 时长效应 = Σ 0.5·(p₀+p₁)·(y₁−y₀)
- 恒等式：(构成效应+时长效应) ≡ m₁−m₀（新增/消失格按 p=0 处理并标记 new/lost，整体计入构成效应）
- 对账残差 = 总变化 − 两效应之和，落库并在页面展示；|残差| 超过 1e-8 视为对账失败。

该分解是描述性账户恒等式，不作因果解释；raking 的方法边界与 001 相同。

# 原野交通抽样校准席 / sample-balance

为原野交通研究所的17位抽样分析员提供匿名出行样本的边际校准、权重诊断和复核交付。全部为合成数据；不用于个人决策。

上游为样本CSV和机构级目标边际；系统替代表格逐列手调权重，下游研究者使用获批权重和描述性加权均值。原有痛点：样本结构与目标人群不同、极端权重不可见、报告只给加权总数而忽略有效样本。该应用不负责问卷收集、投票或路线调度。

## 快速启动

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

访问 http://localhost:18219 ，API文档 http://localhost:19219/docs ，MySQL localhost:17719。五服务为frontend/backend/worker/db/redis，各有实际healthcheck；所有容器依赖数据库/Redis真实健康，worker用心跳。数据库、Redis和共享产物使用命名卷。中文及空格父目录可直接启动。清理本项目演示数据：`docker compose down -v --remove-orphans`。

| 角色 | 账号 | 密码 | 权限/主要动作 |
|---|---|---|---|
| admin 抽样负责人 | admin@balance.demo | Balance2026! | 所有页面；登记目标边际、审批权重、导出与审计 |
| analyst 交通分析师 | analyst@balance.demo | Balance2026! | 创建批次、CSV导入、运行校准、查看诊断与审计；不可改目标或批准 |
| viewer 研究阅读者 | viewer@balance.demo | Balance2026! | 样本/目标/结果只读，获批后CSV下载；无审计权限 |

## 一天的工作剧本

1. 09:00 分析师登录，检查“11月城区出行样本”的800条匿名观测，来源 `yy-travel-sample.csv`。
2. 检查分区、年龄组、出行方式与出行分钟数，不把编号作为统计特征。
3. 负责人登记总量均为10000的三套边际：分区30/45/25%、年龄35/45/20%、方式30/35/35%。
4. 分析师建立“11月城区出行·代表性校准”，容差0.0001、最多500次、截断上限为目标平均权重12倍。
5. 独立worker从Redis接任务，按类别调整权重并持久化结果，页面自动刷新。
6. 查看边际残差、迭代曲线、权重范围、Kish有效样本量及原始/加权平均出行时长。
7. 不收敛时调整兼容目标或上限，保留失败和诊断；负责人不能批准不收敛结果。
8. 负责人记录适用范围并批准，阅读者下载完整权重CSV；记录关联输入SHA256。

## 真实计算口径

实现位置 `backend/src/services/raking_algorithm.py`。

1. Raking/IPF：初始正权重归一到目标总量；按年龄组、方式、分区的固定顺序，用 target_category/current_category 乘法调整。每轮后按 cap_ratio × target_total / n 截断。误差是各类别绝对残差除共同目标总量，达到容差才声明收敛；截断不保持每轮总量，不可行目标可能不收敛，此时结果不能批准。此算法是有界迭代近似，不宣称约束最大熵的精确最优解。
2. Kish有效样本 `ESS=(Σw)^2/Σw²`；设计效应 `n/ESS`；加权均值 `Σw*y/Σw`。这些量只反映权重离散与描述性估计，不包含聚类/分层设计，不保证校准已消除未观测选择偏差，也不作因果解释。

参数：容差1e-6..0.05、迭代1..2000、截断倍数1..30，每批最多5000行。目标每维总量必须一致且正；重复目标、遗漏样本类别、正目标类别无样本支持、零权重支持、非有限/负值被拒绝。目标为零可使相应权重为零。所有影响结果的样本维度、初始权重、出行时长、边际与参数都进入SHA256，并按自然编号/类别排序；行序改变不改变结果。结果保存原始样本与目标诊断快照，后续目标变更不会回写历史运行。

独立4样本oracle：A组目标60、B组40，其余两维50/50，权重应为30/30/20/20；加权时长23、原始25、ESS10000/2600、设计效应1.04。cap_ratio=1不能满足目标，必须显示未收敛。

## 链式比较：冻结口径的逐月分解（页面05）

研究所需要比较逐月加权出行时长，但各批次各用一套目标时，月间差值会混进人群构成变化。链式比较为一组批次**冻结共同目标与各自样本版本**，在同一口径下逐月校准，再把相邻月份的加权均值差值分解为两部分。实现位置 `backend/src/services/chain_service.py` 与 `decomposition.py`，结构见 `backend/migrations/002_chain_comparison.md`。

1. **冻结口径**：建组时把锚点批次的边际目标快照为全组共同目标（`target_snapshot` + SHA256 + epoch），每个链节点冻结各自样本快照（`sample_snapshot` + SHA256 + epoch）。快照不可变，实时批次表后续改动不影响已冻结链；显式更正才推进 epoch 与链版本 `version`。
2. **逐月校准**：worker 在共同目标下对每个节点跑同一套确定性 raking（容差/迭代/截断参数全组冻结），结果缓存于节点；校准输入 SHA256 未变的节点直接复用，不重算。
3. **对称分解**：按 (分区×年龄组×方式) 三维格，以格份额 p 与格内加权平均时长 y 做 Kitagawa/Oaxaca–Blinder 对称分解：构成效应 = Σ½(p₁−p₀)(y₀+y₁)，时长效应 = Σ½(p₀+p₁)(y₁−y₀)。恒等式 (构成+时长) ≡ m₁−m₀ 逐链段落库并对账，残差超 1e-8 判失败；新增/消失格按零份额处理并标记 new/lost，整体计入构成效应。
4. **增量重算**：样本更正只使该节点及其相邻链段失效，其余节点/链段按输入 checksum 原样结转（同 checksum 结果逐字节复用）；共同目标更正才全链重算。每个新版本一行 `chain_segments`，历史版本链段不覆盖。
5. **版本隔离**：队列负载为 `组:版本`。worker 取组行 `FOR UPDATE` 且版本匹配才处理，陈旧任务直接丢弃；组处于 `recomputing` 时更正接口返回 409，并发更正与重算不能混用版本。确定性算法使失败恢复后的结果与一次完成完全一致（测试 `test_failure_recovery_equivalent` 断言逐字节相等）。
6. **原子发布与保留**：仅当整组链段 `completed` 且全部月份收敛才可发布；单事务写入不可变 `chain_publications`（含整组快照与快照 SHA256），同版本重复发布幂等。新链未齐备前已发布版本始终可读，页面可下载发布快照 JSON。
7. **页面能力**：链式比较页可建组（多批次+月份标签）、查看每个节点的样本/目标/参数依赖与全部 checksum、触发样本或目标更正、查看分解对账表与逐格明细、负责人原子发布并浏览历史发布。

## 页面与结构

七个研究页面：总览、样本底册、目标边际、校准配置/记录、链式比较（含依赖、分解对账与原子发布）、诊断结果、复核交付（含有权角色的审计）。BatchSelect、RunTable、CalibrationSummary、MarginTable跨页复用；PageHeading/ErrorNotice/StatusBadge为公共组件；useTask/usePolling与格式工具共享。初始3批共1280条样本、27个目标格、一个实际计算的初始运行及获批记录，另含一个三节点链式比较组及其发布记录，种子73/74/75。

前端：`src/{api,stores,types,components/common,hooks,pages,router,utils,constants}`；后端：`src/{routes,controllers,services,models,schemas,middlewares,utils,constants,config}`；种子在`backend/scripts/seed_demo.py`，结构约束见`backend/migrations/001_schema.md`与`002_chain_comparison.md`。链式比较的无外部依赖回归测试（SQLite 内存库 + 假队列）在 `backend/tests/test_chain.py`（9 项：分解 oracle、增量结转、失败恢复等价、版本隔离、原子发布等），HTTP 端到端在 `backend/tests/test_chain_api.py`，用 `PYTHONPATH` 指向依赖后可直接 `python3 tests/test_chain.py` 运行；截图和TESTING只在批次`test-artifacts/gb-0015/`。

共享枚举Role/RunStatus/Dimension后端在`backend/src/constants/enums.py`（另含链式比较的GroupStatus/ChainNodeStatus），前端在`frontend/src/constants/enums.ts`；角色认证在middlewares/auth、auth store；状态用于run_service/worker/StatusBadge与链服务；Dimension用于target schema、raking维度与目标页。数据库字段采用相同枚举字符串。

## 环境与开发

Python3.11/FastAPI/SQLAlchemy/PyJWT，Vue3.5/TypeScript/Vite6.4.6，MySQL8、Redis7与Nginx1.27。本地API配置DATABASE_URL/REDIS_URL/JWT_SECRET后`uvicorn src.main:app --reload`；前端`npm ci && npm run dev`，/api代理19219。所有计算、校验及持久化在Python后端。

.env.example列出端口、数据库用户/密码、JWT密钥与SEED_DEMO；.env、node_modules、dist和日志被忽略。演示密码是公开本地账号。SEED_DEMO=true自动执行插入式自然键seed，不覆盖改动；失败记日志不阻塞服务。限流使用Redis每IP每分钟360次，统一错误响应并记录写操作审计。

单worker的Redis BLPOP演示队列同时消费校准队列`balance:calibration`与链式比较队列`balance:chain`，无处理中崩溃自动重投；普通校准需重新提交，链任务可在页面“重投当前版本”按同版本恢复（已完成节点/链段跳过，结果与一次完成一致）。CSV为文本导入，整批验证后写入且不覆盖已有编号；类别文本导出已做公式前缀转义。审批后才可导出。首次schema由metadata创建，未实现生产迁移升级。字体依赖网络。详见真实TESTING中的范围和执行结果。

## License

MIT，见LICENSE。

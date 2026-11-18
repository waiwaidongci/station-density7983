# 河砾微站密度巡检图

标准化DBSCAN、噪声比例与邻域连通簇解释。为河砾研究协作组的12位分析与业务人员提供从样本核对、独立worker计算到批准快照交付的完整工作流。

## 真实业务场景

微站环境特征→密度聚类→复访计划；DBSCAN不是地理热点显著性。团队原先将 `station-density-202611-01.csv` 复制到多份人工表格，复核时再逐项对照。三个痛点是口径不可追溯、重复计算易错、业务说明与结果脱节。上游提供合成业务观察，系统承担研究和复核，下游业务人员读取批准快照。全部演示数据是固定随机种子生成的模拟数据。

## 业务模块

1. 业务总览：批次、样本、任务与批准交付账册。
2. 巡测专题：创建/修改来源说明、查看样本、用真实业务字段追加记录；样本可绑定仪器编号。
3. 全网校准：录入同地同次仪器配对差与权重，指定可信仪器为零偏，联合求三特征偏移；发布前核验修正读数。
4. 分析配置：冻结来源、参数和全部样本，并冻结提交时已发布的校准版本，进入Redis队列。
5. 完整结果：真实算法摘要、专属诊断、全部CSV导出和可恢复失败态；显示本轮冻结的校准版本。
6. 复访计划：分析师草案→负责人批准→阅读者下载批准快照。
7. 审计：登录、来源修改、校准录入/发布、任务worker完成、业务说明批准等事件。

## 快速启动

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
# 可重复执行，不覆盖人工修改
docker compose exec -T backend python -m scripts.seed_demo
```

访问 [前端](http://localhost:18246)；[API文档](http://localhost:19246/docs)。数据库仅演示用途映射 `17746`。首次准备依赖和初始化数据库需要等待健康状态：`docker compose ps`，五个服务均需healthy。

| 组件 | 技术与职责 |
|---|---|
| frontend | 原生 HTML / CSS / JS ES Modules，Nginx代理 `/api/` |
| backend | Python3.11 / FastAPI / SQLAlchemy / JWT |
| worker | 独立Python进程，Redis BLPOP队列，持久化结果/CSV/心跳 |
| db | PostgreSQL16，命名卷持久化 |
| redis | Redis7，AOF命名卷 |

恰好五服务，均配置healthcheck；后端/worker依赖数据库与Redis健康，前端依赖后端健康；worker与后端共享 `artifacts` 命名卷。重置演示环境：`docker compose down -v --remove-orphans`，会删除本项目卷。

## 账号与权限

所有演示账号密码：`Research2026!`。

| 邮箱 | 角色 | 权限 |
|---|---|---|
| admin@station-density.demo | admin | 全部录入/执行、批准业务说明 |
| analyst@station-density.demo | analyst | 录入来源/样本、执行、提交说明，无批准权 |
| viewer@station-density.demo | viewer | 只读、导出结果及已批准快照 |

时间在数据库/API中以UTC存储，总览与审计页面明确转换为Asia/Shanghai北京时间。

JWT有效期8小时；PBKDF2盐化哈希保存密码；角色每请求从数据库读取。访客写操作403，匿名访问业务数据401，重复样本409，非法业务字段422，重复批准409。每客户端/路径写请求90次/分钟，超限429。生产使用需修改JWT及演示数据库凭据。

## 实体与字段

Survey / StationReading / DensityRun / VisitPlan 是核心实体；另有User和Audit。数据库实际表：`surveys` → `station_readings` → `density_runs` → `visit_plans`。样本以固定字段JSON保存，后端按以下字段严格验证，不接受未知字段替代必填值：温度 ℃（`temperature`）、相对湿度 %（`humidity`）、颗粒浓度 μg/m³（`particles`）。

实体贯穿 `backend/src/models`、`schemas`、`services`、`controllers`、`routes`，以及 `frontend/src/api/research.js`、`stores/research.js` 和 `pages`。`Source.key` 与 `(Record.source_id, reference)` 为自然键；来源改名不会修改已冻结任务输入。业务说明在创建时保存结果快照，批准后从该快照导出。

校准相关表：`calibration_instruments`（仪器与可信锚点）、`calibration_pairs`（规范化配对差）、`calibration_runs`（求解任务）、`calibration_versions`（不可变校准版本）、`calibration_offsets`（每仪器三特征零偏）、`calibration_residuals`（每条配对残差）、`calibration_component_cache`（按证据指纹复用的分量解）。`station_readings.instrument_key` 为可空列；旧卷启动时自动补列与索引。

## 算法与口径

标准化DBSCAN、噪声比例与邻域连通簇解释，全部计算位于 `backend/src/services/algorithm.py`，前端不拟合模型、不生成模拟结果。输入是冻结业务观察及参数，输出summary、专属诊断、逐行计算明细、seed与输入SHA256。CSV包含全部结果行；页面仅预览前30行。参数：

- `eps`：标准化邻域半径 ε，默认 `0.55`。
- `min_samples`：核心点最少邻居数，默认 `5`。

每个特征先标准化，再按密度连通分区；噪声标签表示稀疏样本，不等同污染或地理热点显著性。

失败态：业务字段缺失/非有限数值、参数越界、样本不足、退化特征或结构错误均明确拒绝或将任务标failed，错误消息保存在任务中。可回到来源或配置修正后重新执行，历史失败记录保留。

已知边界：只在温度/湿度/颗粒三个标准化特征上做密度分区；不是地理显著性、风险诊断或自动污染识别。eps敏感，分区编号没有大小顺序；含常量列明确拒绝。

## 全网校准

### 模型

每台仪器对每个特征有一个加性零偏 `o_i`。一条配对观测记录同地同次另一台仪器的读数差，规范化后 `left_key < right_key`、差值恒为 `left − right`，于是模型方程为 `o_left − o_right = d`，权重 `w`。可信仪器固定 `o = 0`。加权最小二乘的正规方程就是仪器图的加权 Laplacian；温度/湿度/颗粒共用节点、边和权重，是一个联合模型的三组可分离右端。

- 环路矛盾（`A−B=1, B−C=2, C−A=0.2`）无法同时满足。求解只给出最小残差意义下的偏移，矛盾证据完整保留在配对残差表中，不会被摊入零偏。
- 无锚点的连通分量整段隔离：偏移不输出（API 中为 `null`，库内不写行），配对仍计入隔离统计并列出最大示例，绝不以任意基准"补一个零偏"。
- 发布前重新从边证据计算加权残差平方和，与求解报告比对，并核对残差行数；任一不符即拒绝发布。校验和覆盖按键排序后的全部偏移值。

### 有界内存

Laplacian 不建稠密方阵。边数组 O(E)、共轭梯度向量 O(N)，分量逐个求解。求解前用 `estimate_bytes(nodes, edges)` 检查是否超过 128MB 工作集预算，超限直接拒绝而不是尝试分配。实测 20,000 台仪器、约 100,000 条配对：估算工作集 5.8MB，实测进程峰值 RSS 约 56MB，3 个特征 75 次迭代约 1.6 秒。

### 更正、失效与复用

每个连通分量按"排序后的仪器键 + 锚点标记 + 规范化边值"生成证据指纹。配对更正/删除会：

1. 递增图版本 `graph_version`；
2. 只改变受影响分量的指纹，其余分量的解从 `calibration_component_cache` 直接复用；
3. 缓存里的全新分量仍需通过 `converged` 检查才写缓存。

因此更正一条配对不需要重算整网，也不会把无关分量算乱。页面与 API 的 `components.cached` / `components.solved` 明确显示本次复用与新解的分量数。

### 中断恢复与重试

共轭梯度状态 `(x, r, p, rz)` 按"分量指纹 + 特征"原子写入 `artifacts/calibration-checkpoints/`，每 50 次迭代一次。恢复时右端和主对角由边数组确定性重建，再从保存的迭代继续，所以重试是继续而不是重复累加证据；同一份证据的指纹不含行修订号，幂等重投不会使缓存失效，真正的取值更正才会。

worker 启动时及每 30 个心跳周期调用 `recover_interrupted`：心跳超过 180 秒仍为 `running` 的校准任务回到 `queued` 并重新入队。未在 `max_iter` 内收敛的分量不发布：任务标 `failed` 并保留检查点，页面可用 `POST /calibration/runs/{id}/retry` 重试（可提高最大迭代数），从检查点继续。任务领取使用条件 `UPDATE ... WHERE state='queued'`，重复队列消息只有第一个 worker 能把任务从 `queued` 翻到 `running`，因此重试不会重复执行。

### 晚归旧解

三类"晚归"都会被拒绝而不是覆盖新状态：

- 排队期间配对图已变更：任务标记 `stale`，不再求解；
- 求解期间配对图变更：结果不落盘为版本，任务标记 `stale`；
- 更新的一次校准已发布后，更早的任务才返回：不写版本，任务标记 `stale`；发布接口也会拒绝比当前已发布版本更早的任务。

发布本身是单事务原子切换：先核验，再在同一个提交里写 `calibration_versions.state='published'` 与 `calibration_state.published_version_id`。

### 研究与校准版本

新建研究任务在提交时把 `calibration_version_id` 与 `calibration_checksum` 复制进冻结快照；worker 按该版本读取偏移修正读数，并把版本号、修正条数、未校准条数、校验和写回结果。之后再发布新版本不会改动既有研究——快照中的版本号不变，偏移表按版本 id 不可变，结果也不重算。没有已发布版本时研究按原始读数计算。

未绑定仪器或仪器不在校准版本中的读数原样保留并在页面标注"未校准"，不会被静默丢弃。

### 接口

| 方法与路径 | 说明 |
|---|---|
| `GET /calibration/instruments` | 仪器、锚点与图规模/内存预算 |
| `POST /calibration/instruments` | 登记仪器；`is_anchor` 指定可信零偏 |
| `POST /calibration/instruments/{key}/anchor` | 仅负责人可改锚点 |
| `GET/POST /calibration/pairs` | 分页查看 / 批量录入配对（≤1000/次） |
| `PUT/DELETE /calibration/pairs/{id}` | 更正（递增修订号）/ 删除配对 |
| `GET/POST /calibration/runs` | 校准任务列表 / 发起求解 |
| `POST /calibration/runs/{id}/retry` | 失败/失效任务在配对图未变时重新入队续算 |
| `POST /calibration/runs/{id}/publish` | 仅负责人；核验后原子发布 |
| `GET /calibration/versions` | 版本列表与当前发布标记 |
| `GET /calibration/versions/{id}/offsets` | 分页仪器零偏 |
| `GET /calibration/versions/{id}/residuals` | 分页残差，可按特征与最小绝对值筛选 |
| `GET /calibration/versions/{id}/readings?source_id=` | 修正读数核验：原始值与修正值并排 |

## 演示数据与一天剧本

固定seed=42，3个来源 ` HL-202611-01/02/03`，每来源240条，共720条业务记录；三个账号和首轮种子任务。自然键检查后仅插入缺失数据，连跑seed不重复、不覆盖用户修改。`SEED_DEMO=true` 时启动自动执行；失败记录日志但不阻塞服务。首轮任务同样由独立worker真实计算。

0. 08:50 负责人打开“全网校准”，查看 18 台已锚定仪器、6 台锚点与隔离的 `HL-INS-09` 两台仪器；分析师登记一条新配对差。
1. 09:00 负责人查看三份来源和720条样本。
2. 09:10 分析师选择 `HL-202611-01 秋季常规组`，核对 `station-density-202611-01.csv`。
3. 09:15 在“巡测专题”核对温度 ℃（`temperature`）、相对湿度 %（`humidity`）、颗粒浓度 μg/m³（`particles`），追加编号 `HL-NEW-001` 的样本。
4. 09:20 新建“十一月复核研究”，按默认参数冻结241条输入与当前已发布校准版本。
5. 09:21 独立worker消费Redis队列，返回实算结果与输入校验和。
6. 09:25 分析师查看专属诊断及逐行结果，并下载CSV复算。
7. 09:30 提交“十一月业务复核意见”，负责人核对并批准。
8. 09:40 viewer下载批准快照，查看审计中的worker完成、校准发布与批准事件。
9. 09:50 分析师更正一条配对，重新校准：页面显示复用与新解分量数，负责人核验后发布新版本；09:20 的研究仍显示冻结的旧版本与校验和。

## 目录与本地开发

```text
frontend/src/api, stores, types, components/common, hooks, pages, router, utils, constants
backend/src/routes, controllers, services, models, schemas, middlewares, utils, constants, config
backend/scripts/seed_demo.py
backend/migrations/001_initial.sql
```

本地后端：Python3.11虚拟环境安装 `backend/requirements.txt`，设置 `DATABASE_URL`、`REDIS_URL`、`ARTIFACT_DIR`，在backend目录运行 `uvicorn src.main:app --reload`；另开终端运行 `python -m src.worker`。原生前端无需编译，推荐以Compose Nginx访问，保证 `/api/` 代理。前端为Google Fonts双字体、oklch色板，离线字体回退serif；1280/768/390响应布局。

| 环境变量 | 默认与说明 |
|---|---|
| COMPOSE_PROJECT_NAME | `station-density`，隔离容器/网络/命名卷 |
| FRONTEND_PORT / BACKEND_PORT / DB_PORT | `18246 / 19246 / 17746` |
| SEED_DEMO | `true` |
| JWT_SECRET | `.env.example`演示值，部署前修改 |
| DATABASE_URL / REDIS_URL | Compose内部连接，定义在compose的env锚点 |
| ARTIFACT_DIR | `/artifacts`，后端worker共享 |
| CALIBRATION_CHECKPOINT_DIR | 可选，默认 `ARTIFACT_DIR/calibration-checkpoints`；中断恢复的迭代检查点 |

共享枚举：`backend/src/constants/enums.py` 与 `frontend/src/constants/enums.js` 都定义 admin/analyst/viewer、queued/running/completed/failed、draft/approved。后端service检查对应字符串；README为全量值说明。共享前端组件table/status/chart/business跨总览、结果、配置与复核复用；actions hook和format utils负责交互/格式。后端errors与rate_limit是两个中间件。

后端测试在 `backend/tests/`：求解器对稠密最小二乘 oracle、环路残差、无锚点隔离、2 万仪器/10 万配对预算、重试续算与不重复累计；服务层覆盖配对唯一性与方向规范化、分量复用、陈旧/晚归拒绝、发布核验、冻结版本下修正读数、中断重排；HTTP 层覆盖角色权限、配对/求解/发布全流程与研究冻结。运行：

```bash
cd backend
DATABASE_URL=sqlite:////tmp/calibration-test.db python3 -m pytest tests -q
```

测试及截图在批根 `test-artifacts/gb-0042/`，不混入源码。独立算法oracle、HTTP完整流程和真实Playwright浏览器流程见该目录 `TESTING.md`。License：MIT。

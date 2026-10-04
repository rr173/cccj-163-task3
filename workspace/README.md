# 高精地图瓦片 · 分区发布与车辆路线快照服务

零第三方依赖（Python 3.11 标准库 + 原生 HTML/JS），实现"地图编辑 → 接缝/跨区路线验证
→ 按区域代际发布 → 车辆路线快照固定 → 紧急补丁安全过渡 → 离线追赶"的完整闭环。

## 启动

```bash
python3 server.py 8080          # 打开 http://localhost:8080
```

首次启动会自动生成演示世界并落盘到 `data/mapstate.json`（原子写）。
首次使用审计功能会自动创建独立的 `data/audit.json`（append-only）。
删除 `data/` 即可重置。命令行工具 `route_replay.py` / `audit_cli.py`
直接读写各自存储，无需开服务（`audit_cli.py` 对线上存储只读）。

## 领域模型与不变量

| 概念 | 说明 |
|---|---|
| 瓦片 `tile` | 内容以 sha256 寻址、入库即不可变；含节点、车道规则、跨瓦片端口（拓扑边界）、bounds |
| 区域 `region` | 发布分区（演示为 north / south）；每个区域维护单调递增的 `generation` |
| 候选 `candidate` | 编辑器产出，绑定**基线代际**；发布前必须通过接缝与跨区走廊校验 |
| 发布 `release` | 按区域代际推进、原子提交；多区域发布要么全成要么全无；发布失败不产生任何新版本 |
| 有效版本 | `north=N/south=M+P-xxxx[p优先级]…`，其描述符再做一次 sha256（descriptor_hash） |
| 补丁 `patch` | **纯规则叠加**（只能封闭既有车道），固化基础版本瓦片哈希；不含几何/拓扑增删 |
| 快照 `snapshot` | 车辆取路线时固定"版本描述符 + 瓦片哈希集合 + 逐边路径"，永不被后续发布/补丁改写 |

**接缝校验**（候选发布闸门）：端口相互声明、全局坐标重合、限速/车道数 profile 一致、
无孤立端口，外加区域瓦片完整性（必须重述本区全部瓦片）与全部跨区验收走廊可贯通。

**新旧拓扑绝不拼接**：在途快照的 descriptor_hash 与瓦片集合恒定；途中有新封路时只发预警。
车辆在车道上（`on_lane`）请求过渡会被拒绝；必须先完成当前车道、到达节点（`at_node`），
才能切换到基于当前版本重算的**子快照**（父子链可审计）。

**补丁安全**：生成时即在"基础版本+封闭叠加"图上验证每条封闭车道仍存在节点间绕行；
激活前再次核对基础瓦片哈希（漂移则拒绝）、兼容性结论与重复封闭。无绕行的补丁标记
`incompatible` 且无法激活。撤销只影响新路线；历史快照仍按其固定补丁复算。

**离线追赶**：车辆带最后确认事件号重连，服务回放全部错过事件，给出唯一当前版本与
在途快照预警；同步本身幂等（事件检查点持久化）。

**区域变更车辆分组试运行（canary）**：运维只能从**校验通过**且基线为当前代际的候选创建
批次，指定作用区域、车辆对象名单与观察窗口（`window_start/window_end` 或
`window_minutes`）。窗口内名单车辆的新行程解析到**待晋级内容**（候选瓦片覆盖区域 head，
快照版本带 `区域@canary:候选(TB-xxxx)` 标记），名单外车辆继续读取当前已发布内容；活动
补丁叠加对两组一致生效。观察窗口结束（或未开始）的批次保持 `running` 但不再分流、不再
计数。每个批次分别统计两组的**请求量、不可达次数、安全过渡拒绝次数**（不可达拒绝也先
落盘统计）。

- **冲突互斥**：任一区域已有进行中（未决）批次时，同区域新批次与常规发布一律
  `TRIAL_REGION_BUSY` 拒绝（含窗口已结束但尚未晋级/撤回的批次）。
- **阈值晋级**：`promote` 先核对晋级组不可达率/过渡拒绝率/最少样本（默认
  0.1 / 0.2 / 0），未达标 `TRIAL_GATE_BREACHED`（可 `force` 覆盖）；再用"已发布
  head+候选覆盖"工作集原子复核接缝/走廊，通过后才按批次区域推进代际生成新 release，
  失败不动任何区域。
- **撤回**：`withdraw` 不改变任何区域代际；**已发出的试运行快照永不改写**（其描述符、
  瓦片集合、路线固定；历史可逐字节复现），只有新行程回到当前内容。
- **判定唯一**：重复晋级/撤回永远返回首次结果（`idempotent:true`）；已晋级再撤回、
  已撤回再晋级均 409 并附带 `first_result`。
- **持久化**：批次、两组统计、终态与首次结果全部随 mapstate 原子落盘，服务重启恢复；
  所有写操作沿用深拷贝→临时文件→`os.replace`，失败不留部分区域切换。

## API（均为 JSON，前缀 `/api`）

### 线上运维 API

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/status` | 区域代际/head/当前版本/活动补丁 |
| GET | `/tiles` · POST `/tiles` | 瓦片清单 · 导入（内容寻址，重复幂等） |
| GET/POST | `/candidates` | 候选列表 · 创建候选 `{regions, tiles:[{tile_id,hash}]}` |
| POST | `/candidates/{id}/validate` | 接缝+跨区走廊校验 |
| POST | `/candidates/{id}/publish` | 按区域代发布（基线过期→409，校验失败→422 原子拒绝） |
| GET | `/releases` · `/corridors` · `/events` | 发布史/验收走廊/审计日志 |
| GET/POST | `/patches` | 补丁队列 · 创建（自动兼容性+绕行校验） |
| POST | `/patches/{id}/activate` · `/revoke` | 生效（复核基础哈希）· 撤销 |
| GET/POST | `/trials` | 试运行批次清单 · 从校验通过候选创建 `{candidate_id,regions,vehicles,window_minutes|window_start/window_end,max_*}` |
| GET | `/trials/{id}` | 批次详情：两组请求量/不可达/安全过渡拒绝与晋级闸门评估 |
| POST | `/trials/{id}/promote` · `/withdraw` | 阈值闸门一次性晋级（`{force:true}` 可覆盖）· 撤回；重复操作返回首次结果 |
| POST | `/routes` | `{vehicle_id, from_node, to_node}` → 固定快照（名单内车辆在窗口内得到 canary 版本，201） |
| GET | `/snapshots` · `/snapshots/{id}` | 快照清单 · 完整复现载荷（含 tile 内容/预警） |
| POST | `/snapshots/{id}/progress` | 上报 `{step, state: at_node|on_lane}` |
| POST | `/snapshots/{id}/adopt-reroute` | **仅 at_node** 可过渡到绕行子快照 |
| POST | `/snapshots/{id}/complete` | 完成行程 |
| GET | `/vehicles/{id}` · `/vehicles/{id}/sync?last_event_id=N` | 车辆快照链 · 离线追赶 |
| GET | `/view?mode=published\|candidate\|snapshot&ref=` | 审查页地图数据（瓦片/接缝/走廊/路线） |

### 监管审计 API（只读线上；只写独立的 `data/audit.json`）

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/audit/template` | **只读**：按当前线上版本生成一份正确签名的样例证据链（不写档案） |
| POST | `/audit/vehicle-events` | **只读**：把某台线上车辆的快照链签名成可导入事件 |
| POST | `/audit/imports` | 导入证据链 `{import_key, events}`；首次判定，201 通过 / 422 拒绝 |
| GET | `/audit/imports` · `/audit/imports/{key}` | 导入清单 · 链详情（逐条事件摘要） |
| POST | `/audit/imports/{key}/append` | 长序列从持久头部检查点续算（坏批次不污染原链） |
| POST | `/audit/imports/{key}/checkpoints` | 审计员确认检查点 `{seq, note}` |
| GET | `/audit/imports/{key}/checkpoints` · `/branches` | 检查点清单 · 隔离分支清单 |
| POST/GET | `/audit/imports/{key}/query` | 按 `seq` 或 `ts` 重建时刻空间状态 |
| POST | `/audit/imports/{key}/branches` | 从**已确认**检查点建分支：`version_switch` 或 `revoke_closure` |
| GET | `/audit/branches/{id}` | 分支完整演算结果（含 what-if 地图） |
| GET/POST | `/audit/exports` | 导出审计证据包（事件+检查点+分支+bundle 摘要） |
| GET | `/audit/archives` | append-only 审计档案流（查询/分支/确认/导出/续算全部留痕） |

**证据事件**：`{seq, ts, source, type, payload, parent_digest, digest}`，
`digest = sha256(规范化{seq,ts,source,type,payload,parent_digest})`。
类型 `route_pinned / progress / reroute_adopted / trip_completed`。

**拒绝码**：`AUDIT_SEQ_GAP`（序号跳跃）、`AUDIT_DUPLICATE_EVENT`（重复事件）、
`AUDIT_DIGEST_MISMATCH`（摘要被改写）、`AUDIT_PARENT_MISSING`（父事件/父路线缺失），
以及 `AUDIT_CHECKPOINT_NOT_CONFIRMED`（分支点未确认）、`AUDIT_PATCH_NOT_PINNED`
（撤销的管制不在该时刻版本中）。同一 `import_key` 永远返回首次判定
（响应带 `idempotent:true`）。重建时还会逐瓦片复核内容摘要、重算版本描述哈希。

## 页面

- **🗺️ 地图审查**：区域代际表、候选卡片（一键校验/发布）、SVG 拓扑图（接缝绿/红点、
  跨区走廊绿线）、结构化校验报告、发布时间线。
- **🚨 封路补丁**：勾选车道生成补丁（自动给出兼容/不兼容与绕行），激活/撤销。
- **🧪 分组试运行**：从校验通过候选创建批次（区域/车辆名单/观察窗口/晋级阈值），
  卡片展示两组请求量、不可达率、安全过渡拒绝率与阈值闸门，一键晋级或撤回；
  终态批次重复操作显示"幂等返回首次结果"。
- **🚗 路线复现**：请求路线、模拟"进入车道/到达节点"、封路预警、节点处安全过渡、
  行程完成、离线追赶事件回放（canary 快照带 🧪 标记与批次号）。
- **⚖ 时序审计**（只读）：导入带序号/来源/摘要校验的车辆运行事件；在地图上重建
  指定事件序号（或时刻）的空间状态——版本描述、瓦片摘要、行驶步骤、当前位置；
  从已确认检查点一键隔离演算"版本切换"或"撤销道路管制"；导出审计证据包。
  查询/分支/导出只追加审计档案，绝不改动线上。

## CLI：route_replay.py

```bash
./route_replay.py status
./route_replay.py plan VH-01 A00/w B01/e          # 固定快照
./route_replay.py replay S-0001                   # 确定性逐边复现（含版本哈希/瓦片哈希）
./route_replay.py progress S-0001 1 at_node
./route_replay.py advisory S-0001
./route_replay.py adopt-reroute S-0001
./route_replay.py patch A00 lce "事故封路" 100
./route_replay.py activate P-0001
./route_replay.py revoke P-0001 "管制解除"
./route_replay.py validate RC-0003
./route_replay.py publish RC-0002
./route_replay.py trials                                        # 批次与两组统计
./route_replay.py trial-start RC-0002 north VH-CANARY-01 60     # 区域/名单/窗口分钟
./route_replay.py trial-show TB-0001
./route_replay.py trial-promote TB-0001 [--force]               # 阈值达标晋级
./route_replay.py trial-withdraw TB-0001 "观察期不达标"
./route_replay.py sync VH-02 0
```

一键试运行演示：`bash demo_trials.sh`（自动重置 store，覆盖冲突、分流、统计、
阈值拒绝、强制晋级、幂等返回与快照固定）。

## 监管审计 CLI：audit_cli.py（只读线上）

```bash
./audit_cli.py template [import_key] [from] [to] [vehicle]   # 只读样例签名
./audit_cli.py vehicle-events VH-01 EV-77                    # 只读：线上车辆→证据事件
./audit_cli.py import events.json EV-77                      # 导入（同一键返回首次判定）
./audit_cli.py append EV-77 more-events.json                 # 长链从持久检查点续算
./audit_cli.py imports · show EV-77 · archives
./audit_cli.py confirm EV-77 5 "审计确认点"
./audit_cli.py query EV-77 5                    # 重建 seq=5 的版本/瓦片/步骤
./audit_cli.py query EV-77 --ts 2026-10-03T09:54:37+00:00
./audit_cli.py branch-switch EV-77 1 REL-0002  # 隔离演算：版本切换
./audit_cli.py branch-revoke EV-77 5 P-0001    # 隔离演算：撤销道路管制
./audit_cli.py branches EV-77 · branch B-0001
./audit_cli.py export EV-77 audit-bundle.json  # 导出证据包
```

一键审计演示：`bash demo_audit.sh`（自动重置两个 store 并走完整流程）。
自动化校验：`python3 test_audit.py`（覆盖四类拒绝、幂等、续算、两种分支、隔离保证）。

## 推荐演示路径（种子已备好两个候选）

1. 打开地图审查：当前 `north=1/south=1`，4 条接缝全绿、C1/C2 走廊贯通。
2. 点开 **RC-0003（v3-bad）**：南端口限速改 80 + 删西入口车道 → 接缝红点、
   走廊 C1 断裂；点发布得到 422，版本纹丝不动（审计日志记 PUBLISH_REJECTED）。
3. 校验并发布 **RC-0002（v2，内部新增车道、边界不变）**：north→第 2 代。
4. 路线复现页为 VH-01 请求 `A00/w → B01/e`，固定 S-0001。
5. 封路补丁页封闭 `A00/lce`（生成时给出经 B00 的绕行），激活。
6. 回路线页：S-0001 仍是原版本；点"进入车道"后尝试"采用绕行"→ 被拒；
   点"到达节点"后采用 → 生成子快照 S-0002（版本带 `+P-0001`，全程无车道中途拼接）。
7. 撤销补丁：S-0002 历史复算仍封闭，新路线不再封闭；离线追赶一次性回放所有事件。

## 文件

```
engine.py          领域引擎：拓扑图/Dijkstra、校验、发布、补丁、快照、原子持久化
server.py          标准库 HTTP API + 静态文件
route_replay.py    路线复现/生命周期 CLI
audit.py           监管审计引擎：证据链导入/时序重建/检查点/隔离分支/导出（只读线上）
audit_cli.py       审计 CLI（导入/查询/检查点/分支/导出）
test_audit.py      审计需求端到端校验（可重复运行）
test_trials.py     车辆分组试运行需求端到端校验（冲突/分流/统计/阈值/幂等/窗口/重启）
demo_audit.sh      审计功能一键演示
demo_trials.sh     分组试运行一键演示
static/            审查页（index.html / app.js / styles.css，含 🧪 分组试运行 标签页）
data/mapstate.json 线上单一事实源（原子落盘；审计从不写入）
data/audit.json    审计 append-only 存储（imports/checkpoints/branches/exports/archives）
```

## 审计子系统的隔离不变量

- **物理分离**：审计状态全部在独立的 `data/audit.json`；`audit.py` 只以深拷贝方式
  读取线上 `MapEngine.state`（持锁快照），从不调用任何线上写方法，也不实例化/修改线上代际。
- **证据链不可变**：导入即校验 `序号连续 / 无重复 / 摘要匹配 / 父链完整 / 事件语义`；
  被拒导入也留档（`import_rejected` 档案），但不进入证据链。续算的坏批次只记
  `append_rejected`，头部检查点与原链保持不动。
- **判定唯一**：同一 `import_key` 无论再提交什么都返回第一次的判定。
- **只读时序查询**：重建只在内容寻址瓦片库上复核 `sha256(tile)` 与版本描述哈希；
  篡改以 `tile_digest_mismatch / descriptor_hash_mismatch` 预警呈现，不影响查询出证。
- **分支即档案**：每个分支固定在一个已确认检查点，只演算一次假设；版本切换只允许指向
  **已发布**的 release，撤销管制只允许撤销该时刻版本中实际固定的补丁；结果含
  与该版本的瓦片/代际/路径差异。分支永远不写线上。
- **查询/分支/导出都产生新档案**：`A-####` 审计档案 append-only，`result_digest`
  固化每次出证内容；导出包整体再算一个 `bundle_digest`。

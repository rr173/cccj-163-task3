#!/usr/bin/env bash
# End-to-end guided demo of the regional canary-trial (车辆分组试运行) feature.
# Safe to re-run: resets data/ first. Drives route_replay.py against the store.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf data
PY="python3 route_replay.py"
say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

say "1. 校验候选 RC-0002（北区内部新增车道，边界不变）"
$PY validate RC-0002 | python3 -c 'import json,sys;print("  ",json.load(sys.stdin)["status"])'

say "2. 运维创建试运行批次：区域 north / 名单 VH-CANARY-01 / 窗口 60 分钟"
$PY trial-start RC-0002 north VH-CANARY-01 60 \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  ",d["trial_id"],d["phase"],d["window_start"],"~",d["window_end"])'

say "3. 同区域再次创建批次 -> 409 TRIAL_REGION_BUSY（冲突拒绝）"
$PY trial-start RC-0002 north VH-OTHER 10 2>&1 | sed 's/^/  /' || true

say "4. 常规发布同区域候选也被锁定 -> 409"
$PY publish RC-0002 2>&1 | sed 's/^/  /' || true

say "5. 分流：名单内车辆读取待晋级内容（canary 标记）"
$PY plan VH-CANARY-01 A00/w B01/e \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  ",d["snapshot_id"],d["version"]["version"])'

say "6. 名单外车辆继续读取当前已发布内容"
$PY plan VH-01 A00/w B01/e \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  ",d["snapshot_id"],d["version"]["version"])'

say "7. 制造一次不可达（两边各统计）与一次安全过渡拒绝"
$PY patch A00 lce "试运行期事故封路" 100 >/dev/null
$PY activate P-0001 >/dev/null
$PY progress S-0001 1 on_lane >/dev/null
($PY adopt-reroute S-0001 2>&1 | sed 's/^/  /' || true)
$PY progress S-0001 1 at_node >/dev/null
$PY adopt-reroute S-0001 >/dev/null
($PY plan VH-CANARY-01 A00/w ZZ/zz 2>&1 | sed 's/^/  /' || true)
($PY plan VH-01 A00/w ZZ/zz 2>&1 | sed 's/^/  /' || true)

say "8. 查看两组统计（请求量 / 不可达 / 安全过渡拒绝）"
$PY trials

say "9. 阈值未达标直接晋级 -> 422 TRIAL_GATE_BREACHED（可加 --force 强制）"
$PY trial-promote TB-0001 2>&1 | sed 's/^/  /' || true

say "10. 观察结论明确后一次性晋级（--force），north -> 第 2 代"
$PY trial-promote TB-0001 --force \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  ",d["status"],d["result"]["release_id"],d["result"]["generations"])'

say "11. 重复晋级 -> 返回首次结果（idempotent=true）"
$PY trial-promote TB-0001 | python3 -c 'import json,sys;d=json.load(sys.stdin);print("  idempotent=",d["idempotent"],"release=",d["result"]["release_id"])'

say "12. 试运行期间固定的 canary 快照仍可逐字节复现（未被晋级改写）"
$PY replay S-0001 | sed -n '1,7p' | sed 's/^/  /'

say "13. 当前版本已是第 2 代；同车辆新行程直接读已发布内容"
$PY status | python3 -c 'import json,sys;print("  ",json.load(sys.stdin)["current_version"])'

say "完成。界面: python3 server.py 8080 -> 🧪 分组试运行 标签页"

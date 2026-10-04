#!/usr/bin/env python3
"""End-to-end verification of the regional canary-trial (车辆分组试运行) feature.

Resets the live store to a seeded world and drives the domain engine
directly with a controllable clock. Asserts every requirement:

 1. batches can only be created from validated candidates (baseline current)
 2. a region with an in-progress batch rejects conflicting batches/publishes
 3. allowlisted vehicles resolve the candidate (待晋级) content; everyone
    else keeps resolving the current published content
 4. per-group requests / unreachable / safe-transition-rejected statistics
 5. threshold-gated one-shot promotion; forced bypass; verdict-once
 6. withdrawal never rewrites issued snapshots nor rolls generations back
 7. observation window: out-of-window requests are not diverted/counted
 8. batches + statistics survive a process restart (store reload)
 9. failed promotion/conflicts leave no partial region switch
"""
import json
import os
import shutil

import engine
from engine import DomainError, MapEngine

ROOT = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(ROOT, "data", "mapstate.json")

fails = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ FAIL: ") + name
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def expect_error(name, fn, code, status=None):
    try:
        fn()
    except DomainError as e:
        ok = e.code == code and (status is None or e.status == status)
        check(name, ok, f"got {e.status} {e.code}, want {status} {code}")
        return e
    check(name, False, f"expected {code}, no error raised")
    return None


class Clock:
    """Deterministic ISO clock for observation-window control."""
    def __init__(self, iso):
        self.t = iso

    def __call__(self):
        return self.t

    def advance(self, minutes):
        from datetime import datetime, timedelta, timezone
        dt = datetime.fromisoformat(self.t.replace("Z", "+00:00"))
        dt = dt + timedelta(minutes=minutes)
        self.t = dt.replace(microsecond=0).isoformat()


def register_north_candidate(eng, new_cid, note):
    """Create a validated north candidate on the CURRENT baseline, reusing
    the content that the promoted trial just made head (A00=v2)."""
    def m(s):
        rel = next(r for r in s["releases"]
                   if r["release_id"] == s["region_heads"]["north"])
        n = int(new_cid.split("-")[1])
        s["seq"]["candidate"] = max(s["seq"]["candidate"], n)
        s["candidates"][new_cid] = {
            "candidate_id": new_cid, "regions": ["north"],
            "tiles": {tid: h for tid, h in rel["tiles"].items()
                      if s["tiles"][h]["region"] == "north"},
            "note": note, "created_by": "test", "created_at": eng.now(),
            "based_on_generations": {"north": s["region_gen"]["north"]},
            "status": "submitted",
            "validation": {"status": "passed", "errors": [], "warnings": [],
                           "checked_at": eng.now(),
                           "baseline_generations": {"north": s["region_gen"]["north"]}}}
        return None
    eng.commit(m)


def main():
    shutil.rmtree(os.path.join(ROOT, "data"), ignore_errors=True)
    clock = Clock("2026-10-04T09:00:00+00:00")
    eng = MapEngine(LIVE, clock=clock)

    # validate the good internal-change candidate (north only)
    v = eng.validate_candidate("RC-0002")
    check("种子候选 RC-0002 校验通过", v["status"] == "passed", str(v.get("errors")))

    print("\n[1] 创建批次：未校验候选拒绝；区域超集拒绝；空名单拒绝")
    expect_error("未校验候选不能创建批次",
                 lambda: eng.create_trial({"candidate_id": "RC-0003",
                                           "regions": ["north"],
                                           "vehicles": ["VH-CANARY-01"]}),
                 "CANDIDATE_NOT_VALIDATED", 422)
    expect_error("作用区域超出候选区域拒绝",
                 lambda: eng.create_trial({"candidate_id": "RC-0002",
                                           "regions": ["north", "south"],
                                           "vehicles": ["VH-CANARY-01"]}),
                 "TRIAL_REGION_NOT_IN_CANDIDATE", 422)
    expect_error("空车辆名单拒绝",
                 lambda: eng.create_trial({"candidate_id": "RC-0002",
                                           "regions": ["north"],
                                           "vehicles": []}),
                 "BAD_REQUEST", 400)

    print("\n[2] 创建批次 TB-0001（north，名单 2 台，窗口 60 分钟）")
    t = eng.create_trial({
        "candidate_id": "RC-0002", "regions": ["north"],
        "vehicles": ["VH-CANARY-01", "VH-CANARY-02"], "window_minutes": 60,
        "max_unreachable_rate": 0.1, "max_transition_reject_rate": 0.5,
        "note": "北区内部新增联络车道灰度"})
    check("批次号 TB-0001 且观察中", t["trial_id"] == "TB-0001"
          and t["phase"] == "observing", t["trial_id"])
    check("两组统计初始为零",
          t["stats"]["canary"]["requests"] == 0
          and t["stats"]["control"]["requests"] == 0)

    print("\n[3] 区域冲突：同区域第二个批次 / 常规发布 都被拒绝")
    expect_error("同区域进行中批次拒绝冲突",
                 lambda: eng.create_trial({"candidate_id": "RC-0002",
                                           "regions": ["north"],
                                           "vehicles": ["VH-OTHER"]}),
                 "TRIAL_REGION_BUSY", 409)
    expect_error("试运行期间常规发布同区域候选拒绝",
                 lambda: eng.publish_candidate("RC-0002"),
                 "TRIAL_REGION_BUSY", 409)
    # disjoint region is allowed
    eng.validate_candidate  # no south candidate on hand — just assert north blocks

    print("\n[4] 分流：名单内走待晋级内容，名单外走当前内容")
    snap_canary = eng.create_route({"vehicle_id": "VH-CANARY-01",
                                    "from_node": "A00/w", "to_node": "B01/e"})
    check("名单内快照带 canary 标记",
          snap_canary["canary"] is True
          and snap_canary["canary_trials"] == ["TB-0001"],
          str(snap_canary["canary_trials"]))
    check("名单内版本含 @canary:RC-0002(TB-0001)",
          "@canary:RC-0002(TB-0001)" in snap_canary["version"]["version"],
          snap_canary["version"]["version"])
    snap_ctrl = eng.create_route({"vehicle_id": "VH-01",
                                  "from_node": "A00/w", "to_node": "B01/e"})
    check("名单外快照不是 canary", snap_ctrl["canary"] is False
          and "|" not in snap_ctrl["version"]["version"],
          snap_ctrl["version"]["version"])
    check("两份快照的 A00 瓦片哈希不同（待晋级 vs 当前）",
          snap_canary["tiles"]["A00"] != snap_ctrl["tiles"]["A00"])

    t = eng.get_trial("TB-0001")
    check("两组请求量各 +1",
          t["stats"]["canary"]["requests"] == 1
          and t["stats"]["control"]["requests"] == 1,
          json.dumps(t["stats"], ensure_ascii=False))

    print("\n[5] 不可达计数：两组各一次不可达，计入对应组")
    expect_error("名单内请求不存在节点 -> ROUTE_IMPOSSIBLE 且统计落盘",
                 lambda: eng.create_route({"vehicle_id": "VH-CANARY-02",
                                           "from_node": "A00/w",
                                           "to_node": "ZZ/zz"}),
                 "ROUTE_IMPOSSIBLE", 422)
    expect_error("名单外请求不存在节点 -> ROUTE_IMPOSSIBLE",
                 lambda: eng.create_route({"vehicle_id": "VH-01",
                                           "from_node": "A00/w",
                                           "to_node": "ZZ/zz"}),
                 "ROUTE_IMPOSSIBLE", 422)
    t = eng.get_trial("TB-0001")
    check("不可达按组各 1，请求量各 2",
          t["stats"]["canary"]["unreachable"] == 1
          and t["stats"]["control"]["unreachable"] == 1
          and t["stats"]["canary"]["requests"] == 2
          and t["stats"]["control"]["requests"] == 2,
          json.dumps(t["stats"], ensure_ascii=False))

    print("\n[6] 安全过渡拒绝：名单内车辆在车道上过渡被拒，计入 canary 组")
    p = eng.create_patch({"scope": {"tiles": {"A00": ["lce"]}},
                          "reason": "试运行期事故封路", "priority": 100})
    check("封路补丁兼容（有绕行）", p["compatibility"]["status"] == "compatible")
    eng.activate_patch(p["patch_id"])
    sid = snap_canary["snapshot_id"]
    # the pinned canary route crosses A00/lce; enter that lane then try adopt
    eng.update_progress(sid, {"step": 1, "state": "on_lane"})
    adv = eng.get_snapshot(sid)["advisory"]
    check("canary 快照收到封路预警", len(adv["closures_ahead"]) >= 1)
    expect_error("车道上采用过渡 -> NOT_AT_NODE",
                 lambda: eng.adopt_reroute(sid), "NOT_AT_NODE", 409)
    t = eng.get_trial("TB-0001")
    check("canary 组安全过渡拒绝 +1（并计入一次请求）",
          t["stats"]["canary"]["transition_rejected"] == 1
          and t["stats"]["canary"]["requests"] == 3,
          json.dumps(t["stats"]["canary"], ensure_ascii=False))
    check("control 组未被失败过渡影响",
          t["stats"]["control"]["transition_rejected"] == 0
          and t["stats"]["control"]["requests"] == 2)
    # arriving at a node allows the transition (new child on CURRENT content)
    eng.update_progress(sid, {"step": 1, "state": "at_node"})
    child = eng.adopt_reroute(sid)
    check("节点处安全过渡成功，子快照回到当前已发布内容（非 canary）",
          child["canary"] is False and "|" not in child["version"]["version"],
          child["version"]["version"])

    print("\n[7] 阈值闸门：不可达率 1/3 > 10% -> 晋级被 422 拒绝，无部分切换")
    gens_before = dict(eng.state["region_gen"])
    heads_before = dict(eng.state["region_heads"])
    expect_error("阈值未达标禁止晋级",
                 lambda: eng.promote_trial("TB-0001"),
                 "TRIAL_GATE_BREACHED", 422)
    check("闸门拒绝后区域代际/head 不变",
          eng.state["region_gen"] == gens_before
          and eng.state["region_heads"] == heads_before
          and eng.get_trial("TB-0001")["status"] == "running")

    print("\n[8] 强制晋级 -> north 第 2 代；重复晋级幂等返回首次结果")
    promoted = eng.promote_trial("TB-0001", {"force": True})
    check("晋级为 REL-0002 且 north=2",
          promoted["result"]["outcome"] == "promoted"
          and promoted["result"]["release_id"] == "REL-0002"
          and eng.state["region_gen"]["north"] == 2
          and eng.state["region_gen"]["south"] == 1,
          json.dumps(promoted["result"], ensure_ascii=False))
    again = eng.promote_trial("TB-0001")
    check("重复晋级返回首次结果且 idempotent=true",
          again["idempotent"] is True
          and again["result"]["release_id"] == "REL-0002"
          and eng.state["region_gen"]["north"] == 2)
    expect_error("已晋级批次不能再撤回（首次晋级结果保留）",
                 lambda: eng.withdraw_trial("TB-0001"),
                 "TRIAL_ALREADY_PROMOTED", 409)
    # canary snapshot remains byte-identical even though it is now published
    replay = eng.get_snapshot(sid)
    check("晋级不改写已发出的 canary 快照（版本/瓦片/路线不变）",
          replay["version"]["version"] == snap_canary["version"]["version"]
          and replay["tiles"] == snap_canary["tiles"]
          and replay["route"] == snap_canary["route"]
          and replay["version"]["descriptor_hash"]
          == snap_canary["version"]["descriptor_hash"])
    new_route = eng.create_route({"vehicle_id": "VH-CANARY-01",
                                  "from_node": "A00/w", "to_node": "B01/e"})
    check("晋级后同车辆新行程直接读取已发布第 2 代（不再带 canary 标记）",
          new_route["canary"] is False
          and new_route["version"]["base"]["north"]["generation"] == 2,
          new_route["version"]["version"])

    print("\n[9] 撤回批次：不改快照、不倒代际；重复撤回幂等；再晋级 409")
    register_north_candidate(eng, "RC-0004", "基于第2代的再试候选")
    t2 = eng.create_trial({"candidate_id": "RC-0004", "regions": ["north"],
                           "vehicles": ["VH-W01"], "window_minutes": 30})
    tid2 = t2["trial_id"]
    gens_b = dict(eng.state["region_gen"])
    c_snap = eng.create_route({"vehicle_id": "VH-W01",
                               "from_node": "A00/w", "to_node": "B01/e"})
    check("撤回前名单车辆读取试运行内容", c_snap["canary"] is True)
    wd = eng.withdraw_trial(tid2, {"reason": "演示撤回"})
    check("撤回成功且区域代际无变化",
          wd["status"] == "withdrawn"
          and eng.state["region_gen"] == gens_b
          and eng.state["region_gen"]["north"] == 2)
    wd2 = eng.withdraw_trial(tid2, {"reason": "另一个理由"})
    check("重复撤回返回首次结果（理由不变，idempotent=true）",
          wd2["idempotent"] is True
          and wd2["result"]["reason"] == "演示撤回")
    expect_error("已撤回批次不能再晋级",
                 lambda: eng.promote_trial(tid2),
                 "TRIAL_ALREADY_WITHDRAWN", 409)
    frozen = eng.get_snapshot(c_snap["snapshot_id"])
    check("撤回不改写试运行期间已发出的快照",
          frozen["version"]["version"] == c_snap["version"]["version"]
          and frozen["tiles"] == c_snap["tiles"]
          and frozen["route"] == c_snap["route"])
    after = eng.create_route({"vehicle_id": "VH-W01",
                              "from_node": "A00/w", "to_node": "B01/e"})
    check("撤回后名单车辆新行程回到当前已发布内容", after["canary"] is False)

    print("\n[10] 观察窗口：窗口外不分流、不计数")
    t3 = eng.create_trial({"candidate_id": "RC-0004", "regions": ["north"],
                           "vehicles": ["VH-FUTURE"],
                           "window_start": "2026-10-04T12:00:00+00:00",
                           "window_end": "2026-10-04T13:00:00+00:00"})
    check("未到窗口批次 phase=scheduled", t3["phase"] == "scheduled")
    pre = eng.create_route({"vehicle_id": "VH-FUTURE",
                            "from_node": "A00/w", "to_node": "B01/e"})
    check("窗口开始前名单车辆仍读当前内容", pre["canary"] is False)
    check("窗口外请求不计数",
          eng.get_trial(t3["trial_id"])["stats"]["canary"]["requests"] == 0)
    clock.advance(190)  # -> 12:10, inside the 12:00~13:00 window
    inside = eng.create_route({"vehicle_id": "VH-FUTURE",
                               "from_node": "A00/w", "to_node": "B01/e"})
    check("进入窗口后名单车辆读待晋级内容并计数",
          inside["canary"] is True
          and eng.get_trial(t3["trial_id"])["stats"]["canary"]["requests"] == 1)
    clock.advance(60)  # -> 13:10, window over
    expired = eng.create_route({"vehicle_id": "VH-FUTURE",
                                "from_node": "A00/w", "to_node": "B01/e"})
    check("窗口结束后不再分流（批次仍 running/phase=window_closed）",
          expired["canary"] is False
          and eng.get_trial(t3["trial_id"])["phase"] == "window_closed"
          and eng.get_trial(t3["trial_id"])["stats"]["canary"]["requests"] == 1)
    # expired window still blocks conflicting new batches until decided
    expect_error("窗口结束但未决的批次仍占用区域",
                 lambda: eng.create_trial({"candidate_id": "RC-0004",
                                           "regions": ["north"],
                                           "vehicles": ["VH-X"]}),
                 "TRIAL_REGION_BUSY", 409)

    print("\n[11] 重启恢复：批次、统计、终态、快照全部从磁盘恢复")
    eng2 = MapEngine(LIVE, clock=clock)
    restored = {t["trial_id"]: t for t in eng2.list_trials()}
    check("三个批次全部恢复", set(restored) == {"TB-0001", tid2, t3["trial_id"]})
    r1 = restored["TB-0001"]
    check("TB-0001 保持 promoted + 首次结果",
          r1["status"] == "promoted"
          and r1["result"]["release_id"] == "REL-0002"
          and r1["stats"]["canary"]["transition_rejected"] == 1
          and r1["stats"]["control"]["unreachable"] == 1)
    check("撤回批次保持 withdrawn + 首次理由",
          restored[tid2]["status"] == "withdrawn"
          and restored[tid2]["result"]["reason"] == "演示撤回")
    snap_restored = eng2.get_snapshot(sid)
    check("canary 快照重启后仍可逐字节复现",
          snap_restored["tiles"] == snap_canary["tiles"]
          and snap_restored["version"]["version"] == snap_canary["version"]["version"])
    # verdict-once after restart
    again2 = eng2.promote_trial("TB-0001")
    check("重启后重复晋级仍幂等返回首次结果",
          again2["idempotent"] is True
          and again2["result"]["release_id"] == "REL-0002")

    print("\n[12] 无部分区域切换：批次创建不改变代际；运行中批次持续锁定区域")
    gens = dict(eng2.state["region_gen"])
    expect_error("窗口结束但未决的批次仍阻止新批次（上节已验证 TRIAL_REGION_BUSY）",
                 lambda: eng2.create_trial({"candidate_id": "RC-0004",
                                            "regions": ["north"],
                                            "vehicles": ["VH-G"],
                                            "window_minutes": 30}),
                 "TRIAL_REGION_BUSY", 409)
    check("冲突拒绝后区域代际无任何变化（无部分切换）",
          eng2.state["region_gen"] == gens)
    # withdraw the expired-but-undecided batch, then a new one is allowed
    eng2.withdraw_trial(t3["trial_id"], {"reason": "窗口结束，决定不晋级"})
    moved = eng2.create_trial({"candidate_id": "RC-0004", "regions": ["north"],
                               "vehicles": ["VH-G"], "window_minutes": 30})
    st = eng2.region_status()
    check("状态接口暴露进行中批次",
          any(x["trial_id"] == moved["trial_id"] for x in st["trials"]))
    check("区域代际未被试运行创建动作改变",
          eng2.state["region_gen"] == gens)
    # failed promotion (gate breach) leaves generation intact: close the
    # window by injecting one bad canary observation via stats directly is
    # unnecessary — forced validation path is already covered by [7].
    eng2.withdraw_trial(moved["trial_id"])

    print("\n==== 结果 ====")
    if fails:
        print(f"{len(fails)} 项失败：")
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

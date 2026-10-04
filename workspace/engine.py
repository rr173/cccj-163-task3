"""
HD-map tile zoned release & vehicle route snapshot engine.

Core invariants
---------------
1. Tile content is content-addressed (sha256) and immutable once stored.
2. A *release* bumps a per-region generation counter; a region generation pins a
   complete, self-consistent tile set. Multi-region releases are atomic.
3. Emergency patches are rule-only overlays (lane closure) pinned to a base
   version. They never change geometry/connectivity, so they are the only kind
   of update that may interact with an in-flight trip - via a safe transition
   plan at a node boundary, never by splicing topology mid-lane.
4. A route *snapshot* fixes one version descriptor (release heads + ordered
   active patches) and one tile set. Re-fetching a snapshot always reproduces
   the exact same bytes/path; newer releases cannot enter it.
5. Every state transition is append/audit logged and every write is
   all-or-nothing (deep-copy mutate -> atomic temp file + os.replace).
"""

from __future__ import annotations

import copy
import hashlib
import heapq
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

EPS = 1e-6


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(obj)).hexdigest()


def short(h: str, n: int = 10) -> str:
    return h.replace("sha256:", "")[:n]


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def to_body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


# ---------------------------------------------------------------- topology ---

def node_key(tile_id: str, node_id: str) -> str:
    return f"{tile_id}/{node_id}"


def build_graph(tile_objs: list[dict], overlays: Optional[dict] = None) -> dict:
    """Return {nodes: {key: (x,y)}, adj: {key: [edge]}}.

    edge = {"kind":"lane"|"seam", "tile","lane","to","speed","closed",
            "from_tile","from_node","to_tile","to_node"}
    """
    overlays = overlays or {}
    nodes: dict[str, tuple[float, float]] = {}
    adj: dict[str, list[dict]] = {}
    by_id: dict[str, dict] = {}

    for t in tile_objs:
        by_id[t["tile_id"]] = t
        for n in t.get("nodes", []):
            nodes[node_key(t["tile_id"], n["node_id"])] = (n["x"], n["y"])
        adj.setdefault  # noqa  (placeholder to keep linters calm)
        for ln in t.get("lanes", []):
            u = node_key(t["tile_id"], ln["from"])
            v = node_key(t["tile_id"], ln["to"])
            closed = bool(overlays.get(t["tile_id"], {}).get(ln["lane_id"], {}).get("closed"))
            edge = {
                "kind": "lane",
                "tile": t["tile_id"],
                "lane": ln["lane_id"],
                "speed": ln.get("speed", 0),
                "two_way": bool(ln.get("two_way", True)),
                "closed": closed,
            }
            adj.setdefault(u, []).append({**edge, "to": v})
            if ln.get("two_way", True):
                adj.setdefault(v, []).append({**edge, "to": u})

    seam_pairs: set[tuple[str, str]] = set()
    for t in tile_objs:
        for p in t.get("ports", []):
            peer = p.get("peer")
            if not peer:
                continue
            t2 = by_id.get(peer["tile"])
            if t2 is None:
                continue
            p2 = next((q for q in t2.get("ports", []) if q["port_id"] == peer["port"]), None)
            if p2 is None:
                continue
            u = node_key(t["tile_id"], p["node"])
            v = node_key(t2["tile_id"], p2["node"])
            a, b = sorted([u, v])
            if (a, b) in seam_pairs:
                continue
            seam_pairs.add((a, b))
            adj.setdefault(u, []).append(
                {"kind": "seam", "tile": None, "lane": None, "speed": 0,
                 "two_way": True, "closed": False, "to": v,
                 "via_port": f"{t['tile_id']}:{p['port_id']}~{t2['tile_id']}:{p2['port_id']}"})
            adj.setdefault(v, []).append(
                {"kind": "seam", "tile": None, "lane": None, "speed": 0,
                 "two_way": True, "closed": False, "to": u,
                 "via_port": f"{t2['tile_id']}:{p2['port_id']}~{t['tile_id']}:{p['port_id']}"})
    return {"nodes": nodes, "adj": adj}


def _edge_cost(nodes, e: dict) -> float:
    if e["kind"] == "seam":
        return 1.0  # crossing a matched boundary is (near) free
    (x1, y1), (x2, y2) = nodes[e["_from"]], nodes[e["to"]]
    dist = ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5
    speed = max(float(e.get("speed") or 1), 1.0)
    return dist / speed


def dijkstra(graph: dict, start: str, goal: str) -> Optional[list[dict]]:
    nodes, adj = graph["nodes"], graph["adj"]
    if start not in nodes or goal not in nodes:
        return None
    dist = {start: 0.0}
    prev: dict[str, Optional[tuple[str, dict]]] = {start: None}
    pq = [(0.0, start)]
    while pq:
        d, u = heapq.heappop(pq)
        if d != dist.get(u):
            continue
        if u == goal:
            break
        for e in adj.get(u, []):
            if e.get("closed"):
                continue
            v = e["to"]
            full = dict(e, _from=u)
            nd = d + _edge_cost(nodes, full)
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = (u, full)
                heapq.heappush(pq, (nd, v))
    if goal not in prev:
        return None
    steps: list[dict] = []
    cur = goal
    while prev[cur] is not None:
        u, e = prev[cur]
        steps.append(_step_from_edge(u, cur, e))
        cur = u
    steps.reverse()
    return steps


def _step_from_edge(u: str, v: str, e: dict) -> dict:
    ut, un = u.split("/", 1)
    vt, vn = v.split("/", 1)
    if e["kind"] == "seam":
        return {"kind": "seam", "from_tile": ut, "from_node": un,
                "to_tile": vt, "to_node": vn, "via_port": e.get("via_port")}
    return {"kind": "lane", "tile": e["tile"], "lane": e["lane"],
            "speed": e.get("speed"), "from_tile": ut, "from_node": un,
            "to_tile": vt, "to_node": vn}


# ------------------------------------------------------------------- engine ---

class MapEngine:
    def __init__(self, path: str, clock: Callable[[], str] = utcnow_iso):
        self.path = path
        self.now = clock
        self.lock = threading.RLock()
        self.state: dict = {}
        self._load()

    # ------------------------------------------------- persistence ----------
    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self.state = json.load(f)
            # forward-compatibility for stores created before canary trials
            self.state.setdefault("trials", {})
            self.state.setdefault("seq", {}).setdefault("trial", 0)
        else:
            self.state = self._blank()
            self.seed()
            self._persist_locked()

    @staticmethod
    def _blank() -> dict:
        return {
            "tiles": {},          # content_hash -> tile object
            "candidates": {},     # candidate_id -> record
            "releases": [],       # append-only release log
            "region_heads": {},   # region -> release_id
            "region_gen": {},     # region -> int
            "patches": {},        # patch_id -> record
            "trials": {},         # trial_id -> regional canary trial batch
            "snapshots": {},      # snapshot_id -> record (immutable)
            "vehicles": {},       # vehicle_id -> {snapshots:[ids], last_event_id}
            "corridors": [],      # cross-region acceptance routes
            "events": [],
            "seq": {"candidate": 0, "release": 0, "patch": 0, "snapshot": 0,
                    "trial": 0, "event": 0},
        }

    def _persist_locked(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _next(self, s: dict, key: str, prefix: str) -> str:
        s["seq"][key] += 1
        return f"{prefix}-{s['seq'][key]:04d}"

    def _event_locked(self, etype: str, summary: str, ref: str) -> dict:
        self.state["seq"]["event"] += 1
        ev = {"event_id": self.state["seq"]["event"], "ts": self.now(),
              "type": etype, "summary": summary, "ref": ref}
        self.state["events"].append(ev)
        return ev

    def commit(self, mutate: Callable[[dict], Any]) -> Any:
        """Run mutate(copy) then atomically persist; all-or-nothing."""
        with self.lock:
            draft = copy.deepcopy(self.state)
            result = mutate(draft)
            # persist first; only swap state in after durable write succeeds
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(draft, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self.state = draft
            return result

    # --------------------------------------------------------- tiles --------
    @staticmethod
    def _validate_tile_schema(t: dict) -> None:
        for f in ("tile_id", "region", "nodes", "lanes", "ports"):
            if f not in t:
                raise DomainError("TILE_SCHEMA", f"tile missing field: {f}", 400)
        ids = [n["node_id"] for n in t["nodes"]]
        if len(ids) != len(set(ids)):
            raise DomainError("TILE_SCHEMA", "duplicate node ids in tile", 400)
        node_ids = set(ids)
        lane_ids = set()
        for ln in t["lanes"]:
            if ln["lane_id"] in lane_ids:
                raise DomainError("TILE_SCHEMA", "duplicate lane id", 400)
            lane_ids.add(ln["lane_id"])
            if ln["from"] not in node_ids or ln["to"] not in node_ids:
                raise DomainError("TILE_SCHEMA",
                                  f"lane {ln['lane_id']} references unknown node", 400)
        for p in t["ports"]:
            if p["node"] not in node_ids:
                raise DomainError("TILE_SCHEMA",
                                  f"port {p['port_id']} references unknown node", 400)

    def put_tile(self, tile: dict) -> dict:
        self._validate_tile_schema(tile)
        h = sha256(tile)
        existing = self.state["tiles"].get(h)
        if existing:
            return {"hash": h, "stored": False, "tile_id": tile["tile_id"]}

        def m(s):
            s["tiles"][h] = tile
            self._event_locked_run(s, "TILE_IMPORTED",
                                   f"导入瓦片 {tile['tile_id']} ({tile.get('version_tag', '?')})", h)
            return None

        self.commit(m)
        return {"hash": h, "stored": True, "tile_id": tile["tile_id"]}

    @staticmethod
    def _event_locked_run(s, etype, summary, ref):
        s["seq"]["event"] += 1
        s["events"].append({"event_id": s["seq"]["event"], "ts": utcnow_iso(),
                            "type": etype, "summary": summary, "ref": ref})

    def list_tiles(self) -> list[dict]:
        out = []
        for h, t in self.state["tiles"].items():
            out.append({"hash": h, "tile_id": t["tile_id"], "region": t["region"],
                        "version_tag": t.get("version_tag", ""),
                        "nodes": len(t["nodes"]), "lanes": len(t["lanes"]),
                        "ports": len(t["ports"])})
        return sorted(out, key=lambda x: (x["tile_id"], x["hash"]))

    # -------------------------------------------------- baseline/views ------
    def _release(self, s: dict, release_id: str) -> Optional[dict]:
        return next((r for r in s["releases"] if r["release_id"] == release_id), None)

    def _head_release_id(self, s: dict, region: str) -> Optional[str]:
        return s["region_heads"].get(region)

    def _region_tile_map(self, s: dict, region: str, release_id: Optional[str] = None) -> dict:
        """tile_id -> hash for a region at a given (or head) release."""
        rid = release_id or s["region_heads"].get(region)
        rel = self._release(s, rid) if rid else None
        if not rel:
            return {}
        return dict(rel["tiles"])

    def _working_tile_map(self, s: dict, candidate: Optional[dict] = None) -> dict:
        """tile_id -> tile_obj: candidate versions override published heads."""
        tile_map: dict[str, dict] = {}
        candidate_tiles: dict[str, str] = {}
        candidate_regions: set[str] = set()
        if candidate:
            candidate_tiles = dict(candidate["tiles"])
            candidate_regions = set(candidate["regions"])
        for region, rid in s["region_heads"].items():
            if region in candidate_regions:
                continue
            rel = self._release(s, rid)
            # an old multi-region release may carry other regions' tiles;
            # only take the tiles that belong to this region's head
            for tid, h in rel["tiles"].items():
                if s["tiles"][h]["region"] == region:
                    tile_map[tid] = s["tiles"][h]
        for tid, h in candidate_tiles.items():
            if h not in s["tiles"]:
                raise DomainError("TILE_MISSING", f"tile content {short(h)} missing", 409)
            tile_map[tid] = s["tiles"][h]
        return tile_map

    def _published_tile_map_all(self, s: dict) -> dict:
        return self._working_tile_map(s, None)

    # ------------------------------------------------- validation -----------
    def validate_tile_map(self, s: dict, tile_map: dict, candidate_regions: Optional[set] = None
                          ) -> tuple[list[dict], list[dict]]:
        errors: list[dict] = []
        warnings: list[dict] = []
        candidate_regions = candidate_regions or set()

        # 1) per-tile integrity + orphan ports
        for tid, t in sorted(tile_map.items()):
            node_ids = {n["node_id"] for n in t["nodes"]}
            seen_lanes = set()
            for ln in t["lanes"]:
                if ln["lane_id"] in seen_lanes:
                    errors.append(self._err("DANGLING_LANE", tid, f"重复车道 {ln['lane_id']}"))
                seen_lanes.add(ln["lane_id"])
                if ln["from"] not in node_ids or ln["to"] not in node_ids:
                    errors.append(self._err("DANGLING_LANE", tid,
                                            f"车道 {ln['lane_id']} 端点不在瓦片内",
                                            {"lane": ln["lane_id"]}))
            for p in t["ports"]:
                peer = p.get("peer")
                if not peer:
                    if not p.get("external"):
                        errors.append(self._err("SEAM_ORPHAN_PORT", tid,
                                                f"端口 {p['port_id']} 既无对端也非外边界",
                                                {"port": p["port_id"]}))
                    continue
                t2 = tile_map.get(peer["tile"])
                if t2 is None:
                    errors.append(self._err("SEAM_NO_PEER", tid,
                                            f"端口 {p['port_id']} 指向的瓦片 {peer['tile']} 不在工作集",
                                            {"port": p["port_id"], "peer_tile": peer["tile"]}))
                    continue
                p2 = next((q for q in t2["ports"] if q["port_id"] == peer["port"]), None)
                if p2 is None:
                    errors.append(self._err("SEAM_NO_PEER", tid,
                                            f"对端瓦片 {peer['tile']} 无端口 {peer['port']}",
                                            {"port": p["port_id"]}))
                    continue
                # reciprocal declaration
                if not p2.get("peer") or p2["peer"].get("tile") != tid or \
                        p2["peer"].get("port") != p["port_id"]:
                    errors.append(self._err("SEAM_NOT_RECIPROCAL", tid,
                                            f"接缝 {tid}:{p['port_id']} <-> "
                                            f"{peer['tile']}:{peer['port']} 未相互声明",
                                            {"port": p["port_id"]}))
                # coordinate coincidence (global frame)
                if abs(p["x"] - p2["x"]) > EPS or abs(p["y"] - p2["y"]) > EPS:
                    errors.append(self._err("SEAM_COORD_MISMATCH", tid,
                                            f"接缝端口坐标不一致 "
                                            f"({p['x']},{p['y']}) vs ({p2['x']},{p2['y']})",
                                            {"port": p["port_id"]}))
                # socket profile compatibility
                prof, prof2 = p.get("profile", {}), p2.get("profile", {})
                if prof.get("speed") != prof2.get("speed"):
                    errors.append(self._err("SEAM_PROFILE_MISMATCH", tid,
                                            f"接缝限速不一致 {prof.get('speed')} vs "
                                            f"{prof2.get('speed')} (@{tid}:{p['port_id']})",
                                            {"port": p["port_id"], "attr": "speed",
                                             "a": prof.get("speed"), "b": prof2.get("speed")}))
                if prof.get("lanes") != prof2.get("lanes"):
                    errors.append(self._err("SEAM_PROFILE_MISMATCH", tid,
                                            f"接缝车道数不一致 {prof.get('lanes')} vs "
                                            f"{prof2.get('lanes')} (@{tid}:{p['port_id']})",
                                            {"port": p["port_id"], "attr": "lanes"}))

        # 2) region completeness (candidate must restate every tile of its regions)
        for region in candidate_regions:
            rid = s["region_heads"].get(region)
            if rid:
                prev_ids = {tid for tid, h in self._release(s, rid)["tiles"].items()
                            if s["tiles"][h]["region"] == region}
                now_ids = {tid for tid, t in tile_map.items() if t["region"] == region}
                missing = prev_ids - now_ids
                if missing:
                    errors.append(self._err("REGION_INCOMPLETE", region,
                                            f"区域 {region} 候选未覆盖既有瓦片: "
                                            f"{sorted(missing)}",
                                            {"missing": sorted(missing)}))

        # 3) cross-region corridors must be traversable
        graph = build_graph(list(tile_map.values()))
        for cor in s["corridors"]:
            wps = cor["waypoints"]
            ok = True
            broken_segment = None
            for a, b in zip(wps, wps[1:]):
                if a not in graph["nodes"] or b not in graph["nodes"]:
                    ok, broken_segment = False, [a, b]
                    break
                if dijkstra(graph, a, b) is None:
                    ok, broken_segment = False, [a, b]
                    break
            if not ok:
                errors.append(self._err("ROUTE_BROKEN", cor["corridor_id"],
                                        f"跨区走廊 {cor['corridor_id']} 无法贯通: "
                                        f"{broken_segment[0]} -> {broken_segment[1]}",
                                        {"corridor": cor["corridor_id"],
                                         "segment": broken_segment}))
        return errors, warnings

    @staticmethod
    def _err(code: str, where: str, message: str, extra: Optional[dict] = None) -> dict:
        d = {"code": code, "where": where, "message": message}
        if extra:
            d.update(extra)
        return d

    # ------------------------------------------------- candidates -----------
    def create_candidate(self, body: dict) -> dict:
        regions = body.get("regions")
        tiles = body.get("tiles")
        if not regions or not tiles:
            raise DomainError("BAD_REQUEST", "regions 与 tiles 必填", 400)

        def m(s):
            for item in tiles:
                if item["hash"] not in s["tiles"]:
                    raise DomainError("TILE_MISSING",
                                      f"瓦片 {item['tile_id']} 内容 {short(item['hash'])} 未入库",
                                      400)
                t = s["tiles"][item["hash"]]
                if t["tile_id"] != item["tile_id"]:
                    raise DomainError("TILE_ID_CONFLICT",
                                      f"hash 对应 {t['tile_id']}，与声明 {item['tile_id']} 不符", 400)
                if t["region"] not in regions:
                    raise DomainError("REGION_MISMATCH",
                                      f"瓦片 {item['tile_id']} 属于区域 {t['region']}，"
                                      f"不在候选区域 {regions} 内", 400)
            cid = "RC-" + self._next(s, "candidate", "RC").split("-")[1]
            based_on = {r: s["region_gen"].get(r, 0) for r in regions}
            cand = {
                "candidate_id": cid,
                "regions": list(regions),
                "tiles": {it["tile_id"]: it["hash"] for it in tiles},
                "note": body.get("note", ""),
                "created_by": body.get("created_by", "editor"),
                "created_at": self.now(),
                "based_on_generations": based_on,
                "status": "draft",
                "validation": None,
            }
            s["candidates"][cid] = cand
            self._event_locked_run(s, "CANDIDATE_CREATED",
                                   f"创建候选 {cid}（区域 {','.join(regions)}）", cid)
            return copy.deepcopy(cand)

        return self.commit(m)

    def validate_candidate(self, cid: str) -> dict:
        if cid not in self.state["candidates"]:
            raise DomainError("NOT_FOUND", f"候选 {cid} 不存在", 404)

        def m(s):
            cand = s["candidates"][cid]
            tile_map = self._working_tile_map(s, cand)
            errors, warnings = self.validate_tile_map(s, tile_map, set(cand["regions"]))
            cand["validation"] = {
                "status": "passed" if not errors else "failed",
                "errors": errors,
                "warnings": warnings,
                "checked_at": self.now(),
                "baseline_generations": {r: s["region_gen"].get(r, 0) for r in cand["regions"]},
            }
            if errors:
                cand["status"] = "rejected"
            elif cand["status"] in ("draft", "rejected"):
                cand["status"] = "submitted"
            return copy.deepcopy(cand["validation"])

        return self.commit(m)

    def publish_candidate(self, cid: str) -> dict:
        if cid not in self.state["candidates"]:
            raise DomainError("NOT_FOUND", f"候选 {cid} 不存在", 404)

        def m(s):
            cand = s["candidates"][cid]
            if cand["status"] == "published":
                raise DomainError("ALREADY_PUBLISHED", f"候选 {cid} 已发布", 409)
            # while a canary trial observes a region, a normal release would
            # move its generation under both groups; require promote/withdraw
            for t in self._any_running_anytime(s):
                clash = sorted(set(cand["regions"]) & set(t["regions"]))
                if clash:
                    raise DomainError(
                        "TRIAL_REGION_BUSY",
                        f"区域 {clash} 正在试运行 {t['trial_id']}；请先在批次上"
                        f"晋级或撤回，再做常规发布（否则对照基线会被中途改写）", 409,
                        {"regions": clash, "existing_trial": t["trial_id"]})
            # optimistic concurrency: regions must not have moved since baseline
            for r in cand["regions"]:
                base_gen = cand["based_on_generations"].get(r, 0)
                cur_gen = s["region_gen"].get(r, 0)
                if base_gen != cur_gen:
                    raise DomainError("REGION_GENERATION_CONFLICT",
                                      f"区域 {r} 已从第 {base_gen} 代推进到第 {cur_gen} 代，"
                                      f"候选必须基于新基线重新校验后才能发布",
                                      409, {"region": r, "baseline": base_gen,
                                            "current": cur_gen})
            # re-validate atomically against the live baseline
            tile_map = self._working_tile_map(s, cand)
            errors, _ = self.validate_tile_map(s, tile_map, set(cand["regions"]))
            if errors:
                cand["validation"] = {"status": "failed", "errors": errors,
                                      "warnings": [], "checked_at": self.now(),
                                      "baseline_generations": {
                                          r: s["region_gen"].get(r, 0)
                                          for r in cand["regions"]}}
                cand["status"] = "rejected"
                self._persist_draft_failure(s)
                raise DomainError("VALIDATION_FAILED",
                                  f"候选 {cid} 接缝/跨区路线校验未通过，发布已中止",
                                  422, {"errors": errors})

            rid = "REL-" + self._next(s, "release", "REL").split("-")[1]
            new_gens = {}
            for r in cand["regions"]:
                s["region_gen"][r] = s["region_gen"].get(r, 0) + 1
                new_gens[r] = s["region_gen"][r]
            parent = {r: s["region_heads"].get(r) for r in cand["regions"]}
            release = {
                "release_id": rid,
                "candidate_id": cid,
                "regions": list(cand["regions"]),
                "generations": new_gens,
                "tiles": {tid: h for tid, h in cand["tiles"].items()
                          if s["tiles"][h]["region"] in cand["regions"]},
                "parent_releases": parent,
                "note": cand.get("note", ""),
                "created_at": self.now(),
            }
            s["releases"].append(release)
            for r in cand["regions"]:
                s["region_heads"][r] = rid
            cand["status"] = "published"
            cand["published_as"] = rid
            self._event_locked_run(
                s, "RELEASE_PUBLISHED",
                f"发布 {rid}：" + "，".join(f"{r} 第 {g} 代" for r, g in new_gens.items()),
                rid)
            return copy.deepcopy(release)

        return self.commit(m)

    def _persist_draft_failure(self, s):
        # validation failure itself is an auditable state change (handled by commit)
        s["seq"]["event"] += 1
        s["events"].append({"event_id": s["seq"]["event"], "ts": self.now(),
                            "type": "PUBLISH_REJECTED",
                            "summary": "发布失败：校验未通过，未产生任何新版本",
                            "ref": "publish"})

    def list_releases(self) -> list[dict]:
        return copy.deepcopy(self.state["releases"])

    # -------------------------------------------------- effective version ---
    def _active_patches(self, s: dict) -> list[dict]:
        return sorted(
            (p for p in s["patches"].values() if p["status"] == "active"),
            key=lambda p: (-int(p.get("priority", 0)), p["created_at"], p["patch_id"]))

    def resolve_effective(self, s: Optional[dict] = None) -> dict:
        s = s or self.state
        base = {}
        tiles: dict[str, str] = {}
        for region, rid in sorted(s["region_heads"].items()):
            rel = self._release(s, rid)
            base[region] = {"release_id": rid, "generation": rel["generations"][region]}
            for tid, h in rel["tiles"].items():
                # region heads only contribute tiles of their own region
                # (an old joint release must not override another head)
                if s["tiles"][h]["region"] == region:
                    tiles[tid] = h
        active = self._active_patches(s)
        overlays: dict[str, dict] = {}
        for p in active:
            for tid, lanes in p["scope"]["tiles"].items():
                for lid in lanes:
                    overlays.setdefault(tid, {})[lid] = {
                        "closed": True, "patch_id": p["patch_id"],
                        "reason": p.get("reason", "")}
        gen_part = "/".join(f"{r}={b['generation']}" for r, b in base.items())
        patch_part = ",".join(
            f"{p['patch_id']}[p{p.get('priority', 0)}]" for p in active)
        version = f"{gen_part or 'empty'}" + (f"+{patch_part}" if patch_part else "")
        descriptor = {
            "version": version,
            "descriptor_hash": "",
            "base": base,
            "tiles": tiles,
            "patches": [{"patch_id": p["patch_id"], "priority": p["priority"],
                         "scope": p["scope"], "reason": p.get("reason", ""),
                         "content_hash": p["content_hash"],
                         "activated_at": p.get("activated_at")} for p in active],
            "overlays": overlays,
            "resolved_at": self.now(),
        }
        descriptor["descriptor_hash"] = sha256({k: v for k, v in descriptor.items()
                                                if k != "descriptor_hash"})
        return descriptor

    # ------------------------------------------------------- patches --------
    def create_patch(self, body: dict) -> dict:
        scope_tiles = body.get("scope", {}).get("tiles")
        if not scope_tiles:
            raise DomainError("BAD_REQUEST", "scope.tiles 必须指定 {tile_id: [lane_id]}", 400)
        priority = int(body.get("priority", 100))

        def m(s):
            eff = self.resolve_effective(s)
            # compatibility: every target tile/lane must exist in the pinned base
            for tid, lanes in scope_tiles.items():
                if tid not in eff["tiles"]:
                    raise DomainError("PATCH_INCOMPATIBLE",
                                      f"补丁目标瓦片 {tid} 不在当前基础版本中", 422)
                tile = s["tiles"][eff["tiles"][tid]]
                lane_ids = {ln["lane_id"] for ln in tile["lanes"]}
                for lid in lanes:
                    if lid not in lane_ids:
                        raise DomainError("PATCH_INCOMPATIBLE",
                                          f"车道 {tid}/{lid} 在基础版本中不存在；"
                                          f"补丁不允许引入或删除拓扑", 422)
                    if eff["overlays"].get(tid, {}).get(lid, {}).get("closed"):
                        raise DomainError("PATCH_ALREADY_CLOSED",
                                          f"{tid}/{lid} 已被活动补丁封闭", 409)
            pid = "P-" + self._next(s, "patch", "P").split("-")[1]
            content = {"kind": "closure", "scope": {"tiles": scope_tiles},
                       "reason": body.get("reason", "emergency"),
                       "priority": priority,
                       "transition": body.get("transition", {"safe_state": "complete_current_lane"})}
            patch = {
                "patch_id": pid,
                "status": "proposed",
                "priority": priority,
                "kind": "closure",
                "scope": {"tiles": scope_tiles},
                "reason": body.get("reason", ""),
                "base_version": {"base": eff["base"], "tile_hashes": {
                    tid: eff["tiles"][tid] for tid in scope_tiles}},
                "transition": content["transition"],
                "detour": None,
                "compatibility": None,
                "content_hash": sha256(content),
                "created_by": body.get("created_by", "ops"),
                "created_at": self.now(),
                "activated_at": None,
                "revoked_at": None,
            }
            # verify a detour exists in base+overlay-with-closure (safe transition)
            tile_objs = [s["tiles"][h] for h in eff["tiles"].values()]
            ov = copy.deepcopy(eff["overlays"])
            for tid, lanes in scope_tiles.items():
                for lid in lanes:
                    ov.setdefault(tid, {})[lid] = {"closed": True, "patch_id": pid}
            g = build_graph(tile_objs, ov)
            detours = {}
            for tid, lanes in scope_tiles.items():
                tile = s["tiles"][eff["tiles"][tid]]
                for lid in lanes:
                    ln = next(x for x in tile["lanes"] if x["lane_id"] == lid)
                    a, b = node_key(tid, ln["from"]), node_key(tid, ln["to"])
                    via = dijkstra(g, a, b)
                    if via is None:
                        patch["compatibility"] = {
                            "status": "incompatible",
                            "reason": f"封闭 {tid}/{lid} 后无可用绕行，车辆无法安全过渡",
                            "checked_at": self.now()}
                    else:
                        detours[f"{tid}/{lid}"] = via
            if patch["compatibility"] is None:
                patch["detour"] = detours
                patch["compatibility"] = {
                    "status": "compatible",
                    "reason": "基础版本中存在目标车道，且封闭后存在节点间绕行路径",
                    "checked_at": self.now(),
                    "base_descriptor": eff["version"]}
            s["patches"][pid] = patch
            self._event_locked_run(s, "PATCH_PROPOSED",
                                   f"紧急补丁 {pid} 提交（{patch['compatibility']['status']}）",
                                   pid)
            return copy.deepcopy(patch)

        return self.commit(m)

    def activate_patch(self, pid: str) -> dict:
        if pid not in self.state["patches"]:
            raise DomainError("NOT_FOUND", f"补丁 {pid} 不存在", 404)

        def m(s):
            p = s["patches"][pid]
            if p["status"] == "active":
                raise DomainError("PATCH_ACTIVE", "补丁已处于活动状态", 409)
            if p["status"] == "revoked":
                raise DomainError("PATCH_REVOKED", "补丁已撤销，不可重新激活", 409)
            if p.get("compatibility", {}).get("status") != "compatible":
                raise DomainError("PATCH_INCOMPATIBLE",
                                  f"补丁 {pid} 与基础版本不兼容"
                                  f"（{p.get('compatibility', {}).get('reason') or '无安全绕行'}），"
                                  f"不允许生效；车辆无法安全过渡", 422)
            # re-check compatibility against the live base (may have moved)
            eff = self.resolve_effective(s)
            for tid, lanes in p["scope"]["tiles"].items():
                if eff["tiles"].get(tid) != p["base_version"]["tile_hashes"][tid]:
                    raise DomainError("PATCH_BASE_DRIFT",
                                      f"瓦片 {tid} 与补丁固化的基础版本不一致；"
                                      f"请基于当前版本重新生成补丁", 422)
                for lid in lanes:
                    if eff["overlays"].get(tid, {}).get(lid, {}).get("closed"):
                        raise DomainError("PATCH_ALREADY_CLOSED",
                                          f"{tid}/{lid} 已被其他补丁封闭", 409)
            p["status"] = "active"
            p["activated_at"] = self.now()
            self._event_locked_run(
                s, "PATCH_ACTIVATED",
                f"紧急补丁 {pid} 生效：" +
                "；".join(f"{t}/{l}" for t, ls in p["scope"]["tiles"].items() for l in ls) +
                f"（兼容 {eff['version']}，车辆须在节点处过渡）", pid)
            return copy.deepcopy(p)

        return self.commit(m)

    def revoke_patch(self, pid: str, reason: str = "") -> dict:
        if pid not in self.state["patches"]:
            raise DomainError("NOT_FOUND", f"补丁 {pid} 不存在", 404)

        def m(s):
            p = s["patches"][pid]
            if p["status"] != "active":
                raise DomainError("PATCH_NOT_ACTIVE",
                                  f"补丁状态为 {p['status']}，仅活动补丁可撤销", 409)
            p["status"] = "revoked"
            p["revoked_at"] = self.now()
            p["revoke_reason"] = reason
            self._event_locked_run(s, "PATCH_REVOKED",
                                   f"紧急补丁 {pid} 已撤销，车道恢复通行（既有在途快照仍保持其固定版本）",
                                   pid)
            return copy.deepcopy(p)

        return self.commit(m)

    def list_patches(self) -> list[dict]:
        return sorted((copy.deepcopy(p) for p in self.state["patches"].values()),
                      key=lambda p: p["created_at"], reverse=True)

    # ------------------------------------------- regional canary trials ----
    def _parse_iso(self, ts: str) -> datetime:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            raise DomainError("BAD_REQUEST", f"无法解析的 ISO 时间: {ts}", 400)

    def _running_trials(self, s: dict, at: Optional[datetime] = None) -> list[dict]:
        """Batches still taking canary decisions: status running AND inside
        their observation window. Past-window batches stay status=running
        (no state flip) but no longer divert traffic or gather statistics."""
        at = at or self._parse_iso(self.now())
        out = []
        for t in s["trials"].values():
            if t["status"] != "running":
                continue
            if self._parse_iso(t["window_start"]) <= at < self._parse_iso(t["window_end"]):
                out.append(t)
        return out

    def _any_running_anytime(self, s: dict) -> list[dict]:
        """Undecided batches regardless of window (conflict/locking check)."""
        return [t for t in s["trials"].values() if t["status"] == "running"]

    def _tile_region(self, s: dict, tile_id: str) -> Optional[str]:
        # prefer the currently published content of this tile; fall back to
        # any stored version (content never changes its owning region)
        for region, rid in s["region_heads"].items():
            if tile_id in self._release(s, rid)["tiles"]:
                return region
        for t in s["tiles"].values():
            if t["tile_id"] == tile_id:
                return t["region"]
        return None

    @staticmethod
    def _blank_trial_stats() -> dict:
        return {
            "canary": {"requests": 0, "unreachable": 0, "transition_rejected": 0},
            "control": {"requests": 0, "unreachable": 0, "transition_rejected": 0},
        }

    def _active_trials_for_vehicle(self, s: dict, vehicle_id: str,
                                   at: Optional[datetime] = None) -> list[dict]:
        return [t for t in self._running_trials(s, at)
                if vehicle_id in t["vehicle_allowlist"]]

    def _resolve_route_effective(self, s: dict, vehicle_id: str) -> dict:
        """Effective descriptor for a vehicle route request. Vehicles on the
        allowlist of an in-window trial get that candidate's tiles overriding
        the published heads for the trial's regions; everyone else resolves
        the normal published content. Overlays (active patches) still apply."""
        eff = self.resolve_effective(s)
        trials = self._active_trials_for_vehicle(s, vehicle_id)
        if not trials:
            return eff
        tiles = dict(eff["tiles"])
        trial_descs = []
        seen_regions: set[str] = set()
        for t in trials:
            cand = s["candidates"][t["candidate_id"]]
            for tid, h in cand["tiles"].items():
                if h not in s["tiles"]:
                    raise DomainError("TILE_MISSING",
                                      f"试运行 {t['trial_id']} 的瓦片 {tid} 内容缺失", 409)
                tiles[tid] = h
            for r in t["regions"]:
                if r in seen_regions:
                    continue
                seen_regions.add(r)
                trial_descs.append({"region": r, "trial_id": t["trial_id"],
                                    "candidate_id": t["candidate_id"],
                                    "baseline_generation": t["baseline_generations"][r],
                                    "vehicle_allowlist": list(t["vehicle_allowlist"]),
                                    "window_start": t["window_start"],
                                    "window_end": t["window_end"]})
        descriptor = copy.deepcopy(eff)
        descriptor["tiles"] = tiles
        descriptor["trials"] = trial_descs
        descriptor["resolved_at"] = self.now()
        tags = ",".join(
            f"{d['region']}@canary:{d['candidate_id']}({d['trial_id']})"
            for d in trial_descs)
        descriptor["version"] = f"{eff['version']}|{tags}"
        descriptor["descriptor_hash"] = sha256(
            {k: v for k, v in descriptor.items() if k != "descriptor_hash"})
        return descriptor

    def _relevant_running_trials(self, s: dict, tile_ids: set[str],
                                 at: Optional[datetime] = None) -> list[dict]:
        """In-window trials whose acted regions touch the route's endpoint
        tiles — the only batches to which this request's stats belong."""
        regions = {reg for tid in tile_ids
                   if (reg := self._tile_region(s, tid)) is not None}
        return [t for t in self._running_trials(s, at)
                if regions.intersection(t["regions"])]

    def _bump_route_stats(self, s: dict, vehicle_id: str, start: str, goal: str,
                          unreachable: bool) -> None:
        """Attribute the request (and any unreachable outcome) to every
        in-window relevant trial, in the vehicle's group: canary for
        allowlisted vehicles, control otherwise. Durable via outer commit."""
        endpoint_tiles = {start.split("/", 1)[0], goal.split("/", 1)[0]}
        for t in self._relevant_running_trials(s, endpoint_tiles):
            group = "canary" if vehicle_id in t["vehicle_allowlist"] else "control"
            t["stats"][group]["requests"] += 1
            if unreachable:
                t["stats"][group]["unreachable"] += 1

    def _bump_transition_reject_stats(self, s: dict, snap: dict) -> None:
        """A safe-transition attempt refused while the vehicle is on-lane."""
        endpoint_tiles = {snap["from_node"].split("/", 1)[0],
                          snap["to_node"].split("/", 1)[0]}
        canary_ids = {d.get("trial_id")
                      for d in snap["version"].get("trials", [])
                      if d.get("trial_id")}
        for t in self._relevant_running_trials(s, endpoint_tiles):
            group = "canary" if t["trial_id"] in canary_ids else "control"
            t["stats"][group]["transition_rejected"] += 1
            t["stats"][group]["requests"] += 1

    # --------------------------------------- trial batch lifecycle ----------
    def create_trial(self, body: dict) -> dict:
        cid = body.get("candidate_id")
        regions = body.get("regions")
        vehicles = body.get("vehicles") or body.get("vehicle_allowlist")
        if not cid or not regions or not vehicles:
            raise DomainError("BAD_REQUEST",
                              "candidate_id / regions / vehicles(车辆名单) 必填", 400)
        if not isinstance(regions, list) or not isinstance(vehicles, list):
            raise DomainError("BAD_REQUEST", "regions 与 vehicles 必须为数组", 400)
        regions = list(regions)
        vehicles = sorted(set(vehicles))
        if not vehicles:
            raise DomainError("BAD_REQUEST", "车辆名单不能为空", 400)

        # observation window: explicit {window_start, window_end} or
        # {start_at} + window_minutes; default starts now, lasts 60 minutes
        def m(s):
            cand = s["candidates"].get(cid)
            if cand is None:
                raise DomainError("NOT_FOUND", f"候选 {cid} 不存在", 404)
            if cand["status"] not in ("submitted", "published"):
                raise DomainError("CANDIDATE_NOT_VALIDATED",
                                  f"候选 {cid} 状态为 {cand['status']}，"
                                  f"只有校验通过的候选才能创建试运行批次", 422)
            if not set(regions).issubset(set(cand["regions"])):
                raise DomainError("TRIAL_REGION_NOT_IN_CANDIDATE",
                                  f"试运行作用区域 {regions} 不是候选区域 "
                                  f"{cand['regions']} 的子集", 422,
                                  {"regions": regions, "candidate_regions": cand["regions"]})
            for r in regions:
                base_gen = cand["based_on_generations"].get(r, 0)
                cur_gen = s["region_gen"].get(r, 0)
                if base_gen != cur_gen:
                    raise DomainError("REGION_GENERATION_CONFLICT",
                                      f"区域 {r} 已从第 {base_gen} 代推进到第 {cur_gen} 代，"
                                      f"请基于新基线重新校验候选后再发起试运行", 409,
                                      {"region": r, "baseline": base_gen,
                                       "current": cur_gen})
            for t in self._any_running_anytime(s):
                clash = sorted(set(regions) & set(t["regions"]))
                if clash:
                    raise DomainError(
                        "TRIAL_REGION_BUSY",
                        f"区域 {clash} 已存在进行中批次 {t['trial_id']}"
                        f"（候选 {t['candidate_id']}，状态 {t['status']}）；"
                        f"请先晋级或撤回该批次，再发起新的试运行", 409,
                        {"regions": clash, "existing_trial": t["trial_id"]})

            now_dt = self._parse_iso(self.now())
            if body.get("window_start"):
                win_start = self._parse_iso(body["window_start"])
            elif body.get("start_at"):
                win_start = self._parse_iso(body["start_at"])
            else:
                win_start = now_dt
            if body.get("window_end"):
                win_end = self._parse_iso(body["window_end"])
            elif body.get("end_at"):
                win_end = self._parse_iso(body["end_at"])
            else:
                minutes = float(body.get("window_minutes", 60))
                if minutes <= 0:
                    raise DomainError("BAD_REQUEST",
                                      "观察窗口时长 window_minutes 必须为正数", 400)
                win_end = win_start + timedelta(minutes=minutes)
            if win_end <= win_start:
                raise DomainError("BAD_REQUEST", "观察窗口结束时间必须晚于开始时间", 400)

            tid = "TB-" + self._next(s, "trial", "TB").split("-")[1]
            thresholds = {
                "max_unreachable_rate": float(body.get("max_unreachable_rate", 0.1)),
                "max_transition_reject_rate": float(
                    body.get("max_transition_reject_rate", 0.2)),
                "min_requests": int(body.get("min_requests", 0)),
            }
            trial = {
                "trial_id": tid,
                "candidate_id": cid,
                "regions": list(regions),
                "vehicle_allowlist": vehicles,
                "window_start": win_start.replace(microsecond=0).isoformat(),
                "window_end": win_end.replace(microsecond=0).isoformat(),
                "thresholds": thresholds,
                "stats": self._blank_trial_stats(),
                "status": "running",
                "note": body.get("note", ""),
                "created_by": body.get("created_by", "ops"),
                "created_at": self.now(),
                "baseline_generations": {r: s["region_gen"].get(r, 0) for r in regions},
                "promoted_at": None,
                "withdrawn_at": None,
                "result": None,
            }
            s["trials"][tid] = trial
            self._event_locked_run(
                s, "TRIAL_STARTED",
                f"试运行批次 {tid} 启动：候选 {cid}，区域 {','.join(regions)}，"
                f"名单车辆 {len(vehicles)} 台，窗口至 {trial['window_end']}；"
                f"名单内读取待晋级内容，名单外继续读取当前内容", tid)
            return self._trial_view(s, trial)

        return self.commit(m)

    def _trial_view(self, s: dict, t: dict, idempotent: bool = False) -> dict:
        view = copy.deepcopy(t)
        now_dt = self._parse_iso(self.now())
        if t["status"] == "running":
            if now_dt < self._parse_iso(t["window_start"]):
                view["phase"] = "scheduled"
            elif now_dt >= self._parse_iso(t["window_end"]):
                view["phase"] = "window_closed"
            else:
                view["phase"] = "observing"
        else:
            view["phase"] = "finished"
        cand = s["candidates"].get(t["candidate_id"])
        view["candidate_note"] = cand.get("note", "") if cand else ""
        for grp in ("canary", "control"):
            st = view["stats"][grp]
            req = st["requests"]
            st["unreachable_rate"] = (st["unreachable"] / req) if req else 0.0
            st["transition_reject_rate"] = (st["transition_rejected"] / req) if req else 0.0
        view["gate"] = self._gate_evaluation(t)
        view["idempotent"] = idempotent
        return view

    def _gate_evaluation(self, t: dict) -> dict:
        th = t["thresholds"]
        c = t["stats"]["canary"]
        req = c["requests"]
        u_rate = (c["unreachable"] / req) if req else 0.0
        r_rate = (c["transition_rejected"] / req) if req else 0.0
        breaches = []
        if req < th["min_requests"]:
            breaches.append({
                "metric": "requests", "observed": req,
                "threshold": th["min_requests"],
                "message": f"晋级组样本不足：{req} < 最少 {th['min_requests']} 次请求"})
        if u_rate > th["max_unreachable_rate"]:
            breaches.append({
                "metric": "unreachable_rate", "observed": round(u_rate, 4),
                "threshold": th["max_unreachable_rate"],
                "message": f"晋级组不可达率 {u_rate:.2%} 超过阈值 "
                           f"{th['max_unreachable_rate']:.2%}"})
        if r_rate > th["max_transition_reject_rate"]:
            breaches.append({
                "metric": "transition_reject_rate", "observed": round(r_rate, 4),
                "threshold": th["max_transition_reject_rate"],
                "message": f"晋级组安全过渡拒绝率 {r_rate:.2%} 超过阈值 "
                           f"{th['max_transition_reject_rate']:.2%}"})
        return {"can_promote": not breaches, "breaches": breaches,
                "canary_requests": req,
                "unreachable_rate": round(u_rate, 4),
                "transition_reject_rate": round(r_rate, 4)}

    def get_trial(self, tid: str) -> dict:
        with self.lock:
            t = self.state["trials"].get(tid)
            if not t:
                raise DomainError("NOT_FOUND", f"试运行批次 {tid} 不存在", 404)
            return self._trial_view(self.state, t)

    def list_trials(self) -> list[dict]:
        with self.lock:
            out = [self._trial_view(self.state, t)
                   for t in self.state["trials"].values()]
            return sorted(out, key=lambda t: t["created_at"], reverse=True)

    def promote_trial(self, tid: str, body: Optional[dict] = None) -> dict:
        body = body or {}
        if tid not in self.state["trials"]:
            raise DomainError("NOT_FOUND", f"试运行批次 {tid} 不存在", 404)

        def m(s):
            t = s["trials"][tid]
            # verdict-once: a terminal promotion/withdrawal is replayed forever
            if t["status"] == "promoted":
                return self._trial_view(s, t, idempotent=True)
            if t["status"] == "withdrawn":
                raise DomainError("TRIAL_ALREADY_WITHDRAWN",
                                  f"批次 {tid} 已撤回；不能再晋级。首次撤回结果保持不变",
                                  409, {"first_result": t["result"]})

            cand = s["candidates"].get(t["candidate_id"])
            if cand is None:
                raise DomainError("NOT_FOUND",
                                  f"候选 {t['candidate_id']} 已不存在", 404)
            for r in t["regions"]:
                base_gen = t["baseline_generations"].get(r, 0)
                cur_gen = s["region_gen"].get(r, 0)
                if base_gen != cur_gen:
                    raise DomainError("REGION_GENERATION_CONFLICT",
                                      f"区域 {r} 在试运行期间已从第 {base_gen} 代推进到第 "
                                      f"{cur_gen} 代；为避免倒换代际，禁止晋级，请撤回批次",
                                      409, {"region": r, "baseline": base_gen,
                                            "current": cur_gen})

            if not body.get("force"):
                gate = self._gate_evaluation(t)
                if gate["breaches"]:
                    raise DomainError("TRIAL_GATE_BREACHED",
                                      f"批次 {tid} 未达到晋级阈值（共 "
                                      f"{len(gate['breaches'])} 项）；"
                                      f"可继续观察、调整阈值或撤回批次", 422,
                                      {"breaches": gate["breaches"],
                                       "canary_stats": t["stats"]["canary"]})

            # build the exact working set the canary group saw: published
            # heads everywhere, candidate tiles overriding trial regions,
            # then re-run the full seam/corridor gate atomically
            cand_tiles: dict[str, dict] = {}
            regions = set(t["regions"])
            for region, rid in s["region_heads"].items():
                if region in regions:
                    continue
                rel = self._release(s, rid)
                for ttid, h in rel["tiles"].items():
                    if s["tiles"][h]["region"] == region:
                        cand_tiles[ttid] = s["tiles"][h]
            for ttid, h in cand["tiles"].items():
                if h not in s["tiles"]:
                    raise DomainError("TILE_MISSING",
                                      f"候选瓦片 {ttid} 内容 {short(h)} 缺失", 409)
                if s["tiles"][h]["region"] in regions:
                    cand_tiles[ttid] = s["tiles"][h]
            errors, _ = self.validate_tile_map(s, cand_tiles, regions)
            if errors:
                self._event_locked_run(
                    s, "TRIAL_PROMOTE_REJECTED",
                    f"试运行 {tid} 晋级失败：接缝/跨区走廊校验未通过，"
                    f"区域代际与在途快照均未改动", tid)
                raise DomainError("VALIDATION_FAILED",
                                  f"试运行 {tid} 晋级前复核未通过；未推进任何区域代际",
                                  422, {"errors": errors})

            rid = "REL-" + self._next(s, "release", "REL").split("-")[1]
            new_gens = {}
            for r in t["regions"]:
                s["region_gen"][r] = s["region_gen"].get(r, 0) + 1
                new_gens[r] = s["region_gen"][r]
            parent = {r: s["region_heads"].get(r) for r in t["regions"]}
            release = {
                "release_id": rid,
                "candidate_id": t["candidate_id"],
                "regions": list(t["regions"]),
                "generations": new_gens,
                "tiles": {ttid: h for ttid, h in cand["tiles"].items()
                          if s["tiles"][h]["region"] in regions},
                "parent_releases": parent,
                "note": f"由试运行批次 {tid} 晋级（观察窗口内统计达标）"
                        + (f"：{t['note']}" if t.get("note") else ""),
                "created_at": self.now(),
                "promoted_from_trial": tid,
            }
            s["releases"].append(release)
            for r in t["regions"]:
                s["region_heads"][r] = rid
            t["status"] = "promoted"
            t["promoted_at"] = self.now()
            t["result"] = {"outcome": "promoted", "release_id": rid,
                           "generations": new_gens,
                           "stats": copy.deepcopy(t["stats"]),
                           "at": t["promoted_at"]}
            self._event_locked_run(
                s, "TRIAL_PROMOTED",
                f"试运行批次 {tid} 晋级为 {rid}：" +
                "，".join(f"{r} 第 {g} 代" for r, g in new_gens.items()) +
                f"；试运行期间固定的快照永不改写，名单外新行程一并读取新内容", rid)
            return self._trial_view(s, t)

        return self.commit(m)

    def withdraw_trial(self, tid: str, body: Optional[dict] = None) -> dict:
        body = body or {}
        if tid not in self.state["trials"]:
            raise DomainError("NOT_FOUND", f"试运行批次 {tid} 不存在", 404)

        def m(s):
            t = s["trials"][tid]
            # verdict-once for withdrawal as well
            if t["status"] == "withdrawn":
                return self._trial_view(s, t, idempotent=True)
            if t["status"] == "promoted":
                raise DomainError("TRIAL_ALREADY_PROMOTED",
                                  f"批次 {tid} 已晋级为 {t['result'].get('release_id')}；"
                                  f"晋级不可撤回（首次晋级结果保持不变），区域代际不倒退",
                                  409, {"first_result": t["result"]})
            t["status"] = "withdrawn"
            t["withdrawn_at"] = self.now()
            t["result"] = {"outcome": "withdrawn",
                           "reason": body.get("reason", ""),
                           "stats": copy.deepcopy(t["stats"]),
                           "at": t["withdrawn_at"]}
            self._event_locked_run(
                s, "TRIAL_WITHDRAWN",
                f"试运行批次 {tid} 已撤回：新行程统一恢复读取当前已发布内容；"
                f"已发出的试运行快照保持其固定内容不变，区域代际无变化", tid)
            return self._trial_view(s, t)

        return self.commit(m)

    # -------------------------------------------------- routes/snapshots ----
    def create_route(self, body: dict) -> dict:
        vehicle_id = body.get("vehicle_id", "VH-01")
        start, goal = body.get("from_node"), body.get("to_node")
        if not start or not goal:
            raise DomainError("BAD_REQUEST", "from_node / to_node 必填，格式 tile/node", 400)

        deferred: dict = {}

        def m(s):
            eff = self._resolve_route_effective(s, vehicle_id)
            tile_objs = [s["tiles"][h] for h in eff["tiles"].values()]
            g = build_graph(tile_objs, eff["overlays"])
            steps = dijkstra(g, start, goal)
            # statistics are an auditable outcome and must survive even the
            # unreachable refusal — persist them first, raise afterwards
            self._bump_route_stats(s, vehicle_id, start, goal, steps is None)
            if steps is None:
                deferred["err"] = DomainError(
                    "ROUTE_IMPOSSIBLE",
                    f"当前有效版本 {eff['version']} 下 "
                    f"{start} -> {goal} 无可达路径（可能因封路或试运行内容）", 422,
                    {"version": eff["version"]})
                return None
            used_tiles = self._tiles_used_by_steps(s, steps, eff)
            sid = "S-" + self._next(s, "snapshot", "S").split("-")[1]
            trial_descs = copy.deepcopy(eff.get("trials", []))
            snapshot = {
                "snapshot_id": sid,
                "vehicle_id": vehicle_id,
                "from_node": start,
                "to_node": goal,
                "status": "active",
                "created_at": self.now(),
                "canary": bool(trial_descs),
                "canary_trials": [d["trial_id"] for d in trial_descs],
                "version": {
                    "version": eff["version"],
                    "descriptor_hash": eff["descriptor_hash"],
                    "base": eff["base"],
                    "patches": eff["patches"],
                    "trials": trial_descs,
                },
                "tiles": used_tiles,          # tile_id -> hash (the fixed set)
                "overlays": {t: ov for t, ov in eff["overlays"].items() if t in used_tiles},
                "route": steps,
                "progress": {"step": 0, "state": "at_node"},
                "parent_snapshot_id": None,
                "children": [],
                "completed_at": None,
            }
            s["snapshots"][sid] = snapshot
            s["vehicles"].setdefault(vehicle_id, {"snapshots": [], "last_event_id": 0})
            s["vehicles"][vehicle_id]["snapshots"].append(sid)
            tag = "（车辆分组试运行：待晋级内容）" if trial_descs else ""
            self._event_locked_run(
                s, "SNAPSHOT_PINNED",
                f"车辆 {vehicle_id} 固定快照 {sid}（版本 {eff['version']}{tag}，"
                f"{len(used_tiles)} 瓦片，{len(steps)} 步）", sid)
            return self._snapshot_view(s, snapshot)

        out = self.commit(m)
        if "err" in deferred:
            raise deferred["err"]
        return out

    def _tiles_used_by_steps(self, s, steps, eff) -> dict:
        used: dict[str, str] = {}
        for st in steps:
            if st["kind"] == "lane":
                used[st["tile"]] = eff["tiles"][st["tile"]]
            else:
                used[st["from_tile"]] = eff["tiles"][st["from_tile"]]
                used[st["to_tile"]] = eff["tiles"][st["to_tile"]]
        return used

    def _snapshot_view(self, s: dict, snap: dict) -> dict:
        view = copy.deepcopy(snap)
        view["tile_contents"] = {tid: s["tiles"][h] for tid, h in snap["tiles"].items()}
        view["advisory"] = self._advisory(s, snap)
        return view

    def get_snapshot(self, sid: str) -> dict:
        with self.lock:
            snap = self.state["snapshots"].get(sid)
            if not snap:
                raise DomainError("NOT_FOUND", f"快照 {sid} 不存在", 404)
            return self._snapshot_view(self.state, snap)

    def update_progress(self, sid: str, body: dict) -> dict:
        def m(s):
            snap = s["snapshots"].get(sid)
            if not snap:
                raise DomainError("NOT_FOUND", f"快照 {sid} 不存在", 404)
            if snap["status"] != "active":
                raise DomainError("SNAPSHOT_CLOSED",
                                  f"快照状态为 {snap['status']}，不再接受进度", 409)
            step = int(body.get("step", snap["progress"]["step"]))
            state = body.get("state", snap["progress"]["state"])
            if state not in ("at_node", "on_lane"):
                raise DomainError("BAD_REQUEST", "state 仅支持 at_node / on_lane", 400)
            if not (0 <= step <= len(snap["route"])):
                raise DomainError("BAD_REQUEST", "step 超出路线范围", 400)
            snap["progress"] = {"step": step, "state": state, "ts": self.now()}
            return {"progress": copy.deepcopy(snap["progress"]),
                    "advisory": copy.deepcopy(self._advisory(s, snap))}

        return self.commit(m)

    def _advisory(self, s: dict, snap: dict) -> dict:
        """What a pinned trip must know without changing its topology."""
        notes = []
        closures_ahead = []
        current = self.resolve_effective(s)
        i = snap["progress"]["step"]
        on_lane = snap["progress"]["state"] == "on_lane"

        # newer generations: informational only, never spliced into this trip
        for region, b in snap["version"]["base"].items():
            head = current["base"].get(region)
            if head and head["generation"] > b["generation"]:
                notes.append({
                    "level": "new_generation",
                    "message": f"区域 {region} 已发布第 {head['generation']} 代"
                               f"（本快照固定第 {b['generation']} 代）；新拓扑仅在下次路线生效",
                    "region": region, "pinned_generation": b["generation"],
                    "current_generation": head["generation"]})

        pinned_ids = {p["patch_id"] for p in snap["version"]["patches"]}
        for p in self._active_patches(s):
            if p["patch_id"] in pinned_ids:
                continue
            for j, st in enumerate(snap["route"]):
                if st["kind"] != "lane":
                    continue
                if p["scope"]["tiles"].get(st["tile"]) and st["lane"] in \
                        p["scope"]["tiles"][st["tile"]]:
                    safe = not (on_lane and j == i)
                    closures_ahead.append({
                        "level": "reroute" if (j > i or (not on_lane and j >= i)) else "on_segment",
                        "patch_id": p["patch_id"], "reason": p.get("reason", ""),
                        "step_index": j,
                        "tile": st["tile"], "lane": st["lane"],
                        "must_finish_current_lane_first": on_lane,
                        "safe_to_switch_now": (not on_lane) and j >= i,
                        "message": ("封路位于剩余路线第 " if j != i else "封路位于当前车道 ")
                                   + f"{j} 步（{st['tile']}/{st['lane']}）"
                                   + ("；请完成当前车道后在节点处采用绕行" if on_lane
                                      else "；可在当前节点安全采用绕行快照")})

        # pinned-but-now-revoked patches: informational
        for pid in pinned_ids:
            if s["patches"][pid]["status"] == "revoked":
                notes.append({"level": "patch_revoked", "patch_id": pid,
                              "message": f"本快照固定的补丁 {pid} 已被撤销；"
                                         f"历史复算仍按封路执行，新路线不再封闭"})

        # canary (pre-promotion) snapshots: the trial outcome never rewrites
        # the pinned content, it only changes what fresh routes resolve to
        for d in snap["version"].get("trials", []):
            tbid = d.get("trial_id")
            t = s["trials"].get(tbid)
            if not t:
                continue
            if t["status"] == "promoted":
                notes.append({
                    "level": "trial_promoted", "trial_id": tbid,
                    "release_id": (t.get("result") or {}).get("release_id"),
                    "message": f"本快照固定试运行批次 {tbid} 的待晋级内容；该批次已晋级为 "
                               f"{(t.get('result') or {}).get('release_id')}，"
                               f"本快照内容保持不变，新行程直接读取已晋级版本"})
            elif t["status"] == "withdrawn":
                notes.append({
                    "level": "trial_withdrawn", "trial_id": tbid,
                    "message": f"试运行批次 {tbid} 已撤回；本快照固定的待晋级内容与瓦片集合"
                               f"保持不变（撤回不改写行程快照、不倒退区域代际），"
                               f"新行程恢复读取当前已发布内容"})
            elif (self.now() >= t["window_end"]):
                notes.append({
                    "level": "trial_window_closed", "trial_id": tbid,
                    "message": f"试运行批次 {tbid} 观察窗口已结束，等待运维晋级或撤回；"
                               f"本快照仍固定其待晋级内容，新行程不再分流"})

        detour = None
        if closures_ahead:
            # plan detour from the current safe node to the original destination
            if on_lane and i < len(snap["route"]):
                from_node = node_key(snap["route"][i]["to_tile"], snap["route"][i]["to_node"])
            elif i < len(snap["route"]):
                from_node = node_key(snap["route"][i]["from_tile"], snap["route"][i]["from_node"])
            else:
                from_node = snap["to_node"]
            tile_objs = [s["tiles"][h] for h in current["tiles"].values()]
            g = build_graph(tile_objs, current["overlays"])
            detour = dijkstra(g, from_node, snap["to_node"])
        return {"closures_ahead": closures_ahead, "notes": notes,
                "detour_preview": detour,
                "current_version": current["version"]}

    def adopt_reroute(self, sid: str) -> dict:
        deferred: dict = {}

        def m(s):
            old = s["snapshots"].get(sid)
            if not old:
                raise DomainError("NOT_FOUND", f"快照 {sid} 不存在", 404)
            if old["status"] != "active":
                raise DomainError("SNAPSHOT_CLOSED", "快照已关闭，无法过渡", 409)
            adv = self._advisory(s, old)
            if not adv["closures_ahead"]:
                raise DomainError("NO_ADVISORY", "当前无封路预警，无需过渡", 409)
            if old["progress"]["state"] != "at_node":
                # the safe-transition refusal itself is an observed signal:
                # record it under the vehicle's trial group(s), durably,
                # then refuse with the same answer
                self._bump_transition_reject_stats(s, old)
                deferred["err"] = DomainError(
                    "NOT_AT_NODE",
                    "车辆正在车道上：禁止拼接拓扑。请先完成当前车道、到达节点后再过渡",
                    409, {"must_finish_current_lane_first": True})
                return None
            i = old["progress"]["step"]
            from_node = node_key(old["route"][i]["from_tile"], old["route"][i]["from_node"])
            eff = self.resolve_effective(s)
            tile_objs = [s["tiles"][h] for h in eff["tiles"].values()]
            g = build_graph(tile_objs, eff["overlays"])
            steps = dijkstra(g, from_node, old["to_node"])
            if steps is None:
                raise DomainError("ROUTE_IMPOSSIBLE", "当前版本下无安全绕行路径", 422)
            used = self._tiles_used_by_steps(s, steps, eff)
            new_id = "S-" + self._next(s, "snapshot", "S").split("-")[1]
            child = {
                "snapshot_id": new_id, "vehicle_id": old["vehicle_id"],
                "from_node": from_node, "to_node": old["to_node"], "status": "active",
                "created_at": self.now(),
                "canary": False, "canary_trials": [],
                "version": {"version": eff["version"],
                            "descriptor_hash": eff["descriptor_hash"],
                            "base": eff["base"], "patches": eff["patches"],
                            "trials": []},
                "tiles": used,
                "overlays": {t: ov for t, ov in eff["overlays"].items() if t in used},
                "route": steps, "progress": {"step": 0, "state": "at_node"},
                "parent_snapshot_id": sid, "children": [], "completed_at": None,
                "adopted_reason": "emergency_patch_reroute",
            }
            old["status"] = "superseded"
            old["children"].append(new_id)
            s["snapshots"][new_id] = child
            s["vehicles"].setdefault(old["vehicle_id"], {"snapshots": [], "last_event_id": 0})
            s["vehicles"][old["vehicle_id"]]["snapshots"].append(new_id)
            self._event_locked_run(
                s, "SNAPSHOT_SUPERSEDED",
                f"车辆 {old['vehicle_id']} 在节点处由 {sid} 安全过渡至 {new_id}"
                f"（新版本 {eff['version']}，全程未拼接拓扑）", new_id)
            return self._snapshot_view(s, child)

        out = self.commit(m)
        if "err" in deferred:
            raise deferred["err"]
        return out

    def complete_snapshot(self, sid: str) -> dict:
        def m(s):
            snap = s["snapshots"].get(sid)
            if not snap:
                raise DomainError("NOT_FOUND", f"快照 {sid} 不存在", 404)
            snap["status"] = "completed"
            snap["completed_at"] = self.now()
            snap["progress"] = {"step": len(snap["route"]), "state": "at_node"}
            self._event_locked_run(s, "TRIP_COMPLETED",
                                   f"快照 {sid} 行程完成（版本 {snap['version']['version']}）",
                                   sid)
            return {"snapshot_id": sid, "status": "completed"}

        return self.commit(m)

    def vehicle_snapshots(self, vid: str) -> dict:
        with self.lock:
            v = self.state["vehicles"].get(vid)
            if not v:
                raise DomainError("NOT_FOUND", f"车辆 {vid} 不存在", 404)
            return {"vehicle_id": vid,
                    "snapshots": [self._snapshot_summary(self.state, sid)
                                  for sid in v["snapshots"]]}

    def _snapshot_summary(self, s: dict, sid: str) -> dict:
        snap = s["snapshots"][sid]
        return {"snapshot_id": sid, "status": snap["status"],
                "version": snap["version"]["version"],
                "canary": snap.get("canary", False),
                "canary_trials": snap.get("canary_trials", []),
                "from_node": snap["from_node"], "to_node": snap["to_node"],
                "created_at": snap["created_at"],
                "progress": snap["progress"],
                "parent_snapshot_id": snap.get("parent_snapshot_id"),
                "children": snap.get("children", []),
                "tiles": len(snap["tiles"]), "steps": len(snap["route"])}

    def vehicle_sync(self, vid: str, last_event_id: int) -> dict:
        """Offline catch-up: one explainable answer of everything missed."""
        with self.lock:
            s = self.state
            v = s["vehicles"].get(vid)
            if not v:
                raise DomainError("NOT_FOUND", f"车辆 {vid} 不存在", 404)
            missed = [copy.deepcopy(e) for e in s["events"] if e["event_id"] > last_event_id]
            current = self.resolve_effective(s)
            active_sid = next((sid for sid in reversed(v["snapshots"])
                               if s["snapshots"][sid]["status"] == "active"), None)
            payload = {"vehicle_id": vid, "replayed_from_event": last_event_id,
                       "missed_events": missed,
                       "current_version": {k: current[k] for k in
                                           ("version", "descriptor_hash", "base", "patches")},
                       "active_snapshot": None}
            if active_sid:
                snap = s["snapshots"][active_sid]
                payload["active_snapshot"] = self._snapshot_summary(s, active_sid)
                payload["active_snapshot"]["advisory"] = self._advisory(s, snap)
            v["last_event_id"] = s["seq"]["event"]
            self._persist_vehicle_checkpoint(vid, v["last_event_id"])
            return payload

    def _persist_vehicle_checkpoint(self, vid: str, eid: int):
        with self.lock:
            def m(s):
                s["vehicles"][vid]["last_event_id"] = eid
                return None
            self.commit(m)

    # ----------------------------------------------------------- views ------
    def describe_view(self, mode: str = "published", ref: Optional[str] = None) -> dict:
        """Geometry + seams + corridor paths for the web map."""
        with self.lock:
            s = self.state
            errors: list[dict] = []
            if mode == "candidate":
                cand = s["candidates"].get(ref)
                if not cand:
                    raise DomainError("NOT_FOUND", f"候选 {ref} 不存在", 404)
                tile_map = self._working_tile_map(s, cand)
                errors, _ = self.validate_tile_map(s, tile_map, set(cand["regions"]))
                overlays = {}
                version_label = f"{ref}（{'校验通过' if not errors else str(len(errors)) + ' 个错误'}）"
            elif mode == "snapshot":
                snap = s["snapshots"].get(ref)
                if not snap:
                    raise DomainError("NOT_FOUND", f"快照 {ref} 不存在", 404)
                tile_map = {tid: s["tiles"][h] for tid, h in snap["tiles"].items()}
                overlays = snap["overlays"]
                version_label = snap["version"]["version"]
            else:
                eff = self.resolve_effective(s)
                tile_map = {tid: s["tiles"][h] for tid, h in eff["tiles"].items()}
                overlays = eff["overlays"]
                version_label = eff["version"]
            return self._render_view(s, tile_map, overlays, errors, mode, ref, version_label)

    def _render_view(self, s, tile_map, overlays, errors, mode, ref, version_label) -> dict:
        tiles_out, nodes_out, lanes_out, seams_out = [], [], [], []
        for tid, t in sorted(tile_map.items()):
            bounds = t.get("bounds")
            if not bounds:
                xs = [n["x"] for n in t["nodes"]]
                ys = [n["y"] for n in t["nodes"]]
                bounds = [min(xs), min(ys), max(xs), max(ys)]
            tiles_out.append({"tile_id": tid, "region": t["region"], "bounds": bounds,
                              "version_tag": t.get("version_tag", "")})
            for n in t["nodes"]:
                ports = {p["node"]: p for p in t["ports"]}
                p = ports.get(n["node_id"])
                nodes_out.append({"id": node_key(tid, n["node_id"]), "tile": tid,
                                  "node": n["node_id"], "x": n["x"], "y": n["y"],
                                  "external": bool(n.get("external") or (p and p.get("external"))),
                                  "port": p["port_id"] if p else None})
            for ln in t["lanes"]:
                nmap = {n["node_id"]: n for n in t["nodes"]}
                ov = overlays.get(tid, {}).get(ln["lane_id"])
                lanes_out.append({"id": f"{tid}/{ln['lane_id']}", "tile": tid,
                                  "lane": ln["lane_id"],
                                  "from": node_key(tid, ln["from"]),
                                  "to": node_key(tid, ln["to"]),
                                  "x1": nmap[ln["from"]]["x"], "y1": nmap[ln["from"]]["y"],
                                  "x2": nmap[ln["to"]]["x"], "y2": nmap[ln["to"]]["y"],
                                  "speed": ln.get("speed"), "two_way": ln.get("two_way", True),
                                  "closed": bool(ov and ov.get("closed")),
                                  "closed_by": ov.get("patch_id") if ov else None})
        seen = set()
        for tid, t in sorted(tile_map.items()):
            for p in t["ports"]:
                peer = p.get("peer")
                if not peer or peer["tile"] not in tile_map:
                    continue
                key = tuple(sorted([f"{tid}:{p['port_id']}", f"{peer['tile']}:{peer['port']}"]))
                if key in seen:
                    continue
                seen.add(key)
                t2 = tile_map[peer["tile"]]
                p2 = next((q for q in t2["ports"] if q["port_id"] == peer["port"]), None)
                ok = bool(p2 and p2.get("peer", {}).get("tile") == tid and
                          p2.get("peer", {}).get("port") == p["port_id"] and
                          abs(p["x"] - p2["x"]) <= EPS and abs(p["y"] - p2["y"]) <= EPS and
                          p.get("profile") == p2.get("profile"))
                seams_out.append({"a": f"{tid}:{p['port_id']}", "b": f"{peer['tile']}:{peer['port']}",
                                  "x": p["x"], "y": p["y"], "ok": ok})

        # corridor paths on this view
        g = build_graph(list(tile_map.values()), overlays)
        corridors = []
        for cor in s["corridors"]:
            polyline, ok, wps = [], True, cor["waypoints"]
            for a, b in zip(wps, wps[1:]):
                seg = dijkstra(g, a, b) if a in g["nodes"] and b in g["nodes"] else None
                if seg is None:
                    ok = False
                    break
                polyline.append({"start": a, "steps": seg})
            corridors.append({"corridor_id": cor["corridor_id"], "name": cor.get("name", ""),
                              "ok": ok, "segments": polyline})

        route = None
        if mode == "snapshot":
            snap = s["snapshots"][ref]
            route = {"steps": snap["route"], "progress": snap["progress"],
                     "status": snap["status"], "from_node": snap["from_node"],
                     "to_node": snap["to_node"],
                     "parent_snapshot_id": snap.get("parent_snapshot_id")}

        return {"mode": mode, "ref": ref, "version_label": version_label,
                "tiles": tiles_out, "nodes": nodes_out, "lanes": lanes_out,
                "seams": seams_out, "errors": errors,
                "corridors": corridors, "route": route}

    def region_status(self) -> dict:
        with self.lock:
            s = self.state
            eff = self.resolve_effective(s)
            out = []
            for r, rid in sorted(s["region_heads"].items()):
                rel = self._release(s, rid)
                ntiles = sum(1 for tid, h in rel["tiles"].items()
                             if s["tiles"][h]["region"] == r)
                out.append({"region": r, "generation": s["region_gen"][r],
                            "release_id": rid, "tiles": ntiles,
                            "published_at": rel["created_at"],
                            "candidate_id": rel["candidate_id"]})
            return {"regions": out, "current_version": eff["version"],
                    "active_patches": [p["patch_id"] for p in self._active_patches(s)],
                    "trials": [self._trial_view(s, t)
                               for t in self._any_running_anytime(s)]}

    def list_candidates(self) -> list[dict]:
        return sorted((copy.deepcopy(c) for c in self.state["candidates"].values()),
                      key=lambda c: c["created_at"], reverse=True)

    def list_corridors(self) -> list[dict]:
        return copy.deepcopy(self.state["corridors"])

    def list_events(self, limit: int = 60) -> list[dict]:
        return copy.deepcopy(self.state["events"][-limit:][::-1])

    # ------------------------------------------------------------ seed ------
    def seed(self):
        """Initial 2-region world: north(A00,A01) / south(B00,B01), 600-sized."""
        s = self.state

        def tile(tid, region, origin, tag, ports_extra=None, lane_mods=None,
                 speed_override=None):
            ox, oy = origin
            center = (ox + 300, oy + 300)
            P = lambda lx, ly: (ox + lx, oy + ly)
            nodes = [
                {"node_id": "c", "x": center[0], "y": center[1]},
                {"node_id": "w", "x": P(0, 300)[0], "y": P(0, 300)[1]},
                {"node_id": "e", "x": P(600, 300)[0], "y": P(600, 300)[1]},
                {"node_id": "n", "x": P(300, 0)[0], "y": P(300, 0)[1]},
                {"node_id": "s", "x": P(300, 600)[0], "y": P(300, 600)[1]},
            ]
            lanes = [
                {"lane_id": "lcw", "from": "c", "to": "w", "two_way": True, "speed": 60},
                {"lane_id": "lce", "from": "c", "to": "e", "two_way": True, "speed": 60},
                {"lane_id": "lcn", "from": "c", "to": "n", "two_way": True, "speed": 60},
                {"lane_id": "lcs", "from": "c", "to": "s", "two_way": True, "speed": 60},
            ]
            if lane_mods:
                lane_mods(nodes, lanes)
            bounds = [ox, oy, ox + 600, oy + 600]
            return {"tile_id": tid, "region": region, "version_tag": tag,
                    "nodes": nodes, "lanes": lanes, "bounds": bounds,
                    "ports": (ports_extra or [])(P, speed_override)}

        def prof(spd):
            return {"speed": spd, "lanes": 2}

        def ports_a00(P, spd):
            spd = spd or 60
            x, y = P(600, 300)
            xs, ys = P(300, 600)
            return [
                {"port_id": "pw", "node": "w", "side": "west", "x": P(0, 300)[0],
                 "y": 300, "external": True, "peer": None},
                {"port_id": "pn", "node": "n", "side": "north", "x": 300,
                 "y": P(300, 0)[1], "external": True, "peer": None},
                {"port_id": "pe", "node": "e", "side": "east", "x": x, "y": y,
                 "profile": prof(spd), "peer": {"tile": "A01", "port": "pw"}},
                {"port_id": "ps", "node": "s", "side": "south", "x": xs, "y": ys,
                 "profile": prof(spd), "peer": {"tile": "B00", "port": "pn"}},
            ]

        def ports_a01(P, spd):
            return [
                {"port_id": "pw", "node": "w", "side": "west", "x": 600, "y": 300,
                 "profile": prof(60), "peer": {"tile": "A00", "port": "pe"}},
                {"port_id": "pe", "node": "e", "side": "east", "x": 1200, "y": 300,
                 "external": True, "peer": None},
                {"port_id": "pn", "node": "n", "side": "north", "x": 900, "y": 0,
                 "external": True, "peer": None},
                {"port_id": "ps", "node": "s", "side": "south", "x": 900, "y": 600,
                 "profile": prof(60), "peer": {"tile": "B01", "port": "pn"}},
            ]

        def ports_b00(P, spd):
            return [
                {"port_id": "pn", "node": "n", "side": "north", "x": 300, "y": 600,
                 "profile": prof(60), "peer": {"tile": "A00", "port": "ps"}},
                {"port_id": "ps", "node": "s", "side": "south", "x": 300, "y": 1200,
                 "external": True, "peer": None},
                {"port_id": "pw", "node": "w", "side": "west", "x": 0, "y": 900,
                 "external": True, "peer": None},
                {"port_id": "pe", "node": "e", "side": "east", "x": 600, "y": 900,
                 "profile": prof(60), "peer": {"tile": "B01", "port": "pw"}},
            ]

        def ports_b01(P, spd):
            return [
                {"port_id": "pn", "node": "n", "side": "north", "x": 900, "y": 600,
                 "profile": prof(60), "peer": {"tile": "A01", "port": "ps"}},
                {"port_id": "pw", "node": "w", "side": "west", "x": 600, "y": 900,
                 "profile": prof(60), "peer": {"tile": "B00", "port": "pe"}},
                {"port_id": "pe", "node": "e", "side": "east", "x": 1200, "y": 900,
                 "external": True, "peer": None},
                {"port_id": "ps", "node": "s", "side": "south", "x": 900, "y": 1200,
                 "external": True, "peer": None},
            ]

        a00 = tile("A00", "north", (0, 0), "v1", ports_a00)
        a01 = tile("A01", "north", (600, 0), "v1", ports_a01)
        b00 = tile("B00", "south", (0, 600), "v1", ports_b00)
        b01 = tile("B01", "south", (600, 600), "v1", ports_b01)

        hashes = {}
        for t in (a00, a01, b00, b01):
            h = sha256(t)
            s["tiles"][h] = t
            hashes[t["tile_id"]] = h

        # cross-region acceptance corridors
        s["corridors"] = [
            {"corridor_id": "C1", "name": "西入口→东出口（跨南北区）",
             "waypoints": ["A00/w", "B01/e"]},
            {"corridor_id": "C2", "name": "北入口→南出口（跨东西瓦）",
             "waypoints": ["A01/n", "B00/s"]},
        ]

        # generation 1: publish initial world through the normal pipeline
        cand = {"regions": ["north", "south"],
                "tiles": {tid: hashes[tid] for tid in ("A00", "A01", "B00", "B01")}}
        cid = "RC-0001"
        s["seq"]["candidate"] = 1
        s["candidates"][cid] = {
            "candidate_id": cid, "regions": cand["regions"], "tiles": cand["tiles"],
            "note": "初始地图（四区瓦，南北两个发布区域）", "created_by": "system",
            "created_at": self.now(),
            "based_on_generations": {"north": 0, "south": 0},
            "status": "draft", "validation": None}
        tile_map = {tid: s["tiles"][h] for tid, h in cand["tiles"].items()}
        errs, _ = self.validate_tile_map(s, tile_map, set(cand["regions"]))
        assert not errs, errs
        s["candidates"][cid]["validation"] = {
            "status": "passed", "errors": [], "warnings": [],
            "checked_at": self.now(),
            "baseline_generations": {"north": 0, "south": 0}}
        s["candidates"][cid]["status"] = "submitted"
        for region in ("north", "south"):
            s["region_gen"][region] = 1
        rel = {"release_id": "REL-0001", "candidate_id": cid,
               "regions": ["north", "south"],
               "generations": {"north": 1, "south": 1},
               "tiles": dict(cand["tiles"]),
               "parent_releases": {"north": None, "south": None},
               "note": "初始发布", "created_at": self.now()}
        s["releases"].append(rel)
        s["region_heads"] = {"north": "REL-0001", "south": "REL-0001"}
        s["seq"]["release"] = 1
        s["candidates"][cid]["status"] = "published"
        s["candidates"][cid]["published_as"] = "REL-0001"

        # ---- editor variant RC-good: internal-only change in A00 (v2) ----
        def good_mods(nodes, lanes):
            # add a direct diagonal lane n<->e (internal geometry, same boundary)
            lanes.append({"lane_id": "lcne", "from": "n", "to": "e",
                          "two_way": True, "speed": 50})

        a00v2 = tile("A00", "north", (0, 0), "v2", ports_a00, lane_mods=good_mods)
        h = sha256(a00v2)
        s["tiles"][h] = a00v2
        hashes["A00@v2"] = h
        s["seq"]["candidate"] = 2
        s["candidates"]["RC-0002"] = {
            "candidate_id": "RC-0002", "regions": ["north"],
            "tiles": {"A00": h, "A01": hashes["A01"]},
            "note": "北区内部新增 n-e 联络车道（边界拓扑不变，应通过校验）",
            "created_by": "editor", "created_at": self.now(),
            "based_on_generations": {"north": 1}, "status": "draft",
            "validation": None}

        # ---- editor variant RC-bad: south seam speed 80 + deletes c-w lane ----
        def bad_mods(nodes, lanes):
            # cutting the only lane to the west entrance isolates A00/w,
            # so acceptance corridor C1 (starting at A00/w) becomes untraversable
            lanes[:] = [ln for ln in lanes if ln["lane_id"] != "lcw"]

        a00v3 = tile("A00", "north", (0, 0), "v3-bad",
                     lambda P, spd: ports_a00(P, 80), lane_mods=bad_mods)
        h3 = sha256(a00v3)
        s["tiles"][h3] = a00v3
        hashes["A00@v3"] = h3
        s["seq"]["candidate"] = 3
        s["candidates"]["RC-0003"] = {
            "candidate_id": "RC-0003", "regions": ["north"],
            "tiles": {"A00": h3, "A01": hashes["A01"]},
            "note": "北区改稿：南端口限速改 80 且删除西入口车道（接缝限速+跨区走廊必失败）",
            "created_by": "editor", "created_at": self.now(),
            "based_on_generations": {"north": 1}, "status": "draft",
            "validation": None}

        s["vehicles"]["VH-01"] = {"snapshots": [], "last_event_id": 0}
        self._event_locked_run(s, "SYSTEM_SEEDED", "初始地图与示例候选已生成", "REL-0001")

"""
Zero-dependency HTTP layer for the map release & route snapshot service.
Python stdlib only (http.server / json).

Endpoints (all JSON under /api):
  GET  /api/status
  GET  /api/events
  GET  /api/tiles
  GET  /api/candidates        POST /api/candidates
  POST /api/candidates/{id}/validate
  POST /api/candidates/{id}/publish
  GET  /api/releases
  GET  /api/corridors
  GET  /api/patches           POST /api/patches
  POST /api/patches/{id}/activate     /revoke
  POST /api/routes            (create pinned snapshot)
  GET  /api/snapshots
  GET  /api/snapshots/{id}
  POST /api/snapshots/{id}/progress
  POST /api/snapshots/{id}/adopt-reroute
  POST /api/snapshots/{id}/complete
  GET  /api/vehicles/{id}
  GET  /api/vehicles/{id}/sync?last_event_id=N
  GET  /api/view?mode=published|candidate|snapshot&ref=...

Regulatory audit (read-only over live state; appends only to data/audit.json):
  POST /api/audit/template            (read-only signed demo-chain authoring)
  POST /api/audit/vehicle-events      (read-only sign a live vehicle chain)
  GET  /api/audit/imports             POST /api/audit/imports
  POST /api/audit/imports/{key}/append
  GET  /api/audit/imports/{key}       GET .../checkpoints  POST .../checkpoints
  GET|POST /api/audit/imports/{key}/query?seq=|ts=
  GET  /api/audit/imports/{key}/branches   POST /api/audit/imports/{key}/branches
  GET  /api/audit/branches/{id}
  GET|POST /api/audit/exports
  GET  /api/audit/archives
Static UI served at /
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from engine import DomainError, MapEngine, short
from audit import AuditEngine

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mapstate.json")
AUDIT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "audit.json")

ENGINE = MapEngine(DATA_PATH)
# audit subsystem: separate append-only store; ENGINE is only ever read by it
AUDIT = AuditEngine(AUDIT_PATH, ENGINE)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "HDMapTiles/1.0"

    def log_message(self, fmt, *args):  # concise stderr logs
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    # ------------------------------------------------ helpers ----------
    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data
        except Exception as e:
            raise DomainError("BAD_JSON", f"请求体不是合法 JSON: {e}", 400)

    def _guard(self, fn):
        try:
            return fn()
        except DomainError as e:
            self._send_json(e.to_body(), e.status)
        except FileNotFoundError as e:
            self._send_json({"error": {"code": "NOT_FOUND", "message": str(e)}}, 404)
        except Exception as e:  # never leave the API in an unexplainable state
            self._send_json({"error": {"code": "INTERNAL", "message": repr(e)}}, 500)

    # ------------------------------------------------ routing ----------
    def do_GET(self):
        self._guard(self._do_get)

    def do_POST(self):
        self._guard(self._do_post)

    def _do_get(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path.startswith("/api/"):
            return self._api_get(path, q)
        return self._serve_static(path)

    def _do_post(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        body = self._read_json()

        m = re.match(r"^/api/candidates/([^/]+)/(validate|publish)$", path)
        if m:
            cid, action = m.group(1), m.group(2)
            if action == "validate":
                return self._send_json(ENGINE.validate_candidate(cid))
            return self._send_json(ENGINE.publish_candidate(cid))
        m = re.match(r"^/api/patches/([^/]+)/(activate|revoke)$", path)
        if m:
            pid, action = m.group(1), m.group(2)
            if action == "activate":
                return self._send_json(ENGINE.activate_patch(pid))
            return self._send_json(ENGINE.revoke_patch(pid, body.get("reason", "")))
        m = re.match(r"^/api/snapshots/([^/]+)/(progress|adopt-reroute|complete)$", path)
        if m:
            sid, action = m.group(1), m.group(2)
            if action == "progress":
                return self._send_json(ENGINE.update_progress(sid, body))
            if action == "complete":
                return self._send_json(ENGINE.complete_snapshot(sid))
            return self._send_json(ENGINE.adopt_reroute(sid))

        # ---------------- regulatory audit (read-only over live state) --------
        if path == "/api/audit/imports":
            verdict = AUDIT.import_events(body)
            # a rejected chain is still recorded (verdict-once) but answers 422
            return self._send_json(
                verdict, 200 if verdict.get("idempotent") else
                (422 if verdict["status"] == "rejected" else 201))
        if path == "/api/audit/template":
            # read-only evidence authoring aid; writes no archive
            return self._send_json(AUDIT.build_template_events(body))
        if path == "/api/audit/vehicle-events":
            # read-only: export one live vehicle's snapshot chain as signed events
            return self._send_json(AUDIT.build_vehicle_events(body))
        m = re.match(r"^/api/audit/imports/([^/]+)/append$", path)
        if m:
            return self._send_json(AUDIT.append_events(m.group(1),
                                                       body.get("events", [])), 201)
        m = re.match(r"^/api/audit/imports/([^/]+)/checkpoints$", path)
        if m:
            return self._send_json(AUDIT.confirm_checkpoint(
                m.group(1), int(body.get("seq")), body.get("note", ""),
                body.get("confirmed_by", "auditor")), 201)
        m = re.match(r"^/api/audit/imports/([^/]+)/query$", path)
        if m:
            return self._send_json(AUDIT.query_state(
                m.group(1), body.get("seq"), body.get("ts")), 201)
        m = re.match(r"^/api/audit/imports/([^/]+)/branches$", path)
        if m:
            return self._send_json(AUDIT.create_branch(m.group(1), body), 201)
        if path == "/api/audit/exports":
            key = body.get("import_key")
            if not key:
                raise DomainError("BAD_REQUEST", "import_key 必填", 400)
            return self._send_json(AUDIT.create_export(key, body), 201)

        table = {
            "/api/candidates": lambda: self._send_json(ENGINE.list_candidates()),
            "/api/patches": lambda: self._send_json(ENGINE.create_patch(body)),
            "/api/routes": lambda: self._send_json(ENGINE.create_route(body), 201),
            "/api/tiles": lambda: self._send_json(ENGINE.put_tile(body), 201),
        }
        if path in table:
            return table[path]()
        raise DomainError("NOT_FOUND", f"无此端点: POST {path}", 404)

    def _api_get(self, path: str, q: dict):
        m = re.match(r"^/api/snapshots/([^/]+)$", path)
        if m:
            return self._send_json(ENGINE.get_snapshot(m.group(1)))
        m = re.match(r"^/api/vehicles/([^/]+)/sync$", path)
        if m:
            last = int(q.get("last_event_id", ["0"])[0])
            return self._send_json(ENGINE.vehicle_sync(m.group(1), last))
        m = re.match(r"^/api/vehicles/([^/]+)$", path)
        if m:
            return self._send_json(ENGINE.vehicle_snapshots(m.group(1)))

        # -------- regulatory audit (all of these only append audit archives) ---
        if path == "/api/audit/imports":
            return self._send_json({"imports": AUDIT.list_imports()})
        if path == "/api/audit/archives":
            return self._send_json(
                {"archives": AUDIT.list_archives(int(q.get("limit", ["100"])[0]))})
        if path == "/api/audit/exports":
            return self._send_json({"exports": AUDIT.list_exports(
                q.get("import_key", [None])[0])})
        m = re.match(r"^/api/audit/branches/([^/]+)$", path)
        if m:
            return self._send_json(AUDIT.get_branch(m.group(1), True))
        m = re.match(r"^/api/audit/imports/([^/]+)$", path)
        if m:
            return self._send_json(AUDIT.get_import(m.group(1)))
        m = re.match(r"^/api/audit/imports/([^/]+)/checkpoints$", path)
        if m:
            return self._send_json(
                {"checkpoints": AUDIT.list_checkpoints(m.group(1))})
        m = re.match(r"^/api/audit/imports/([^/]+)/branches$", path)
        if m:
            return self._send_json({"branches": AUDIT.list_branches(m.group(1))})
        m = re.match(r"^/api/audit/imports/([^/]+)/query$", path)
        if m:
            seq = q.get("seq", [None])[0]
            ts = q.get("ts", [None])[0]
            return self._send_json(AUDIT.query_state(
                m.group(1), int(seq) if seq is not None else None, ts), 201)

        table = {
            "/api/status": lambda: self._send_json(ENGINE.region_status()),
            "/api/events": lambda: self._send_json(
                {"events": ENGINE.list_events(int(q.get("limit", ["60"])[0]))}),
            "/api/tiles": lambda: self._send_json({"tiles": ENGINE.list_tiles()}),
            "/api/candidates": lambda: self._send_json({"candidates": ENGINE.list_candidates()}),
            "/api/releases": lambda: self._send_json({"releases": ENGINE.list_releases()}),
            "/api/corridors": lambda: self._send_json({"corridors": ENGINE.list_corridors()}),
            "/api/patches": lambda: self._send_json({"patches": ENGINE.list_patches()}),
            "/api/snapshots": lambda: self._send_json(self._all_snapshots()),
            "/api/view": lambda: self._send_json(
                ENGINE.describe_view(q.get("mode", ["published"])[0],
                                     q.get("ref", [None])[0])),
        }
        if path in table:
            return table[path]()
        raise DomainError("NOT_FOUND", f"无此端点: GET {path}", 404)

    def _all_snapshots(self) -> dict:
        out = []
        for vid, v in ENGINE.state["vehicles"].items():
            for sid in v["snapshots"]:
                snap = ENGINE.state["snapshots"][sid]
                out.append({
                    "snapshot_id": sid, "vehicle_id": vid,
                    "status": snap["status"], "version": snap["version"]["version"],
                    "from_node": snap["from_node"], "to_node": snap["to_node"],
                    "progress": snap["progress"],
                    "parent_snapshot_id": snap.get("parent_snapshot_id"),
                    "children": snap.get("children", []),
                    "steps": len(snap["route"]),
                    "tiles": [{"tile_id": t, "hash": short(h, 12)}
                              for t, h in sorted(snap["tiles"].items())],
                    "created_at": snap["created_at"]})
        return {"snapshots": sorted(out, key=lambda x: x["created_at"], reverse=True)}

    # ------------------------------------------------ static ------------
    def _serve_static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/")).replace("..", "")
        fp = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(fp):
            self._send_json({"error": {"code": "NOT_FOUND", "message": path}}, 404)
            return
        ext = os.path.splitext(fp)[1]
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(host: str = "0.0.0.0", port: int = 8080):
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"HD-map tile service on http://{host}:{port}  (data: {DATA_PATH})")
    httpd.serve_forever()


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    main(port=p)

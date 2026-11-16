"""HTTP 服务：JSON API + 静态页面（仅标准库，不连接任何真实支付）。"""
from __future__ import annotations

import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import engine
from .scenarios import WINDOW
from .store import Conflict, NotFound, Store
from .util import CLOSE_LAG_DAYS

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class Api:
    """把 HTTP 层与领域逻辑隔开，便于测试。"""

    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _is_late(e) -> bool:
        delay = (date.fromisoformat(e["arrived_at"])
                 - date.fromisoformat(e["occurred_at"])).days
        return delay > CLOSE_LAG_DAYS

    # ---- 元信息 ----
    def meta(self):
        ds = self.store.dataset
        late = [e for e in ds["events"] if self._is_late(e)]
        return {
            "data_version": self.store.data_version,
            "as_of": ds["meta"]["as_of"],
            "close_lag_days": CLOSE_LAG_DAYS,
            "window": WINDOW,
            "counts": {
                "customers": len(ds["customers"]),
                "subscriptions": len(ds["subs"]),
                "usage_events": len(ds["events"]),
                "late_events": len(late),
                "refunds": len(ds["refunds"]),
            },
            "price_books": [
                {**{k: p[k] for k in ("id", "name", "status", "effective_from")},
                 "hash": self.store.pb_hash(p["id"])}
                for p in self.store.price_books.values()
            ],
        }

    def price_books(self):
        return list(self.store.price_books.values())

    def subscriptions(self):
        ds = self.store.dataset
        names = {c["id"]: c["name"] for c in ds["customers"]}
        return [
            {**s, "member_names": [names.get(m, m) for m in s["members"]]}
            for s in ds["subs"]
        ]

    # ---- 账单重放 ----
    def bill(self, q):
        sub = self._sub(q["sub_id"][0])
        pb = self._pb(q.get("pb", ["pb-2026.06"])[0])
        period = q["period"][0]
        as_of = q.get("as_of", [self.store.dataset["meta"]["as_of"]])[0]
        include = q.get("include_events", ["0"])[0] == "1"
        ds = self.store.dataset
        bill = engine.compute_bill(sub, ds["events"], ds["sub_events"],
                                   ds["refunds"], pb, period, as_of,
                                   include_events=include)
        if include:
            from .util import month_bounds, to_date
            first, last = month_bounds(period)
            bill["events"] = sorted(
                (e for e in ds["events"]
                 if e["sub_id"] == sub["id"]
                 and first <= to_date(e["occurred_at"]) <= last
                 and to_date(e["arrived_at"]) <= to_date(as_of)),
                key=lambda e: (e["occurred_at"], e["id"]),
            )
        return bill

    def bill_diff(self, q):
        sub = self._sub(q["sub_id"][0])
        period = q["period"][0]
        as_of = self.store.dataset["meta"]["as_of"]
        ds = self.store.dataset
        a = engine.compute_bill(sub, ds["events"], ds["sub_events"], ds["refunds"],
                                self._pb(q["from_pb"][0]), period, as_of)
        b = engine.compute_bill(sub, ds["events"], ds["sub_events"], ds["refunds"],
                                self._pb(q["to_pb"][0]), period, as_of)
        rows = engine.diff_bills(a, b)
        # 下钻：该订阅该账期的全部原始事件，供差额行溯源
        from .util import month_bounds
        first, last = month_bounds(period)
        events = sorted(
            (e for e in ds["events"]
             if e["sub_id"] == sub["id"] and first.isoformat() <= e["occurred_at"] <= last.isoformat()),
            key=lambda e: (e["occurred_at"], e["id"]),
        )
        return {
            "sub_id": sub["id"], "period": period,
            "from": {"price_book_id": a["price_book_id"], "total_cents": a["total_cents"]},
            "to": {"price_book_id": b["price_book_id"], "total_cents": b["total_cents"]},
            "delta_cents": b["total_cents"] - a["total_cents"],
            "line_diffs": rows,
            "events": events,
        }

    def adjustments(self, q):
        sub = self._sub(q["sub_id"][0])
        pb = self._pb(q.get("pb", ["pb-2026.06"])[0])
        return engine.compute_adjustment(
            sub, self.store.dataset["events"], self.store.dataset["sub_events"],
            self.store.dataset["refunds"], pb, q["period"][0],
            self.store.dataset["meta"]["as_of"])

    def late_usage(self):
        ds = self.store.dataset
        late = [e for e in ds["events"] if self._is_late(e)]
        by_pair = {}
        for e in late:
            key = (e["sub_id"], e["occurred_at"][:7])
            by_pair.setdefault(key, []).append(e)
        impacted = [
            {"sub_id": k[0], "period": k[1], "late_count": len(v),
             "max_delay_days": max(
                 (date.fromisoformat(x["arrived_at"])
                  - date.fromisoformat(x["occurred_at"])).days
                 for x in v)}
            for k, v in sorted(by_pair.items())
        ]
        sample = sorted(late, key=lambda e: e["arrived_at"], reverse=True)[:200]
        return {"total_late": len(late), "impacted": impacted, "sample": sample}

    def inject_late(self, body):
        """注入一笔迟到用量（虚构数据演示用），返回新的数据版本。"""
        ds = self.store.dataset
        sub = self._sub(body["sub_id"])
        n = len(ds["events"])
        ev = {
            "id": f"u-x{n + 1:05d}",
            "sub_id": sub["id"],
            "customer_id": sub["members"][0],
            "metric": body["metric"],
            "quantity": float(body["quantity"]),
            "occurred_at": body["occurred_at"],
            "arrived_at": ds["meta"]["as_of"],
        }
        new_version = self.store.inject_late_events([ev])
        return {"event": ev, "data_version": new_version}

    # ---- 试算与确认 ----
    def run_scenario(self, body):
        return self.store.run_scenario(body)

    def scenarios(self):
        return self.store.list_scenarios()

    def scenario_detail(self, sid):
        return self.store.get_scenario(sid)

    def recompute(self, sid):
        return self.store.recompute(sid)

    def confirm(self, sid, body):
        return self.store.confirm(sid, body.get("expected", {}),
                                  body.get("note", ""))

    def decisions(self):
        return self.store.list_decisions()

    # ---- helpers ----
    def _sub(self, sid):
        for s in self.store.dataset["subs"]:
            if s["id"] == sid:
                return s
        raise NotFound(f"subscription {sid}")

    def _pb(self, pid):
        if pid not in self.store.price_books:
            raise NotFound(f"price book {pid}")
        return self.store.price_books[pid]


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, path):
            f = WEB_DIR / ("index.html" if path == "/" else path.lstrip("/"))
            if not f.is_file() or WEB_DIR not in f.resolve().parents:
                self._send({"error": "not found"}, 404)
                return
            ctype = {".html": "text/html", ".js": "text/javascript",
                     ".css": "text/css"}.get(f.suffix, "application/octet-stream")
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            p = u.path
            try:
                if p == "/" or p.startswith("/app.") or p.startswith("/style."):
                    return self._static(p)
                if p == "/api/meta":
                    return self._send(api.meta())
                if p == "/api/price-books":
                    return self._send(api.price_books())
                if p == "/api/subscriptions":
                    return self._send(api.subscriptions())
                if p == "/api/bills":
                    return self._send(api.bill(q))
                if p == "/api/bills/diff":
                    return self._send(api.bill_diff(q))
                if p == "/api/bills/adjustments":
                    return self._send(api.adjustments(q))
                if p == "/api/late-usage":
                    return self._send(api.late_usage())
                if p == "/api/scenarios":
                    return self._send(api.scenarios())
                m = re.fullmatch(r"/api/scenarios/(sc-\d+)", p)
                if m:
                    return self._send(api.scenario_detail(m.group(1)))
                if p == "/api/decisions":
                    return self._send(api.decisions())
                self._send({"error": "not found"}, 404)
            except NotFound as e:
                self._send({"error": str(e)}, 404)
            except (KeyError, ValueError) as e:
                self._send({"error": f"bad request: {e}"}, 400)
            except Exception as e:  # noqa: BLE001
                self._send({"error": f"internal: {e}"}, 500)

        def do_POST(self):
            u = urlparse(self.path)
            p = u.path
            try:
                body = self._body()
                if p == "/api/scenarios/run":
                    return self._send(api.run_scenario(body))
                m = re.fullmatch(r"/api/scenarios/(sc-\d+)/(recompute|confirm)", p)
                if m and m.group(2) == "recompute":
                    return self._send(api.recompute(m.group(1)))
                if m and m.group(2) == "confirm":
                    return self._send(api.confirm(m.group(1), body))
                if p == "/api/late-usage/inject":
                    return self._send(api.inject_late(body))
                self._send({"error": "not found"}, 404)
            except Conflict as e:
                self._send({"error": "依据已变化，确认被拒绝",
                            "mismatches": e.mismatches}, 409)
            except NotFound as e:
                self._send({"error": str(e)}, 404)
            except (KeyError, ValueError) as e:
                self._send({"error": f"bad request: {e}"}, 400)
            except Exception as e:  # noqa: BLE001
                self._send({"error": f"internal: {e}"}, 500)

        def log_message(self, *args):  # 静音
            pass

    return Handler


def serve(store: Store, port: int = 8321):
    api = Api(store)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(api))
    print(f"pointwise 计费分析已启动: http://127.0.0.1:{port}")
    print(f"数据版本: {store.data_version}（虚构数据，不连接真实支付）")
    httpd.serve_forever()

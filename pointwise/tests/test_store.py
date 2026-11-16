"""版本绑定与确认流程测试：依据变化拒绝确认、重算复现、正式记录分离。"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from billing import dataset as ds_mod
from billing.store import Conflict, Store


def make_store(tmp: str) -> Store:
    d = Path(tmp)
    ds = ds_mod.generate()
    # 缩小数据集加速测试：只保留 8 月的事件
    ds["events"] = [e for e in ds["events"] if e["occurred_at"] < "2026-09-01"]
    (d / "dataset.json").write_text(json.dumps(ds, ensure_ascii=False))
    (d / "price_books.json").write_text(json.dumps(ds_mod.PRICE_BOOKS,
                                                  ensure_ascii=False))
    (d / "scenarios.jsonl").touch()
    (d / "decisions.jsonl").touch()
    return Store(d)


class TestVersionBinding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.store = make_store(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_scenario_binds_versions(self):
        rec = self.store.run_scenario({
            "price_book_id": "pb-2027.a", "policy": "unified"})
        self.assertEqual(rec["kind"], "trial")
        self.assertEqual(rec["data_version"], self.store.data_version)
        self.assertEqual(rec["price_book_hash"],
                         self.store.pb_hash("pb-2027.a"))
        self.assertTrue(rec["config_hash"])
        self.assertTrue(rec["result_hash"])

    def test_recompute_reproduces(self):
        rec = self.store.run_scenario({
            "price_book_id": "pb-2027.b", "policy": "grandfather",
            "cutoff": "2026-08-01"})
        chk = self.store.recompute(rec["scenario_id"])
        self.assertEqual(chk["status"], "reproduced")

    def test_confirm_writes_formal_record_separately(self):
        rec = self.store.run_scenario({
            "price_book_id": "pb-2027.a", "policy": "unified"})
        decision = self.store.confirm(rec["scenario_id"], {
            "data_version": rec["data_version"],
            "price_book_hash": rec["price_book_hash"],
            "config_hash": rec["config_hash"],
        }, note="测试确认")
        self.assertEqual(decision["kind"], "formal")
        # 正式记录在 decisions.jsonl，情景结果在 scenarios.jsonl，互不混入
        decisions = [json.loads(l) for l in
                     (Path(self.tmp) / "decisions.jsonl").read_text().splitlines()]
        scenarios = [json.loads(l) for l in
                     (Path(self.tmp) / "scenarios.jsonl").read_text().splitlines()]
        self.assertTrue(any(d["decision_id"] == decision["decision_id"]
                            for d in decisions))
        self.assertFalse(any("decision_id" in s for s in scenarios))
        self.assertFalse(any("scenario_id" in d and "result" in d
                             for d in decisions))

    def test_confirm_rejected_when_basis_changed(self):
        rec = self.store.run_scenario({
            "price_book_id": "pb-2027.a", "policy": "unified"})
        # 试算后补计一笔迟到用量 → 数据版本变化。
        # 选一个 8 月已生效的订阅，并注入足够大的量确保改变账单。
        ds = self.store.dataset
        active = next(
            s for s in ds["subs"]
            if any(e["type"] == "subscribe" and e["at"] <= "2026-08-01"
                   for e in ds["sub_events"] if e["sub_id"] == s["id"]))
        self.store.inject_late_events([{
            "id": "u-late-x", "sub_id": active["id"],
            "customer_id": active["members"][0],
            "metric": "api_calls", "quantity": 9000000,
            "occurred_at": "2026-08-15", "arrived_at": "2026-11-17",
        }])
        with self.assertRaises(Conflict) as ctx:
            self.store.confirm(rec["scenario_id"], {
                "data_version": rec["data_version"],
                "price_book_hash": rec["price_book_hash"],
                "config_hash": rec["config_hash"],
            })
        fields = {m["field"] for m in ctx.exception.mismatches}
        self.assertIn("data_version", fields)
        # 重算同一试算 → 结果漂移
        chk = self.store.recompute(rec["scenario_id"])
        self.assertEqual(chk["status"], "drifted")

    def test_confirm_rejected_on_stale_client_version(self):
        rec = self.store.run_scenario({
            "price_book_id": "pb-2027.b", "policy": "unified"})
        with self.assertRaises(Conflict):
            self.store.confirm(rec["scenario_id"], {
                "data_version": "deadbeef0000",
                "price_book_hash": rec["price_book_hash"],
                "config_hash": rec["config_hash"],
            })


class TestScenarioMetrics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.store = make_store(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_unified_vs_grandfather_assignment(self):
        unified = self.store.run_scenario({
            "price_book_id": "pb-2027.a", "policy": "unified"})
        gf = self.store.run_scenario({
            "price_book_id": "pb-2027.a", "policy": "grandfather",
            "cutoff": "2026-08-15"})
        u_assign = {s["assigned_price_book"] for s in unified["result"]["subs"]}
        self.assertEqual(u_assign, {"pb-2027.a"})
        g_assign = {s["assigned_price_book"] for s in gf["result"]["subs"]}
        self.assertEqual(g_assign, {"pb-2027.a", "pb-2026.06"})
        # 统一迁移下收入变化应不小于保留老客（同一候选方案）
        self.assertGreaterEqual(
            abs(unified["result"]["totals"]["delta_cents"]),
            abs(gf["result"]["totals"]["delta_cents"]))

    def test_assumptions_labeled_and_separate(self):
        rec = self.store.run_scenario({
            "price_book_id": "pb-2027.a", "policy": "unified",
            "assumptions": {
                "churn": {"enabled": True, "threshold_pct": 10, "churn_rate": 0.5},
                "usage_growth": {"enabled": True, "pct": 20},
            }})
        res = rec["result"]
        self.assertTrue(res["assumptions_applied"])
        self.assertIn("with_assumptions", res)
        self.assertTrue(any("行为假设" in s for s in res["declared_assumptions"]))
        # 无假设的重放结果仍在，未被假设污染
        self.assertIn("totals", res)
        plain = self.store.run_scenario({
            "price_book_id": "pb-2027.a", "policy": "unified"})
        self.assertNotIn("with_assumptions", plain["result"])
        self.assertEqual(plain["result"]["declared_assumptions"], [])

    def test_implementation_cost_is_estimate(self):
        rec = self.store.run_scenario({
            "price_book_id": "pb-2027.a", "policy": "grandfather"})
        cost = rec["result"]["implementation_cost"]
        self.assertTrue(cost["estimate"])
        self.assertIn("估算", cost["note"])


if __name__ == "__main__":
    unittest.main()

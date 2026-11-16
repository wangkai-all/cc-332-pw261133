"""计费引擎测试：重放正确性、退款幂等、补计差额、共享套餐池化。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from billing.engine import (active_intervals, compute_adjustment, compute_bill,
                            diff_bills)

PB_V1 = {
    "id": "pb-v1",
    "tiers": {
        "basic": {
            "base_fee_cents": 3000,
            "included": {"api_calls": 10000, "egress_gb": 10, "storage_gb_day": 300},
            "unit_price_cents": {"api_calls": 100, "egress_gb": 100, "storage_gb_day": 1},
        },
        "team": {
            "base_fee_cents": 9000,
            "included": {"api_calls": 30000, "egress_gb": 30, "storage_gb_day": 900},
            "unit_price_cents": {"api_calls": 80, "egress_gb": 80, "storage_gb_day": 1},
        },
    },
}
PB_V2 = {
    "id": "pb-v2",
    "tiers": {
        "basic": {
            "base_fee_cents": 2000,
            "included": {"api_calls": 5000, "egress_gb": 5, "storage_gb_day": 150},
            "unit_price_cents": {"api_calls": 200, "egress_gb": 200, "storage_gb_day": 2},
        },
        "team": {
            "base_fee_cents": 6000,
            "included": {"api_calls": 15000, "egress_gb": 15, "storage_gb_day": 450},
            "unit_price_cents": {"api_calls": 160, "egress_gb": 160, "storage_gb_day": 2},
        },
    },
}


def ev(eid, sub, metric, qty, occurred, arrived=None, customer="c-1"):
    return {
        "id": eid, "sub_id": sub, "customer_id": customer, "metric": metric,
        "quantity": qty, "occurred_at": occurred,
        "arrived_at": arrived or occurred,
    }


class TestIntervals(unittest.TestCase):
    def test_mid_month_subscribe_and_unsubscribe(self):
        sub_events = [
            {"seq": 1, "type": "subscribe", "sub_id": "s1", "tier": "basic",
             "at": "2026-08-10"},
            {"seq": 2, "type": "unsubscribe", "sub_id": "s1", "at": "2026-08-20"},
        ]
        iv = active_intervals("s1", sub_events,
                              __import__("datetime").date(2026, 8, 1),
                              __import__("datetime").date(2026, 8, 31))
        self.assertEqual(len(iv), 1)
        self.assertEqual((iv[0]["start"].isoformat(), iv[0]["end"].isoformat()),
                         ("2026-08-10", "2026-08-20"))

    def test_tier_change_splits_interval(self):
        sub_events = [
            {"seq": 1, "type": "subscribe", "sub_id": "s1", "tier": "basic",
             "at": "2026-08-01"},
            {"seq": 2, "type": "tier_change", "sub_id": "s1", "to_tier": "team",
             "at": "2026-08-16"},
        ]
        iv = active_intervals("s1", sub_events,
                              __import__("datetime").date(2026, 8, 1),
                              __import__("datetime").date(2026, 8, 31))
        self.assertEqual([i["tier"] for i in iv], ["basic", "team"])
        self.assertEqual(iv[0]["end"].isoformat(), "2026-08-15")
        self.assertEqual(iv[1]["start"].isoformat(), "2026-08-16")


class TestReplay(unittest.TestCase):
    def setUp(self):
        self.sub = {"id": "s1", "tier": "basic", "members": ["c-1"], "shared": False}
        self.sub_events = [
            {"seq": 1, "type": "subscribe", "sub_id": "s1", "tier": "basic",
             "at": "2026-08-01"},
        ]
        self.events = [
            ev("e1", "s1", "api_calls", 20000, "2026-08-05"),
            ev("e2", "s1", "api_calls", 5000, "2026-08-20"),
        ]

    def test_replay_deterministic(self):
        b1 = compute_bill(self.sub, self.events, self.sub_events, [], PB_V1,
                          "2026-08", "2026-09-03")
        b2 = compute_bill(self.sub, self.events, self.sub_events, [], PB_V1,
                          "2026-08", "2026-09-03")
        self.assertEqual(b1["bill_hash"], b2["bill_hash"])
        # 25000 次调用，额度 10000，超额 15000 → 15 × 100 分
        self.assertEqual(b1["total_cents"], 3000 + 1500)

    def test_replay_not_naive_reprice(self):
        """红线：换价目表必须重放原始用量，不能拿汇总账单金额套新单价。

        v1 下超额 15000 次；若错误地把 v1 的超额费用按单价比例折算
        （1500 × 200/100 = 3000），会与正确重放（超额 20000 × 0.2 = 4000）
        不一致——因为 v2 的包含额度变了。
        """
        b_v1 = compute_bill(self.sub, self.events, self.sub_events, [], PB_V1,
                            "2026-08", "2026-09-03")
        b_v2 = compute_bill(self.sub, self.events, self.sub_events, [], PB_V2,
                            "2026-08", "2026-09-03")
        usage_v1 = next(l for l in b_v1["lines"]
                        if l["kind"] == "usage" and l["metric"] == "api_calls")
        naive = usage_v1["amount_cents"] * 200 // 100  # 错误做法：按单价比例折算
        usage_v2 = next(l for l in b_v2["lines"]
                        if l["kind"] == "usage" and l["metric"] == "api_calls")
        self.assertEqual(usage_v2["amount_cents"], 20000 * 200 // 1000)
        self.assertNotEqual(naive, usage_v2["amount_cents"])

    def test_shared_plan_quota_pooled(self):
        """共享套餐：额度在订阅层池化，两个成员各自未超个人一半额度，
        但合计超额时只在订阅层计一次超额。"""
        sub = {"id": "s2", "tier": "team", "members": ["c-1", "c-2"], "shared": True}
        sub_events = [
            {"seq": 1, "type": "subscribe", "sub_id": "s2", "tier": "team",
             "at": "2026-08-01"},
        ]
        events = [
            ev("e1", "s2", "api_calls", 20000, "2026-08-05", customer="c-1"),
            ev("e2", "s2", "api_calls", 20000, "2026-08-06", customer="c-2"),
        ]
        bill = compute_bill(sub, events, sub_events, [], PB_V1, "2026-08",
                            "2026-09-03")
        usage = [l for l in bill["lines"]
                 if l["kind"] == "usage" and l["metric"] == "api_calls"]
        self.assertEqual(len(usage), 1)  # 订阅层一行，不按成员拆
        # 合计 40000，池化额度 30000，超额 10000 → 10 × 80
        self.assertEqual(usage[0]["amount_cents"], 800)


class TestRefundIdempotency(unittest.TestCase):
    def setUp(self):
        self.sub = {"id": "s1", "tier": "basic", "members": ["c-1"], "shared": False}
        self.sub_events = [
            {"seq": 1, "type": "subscribe", "sub_id": "s1", "tier": "basic",
             "at": "2026-08-01"},
            {"seq": 2, "type": "unsubscribe", "sub_id": "s1", "at": "2026-08-16"},
        ]
        self.refunds = [{
            "id": "rf-s1-2026-08-16", "sub_id": "s1", "period": "2026-08",
            "amount_cents": 1452, "reason": "退订折算退款", "event_seq": 2,
        }]

    def test_refund_applied_exactly_once(self):
        bill = compute_bill(self.sub, [], self.sub_events, self.refunds, PB_V1,
                            "2026-08", "2026-09-03")
        credits = [l for l in bill["lines"] if l["kind"] == "credit"]
        self.assertEqual(len(credits), 1)
        self.assertEqual(credits[0]["amount_cents"], -1452)

    def test_duplicate_refund_record_rejected(self):
        dup = self.refunds + [dict(self.refunds[0])]
        with self.assertRaises(ValueError):
            compute_bill(self.sub, [], self.sub_events, dup, PB_V1, "2026-08",
                         "2026-09-03")

    def test_late_usage_replay_does_not_double_deduct(self):
        """补计重放只产生用量差额，退款不会被重复抵扣。"""
        events_late = [
            ev("e1", "s1", "api_calls", 20000, "2026-08-10",
               arrived="2026-08-11"),
            ev("e2", "s1", "api_calls", 9000, "2026-08-12",
               arrived="2026-09-10"),  # 关账后迟到
        ]
        adj = compute_adjustment(self.sub, events_late, self.sub_events,
                                 self.refunds, PB_V1, "2026-08", "2026-09-15")
        self.assertTrue(adj["refund_not_double_counted"])
        credit_diffs = [d for d in adj["line_diffs"] if d["kind"] == "credit"]
        self.assertTrue(all(d["delta_cents"] == 0 for d in credit_diffs))
        # 迟到的 9000 次全部超额（原 20000 已超 8 月半月额度）
        self.assertEqual(adj["delta_cents"], 9000 * 100 // 1000)


class TestAdjustment(unittest.TestCase):
    def test_late_events_drive_delta(self):
        sub = {"id": "s1", "tier": "basic", "members": ["c-1"], "shared": False}
        sub_events = [
            {"seq": 1, "type": "subscribe", "sub_id": "s1", "tier": "basic",
             "at": "2026-08-01"},
        ]
        events = [
            ev("e1", "s1", "egress_gb", 50.0, "2026-08-10", arrived="2026-08-11"),
            ev("e2", "s1", "egress_gb", 30.0, "2026-08-20", arrived="2026-09-20"),
        ]
        adj = compute_adjustment(sub, events, sub_events, [], PB_V1, "2026-08",
                                 "2026-10-01")
        self.assertEqual(adj["late_event_count"], 1)
        self.assertEqual(adj["late_events"][0]["id"], "e2")
        # 关账快照：50GB，额度 10 → 超 40；当前快照：80GB → 超 70；差额 30GB
        self.assertEqual(adj["delta_cents"], 30 * 100)

    def test_diff_bills_aligns_lines(self):
        sub = {"id": "s1", "tier": "basic", "members": ["c-1"], "shared": False}
        sub_events = [
            {"seq": 1, "type": "subscribe", "sub_id": "s1", "tier": "basic",
             "at": "2026-08-01"},
        ]
        events = [ev("e1", "s1", "api_calls", 20000, "2026-08-05")]
        a = compute_bill(sub, events, sub_events, [], PB_V1, "2026-08", "2026-09-03")
        b = compute_bill(sub, events, sub_events, [], PB_V2, "2026-08", "2026-09-03")
        rows = diff_bills(a, b)
        api = next(r for r in rows if r["metric"] == "api_calls")
        self.assertEqual(api["a_amount_cents"], 1000)
        self.assertEqual(api["b_amount_cents"], 3000)
        self.assertEqual(api["delta_cents"], 2000)
        base = next(r for r in rows if r["kind"] == "base")
        self.assertEqual(base["delta_cents"], 2000 - 3000)


if __name__ == "__main__":
    unittest.main()

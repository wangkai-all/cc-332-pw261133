"""虚构数据集生成器（确定性，seed 固定，可重复生成）。

生成内容：
- 客户与订阅（含共享套餐：team 档一个订阅挂多个成员，用量在订阅层汇总）
- 订阅生命周期事件：订阅 / 档位变更 / 退订（退订派生退款事实，带唯一 id）
- 原始用量事件：按 订阅×成员×指标×天 粒度，含 occurred_at / arrived_at；
  约 2.5% 的事件迟到 4~30 天到达，构成"补计用量"
- 三份价目表：现价 + 两个候选方案
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

from .util import cents_of, month_bounds

SEED = 42
AS_OF = date(2026, 11, 17)  # 数据集快照时点（固定，保证可复现）
FIRST_MONTH = date(2026, 6, 1)

PRICE_BOOKS = [
    {
        "id": "pb-2026.06",
        "name": "现价方案（2026.06 版）",
        "status": "current",
        "effective_from": "2026-06-01",
        "currency": "CNY",
        "tiers": {
            "basic": {
                "base_fee_cents": 4900,
                "included": {"api_calls": 100000, "egress_gb": 20, "storage_gb_day": 600},
                "unit_price_cents": {"api_calls": 150, "egress_gb": 200, "storage_gb_day": 3},
            },
            "pro": {
                "base_fee_cents": 19900,
                "included": {"api_calls": 500000, "egress_gb": 100, "storage_gb_day": 3000},
                "unit_price_cents": {"api_calls": 120, "egress_gb": 150, "storage_gb_day": 2},
            },
            "team": {
                "base_fee_cents": 59900,
                "included": {"api_calls": 2000000, "egress_gb": 500, "storage_gb_day": 15000},
                "unit_price_cents": {"api_calls": 100, "egress_gb": 120, "storage_gb_day": 2},
            },
        },
    },
    {
        "id": "pb-2027.a",
        "name": "候选 A · 高保底低单价",
        "status": "candidate",
        "effective_from": "2026-12-01",
        "currency": "CNY",
        "tiers": {
            "basic": {
                "base_fee_cents": 6900,
                "included": {"api_calls": 150000, "egress_gb": 30, "storage_gb_day": 900},
                "unit_price_cents": {"api_calls": 105, "egress_gb": 140, "storage_gb_day": 2},
            },
            "pro": {
                "base_fee_cents": 27900,
                "included": {"api_calls": 750000, "egress_gb": 150, "storage_gb_day": 4500},
                "unit_price_cents": {"api_calls": 84, "egress_gb": 105, "storage_gb_day": 1},
            },
            "team": {
                "base_fee_cents": 83900,
                "included": {"api_calls": 3000000, "egress_gb": 750, "storage_gb_day": 22500},
                "unit_price_cents": {"api_calls": 70, "egress_gb": 84, "storage_gb_day": 1},
            },
        },
    },
    {
        "id": "pb-2027.b",
        "name": "候选 B · 低保底高单价",
        "status": "candidate",
        "effective_from": "2026-12-01",
        "currency": "CNY",
        "tiers": {
            "basic": {
                "base_fee_cents": 3400,
                "included": {"api_calls": 60000, "egress_gb": 12, "storage_gb_day": 360},
                "unit_price_cents": {"api_calls": 188, "egress_gb": 250, "storage_gb_day": 4},
            },
            "pro": {
                "base_fee_cents": 13900,
                "included": {"api_calls": 300000, "egress_gb": 60, "storage_gb_day": 1800},
                "unit_price_cents": {"api_calls": 150, "egress_gb": 188, "storage_gb_day": 3},
            },
            "team": {
                "base_fee_cents": 41900,
                "included": {"api_calls": 1200000, "egress_gb": 300, "storage_gb_day": 9000},
                "unit_price_cents": {"api_calls": 125, "egress_gb": 150, "storage_gb_day": 3},
            },
        },
    },
]

_NAME_A = ["澄江", "岚山", "青梧", "望舒", "白泽", "归墟", "星野", "临渊",
           "扶摇", "濯缨", "沧浪", "云梦", "栖霞", "沐川", "砚溪", "疏影"]
_NAME_B = ["数据", "物联", "智造", "文旅", "物流", "传媒", "零售", "医疗",
           "教育", "能源", "农业", "安防"]

# 每档的用量画像：API 次日均/人、出站 GB 日均/人、存储 GB 基线/订阅
_PROFILE = {
    "basic": {"api": (2000, 8000), "egress": (0.1, 0.6), "storage": (5, 25)},
    "pro": {"api": (10000, 40000), "egress": (0.5, 2.0), "storage": (20, 80)},
    "team": {"api": (15000, 60000), "egress": (1.0, 5.0), "storage": (60, 300)},
}


def _gen_customers(rng: random.Random) -> list[dict]:
    customers = []
    for i in range(1, 71):
        name = f"{_NAME_A[i % len(_NAME_A)]}{_NAME_B[(i * 5) % len(_NAME_B)]}{i:02d}"
        customers.append({"id": f"c-{i:03d}", "name": name})
    return customers


def generate() -> dict:
    rng = random.Random(SEED)
    customers = _gen_customers(rng)
    rng.shuffle(customers)

    subs, sub_events, refunds = [], [], []
    seq = 0

    def next_seq():
        nonlocal seq
        seq += 1
        return seq

    tier_plan = (["basic"] * 14) + (["pro"] * 12) + (["team"] * 10)
    rng.shuffle(tier_plan)
    cidx = 0
    for n, tier in enumerate(tier_plan, start=1):
        sid = f"sub-{n:03d}"
        n_members = rng.randint(2, 4) if tier == "team" else 1
        members = [c["id"] for c in customers[cidx:cidx + n_members]]
        cidx += n_members
        # 订阅起始日：5 月下旬 ~ 10 月，部分月中起始（演示按天折算）
        start = FIRST_MONTH - timedelta(days=rng.randint(0, 12)) + timedelta(
            days=rng.randint(0, 150)
        )
        sub_events.append({
            "seq": next_seq(), "type": "subscribe", "sub_id": sid,
            "tier": tier, "at": start.isoformat(),
        })
        subs.append({
            "id": sid,
            "name": f"{tier}-{n:03d}",
            "tier": tier,
            "members": members,
            "shared": n_members > 1,
        })

    # 退订 4 笔（月中退订 → 按剩余天数折算退款，退款为带唯一 id 的事实）
    for sid, when in [("sub-002", date(2026, 8, 19)), ("sub-009", date(2026, 10, 7)),
                      ("sub-017", date(2026, 11, 9)), ("sub-024", date(2026, 9, 25))]:
        sub = next(s for s in subs if s["id"] == sid)
        ev = {"seq": next_seq(), "type": "unsubscribe", "sub_id": sid, "at": when.isoformat()}
        sub_events.append(ev)
        first, last = month_bounds(f"{when.year:04d}-{when.month:02d}")
        days_total = (last - first).days + 1
        remaining = (last - when).days
        base = PRICE_BOOKS[0]["tiers"][sub["tier"]]["base_fee_cents"]
        amount = cents_of(base * remaining / days_total)
        refunds.append({
            "id": f"rf-{sid}-{when.isoformat()}",
            "sub_id": sid,
            "period": f"{when.year:04d}-{when.month:02d}",
            "amount_cents": amount,
            "reason": f"退订 {when.isoformat()}，按剩余 {remaining}/{days_total} 天退基础费",
            "event_seq": ev["seq"],
        })

    # 档位变更 3 笔（月中变更 → 账单拆区间）
    for sid, to_tier, when in [("sub-005", "pro", date(2026, 9, 12)),
                               ("sub-011", "team", date(2026, 10, 3)),
                               ("sub-021", "basic", date(2026, 8, 28))]:
        sub_events.append({
            "seq": next_seq(), "type": "tier_change", "sub_id": sid,
            "to_tier": to_tier, "at": when.isoformat(),
        })
        next(s for s in subs if s["id"] == sid)["tier"] = to_tier

    # ---- 原始用量事件 ----
    events = []
    eid = 0
    # 每个订阅的强度系数：少数重度客户制造超额
    intensity = {s["id"]: (rng.uniform(2.0, 4.0) if rng.random() < 0.22
                           else rng.uniform(0.5, 1.2)) for s in subs}
    member_factor = {}
    for s in subs:
        for m in s["members"]:
            member_factor[m] = rng.uniform(0.6, 1.4)

    def tier_at(sid, day):
        tier = None
        for e in sorted((x for x in sub_events if x["sub_id"] == sid),
                        key=lambda x: x["seq"]):
            if e["type"] == "subscribe" and to_d(e["at"]) <= day:
                tier = e["tier"]
            elif e["type"] == "tier_change" and to_d(e["at"]) <= day:
                tier = e["to_tier"]
            elif e["type"] == "unsubscribe" and to_d(e["at"]) < day:
                return None
        return tier

    def to_d(s):
        return date.fromisoformat(s)

    day = FIRST_MONTH
    while day <= AS_OF:
        weekday_boost = 1.0 if day.weekday() < 5 else 0.55
        growth = 1.0 + 0.004 * (day - FIRST_MONTH).days / 7  # 每周约 +0.4%
        for s in subs:
            tier = tier_at(s["id"], day)
            if not tier:
                continue
            prof = _PROFILE[tier]
            k = intensity[s["id"]] * weekday_boost * growth
            for m in s["members"]:
                mf = member_factor[m]
                api = int(rng.uniform(*prof["api"]) * k * mf * rng.uniform(0.7, 1.3))
                egr = round(rng.uniform(*prof["egress"]) * k * mf * rng.uniform(0.7, 1.3), 2)
                sto = round(
                    rng.uniform(*prof["storage"]) / len(s["members"])
                    * (1 + 0.001 * (day - FIRST_MONTH).days) * mf
                    * rng.uniform(0.9, 1.1),
                    1,
                )
                for metric, qty in (("api_calls", api), ("egress_gb", egr),
                                    ("storage_gb_day", sto)):
                    if qty <= 0:
                        continue
                    eid += 1
                    # 迟到用量：约 2.5% 晚到 4~30 天（不超过快照时点）
                    arrived = day + timedelta(days=1)
                    if rng.random() < 0.025 and day + timedelta(days=4) <= AS_OF:
                        arrived = min(day + timedelta(days=rng.randint(4, 30)), AS_OF)
                    events.append({
                        "id": f"u-{eid:06d}",
                        "sub_id": s["id"],
                        "customer_id": m,
                        "metric": metric,
                        "quantity": qty,
                        "occurred_at": day.isoformat(),
                        "arrived_at": arrived.isoformat(),
                    })
        day += timedelta(days=1)

    ds = {
        "meta": {
            "seed": SEED,
            "as_of": AS_OF.isoformat(),
            "generated_note": "虚构数据，仅用于计费方案分析演示",
        },
        "customers": customers,
        "subs": subs,
        "sub_events": sub_events,
        "refunds": refunds,
        "events": events,
    }
    return ds


def ensure_dataset(data_dir: Path) -> dict:
    """数据集不存在则生成并落盘；返回数据集 dict。"""
    data_dir = Path(data_dir)
    path = data_dir / "dataset.json"
    pb_path = data_dir / "price_books.json"
    if not path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        ds = generate()
        path.write_text(json.dumps(ds, ensure_ascii=False), encoding="utf-8")
        pb_path.write_text(json.dumps(PRICE_BOOKS, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        for f in ("scenarios.jsonl", "decisions.jsonl"):
            (data_dir / f).touch()
        return ds
    ds = json.loads(path.read_text(encoding="utf-8"))
    if not pb_path.exists():
        pb_path.write_text(json.dumps(PRICE_BOOKS, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    return ds

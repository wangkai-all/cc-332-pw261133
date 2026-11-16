"""计费引擎：始终从原始用量事件重放账单。

设计红线：
1. 重放（replay）：任何"换价目表重算"都必须从原始用量事件出发，
   重新计算套餐内额度与超额部分，禁止拿已汇总账单的金额直接套新单价
   （额度、阶梯、按天折算都会因此算错，见 tests 中的对照用例）。
2. 退款幂等：退订产生的退款是有唯一 id 的事实记录，入账时按 id 去重，
   同一笔退款在任何一张账单里至多抵扣一次；补计重放只产生差额行，
   不会重新触发退款。
3. 迟到用量：事件带 occurred_at（发生时间）与 arrived_at（到达时间），
   账单按 as_of 快照重放，两个快照之差即为补计调整。
"""
from __future__ import annotations

from datetime import timedelta

from .util import CLOSE_LAG_DAYS, cents_of, month_bounds, short_hash, to_date
from decimal import Decimal

METRICS = ["api_calls", "egress_gb", "storage_gb_day"]
METRIC_LABEL = {
    "api_calls": "API 调用",
    "egress_gb": "出站流量",
    "storage_gb_day": "存储",
}
METRIC_UNIT = {
    "api_calls": "次",
    "egress_gb": "GB",
    "storage_gb_day": "GB·日",
}


def charge_cents(metric: str, overage: float, tier: dict) -> int:
    """超额部分的费用（分）。api_calls 按千次计价，其余按单位量计价。"""
    if overage <= 0:
        return 0
    price = tier["unit_price_cents"][metric]
    if metric == "api_calls":
        return cents_of(Decimal(str(overage)) / 1000 * price)
    return cents_of(Decimal(str(overage)) * price)


def prorate_cents(amount_cents: int, days: int, total_days: int) -> int:
    """按天折算金额（分），四舍五入。"""
    return cents_of(Decimal(amount_cents) * days / total_days)


def active_intervals(sub_id: str, sub_events: list[dict], first, last) -> list[dict]:
    """根据订阅生命周期事件，求账期内每个资费档位生效的日期区间。

    支持：订阅（subscribe）、档位变更（tier_change）、退订（unsubscribe）。
    月中变更会拆成多个区间，分别按天折算。
    """
    evs = sorted(
        (e for e in sub_events if e["sub_id"] == sub_id),
        key=lambda e: (e["at"], e.get("seq", 0)),
    )
    raw, cur_tier, cur_start = [], None, None
    for e in evs:
        t = e["type"]
        if t == "subscribe":
            cur_tier, cur_start = e["tier"], to_date(e["at"])
        elif t == "tier_change" and cur_tier:
            raw.append((cur_tier, cur_start, to_date(e["at"]) - timedelta(days=1)))
            cur_tier, cur_start = e["to_tier"], to_date(e["at"])
        elif t == "unsubscribe" and cur_tier:
            raw.append((cur_tier, cur_start, to_date(e["at"])))
            cur_tier, cur_start = None, None
    if cur_tier:
        raw.append((cur_tier, cur_start, None))

    out = []
    for tier, s, e in raw:
        s2 = max(s, first)
        e2 = min(e, last) if e else last
        if s2 <= e2:
            out.append({"tier": tier, "start": s2, "end": e2})
    return out


def compute_bill(
    sub: dict,
    events: list[dict],
    sub_events: list[dict],
    refunds: list[dict],
    price_book: dict,
    period: str,
    as_of,
    include_events: bool = False,
) -> dict:
    """从原始事件重放某个订阅在某个账期、某份价目表下的账单。

    as_of：数据快照时点，只有 arrived_at <= as_of 的事件参与计算。
    """
    first, last = month_bounds(period)
    as_of = to_date(as_of)
    days_total = (last - first).days + 1
    intervals = active_intervals(sub["id"], sub_events, first, last)

    pevents = [
        e
        for e in events
        if e["sub_id"] == sub["id"]
        and first <= to_date(e["occurred_at"]) <= last
        and to_date(e["arrived_at"]) <= as_of
    ]

    lines: list[dict] = []
    for iv in intervals:
        tier = price_book["tiers"][iv["tier"]]
        fdays = (iv["end"] - iv["start"]).days + 1
        interval = [iv["start"].isoformat(), iv["end"].isoformat()]
        lines.append(
            {
                "kind": "base",
                "tier": iv["tier"],
                "interval": interval,
                "metric": None,
                "quantity": fdays,
                "unit": "天",
                "unit_price_cents": tier["base_fee_cents"],
                "amount_cents": prorate_cents(tier["base_fee_cents"], fdays, days_total),
                "note": f"{iv['tier']} 基础费，按 {fdays}/{days_total} 天折算",
            }
        )
        for m in METRICS:
            evs = [
                e
                for e in pevents
                if e["metric"] == m and iv["start"] <= to_date(e["occurred_at"]) <= iv["end"]
            ]
            qty = round(sum(e["quantity"] for e in evs), 4)
            included = round(tier["included"][m] * fdays / days_total, 4)
            over = round(max(0.0, qty - included), 4)
            line = {
                "kind": "usage",
                "tier": iv["tier"],
                "interval": interval,
                "metric": m,
                "quantity": qty,
                "included": included,
                "overage": over,
                "unit": METRIC_UNIT[m],
                "unit_price_cents": tier["unit_price_cents"][m],
                "amount_cents": charge_cents(m, over, tier),
                "event_count": len(evs),
                "note": f"{METRIC_LABEL[m]}：用量 {qty}{METRIC_UNIT[m]}，"
                f"额度 {included}，超额 {over}",
            }
            if include_events:
                line["event_refs"] = [e["id"] for e in evs]
            lines.append(line)

    # 退款抵扣：按退款 id 去重，同一笔退款至多入账一次（幂等）。
    seen: set[str] = set()
    for r in refunds:
        if r["sub_id"] != sub["id"] or r["period"] != period:
            continue
        if r["id"] in seen:
            raise ValueError(f"退款重复入账: {r['id']}")
        seen.add(r["id"])
        lines.append(
            {
                "kind": "credit",
                "refund_id": r["id"],
                "tier": None,
                "interval": None,
                "metric": None,
                "amount_cents": -r["amount_cents"],
                "note": r["reason"],
            }
        )

    total = sum(l["amount_cents"] for l in lines)
    bill = {
        "sub_id": sub["id"],
        "period": period,
        "price_book_id": price_book["id"],
        "as_of": as_of.isoformat(),
        "lines": lines,
        "total_cents": total,
    }
    bill["bill_hash"] = short_hash(bill)
    return bill


def _line_key(line: dict):
    if line["kind"] == "credit":
        return ("credit", line.get("refund_id"))
    return (line["kind"], line.get("metric"), line.get("tier"),
            (line.get("interval") or [None])[0])


def diff_bills(bill_a: dict, bill_b: dict) -> list[dict]:
    """两张账单逐行对齐比较，返回差额行（用于下钻一笔差额）。"""
    rows = []
    keys = list(dict.fromkeys([_line_key(l) for l in bill_a["lines"]]
                              + [_line_key(l) for l in bill_b["lines"]]))
    for k in keys:
        la = next((l for l in bill_a["lines"] if _line_key(l) == k), None)
        lb = next((l for l in bill_b["lines"] if _line_key(l) == k), None)
        ref = la or lb
        rows.append(
            {
                "kind": ref["kind"],
                "metric": ref.get("metric"),
                "tier": ref.get("tier"),
                "interval": ref.get("interval"),
                "refund_id": ref.get("refund_id"),
                "a_amount_cents": la["amount_cents"] if la else 0,
                "b_amount_cents": lb["amount_cents"] if lb else 0,
                "delta_cents": (lb["amount_cents"] if lb else 0)
                - (la["amount_cents"] if la else 0),
                "a_quantity": la.get("quantity") if la else None,
                "b_quantity": lb.get("quantity") if lb else None,
                "a_unit_price_cents": la.get("unit_price_cents") if la else None,
                "b_unit_price_cents": lb.get("unit_price_cents") if lb else None,
                "a_included": la.get("included") if la else None,
                "b_included": lb.get("included") if lb else None,
            }
        )
    return rows


def compute_adjustment(
    sub: dict,
    events: list[dict],
    sub_events: list[dict],
    refunds: list[dict],
    price_book: dict,
    period: str,
    as_of,
) -> dict:
    """补计调整：关账时点快照 vs 当前快照的差额，并列出迟到的原始事件。

    退款行在两个快照中由同一批退款事实产生（id 相同、金额相同），
    因此差额里不含退款——退款不会被重复抵扣。
    """
    first, last = month_bounds(period)
    close = last  # 账期关账时点 = 账期末 + 关账滞后
    from datetime import timedelta as _td

    close = last + _td(days=CLOSE_LAG_DAYS)
    as_of = to_date(as_of)
    bill_then = compute_bill(sub, events, sub_events, refunds, price_book, period, close)
    bill_now = compute_bill(sub, events, sub_events, refunds, price_book, period, as_of)

    late_events = [
        e
        for e in events
        if e["sub_id"] == sub["id"]
        and first <= to_date(e["occurred_at"]) <= last
        and close < to_date(e["arrived_at"]) <= as_of
    ]
    late_events = sorted(late_events, key=lambda e: (e["occurred_at"], e["id"]))

    credit_then = sorted(
        (l["refund_id"], l["amount_cents"]) for l in bill_then["lines"] if l["kind"] == "credit"
    )
    credit_now = sorted(
        (l["refund_id"], l["amount_cents"]) for l in bill_now["lines"] if l["kind"] == "credit"
    )

    return {
        "sub_id": sub["id"],
        "period": period,
        "price_book_id": price_book["id"],
        "snapshot_then": close.isoformat(),
        "snapshot_now": as_of.isoformat(),
        "total_then_cents": bill_then["total_cents"],
        "total_now_cents": bill_now["total_cents"],
        "delta_cents": bill_now["total_cents"] - bill_then["total_cents"],
        "line_diffs": diff_bills(bill_then, bill_now),
        "late_events": late_events,
        "late_event_count": len(late_events),
        "refund_not_double_counted": credit_then == credit_now,
    }

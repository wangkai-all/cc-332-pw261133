"""迁移情景试算：从原始用量重放现价与候选方案，输出收入、账单波动与实现成本。

原则：
- 试算结果（scenario）是"情景结果"，与正式决定（decision）分开存储；
- 行为假设（涨价流失、用量增长）默认关闭，开启后单独输出
  with_assumptions 区块，并显式标注"行为假设"，不得当作既定事实；
- 实现成本是静态估算值，同样标注为估算。
"""
from __future__ import annotations

import statistics

from .engine import compute_bill
from .util import short_hash

# 重放窗口：三个完整账期（11 月未结束，不参与）
WINDOW = ["2026-08", "2026-09", "2026-10"]
CURRENT_PB = "pb-2026.06"

POLICIES = {
    "grandfather": "保留老客原约定（cutoff 前签约的订阅继续用现价）",
    "unified": "统一迁移（所有订阅切到候选方案）",
}

# 实现成本：静态估算（非实测），单位：人周
IMPLEMENTATION_COST = {
    "grandfather": {
        "engineering_weeks": 10,
        "ops_weeks": 4,
        "notes": "需长期并行维护两套价目与权益判定；老客原约定要逐单留痕，"
                 "后续每次换版都要兼容存量约定",
        "estimate": True,
    },
    "unified": {
        "engineering_weeks": 5,
        "ops_weeks": 8,
        "notes": "价目单一、计费路径只有一条；但需要全量迁移、客户沟通、"
                 "账单争议与退订挽留的运营成本",
        "estimate": True,
    },
}

DEFAULT_ASSUMPTIONS = {
    "churn": {"enabled": False, "threshold_pct": 15.0, "churn_rate": 0.3},
    "usage_growth": {"enabled": False, "pct": 10.0},
}


def normalize_config(cfg: dict) -> dict:
    """补齐默认值并做基本校验，保证同一配置哈希唯一。"""
    pb = cfg.get("price_book_id")
    if pb not in ("pb-2027.a", "pb-2027.b"):
        raise ValueError(f"未知候选方案: {pb}")
    policy = cfg.get("policy")
    if policy not in POLICIES:
        raise ValueError(f"未知迁移策略: {policy}")
    cutoff = cfg.get("cutoff") or "2026-12-01"
    asm = cfg.get("assumptions") or {}
    churn = {**DEFAULT_ASSUMPTIONS["churn"], **(asm.get("churn") or {})}
    growth = {**DEFAULT_ASSUMPTIONS["usage_growth"], **(asm.get("usage_growth") or {})}
    return {
        "price_book_id": pb,
        "policy": policy,
        "cutoff": cutoff,
        "assumptions": {"churn": churn, "usage_growth": growth},
    }


def _assign_pb(policy: str, cutoff: str, sub_created: str, candidate: str) -> str:
    if policy == "unified":
        return candidate
    return candidate if sub_created >= cutoff else CURRENT_PB


def _sub_created(sub_id: str, sub_events: list[dict]) -> str:
    for e in sub_events:
        if e["sub_id"] == sub_id and e["type"] == "subscribe":
            return e["at"]
    return "9999-12-31"


def _replay(dataset, price_books, cfg, events) -> dict:
    """对窗口内每个订阅×账期，分别从原始事件重放现价与候选价账单。"""
    pb_cur = price_books[CURRENT_PB]
    pb_cand = price_books[cfg["price_book_id"]]
    as_of = dataset["meta"]["as_of"]
    per_period = {
        p: {"current_cents": 0, "candidate_cents": 0} for p in WINDOW
    }
    rows = []
    for sub in dataset["subs"]:
        created = _sub_created(sub["id"], dataset["sub_events"])
        assigned = _assign_pb(cfg["policy"], cfg["cutoff"], created, pb_cand["id"])
        pb_new = price_books[assigned]
        cur_total = new_total = 0
        for p in WINDOW:
            b_cur = compute_bill(sub, events, dataset["sub_events"],
                                 dataset["refunds"], pb_cur, p, as_of)
            b_new = compute_bill(sub, events, dataset["sub_events"],
                                 dataset["refunds"], pb_new, p, as_of)
            per_period[p]["current_cents"] += b_cur["total_cents"]
            per_period[p]["candidate_cents"] += b_new["total_cents"]
            cur_total += b_cur["total_cents"]
            new_total += b_new["total_cents"]
        if cur_total == 0 and new_total == 0:
            continue
        rows.append({
            "sub_id": sub["id"],
            "tier": sub["tier"],
            "shared": sub["shared"],
            "assigned_price_book": assigned,
            "current_cents": cur_total,
            "candidate_cents": new_total,
            "delta_cents": new_total - cur_total,
            "delta_pct": (round((new_total - cur_total) / cur_total * 100, 2)
                          if cur_total else None),
        })
    return {"per_period": per_period, "subs": rows}


def _volatility(rows: list[dict]) -> dict:
    pcts = sorted(r["delta_pct"] for r in rows if r["delta_pct"] is not None)
    if not pcts:
        return {"median_pct": 0, "p90_pct": 0, "max_abs_pct": 0,
                "share_over_20pct": 0, "sample": 0}
    p90 = pcts[min(len(pcts) - 1, int(len(pcts) * 0.9))]
    return {
        "median_pct": round(statistics.median(pcts), 2),
        "p90_pct": round(p90, 2),
        "max_abs_pct": round(max(abs(x) for x in pcts), 2),
        "share_over_20pct": round(
            sum(1 for x in pcts if abs(x) > 20) / len(pcts) * 100, 1),
        "sample": len(pcts),
    }


def run_scenario(config: dict, dataset: dict, price_books: dict) -> dict:
    """执行一次试算。config 必须先经 normalize_config 规范化。"""
    events = dataset["events"]
    base = _replay(dataset, price_books, config, events)
    rows = base["subs"]

    declared = []  # 显式声明的行为假设（非事实）
    with_asm = None
    asm = config["assumptions"]

    if asm["usage_growth"]["enabled"]:
        pct = asm["usage_growth"]["pct"]
        scaled = [dict(e, quantity=round(e["quantity"] * (1 + pct / 100), 4))
                  for e in events]
        grown = _replay(dataset, price_books, config, scaled)
        with_asm = grown
        declared.append(
            f"行为假设：用量整体 {'增长' if pct >= 0 else '下降'} {abs(pct)}%"
            "（假设，不代表客户实际行为）")

    if asm["churn"]["enabled"]:
        th = asm["churn"]["threshold_pct"]
        rate = asm["churn"]["churn_rate"]
        src = with_asm["subs"] if with_asm else rows
        churned = [r for r in src
                   if r["delta_pct"] is not None and r["delta_pct"] > th]
        loss = sum(round(r["candidate_cents"] * rate) for r in churned)
        target = with_asm if with_asm else base
        with_asm = with_asm or {"per_period": target["per_period"], "subs": rows}
        with_asm["churn_overlay"] = {
            "threshold_pct": th,
            "churn_rate": rate,
            "exposed_subs": len(churned),
            "expected_loss_cents": loss,
        }
        declared.append(
            f"行为假设：账单涨幅超过 {th}% 的客户以 {rate:.0%} 概率流失"
            "（假设，不能声称客户一定接受或离开）")

    per_period = {}
    for p, v in base["per_period"].items():
        per_period[p] = {**v, "delta_cents": v["candidate_cents"] - v["current_cents"]}
    totals = {
        "current_cents": sum(v["current_cents"] for v in base["per_period"].values()),
        "candidate_cents": sum(v["candidate_cents"] for v in base["per_period"].values()),
    }
    totals["delta_cents"] = totals["candidate_cents"] - totals["current_cents"]

    result = {
        "window": WINDOW,
        "per_period": per_period,
        "totals": totals,
        "subs": rows,
        "volatility": _volatility(rows),
        "implementation_cost": {
            **IMPLEMENTATION_COST[config["policy"]],
            "note": "实现成本为人周估算（estimate），非实测",
        },
        "declared_assumptions": declared,
        "assumptions_applied": bool(declared),
    }
    if with_asm:
        # 假设情景单独成块，绝不与实测重放混排
        wa_period = {}
        for p, v in with_asm["per_period"].items():
            wa_period[p] = {**v, "delta_cents": v["candidate_cents"] - v["current_cents"]}
        result["with_assumptions"] = {
            "per_period": wa_period,
            "churn_overlay": with_asm.get("churn_overlay"),
            "label": "以下数字依赖行为假设，仅为敏感性测算，不是预测",
        }
    return result


def config_hash(config: dict) -> str:
    return short_hash(config)

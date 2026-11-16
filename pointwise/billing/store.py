"""存储与版本绑定。

- 数据版本 data_version：原始事件 + 生命周期事件 + 退款事实的哈希；
  任何补计、修正都会改变它。
- 价目表哈希、试算配置哈希：同理。
- 试算（scenario）写入 scenarios.jsonl —— 情景结果；
  确认后的正式决定写入 decisions.jsonl —— 正式记录。两者物理分离。
- 确认（confirm）时重新核对三项版本依据，任一变化即拒绝（409）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import scenarios
from .dataset import PRICE_BOOKS
from .util import short_hash


class Conflict(Exception):
    """确认时依据已变化。"""

    def __init__(self, mismatches: list[dict]):
        super().__init__("basis changed")
        self.mismatches = mismatches


class NotFound(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.dataset = json.loads((self.dir / "dataset.json").read_text("utf-8"))
        pb_path = self.dir / "price_books.json"
        pbs = json.loads(pb_path.read_text("utf-8")) if pb_path.exists() else PRICE_BOOKS
        self.price_books = {p["id"]: p for p in pbs}
        self.scenarios_path = self.dir / "scenarios.jsonl"
        self.decisions_path = self.dir / "decisions.jsonl"
        self._scenarios = self._load_jsonl(self.scenarios_path)
        self._decisions = self._load_jsonl(self.decisions_path)
        self._data_version = None

    @staticmethod
    def _load_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text("utf-8").splitlines() if l.strip()]

    @staticmethod
    def _append_jsonl(path: Path, obj: dict):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ---- 版本 ----
    @property
    def data_version(self) -> str:
        if self._data_version is None:
            self._data_version = short_hash({
                "events": self.dataset["events"],
                "sub_events": self.dataset["sub_events"],
                "refunds": self.dataset["refunds"],
            })
        return self._data_version

    def pb_hash(self, pb_id: str) -> str:
        return short_hash(self.price_books[pb_id])

    # ---- 试算 ----
    def run_scenario(self, raw_config: dict) -> dict:
        config = scenarios.normalize_config(raw_config)
        result = scenarios.run_scenario(config, self.dataset, self.price_books)
        rec = {
            "scenario_id": f"sc-{len(self._scenarios) + 1:03d}",
            "kind": "trial",  # 情景结果，非正式记录
            "created_at": _now(),
            "config": config,
            "config_hash": scenarios.config_hash(config),
            "data_version": self.data_version,
            "price_book_id": config["price_book_id"],
            "price_book_hash": self.pb_hash(config["price_book_id"]),
            "result": result,
            "result_hash": short_hash(result),
        }
        self._scenarios.append(rec)
        self._append_jsonl(self.scenarios_path, rec)
        return rec

    def get_scenario(self, sid: str) -> dict:
        for s in self._scenarios:
            if s["scenario_id"] == sid:
                return s
        raise NotFound(sid)

    def list_scenarios(self) -> list[dict]:
        return [
            {k: s[k] for k in ("scenario_id", "kind", "created_at", "config",
                               "config_hash", "data_version", "price_book_id",
                               "price_book_hash", "result_hash")}
            | {"totals": s["result"]["totals"], "volatility": s["result"]["volatility"]}
            for s in self._scenarios
        ]

    def recompute(self, sid: str) -> dict:
        """恢复计算：按存档配置在当前数据上重放，核对结果哈希是否复现。"""
        rec = self.get_scenario(sid)
        result2 = scenarios.run_scenario(rec["config"], self.dataset, self.price_books)
        h2 = short_hash(result2)
        return {
            "scenario_id": sid,
            "status": "reproduced" if h2 == rec["result_hash"] else "drifted",
            "original_result_hash": rec["result_hash"],
            "recomputed_result_hash": h2,
            "data_version_then": rec["data_version"],
            "data_version_now": self.data_version,
            "note": ("结果完全复现" if h2 == rec["result_hash"]
                     else "结果未复现：数据或价目表版本已变化"),
        }

    # ---- 确认 → 正式记录 ----
    def confirm(self, sid: str, expected: dict, note: str = "") -> dict:
        rec = self.get_scenario(sid)
        mismatches = []
        if expected.get("data_version") != rec["data_version"]:
            mismatches.append({"field": "data_version",
                               "expected": expected.get("data_version"),
                               "scenario": rec["data_version"]})
        if expected.get("price_book_hash") != rec["price_book_hash"]:
            mismatches.append({"field": "price_book_hash",
                               "expected": expected.get("price_book_hash"),
                               "scenario": rec["price_book_hash"]})
        if expected.get("config_hash") != rec["config_hash"]:
            mismatches.append({"field": "config_hash",
                               "expected": expected.get("config_hash"),
                               "scenario": rec["config_hash"]})
        # 试算之后依据是否又发生了变化（迟到用量、价目换版）
        if self.data_version != rec["data_version"]:
            mismatches.append({"field": "data_version",
                               "expected": rec["data_version"],
                               "now": self.data_version,
                               "detail": "试算之后数据已变化（如补计用量），需重新试算"})
        if self.pb_hash(rec["price_book_id"]) != rec["price_book_hash"]:
            mismatches.append({"field": "price_book_hash",
                               "expected": rec["price_book_hash"],
                               "now": self.pb_hash(rec["price_book_id"]),
                               "detail": "价目表已换版，需重新试算"})
        if mismatches:
            raise Conflict(mismatches)
        decision = {
            "decision_id": f"dc-{len(self._decisions) + 1:03d}",
            "kind": "formal",  # 正式记录
            "scenario_id": sid,
            "confirmed_at": _now(),
            "note": note,
            "versions": {
                "data_version": rec["data_version"],
                "price_book_id": rec["price_book_id"],
                "price_book_hash": rec["price_book_hash"],
                "config_hash": rec["config_hash"],
                "result_hash": rec["result_hash"],
            },
        }
        self._decisions.append(decision)
        self._append_jsonl(self.decisions_path, decision)
        return decision

    def list_decisions(self) -> list[dict]:
        return list(self._decisions)

    # ---- 补计：注入迟到用量（演示/测试用，会改变数据版本） ----
    def inject_late_events(self, new_events: list[dict]) -> str:
        self.dataset["events"].extend(new_events)
        (self.dir / "dataset.json").write_text(
            json.dumps(self.dataset, ensure_ascii=False), encoding="utf-8")
        self._data_version = None
        return self.data_version

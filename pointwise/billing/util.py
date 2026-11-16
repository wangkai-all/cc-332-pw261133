"""通用工具：规范哈希、金额取整、账期边界。

约定：
- 所有金额一律用整数分（cents）表示，杜绝浮点误差。
- 所有需要"绑定版本"的对象（数据快照、价目表、试算配置、试算结果）
  都通过 canon() 序列化后取 sha256 短哈希作为版本号。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

# 账期正常关账滞后天数：账期结束后 3 天内到达的用量视为正常，
# 超过该时点到达的视为"补计（迟到）用量"。
CLOSE_LAG_DAYS = 3


def canon(obj) -> str:
    """规范 JSON 序列化（键排序、紧凑分隔符），用于哈希。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def short_hash(obj) -> str:
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()[:12]


def cents_of(x) -> int:
    """把数值四舍五入（ROUND_HALF_UP）为整数分。"""
    return int(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def yuan(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}¥{abs(cents) / 100:,.2f}"


def month_bounds(period: str) -> tuple[date, date]:
    """'2026-08' -> (2026-08-01, 2026-08-31)"""
    y, m = (int(x) for x in period.split("-"))
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return first, nxt - timedelta(days=1)


def to_date(s) -> date:
    return date.fromisoformat(s) if isinstance(s, str) else s

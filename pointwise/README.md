# pointwise · 计费方案分析

数据服务计费方案调整的分析环境：**虚构数据**、纯 Python 标准库 + 原生前端，
不连接任何真实支付。

## 运行

```bash
python3 run.py            # 生成虚构数据（如缺失）并启动 http://127.0.0.1:8321
python3 run.py --regen    # 重新生成数据
python3 -m unittest discover -s tests   # 18 个单元测试
```

## 设计红线（对应业务约束）

1. **从原始用量重放，不拿汇总账单套新单价**
   换价目表重算时，引擎（`billing/engine.py`）总是从原始用量事件出发，
   重新计算套餐内额度与超额。`tests/test_engine.py::test_replay_not_naive_reprice`
   证明"按单价比例折算已汇总账单"会得到错误结果（额度变化被丢失）。

2. **退款不重复抵扣**
   退订派生的退款是带唯一 id 的事实记录；入账按 id 去重，重复记录直接报错；
   补计重放只产生用量差额行，退款行在两个快照间保持一致
   （`refund_not_double_counted` 校验）。

3. **共享套餐池化**
   team 档订阅挂多个成员，用量在订阅层汇总、额度池化，超额只计一次；
   下钻时可看成员级明细。

4. **版本绑定**
   每次试算绑定：数据版本（原始事件+生命周期+退款的哈希）、价目表哈希、
   配置哈希。确认时重新核对三项依据，任一变化 → 409 拒绝并列出差异；
   "重算"可在当前数据上重放历史试算配置，验证结果哈希是否复现（恢复计算）。

5. **行为假设显式标注**
   涨价流失、用量增长等假设默认关闭；开启后结果单独成块
   （`with_assumptions`），并声明"假设，不能声称客户一定接受"。
   实现成本为人周估算，同样标注。

6. **正式记录与情景结果分离**
   试算写 `data/scenarios.jsonl`（kind=trial），确认后的正式决定写
   `data/decisions.jsonl`（kind=formal），互不混入。

## 页面功能

- **方案对比**：候选方案 × 迁移策略（保留老客原约定 / 统一迁移）试算，
  输出收入、分账期对比、客户级账单波动、实现成本估算。
- **账单重放与下钻**：任意订阅 × 账期 × 价目表重放；两版价目逐行对差，
  点击差额行下钻到驱动差额的原始事件。
- **补计用量**：迟到事件清单、账期调整（关账快照 vs 当前快照）、
  注入迟到用量演示数据版本变化。
- **版本与确认**：试算记录及绑定版本、重算复现、确认（409 依据检查）、
  正式决定列表。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/meta` | 数据版本、价目表哈希、计数 |
| GET | `/api/bills?sub_id&period&pb&include_events=1` | 账单重放 |
| GET | `/api/bills/diff?sub_id&period&from_pb&to_pb` | 两版价目逐行差额 + 原始事件 |
| GET | `/api/bills/adjustments?sub_id&period&pb` | 补计调整（关账 vs 当前快照） |
| GET | `/api/late-usage` | 迟到用量汇总 |
| POST | `/api/late-usage/inject` | 注入迟到用量（改变数据版本） |
| POST | `/api/scenarios/run` | 运行试算（绑定版本） |
| POST | `/api/scenarios/{id}/recompute` | 重算复现校验 |
| POST | `/api/scenarios/{id}/confirm` | 确认（依据核对，409 拒绝） |
| GET | `/api/decisions` | 正式决定记录 |

## 目录

```
billing/    引擎、数据生成、情景试算、存储与版本绑定、HTTP 服务
web/        分析页（原生 HTML/JS/CSS，无构建步骤）
tests/      单元测试（重放正确性、退款幂等、版本绑定、假设标注）
data/       运行时生成的虚构数据与记录（已 gitignore）
```

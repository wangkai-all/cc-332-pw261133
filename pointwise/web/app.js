/* pointwise 分析页前端（无框架，直连 JSON API） */
"use strict";

const S = { meta: null, subs: [], pbs: [], scenarios: [], decisions: [] };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtC = (c) => (c < 0 ? "-" : "") + "¥" + (Math.abs(c) / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = (p) => (p === null || p === undefined) ? "—" : (p > 0 ? "+" : "") + p + "%";
const deltaCls = (c) => c > 0 ? "pos" : c < 0 ? "neg" : "";
const METRIC_LABEL = { api_calls: "API 调用", egress_gb: "出站流量", storage_gb_day: "存储" };
const METRIC_UNIT = { api_calls: "次", egress_gb: "GB", storage_gb_day: "GB·日" };

async function api(path, opts) {
  const r = await fetch(path, opts && {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts),
  });
  const body = await r.json();
  if (!r.ok) { const e = new Error(body.error || r.statusText); e.status = r.status; e.body = body; throw e; }
  return body;
}

/* ---------- 通用渲染 ---------- */
function cards(items) {
  return `<div class="cards">${items.map(([k, v, cls]) =>
    `<div class="card"><div class="k">${esc(k)}</div><div class="v ${cls || ""}">${v}</div></div>`).join("")}</div>`;
}

function subLabel(s) {
  return `${s.id} · ${s.name}${s.shared ? "（共享 " + s.members.length + " 人）" : ""}`;
}

/* ---------- 初始化 ---------- */
async function init() {
  [S.meta, S.subs, S.pbs] = await Promise.all([
    api("/api/meta"), api("/api/subscriptions"), api("/api/price-books"),
  ]);
  renderMeta();
  fillSelects();
  bindTabs();
  bindCompare();
  bindBills();
  bindLate();
  await refreshVersions();
}

function renderMeta() {
  const m = S.meta, c = m.counts;
  $("metaBar").innerHTML =
    `数据版本 <code>${m.data_version}</code> · 快照 ${m.as_of} · ` +
    `${c.subscriptions} 订阅 / ${c.usage_events.toLocaleString()} 用量事件 / ` +
    `${c.late_events} 迟到 / ${c.refunds} 退款 · 重放窗口 ${m.window.join("、")}`;
}

function fillSelects() {
  const pbOpts = S.pbs.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  const candOpts = S.pbs.filter((p) => p.status === "candidate")
    .map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $("cmpPb").innerHTML = candOpts;
  $("billPb").innerHTML = pbOpts;
  $("billPb2").innerHTML = pbOpts;
  $("billPb2").selectedIndex = 1;
  const subOpts = S.subs.map((s) => `<option value="${s.id}">${esc(subLabel(s))}</option>`).join("");
  $("billSub").innerHTML = subOpts;
  $("injSub").innerHTML = subOpts;
  const periods = [];
  for (let m = 6; m <= 11; m++) periods.push(`2026-${String(m).padStart(2, "0")}`);
  $("billPeriod").innerHTML = periods.map((p) =>
    `<option ${p === "2026-10" ? "selected" : ""}>${p}</option>`).join("");
}

function bindTabs() {
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll(".tabs button").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      $("tab-" + b.dataset.tab).classList.add("active");
      if (b.dataset.tab === "versions") refreshVersions();
      if (b.dataset.tab === "late") loadLate();
    };
  });
}

/* ---------- 方案对比 ---------- */
function bindCompare() {
  $("btnRun").onclick = async () => {
    $("btnRun").disabled = true;
    $("runHint").textContent = "重放中…";
    try {
      const cfg = {
        price_book_id: $("cmpPb").value,
        policy: $("cmpPolicy").value,
        cutoff: $("cmpCutoff").value,
        assumptions: {
          churn: {
            enabled: $("asmChurn").checked,
            threshold_pct: +$("asmChurnTh").value,
            churn_rate: +$("asmChurnRate").value / 100,
          },
          usage_growth: {
            enabled: $("asmGrowth").checked,
            pct: +$("asmGrowthPct").value,
          },
        },
      };
      const rec = await api("/api/scenarios/run", cfg);
      renderScenarioResult(rec);
      $("runHint").textContent = `已存为试算 ${rec.scenario_id}（绑定数据版本 ${rec.data_version}）`;
    } catch (e) {
      $("runHint").textContent = "失败：" + e.message;
    } finally {
      $("btnRun").disabled = false;
    }
  };
}

function renderScenarioResult(rec) {
  const r = rec.result, t = r.totals;
  const pbName = (S.pbs.find((p) => p.id === rec.price_book_id) || {}).name || rec.price_book_id;
  const policyName = rec.config.policy === "unified" ? "统一迁移" : "保留老客原约定";
  let html = `<div class="panel"><h2>${esc(pbName)} × ${policyName}
    <span class="tag">试算 ${rec.scenario_id} · 数据版本 <span class="mono">${rec.data_version}</span></span></h2>`;
  html += cards([
    ["现价收入（3 期）", fmtC(t.current_cents)],
    ["候选收入（3 期）", fmtC(t.candidate_cents)],
    ["收入差额", `<span class="${deltaCls(t.delta_cents)}">${fmtC(t.delta_cents)}</span>`],
    ["波动中位数", fmtPct(r.volatility.median_pct)],
    ["P90 波动", fmtPct(r.volatility.p90_pct)],
    ["|Δ|>20% 客户占比", r.volatility.share_over_20pct + "%"],
  ]);

  // 分账期对比条
  const maxV = Math.max(...r.window.map((p) =>
    Math.max(r.per_period[p].current_cents, r.per_period[p].candidate_cents)));
  html += `<h3>分账期收入（重放原始用量）</h3><div class="legend">
    <i style="background:#9db8e8"></i>现价 <i style="background:#2f6fed"></i>候选</div><div class="bars">`;
  for (const p of r.window) {
    const v = r.per_period[p];
    html += `<div class="bar-row"><span>${p}</span>
      <div class="bar-track"><div class="bar-fill cur" style="width:${v.current_cents / maxV * 100}%"></div></div>
      <span class="bar-val">${fmtC(v.current_cents)}</span></div>
      <div class="bar-row"><span></span>
      <div class="bar-track"><div class="bar-fill new" style="width:${v.candidate_cents / maxV * 100}%"></div></div>
      <span class="bar-val">${fmtC(v.candidate_cents)} <span class="${deltaCls(v.delta_cents)}">(${fmtC(v.delta_cents)})</span></span></div>`;
  }
  html += `</div>`;

  // 客户级波动
  const rows = [...r.subs].sort((a, b) => Math.abs(b.delta_cents) - Math.abs(a.delta_cents));
  html += `<h3 class="section-gap">客户级影响（按 |差额| 排序，共 ${rows.length} 户）</h3>
    <table><thead><tr><th>订阅</th><th>档</th><th>适用价目</th>
    <th class="num">现价</th><th class="num">候选</th><th class="num">差额</th><th class="num">幅度</th></tr></thead><tbody>`;
  for (const s of rows.slice(0, 20)) {
    html += `<tr><td class="mono">${s.sub_id}${s.shared ? ' <span class="badge">共享</span>' : ""}</td>
      <td>${s.tier}</td><td class="mono">${s.assigned_price_book}</td>
      <td class="num">${fmtC(s.current_cents)}</td><td class="num">${fmtC(s.candidate_cents)}</td>
      <td class="num ${deltaCls(s.delta_cents)}">${fmtC(s.delta_cents)}</td>
      <td class="num ${deltaCls(s.delta_cents)}">${fmtPct(s.delta_pct)}</td></tr>`;
  }
  html += `</tbody></table>${rows.length > 20 ? `<p class="hint">仅显示前 20 户</p>` : ""}`;

  // 实现成本（估算）
  const c = r.implementation_cost;
  html += `<h3 class="section-gap">实现成本 <span class="tag">估算值</span></h3>
    <p>工程 ${c.engineering_weeks} 人周 · 运营 ${c.ops_weeks} 人周 — ${esc(c.notes)}<br>
    <span class="hint">${esc(c.note)}</span></p>`;

  // 行为假设
  if (r.assumptions_applied) {
    html += `<div class="assume-out"><h3>⚠ 行为假设情景（敏感性测算，不是预测）</h3>
      <ul>${r.declared_assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>`;
    const wa = r.with_assumptions;
    if (wa) {
      html += `<table><thead><tr><th>账期</th><th class="num">现价</th><th class="num">候选（含假设）</th><th class="num">差额</th></tr></thead><tbody>`;
      for (const p of r.window) {
        const v = wa.per_period[p];
        html += `<tr><td>${p}</td><td class="num">${fmtC(v.current_cents)}</td>
          <td class="num">${fmtC(v.candidate_cents)}</td>
          <td class="num ${deltaCls(v.delta_cents)}">${fmtC(v.delta_cents)}</td></tr>`;
      }
      html += `</tbody></table>`;
      if (wa.churn_overlay) {
        const o = wa.churn_overlay;
        html += `<p class="hint">流失假设覆盖 ${o.exposed_subs} 户，预期收入损失 ${fmtC(o.expected_loss_cents)}（假设口径）</p>`;
      }
      html += `<p class="hint">${esc(wa.label)}</p>`;
    }
    html += `</div>`;
  } else {
    html += `<p class="hint">未启用行为假设：以上全部为原始用量的确定性重放。</p>`;
  }
  html += `</div>`;
  $("cmpResult").innerHTML = html;
}

/* ---------- 账单重放与下钻 ---------- */
function bindBills() {
  $("billDiffMode").onchange = () => {
    document.querySelectorAll(".diff-only").forEach((x) => x.hidden = !$("billDiffMode").checked);
  };
  $("btnBill").onclick = () => ($("billDiffMode").checked ? loadDiff() : loadBill());
}

async function loadBill() {
  const q = `sub_id=${$("billSub").value}&period=${$("billPeriod").value}&pb=${$("billPb").value}&include_events=1`;
  const b = await api("/api/bills?" + q);
  let html = `<div class="panel"><h2>账单 ${b.sub_id} · ${b.period} · <span class="mono">${b.price_book_id}</span>
    <span class="tag">as-of ${b.as_of} · 账单哈希 <span class="mono">${b.bill_hash}</span></span></h2>`;
  html += cards([["合计", fmtC(b.total_cents)], ["行项目", b.lines.length],
    ["原始事件", (b.events || []).length]]);
  html += `<table><thead><tr><th>类型</th><th>指标</th><th>区间</th>
    <th class="num">用量/额度/超额</th><th class="num">单价(分)</th><th class="num">金额</th><th>说明</th></tr></thead><tbody>`;
  b.lines.forEach((l, i) => {
    const qty = l.kind === "usage" ? `${l.quantity}/${l.included}/${l.overage}` :
      l.kind === "base" ? `${l.quantity} 天` : "—";
    html += `<tr class="${l.kind === "usage" ? "clickable" : ""}" data-line="${i}">
      <td>${l.kind === "base" ? "基础费" : l.kind === "usage" ? "用量" : "退款抵扣"}</td>
      <td>${l.metric ? METRIC_LABEL[l.metric] : "—"}</td>
      <td class="mono">${l.interval ? l.interval.join(" ~ ") : "—"}</td>
      <td class="num">${qty}</td><td class="num">${l.unit_price_cents ?? "—"}</td>
      <td class="num ${deltaCls(l.amount_cents)}">${fmtC(l.amount_cents)}</td>
      <td>${esc(l.note || "")}${l.event_count ? ` <span class="badge">${l.event_count} 事件</span>` : ""}</td></tr>`;
  });
  html += `</tbody></table><div id="drill"></div></div>`;
  $("billResult").innerHTML = html;
  document.querySelectorAll("#billResult tr.clickable").forEach((tr) => {
    tr.onclick = () => {
      const l = b.lines[+tr.dataset.line];
      renderDrill(b.events.filter((e) => e.metric === l.metric &&
        e.occurred_at >= l.interval[0] && e.occurred_at <= l.interval[1]),
        `原始事件 · ${METRIC_LABEL[l.metric]} · ${l.interval.join(" ~ ")}`);
    };
  });
}

async function loadDiff() {
  const q = `sub_id=${$("billSub").value}&period=${$("billPeriod").value}&from_pb=${$("billPb").value}&to_pb=${$("billPb2").value}`;
  const d = await api("/api/bills/diff?" + q);
  let html = `<div class="panel"><h2>差额下钻 ${d.sub_id} · ${d.period}
    <span class="tag mono">${d.from.price_book_id} → ${d.to.price_book_id}</span></h2>`;
  html += cards([
    [`${d.from.price_book_id} 合计`, fmtC(d.from.total_cents)],
    [`${d.to.price_book_id} 合计`, fmtC(d.to.total_cents)],
    ["总差额", `<span class="${deltaCls(d.delta_cents)}">${fmtC(d.delta_cents)}</span>`],
  ]);
  html += `<p class="hint">点击用量行可下钻到驱动该差额的原始事件。</p>
    <table><thead><tr><th>类型</th><th>指标</th><th>区间</th>
    <th class="num">旧单价</th><th class="num">新单价</th><th class="num">旧额度</th><th class="num">新额度</th>
    <th class="num">旧金额</th><th class="num">新金额</th><th class="num">差额</th></tr></thead><tbody>`;
  d.line_diffs.forEach((r, i) => {
    html += `<tr class="${r.kind === "usage" ? "clickable" : ""}" data-line="${i}">
      <td>${r.kind === "base" ? "基础费" : r.kind === "usage" ? "用量" : "退款抵扣"}</td>
      <td>${r.metric ? METRIC_LABEL[r.metric] : "—"}</td>
      <td class="mono">${r.interval ? r.interval.join(" ~ ") : "—"}</td>
      <td class="num">${r.a_unit_price_cents ?? "—"}</td><td class="num">${r.b_unit_price_cents ?? "—"}</td>
      <td class="num">${r.a_included ?? "—"}</td><td class="num">${r.b_included ?? "—"}</td>
      <td class="num">${fmtC(r.a_amount_cents)}</td><td class="num">${fmtC(r.b_amount_cents)}</td>
      <td class="num ${deltaCls(r.delta_cents)}">${fmtC(r.delta_cents)}</td></tr>`;
  });
  html += `</tbody></table><div id="drill"></div></div>`;
  $("billResult").innerHTML = html;
  document.querySelectorAll("#billResult tr.clickable").forEach((tr) => {
    tr.onclick = () => {
      const r = d.line_diffs[+tr.dataset.line];
      renderDrill(d.events.filter((e) => e.metric === r.metric &&
        (!r.interval || (e.occurred_at >= r.interval[0] && e.occurred_at <= r.interval[1]))),
        `驱动差额的原始事件 · ${METRIC_LABEL[r.metric]} · 差额 ${fmtC(r.delta_cents)}`);
    };
  });
}

function renderDrill(events, title) {
  const byMember = {};
  for (const e of events) byMember[e.customer_id] = (byMember[e.customer_id] || 0) + e.quantity;
  let html = `<div class="drill"><h3>${esc(title)}（${events.length} 条）</h3>
    <p class="hint">成员小计：${Object.entries(byMember).map(([m, q]) =>
      `<span class="mono">${m}</span> ${Math.round(q * 100) / 100}`).join(" · ")}</p>
    <table><thead><tr><th>事件</th><th>成员</th><th class="num">数量</th>
    <th>发生</th><th>到达</th><th></th></tr></thead><tbody>`;
  for (const e of events.slice(0, 120)) {
    const late = (new Date(e.arrived_at) - new Date(e.occurred_at)) / 86400000 > 3;
    html += `<tr><td class="mono">${e.id}</td><td class="mono">${e.customer_id}</td>
      <td class="num">${e.quantity}</td><td class="mono">${e.occurred_at}</td>
      <td class="mono">${e.arrived_at}</td>
      <td>${late ? '<span class="badge late">补计</span>' : ""}</td></tr>`;
  }
  html += `</tbody></table>${events.length > 120 ? `<p class="hint">仅显示前 120 条</p>` : ""}</div>`;
  $("drill").innerHTML = html;
  $("drill").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ---------- 补计用量 ---------- */
function bindLate() {
  $("btnInject").onclick = async () => {
    try {
      const r = await api("/api/late-usage/inject", {
        sub_id: $("injSub").value,
        metric: $("injMetric").value,
        quantity: +$("injQty").value,
        occurred_at: $("injDate").value,
      });
      $("injResult").innerHTML = `<p class="warn">已注入 ${r.event.id}，
        新数据版本 <code>${r.data_version}</code>。此前的试算若现在确认将被版本检查拒绝。</p>`;
      S.meta = await api("/api/meta");
      renderMeta();
      loadLate();
    } catch (e) {
      $("injResult").innerHTML = `<p class="mismatch">失败：${esc(e.message)}</p>`;
    }
  };
}

async function loadLate() {
  const d = await api("/api/late-usage");
  $("lateSummary").innerHTML = cards([
    ["迟到事件总数", d.total_late], ["受影响 订阅×账期", d.impacted.length],
  ]);
  let html = `<table><thead><tr><th>订阅</th><th>账期</th>
    <th class="num">迟到事件</th><th class="num">最大延迟(天)</th><th></th></tr></thead><tbody>`;
  for (const x of d.impacted) {
    html += `<tr><td class="mono">${x.sub_id}</td><td>${x.period}</td>
      <td class="num">${x.late_count}</td><td class="num">${x.max_delay_days}</td>
      <td><button class="small" data-sub="${x.sub_id}" data-period="${x.period}">查看调整</button></td></tr>`;
  }
  html += `</tbody></table>`;
  $("lateImpacted").innerHTML = html;
  $("lateImpacted").querySelectorAll("button").forEach((b) => {
    b.onclick = () => loadAdjustment(b.dataset.sub, b.dataset.period);
  });
}

async function loadAdjustment(subId, period) {
  const a = await api(`/api/bills/adjustments?sub_id=${subId}&period=${period}&pb=pb-2026.06`);
  let html = `<div class="panel"><h2>补计调整 ${a.sub_id} · ${a.period}
    <span class="tag">关账快照 ${a.snapshot_then} → 当前快照 ${a.snapshot_now}</span></h2>`;
  html += cards([
    ["关账时账单", fmtC(a.total_then_cents)],
    ["补计后账单", fmtC(a.total_now_cents)],
    ["调整差额", `<span class="${deltaCls(a.delta_cents)}">${fmtC(a.delta_cents)}</span>`],
    ["迟到事件", a.late_event_count],
    ["退款未重复抵扣", a.refund_not_double_counted
      ? '<span class="badge ok">已验证</span>' : '<span class="badge drift">异常</span>'],
  ]);
  const diffs = a.line_diffs.filter((r) => r.delta_cents !== 0);
  if (diffs.length) {
    html += `<table><thead><tr><th>指标</th><th class="num">关账时</th>
      <th class="num">补计后</th><th class="num">差额</th></tr></thead><tbody>`;
    for (const r of diffs) {
      html += `<tr><td>${r.metric ? METRIC_LABEL[r.metric] : r.kind}</td>
        <td class="num">${fmtC(r.a_amount_cents)}</td><td class="num">${fmtC(r.b_amount_cents)}</td>
        <td class="num ${deltaCls(r.delta_cents)}">${fmtC(r.delta_cents)}</td></tr>`;
    }
    html += `</tbody></table>`;
  } else {
    html += `<p class="hint">迟到事件未改变金额（用量仍在套餐额度内）。</p>`;
  }
  html += `<h3 class="section-gap">迟到的原始事件</h3>
    <table><thead><tr><th>事件</th><th>指标</th><th class="num">数量</th>
    <th>发生</th><th>到达</th></tr></thead><tbody>`;
  for (const e of a.late_events) {
    html += `<tr><td class="mono">${e.id}</td><td>${METRIC_LABEL[e.metric]}</td>
      <td class="num">${e.quantity} ${METRIC_UNIT[e.metric]}</td>
      <td class="mono">${e.occurred_at}</td><td class="mono">${e.arrived_at}</td></tr>`;
  }
  html += `</tbody></table></div>`;
  $("adjResult").innerHTML = html;
  $("adjResult").scrollIntoView({ behavior: "smooth" });
}

/* ---------- 版本与确认 ---------- */
async function refreshVersions() {
  [S.scenarios, S.decisions] = await Promise.all([
    api("/api/scenarios"), api("/api/decisions")]);
  renderScenarios();
  renderDecisions();
}

function renderScenarios() {
  if (!S.scenarios.length) {
    $("scenarioList").innerHTML = `<p class="hint">还没有试算。到「方案对比」运行一次。</p>`;
    return;
  }
  let html = `<table><thead><tr><th>试算</th><th>配置</th><th>数据版本</th>
    <th>价目哈希</th><th>配置哈希</th><th class="num">收入差额</th><th>操作</th></tr></thead><tbody>`;
  for (const s of [...S.scenarios].reverse()) {
    const dvNow = S.meta.data_version;
    const stale = s.data_version !== dvNow;
    html += `<tr><td class="mono">${s.scenario_id}</td>
      <td class="mono">${s.config.price_book_id} · ${s.config.policy}</td>
      <td class="mono">${s.data_version}${stale ? ' <span class="badge drift">已过期</span>' : ""}</td>
      <td class="mono">${s.price_book_hash}</td><td class="mono">${s.config_hash}</td>
      <td class="num ${deltaCls(s.totals.delta_cents)}">${fmtC(s.totals.delta_cents)}</td>
      <td><button class="small" data-act="recompute" data-id="${s.scenario_id}">重算</button>
          <button class="small" data-act="confirm" data-id="${s.scenario_id}">确认</button></td></tr>`;
  }
  html += `</tbody></table><div id="versionMsg"></div>`;
  $("scenarioList").innerHTML = html;
  $("scenarioList").querySelectorAll("button").forEach((b) => {
    b.onclick = () => scenarioAction(b.dataset.act, b.dataset.id);
  });
}

async function scenarioAction(act, sid) {
  const msg = $("versionMsg");
  try {
    if (act === "recompute") {
      const r = await api(`/api/scenarios/${sid}/recompute`, {});
      msg.innerHTML = r.status === "reproduced"
        ? `<p><span class="badge ok">复现成功</span> ${sid} 重算结果哈希一致（${r.recomputed_result_hash}），计算可恢复。</p>`
        : `<p><span class="badge drift">结果漂移</span> ${sid} 重算哈希 ${r.recomputed_result_hash} ≠ 原 ${r.original_result_hash}；数据版本 ${r.data_version_then} → ${r.data_version_now}，需重新试算。</p>`;
    } else {
      const s = S.scenarios.find((x) => x.scenario_id === sid);
      const r = await api(`/api/scenarios/${sid}/confirm`, {
        expected: {
          data_version: s.data_version,
          price_book_hash: s.price_book_hash,
          config_hash: s.config_hash,
        },
        note: "分析页确认",
      });
      msg.innerHTML = `<p><span class="badge ok">已确认</span> 生成正式记录 ${r.decision_id}（与情景结果分开存储）。</p>`;
      S.decisions = await api("/api/decisions");
      renderDecisions();
    }
  } catch (e) {
    if (e.status === 409) {
      msg.innerHTML = `<p class="mismatch"><b>确认被拒绝（依据已变化）：</b></p><ul>` +
        e.body.mismatches.map((m) =>
          `<li class="mismatch">${m.field}：期望 <code>${m.expected}</code>` +
          (m.now ? `，当前 <code>${m.now}</code>` : "") +
          (m.detail ? ` — ${esc(m.detail)}` : "") + `</li>`).join("") + `</ul>`;
    } else {
      msg.innerHTML = `<p class="mismatch">失败：${esc(e.message)}</p>`;
    }
  }
}

function renderDecisions() {
  if (!S.decisions.length) {
    $("decisionList").innerHTML = `<p class="hint">暂无正式决定。</p>`;
    return;
  }
  let html = `<table><thead><tr><th>决定</th><th>来源试算</th><th>确认时间</th>
    <th>绑定版本</th><th>备注</th></tr></thead><tbody>`;
  for (const d of S.decisions) {
    html += `<tr><td class="mono">${d.decision_id}</td><td class="mono">${d.scenario_id}</td>
      <td class="mono">${d.confirmed_at}</td>
      <td class="mono">data:${d.versions.data_version} pb:${d.versions.price_book_id}@${d.versions.price_book_hash}</td>
      <td>${esc(d.note || "")}</td></tr>`;
  }
  html += `</tbody></table>`;
  $("decisionList").innerHTML = html;
}

init().catch((e) => { $("metaBar").textContent = "加载失败：" + e.message; });

/* 字体资产许可与嵌入合规门禁 — 前端逻辑（原生 JavaScript，依赖 app.js 的 api/esc） */
"use strict";

const SCENARIO_LABELS = { web: "Web", desktop: "桌面", print: "印刷" };
const FSTYPE_FLAG_LABELS_ZH = {
  restricted: "禁止嵌入（受限）",
  "preview-print-only": "仅预览/印刷（不可编辑）",
  editable: "可编辑嵌入",
  "no-subsetting": "禁止子集化",
  "bitmap-only": "仅允许位图嵌入",
};
const FIELD_LABELS = {
  license_description: "许可描述（nameID 13）",
  license_url: "许可 URL（nameID 14）",
};
const POLICY_BOOL_FIELDS = [
  ["deny_on_fstype_reserved_bits", "保留位置位即拒绝"],
  ["deny_on_embedding_flags_conflict", "嵌入权限类型互斥/保留值（1/3/6/…）即拒绝"],
  ["deny_on_ttc_member_license_mismatch", "TTC 成员许可不一致即拒绝"],
  ["deny_on_incomplete_collection_evidence", "集合/解包证据缺失即拒绝"],
  ["require_https_license_url", "许可 URL 必须为 https"],
];
const FINDING_LABELS = {
  "font-digest-mismatch": "字体摘要不匹配",
  "evidence-incomplete-collection": "集合/解包证据缺失",
  "fstype-missing": "缺少 OS/2 fsType",
  "fstype-reserved-bits": "fsType 保留位置位",
  "fstype-embedding-flags-conflict": "fsType 嵌入权限位互斥冲突",
  "fstype-mask-not-allowed": "fsType 位掩码不被允许",
  "fstype-bit-forbidden": "fsType 含禁止位",
  "name-field-missing": "必需 name 许可字段缺失",
  "name-field-bad-url": "许可 URL 不合规",
  "license-text-forbidden": "许可文本命中禁止模式",
  "fstype-text-conflict": "fsType 与许可文本冲突",
  "ttc-member-license-mismatch": "TTC 成员许可冲突",
  "stack-license-conflict": "字体栈许可组合冲突",
};

const gate = {
  scenario: "web",
  policies: [],
  stack: [],
  fonts: [],
  manifest: null,
  frozen: false,
  decisionId: null,
};

const g$ = (sel) => document.querySelector(sel);

/* ------------------------------------------------------------- 场景切换 */

document.querySelectorAll(".scenario-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".scenario-btn").forEach((b) =>
      b.classList.toggle("active", b === btn));
    gate.scenario = btn.dataset.scenario;
    loadGateScenario();
  });
});

/* ------------------------------------------------------------- 数据加载 */

async function loadGateScenario() {
  gate.stack = [];
  gate.manifest = null;
  g$("#gate-result").hidden = true;
  g$("#results-empty").hidden = false;
  g$("#results-content").hidden = true;
  await Promise.all([loadGatePolicies(), loadGateFonts(),
                     loadGateExemptions(), loadGateDecisions()]);
  renderGateStack();
  showGateEmptyHint();
}

async function loadGatePolicies() {
  gate.policies = await api(
    `/api/license/policies?scenario=${gate.scenario}`);
  renderGatePolicy();
  const active = gate.policies.find((p) => p.active);
  if (active) {
    g$("#np-expected").value = active.version;
    fillPolicyEditor(active.policy);
  }
}

async function loadGateFonts() {
  gate.fonts = await api("/api/fonts");
  renderGatePicker();
  renderExemptionFontSelect();
}

async function loadGateExemptions() {
  const list = await api(
    `/api/license/exemptions?scenario=${gate.scenario}&include_revoked=true`);
  const box = g$("#gate-exemptions");
  if (!list.length) {
    box.innerHTML = `<p class="hint">该场景尚无豁免。</p>`;
    return;
  }
  const now = Date.now();
  box.innerHTML = list.map((e) => {
    const expired = e.expires_at && new Date(e.expires_at).getTime() <= now;
    const cls = e.revoked ? "revoked" : expired ? "expired" : "active";
    const state = e.revoked ? "已撤销" : expired ? "已过期" : "有效";
    const f = gate.fonts.find((x) => x.sha256 === e.font_digest);
    return `<div class="ex-card ${cls}">
      <div>
        <b>${esc(state)}</b>
        v${e.policy_version} · ${esc((f && f.family) || e.font_digest.slice(0, 12))}
        <small class="mono">${esc(e.rule_codes.join(", "))}</small>
        <div class="hint">${esc(e.justification)}</div>
        <div class="meta mono">
          ${e.expires_at ? "到期 " + esc(e.expires_at) + " · " : "长期 · "}
          ${esc(e.requested_by)} · #${e.id}
          ${e.applies_to_stack_conflict ? " · 可覆盖栈冲突" : ""}
        </div>
      </div>
      ${(e.revoked ? "" :
         `<button class="danger small" data-act="revoke" data-id="${e.id}">撤销</button>`)}
    </div>`;
  }).join("");
}

g$("#gate-exemptions").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act=revoke]");
  if (!btn) return;
  if (!confirm("撤销该豁免？撤销后所有评估将不再放行对应违规。")) return;
  await api(`/api/license/exemptions/${btn.dataset.id}/revoke`,
            { method: "POST" });
  await loadGateExemptions();
});

async function loadGateDecisions() {
  const list = await api(`/api/license/decisions?scenario=${gate.scenario}`);
  const box = g$("#gate-decisions");
  if (!list.length) {
    box.innerHTML = `<p class="hint">尚无冻结决定。</p>`;
    return;
  }
  box.innerHTML = list.map((d) => `
    <div class="decision-card" data-id="${d.id}" style="cursor:pointer">
      <b>#${d.id}</b>
      <span class="mono taglike">${esc(SCENARIO_LABELS[d.scenario])} v${d.policy_version}</span>
      <span class="mono taglike">${d.font_stack.length} 字体</span>
      <span class="mono" title="${d.fingerprint}">fp ${esc(d.fingerprint.slice(0, 12))}…</span>
      <span class="meta mono">${esc(d.created_at)}${
        d.request_id ? " · req=" + esc(d.request_id) : ""}</span>
      <span class="btns">
        <button data-act="load" data-id="${d.id}">复核</button>
        <button data-act="export" data-id="${d.id}">导出 JSON</button>
      </span>
    </div>`).join("");
}

g$("#gate-decisions").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const id = btn.dataset.id;
  if (btn.dataset.act === "export") {
    window.location.href = `/api/license/decisions/${id}/export`;
  } else if (btn.dataset.act === "load") {
    const rec = await api(`/api/license/decisions/${id}`);
    showGateManifest(rec.manifest, true, id);
  }
});

/* ------------------------------------------------------------- 策略展示/编辑 */

function renderGatePolicy() {
  const box = g$("#gate-policy");
  if (!gate.policies.length) {
    box.innerHTML = `<p class="hint">该场景尚未登记任何策略版本。</p>`;
    return;
  }
  const versions = gate.policies.slice().reverse();
  box.innerHTML = versions.map((p) => `
    <details class="policy-version ${p.active ? "active" : ""}" ${p.active ? "open" : ""}>
      <summary>
        <b>v${p.version}</b> ${p.active ? '<span class="pbadge">生效中</span>' : ""}
        <span class="meta mono">${esc(p.created_at)} · ${esc(p.registered_by)}</span>
        ${p.active ? "" : `<button type="button" class="small" data-act="activate"
             data-v="${p.version}">CAS 激活</button>`}
      </summary>
      <pre class="policy-json">${esc(JSON.stringify(p.policy, null, 2))}</pre>
    </details>`).join("");
}

g$("#gate-policy").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act=activate]");
  if (!btn) return;
  const active = gate.policies.find((p) => p.active);
  const expected = active ? active.version : 0;
  if (!confirm(`以 CAS 基准 ${expected} 激活 v${btn.dataset.v}？`)) return;
  try {
    await api(`/api/license/policies/${gate.scenario}/versions/${btn.dataset.v}/activate`,
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_active_version: expected }) });
    g$("#gate-msg").className = "msg ok";
    g$("#gate-msg").textContent = "已激活";
    await loadGatePolicies();
  } catch (err) { alert(err.message); }
});

function fillPolicyEditor(p) {
  g$("#np-masks").value = (p.allowed_fstype_masks || [])
    .map((m) => "0x" + m.toString(16).padStart(4, "0")).join(", ");
  g$("#np-bits").value = (p.forbidden_fstype_bits || [])
    .map((m) => "0x" + m.toString(16).padStart(4, "0")).join(", ");
  g$("#np-patterns").value = (p.forbidden_license_patterns || []).join("\n");
  g$("#np-rules").value = JSON.stringify(p.conflict_rules || [], null, 1);
  g$("#np-flags").innerHTML = POLICY_BOOL_FIELDS.map(([key, label]) => `
    <label class="chk"><input type="checkbox" data-k="${key}"
      ${p[key] ? "checked" : ""}> ${label}</label>`).join("");
  g$("#np-namefields").innerHTML = Object.keys(FIELD_LABELS).map((k) => `
    <label class="chk"><input type="checkbox" data-k="${k}"
      ${(p.required_name_fields || []).includes(k) ? "checked" : ""}>
      ${FIELD_LABELS[k]}</label>`).join("");
}

function parseHexList(text) {
  return text.split(",").map((s) => s.trim()).filter(Boolean)
    .map((s) => {
      const n = parseInt(s, s.toLowerCase().startsWith("0x") ? 16 : 10);
      if (Number.isNaN(n)) throw new Error(`无法解析整数: ${s}`);
      return n;
    });
}

function buildPolicyFromEditor() {
  const flags = {};
  g$("#np-flags").querySelectorAll("input[data-k]").forEach((el) => {
    flags[el.dataset.k] = el.checked;
  });
  const nameFields = [...g$("#np-namefields").querySelectorAll("input[data-k]")]
    .filter((el) => el.checked).map((el) => el.dataset.k);
  let rules = [];
  const rulesText = g$("#np-rules").value.trim();
  if (rulesText) {
    rules = JSON.parse(rulesText);
    if (!Array.isArray(rules)) throw new Error("conflict_rules 必须是数组");
  }
  return {
    name: `${SCENARIO_LABELS[gate.scenario]}策略`,
    allowed_fstype_masks: parseHexList(g$("#np-masks").value),
    forbidden_fstype_bits: parseHexList(g$("#np-bits").value),
    required_name_fields: nameFields,
    forbidden_license_patterns: g$("#np-patterns").value.split("\n")
      .map((s) => s.trim()).filter(Boolean),
    conflict_rules: rules,
    ...flags,
  };
}

async function submitNewPolicy(activate) {
  const msg = g$("#np-msg");
  msg.className = "msg";
  let body;
  try {
    body = {
      scenario: gate.scenario,
      expected_active_version: parseInt(g$("#np-expected").value, 10) || 0,
      registered_by: g$("#np-by").value.trim() || "admin",
      activate,
      policy: buildPolicyFromEditor(),
    };
  } catch (err) {
    msg.className = "msg error"; msg.textContent = "编辑失败：" + err.message;
    return;
  }
  try {
    const r = await api("/api/license/policies", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    msg.className = "msg ok";
    msg.textContent = `已登记 v${r.version}${activate ? " 并激活" : "（未激活）"}`;
    await loadGatePolicies();
  } catch (err) {
    msg.className = "msg error"; msg.textContent = "失败：" + err.message;
  }
}
g$("#np-submit").addEventListener("click", () => submitNewPolicy(true));
g$("#np-draft-only").addEventListener("click", () => submitNewPolicy(false));

/* ------------------------------------------------------------- 字体栈选择 */

function renderGatePicker() {
  const box = g$("#gate-font-picker");
  const available = gate.fonts.filter((f) => !gate.stack.includes(f.sha256));
  if (!available.length) {
    box.innerHTML = `<p class="hint">${
      gate.fonts.length ? "字体库中的字体均已加入。" :
      "请先在「字体库」上传字体。"}</p>`;
    return;
  }
  box.innerHTML = `<select id="gate-font-select">` + available.map((f) =>
    `<option value="${f.sha256}">${esc(f.family)} ${esc(f.subfamily || "")}
      (${f.container}) ${f.sha256.slice(0, 10)}…</option>`).join("") +
    `</select> <button type="button" id="gate-add">➕ 加入</button>`;
  g$("#gate-add").addEventListener("click", () => {
    gate.stack.push(g$("#gate-font-select").value);
    renderGateStack(); renderGatePicker();
  });
}

function renderGateStack() {
  g$("#gate-stack-list").innerHTML = gate.stack.map((d, i) => {
    const f = gate.fonts.find((x) => x.sha256 === d);
    return `<li class="stack-item" data-d="${d}">
      <span class="pos">${i + 1}</span>
      <span class="nm">${f ? esc(f.family) : "⚠ 缺失"}
        <small>${f ? f.container + " · " + f.sha256.slice(0, 12) + "…" : d.slice(0, 12)}</small>
      </span>
      <span class="ctrls">
        <button data-act="up" ${i === 0 ? "disabled" : ""}>↑</button>
        <button data-act="down" ${i === gate.stack.length - 1 ? "disabled" : ""}>↓</button>
        <button data-act="rm">✕</button>
      </span>
    </li>`;
  }).join("");
}

g$("#gate-stack-list").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const li = e.target.closest(".stack-item");
  const i = gate.stack.indexOf(li.dataset.d);
  if (btn.dataset.act === "rm") gate.stack.splice(i, 1);
  if (btn.dataset.act === "up" && i > 0)
    [gate.stack[i - 1], gate.stack[i]] = [gate.stack[i], gate.stack[i - 1]];
  if (btn.dataset.act === "down" && i < gate.stack.length - 1)
    [gate.stack[i + 1], gate.stack[i]] = [gate.stack[i], gate.stack[i + 1]];
  renderGateStack(); renderGatePicker();
});

/* ------------------------------------------------------------- 豁免表单 */

function renderExemptionFontSelect() {
  const sel = g$("#ex-font");
  if (!sel) return;
  sel.innerHTML = gate.fonts.map((f) =>
    `<option value="${f.sha256}">${esc(f.family)} · ${f.sha256.slice(0, 12)}…</option>`)
    .join("");
}

g$("#ex-submit").addEventListener("click", async () => {
  const msg = g$("#ex-msg");
  msg.className = "msg";
  const active = gate.policies.find((p) => p.active);
  if (!active) { msg.className = "msg error"; msg.textContent = "该场景无生效策略"; return; }
  const codes = g$("#ex-codes").value.split(",").map((s) => s.trim()).filter(Boolean);
  const why = g$("#ex-why").value.trim();
  if (!codes.length) { msg.className = "msg error"; msg.textContent = "请填写豁免代码"; return; }
  if (!why) { msg.className = "msg error"; msg.textContent = "豁免理由必填"; return; }
  const local = g$("#ex-expires").value;
  const expires_at = local ? new Date(local).toISOString() : null;
  try {
    await api("/api/license/exemptions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scenario: gate.scenario, policy_version: active.version,
        font_digest: g$("#ex-font").value, rule_codes: codes,
        justification: why, requested_by: "admin", expires_at,
        applies_to_stack_conflict: g$("#ex-stack").checked,
      }),
    });
    msg.className = "msg ok"; msg.textContent = "豁免已登记";
    g$("#ex-why").value = "";
    await loadGateExemptions();
  } catch (err) { msg.className = "msg error"; msg.textContent = "失败：" + err.message; }
});

/* ------------------------------------------------------------- 评估 / 发布 */

g$("#gate-evaluate").addEventListener("click", () => runGate(false));
g$("#gate-publish").addEventListener("click", () => runGate(true));

async function runGate(publish) {
  const msg = g$("#gate-msg");
  msg.className = "msg";
  if (!gate.stack.length) {
    msg.className = "msg error"; msg.textContent = "字体栈为空"; return;
  }
  const body = { scenario: gate.scenario, font_stack: gate.stack,
                 policy_version: null };
  if (publish) body.request_id = g$("#gate-request-id").value.trim() || null;
  try {
    const r = await api(publish ? "/api/license/publish" : "/api/license/evaluate",
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
    showGateManifest(r.manifest, !!r.frozen, r.decision_id);
    msg.className = "msg " + (r.manifest.decision === "allow" ? "ok" : "error");
    msg.textContent = (publish ? "已冻结发布：" : "即时评估：") +
      (r.manifest.decision === "allow" ? "放行 ✓" : "拒绝 ✗");
    if (publish) loadGateDecisions();
  } catch (err) {
    msg.className = "msg error"; msg.textContent = "失败：" + err.message;
  }
}

/* ------------------------------------------------------------- 结果渲染 */

function showGateManifest(m, frozen, decisionId) {
  gate.manifest = m; gate.frozen = frozen; gate.decisionId = decisionId;
  g$("#results-empty").hidden = true;
  g$("#results-content").hidden = true;
  const panel = g$("#gate-result");
  panel.hidden = false;
  g$("#gate-frozen-badge").hidden = !frozen;
  const exportBtn = g$("#gate-export");
  exportBtn.hidden = !frozen;

  const allow = m.decision === "allow";
  const denyCount = m.deny_codes.length;
  const warnCount = m.findings.filter((f) => f.severity === "warn").length;
  const fonts = m.font_stack.map((f, i) => fontEvidenceBlock(f, i)).join("");

  g$("#gate-result-body").innerHTML = `
    <div class="gate-verdict ${allow ? "allow" : "deny"}">
      <div class="v-big">${allow ? "✓ 放行 ALLOW" : "✗ 拒绝 DENY"}</div>
      <div class="v-meta">
        ${esc(SCENARIO_LABELS[m.scenario])} 场景 ·
        策略 v${m.policy.version}（${esc(m.policy.created_at || "")}）·
        评估时间 ${esc(m.evaluated_at)} ·
        <span class="mono">fp ${esc(m.fingerprint.slice(0, 20))}…</span>
      </div>
      <div class="v-stats">
        <span class="taglike">拒绝项 ${denyCount}</span>
        <span class="taglike">已豁免（警告） ${warnCount}</span>
        <span class="taglike">证据条目 ${m.findings.length}</span>
        <span class="taglike">字体 ${m.font_stack.length}</span>
      </div>
    </div>

    <h3>逐字体许可证据</h3>
    <div class="gate-ev-list">${fonts || `<p class="hint">无</p>`}</div>

    <h3>逐条检查证据（${m.findings.length}）</h3>
    ${renderFindings(m.findings)}

    <h3>豁免记录</h3>
    ${m.exemptions_applied.length ? `<div class="gate-ex-applied">` +
      m.exemptions_applied.map((a) =>
        `<div>检查 <code>${esc(a.finding_code)}</code>${
          a.font_digest ? " · " + esc(a.font_digest.slice(0, 12)) + "…" : ""}
          → 豁免 #${a.exemption_id}</div>`).join("") + `</div>`
      : `<p class="hint">本次未应用任何豁免。</p>`}
    <details><summary class="hint">本次评估考虑的全部豁免（${m.exemptions_considered.length}）</summary>
      <pre class="policy-json">${esc(JSON.stringify(m.exemptions_considered, null, 2))}</pre></details>

    <details><summary class="hint">完整冻结清单（JSON）</summary>
      <pre class="policy-json">${esc(JSON.stringify(m, null, 2))}</pre></details>`;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function fontEvidenceBlock(f, i) {
  const ev = f.license_evidence || {};
  const members = ev.members || [];
  const memberRows = members.map((mm) => `
    <tr>
      <td>#${mm.member_index}</td>
      <td>${esc(mm.family || "")}</td>
      <td class="mono">${mm.fstype == null ? "—" :
        "0x" + mm.fstype.toString(16).padStart(4, "0")}</td>
      <td>${(mm.fstype_flag_labels || []).map((l) =>
        `<span class="taglike">${esc(FSTYPE_FLAG_LABELS_ZH[l] || l)}</span>`).join(" ")
        || '<span class="hint">无嵌入位</span>'}</td>
      <td class="mono">${mm.fstype_reserved_bits ?
        `<b style="color:var(--bad)">0x${mm.fstype_reserved_bits.toString(16)}</b>` : "0"}</td>
      <td>${mm.name_license_description ?
        esc(mm.name_license_description.slice(0, 100)) +
          (mm.name_license_description.length > 100 ? "…" : "")
        : '<b style="color:var(--bad)">缺失</b>'}</td>
      <td class="mono">${mm.name_license_url ?
        esc(mm.name_license_url) : '<b style="color:var(--bad)">缺失</b>'}</td>
    </tr>`).join("");
  return `<div class="gate-ev">
    <div class="gate-ev-head">
      <b>[${i + 1}] ${esc(f.family || "")}</b>
      <span class="taglike">${esc(ev.container || "?")}</span>
      ${ev.members_complete ? "" : '<span class="taglike bad">证据不完整</span>'}
      <span class="meta mono">${esc(f.sha256.slice(0, 16))}… · ${esc(f.filename || "")}</span>
    </div>
    <table class="ev-table">
      <thead><tr><th>成员</th><th>家族</th><th>fsType</th><th>位语义</th>
        <th>保留位</th><th>许可描述 name13</th><th>许可 URL name14</th></tr></thead>
      <tbody>${memberRows}</tbody>
    </table>
    <details><summary class="hint">解包摘要</summary>
      <pre class="policy-json">${esc(JSON.stringify(ev.unpack_summary || {}, null, 2))}</pre></details>
  </div>`;
}

function renderFindings(findings) {
  if (!findings.length) {
    return `<div class="finding ok"><b>无任何违规或警告。</b></div>`;
  }
  return findings.map((f) => {
    const sev = f.severity === "deny" ? "deny" : "warn";
    const where = [];
    if (f.font_position != null) where.push("栈位 " + (f.font_position + 1));
    if (f.member_index != null) where.push("TTC 成员 #" + f.member_index);
    if (f.field) where.push(FIELD_LABELS[f.field] || f.field);
    return `<div class="finding ${sev}">
      <div class="f-head">
        <span class="f-sev">${sev === "deny" ? "✗ 拒绝" : "⚠ 已豁免"}</span>
        <code>${esc(f.code)}</code>
        <span class="hint">${esc(FINDING_LABELS[f.code] || "")}</span>
        <span class="hint">${where.join(" · ")}</span>
      </div>
      <div class="f-msg">${esc(f.message)}</div>
      ${f.exemption ? `<div class="f-ex">豁免 #${f.exemption.id}：${
        esc(f.exemption.justification)}
        （${f.exemption.expires_at ? "到期 " + esc(f.exemption.expires_at) : "长期"}）</div>` : ""}
      <details><summary class="hint">证据</summary>
        <pre class="policy-json">${esc(JSON.stringify(f.evidence, null, 2))}</pre></details>
    </div>`;
  }).join("");
}

g$("#gate-export").addEventListener("click", () => {
  if (gate.frozen && gate.decisionId) {
    window.location.href = `/api/license/decisions/${gate.decisionId}/export`;
    return;
  }
  const blob = new Blob([JSON.stringify(gate.manifest, null, 2)],
                        { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "license-gate-eval.json";
  a.click();
  URL.revokeObjectURL(a.href);
});

/* ------------------------------------------------------------- 标签联动 */

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (btn.dataset.tab === "gate") {
      if (!gate.policies.length) {
        await loadGateScenario();
      } else {
        // 字体库可能有新上传：刷新清单但保留当前栈/结果
        await loadGateFonts();
        renderGateStack();
        await loadGateExemptions();
        await loadGateDecisions();
      }
      g$("#results-empty").hidden = true;
      g$("#results-content").hidden = true;
      g$("#gate-result").hidden = !gate.manifest;
      if (!gate.manifest) showGateEmptyHint();
    } else if (gate.manifest || g$("#gate-result").hidden === false) {
      g$("#gate-result").hidden = true;
      g$("#results-empty").hidden = !!state.result;
      g$("#results-content").hidden = !state.result;
    }
  });
});

function showGateEmptyHint() {
  g$("#results-empty").hidden = false;
  g$("#results-empty").innerHTML = `
    <p>在左侧「许可合规门禁」选择场景、加入字体栈后执行评估。</p>
    <p class="hint">系统读取每个字体的 OS/2 fsType、name 表许可描述与 URL、
      集合字体全部成员及解包摘要，按字体栈组合给出逐条证据，
      并返回确定性的放行 / 拒绝结论。发布时字体摘要、策略版本与决定将原子冻结。</p>`;
}

/* 初始化：数据在首次切到门禁标签时按需加载 */

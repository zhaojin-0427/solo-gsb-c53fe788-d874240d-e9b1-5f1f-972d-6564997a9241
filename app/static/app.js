/* 字体回退与字形审查台 — 前端逻辑（原生 JavaScript） */
"use strict";

const SCRIPT_LABELS = {
  Latn: "拉丁", Cyrl: "西里尔", Grek: "希腊", Hebr: "希伯来", Arab: "阿拉伯",
  Hani: "汉字", Hira: "平假名", Kana: "片假名", Hang: "韩文", Deva: "天城文",
  Beng: "孟加拉", Thai: "泰文", Ethi: "吉兹", Nkoo: "NKo", Zyyy: "通用",
  Zinh: "继承", Zzzz: "未分配",
};
const KIND_LABELS = {
  single: "单码位", "base+marks": "基字+附加符",
  "variation-sequence": "变体序列", "zwj-sequence": "ZWJ 序列",
  "symbol/emoji": "符号/Emoji", "multi-codepoint": "多码位",
};
const COMMON_FEATURES = [
  ["liga", "标准连字"], ["rlig", "必需连字"], ["dlig", "自由连字"],
  ["calt", "上下文替代"], ["kern", "字偶距"], ["mark", "标记定位"],
  ["mkmk", "标记间定位"], ["smcp", "小型大写"], ["onum", "旧式数字"],
  ["tnum", "等宽数字"], ["locl", "本地化字形"], ["ss01", "风格集 1"],
  ["isol", "阿拉伯独立形"], ["fina", "阿拉伯词尾形"],
  ["medi", "阿拉伯词中形"], ["init", "阿拉伯词首形"],
];

const state = {
  fonts: [],
  stack: [],          // sha256 列表
  features: [],       // {tag, value:'on'|'off'}
  result: null,
  fontSummaries: null,
  request: null,
  reviewId: null,
  selectedCluster: null,
  tableMode: false,
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (m) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
const fontByDigest = (d) => state.fonts.find((f) => f.sha256 === d);

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

/* ------------------------------------------------------------- tabs */

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tabpane").forEach((p) =>
      p.hidden = p.dataset.pane !== btn.dataset.tab);
  });
});

/* ------------------------------------------------------------- fonts */

async function loadFonts() {
  state.fonts = await api("/api/fonts");
  renderFontList();
  renderStack();
  fillScriptSelect();
}

function fillScriptSelect() {
  const sel = $("#cfg-script");
  const current = sel.value;
  const tags = new Set();
  state.fonts.forEach((f) => (f.scripts || []).forEach((s) => tags.add(s)));
  sel.innerHTML = `<option value="">自动（按 UAX #24）</option>` +
    [...tags].filter((t) => !t.includes("-")).sort()
      .map((t) => `<option value="${t}">${t} — ${esc(SCRIPT_LABELS[t] || "")}</option>`)
      .join("");
  sel.value = current;
}

function renderFontList() {
  $("#font-count").textContent = state.fonts.length;
  const box = $("#font-list");
  if (!state.fonts.length) {
    box.innerHTML = `<p class="hint">尚未上传字体。</p>`;
    return;
  }
  box.innerHTML = state.fonts.map((f) => {
    const inStack = state.stack.includes(f.sha256);
    return `<div class="font-card" data-d="${f.sha256}">
      <div class="row1">
        <div>
          <span class="fname">${esc(f.family)}</span>
          <span class="fsub">${esc(f.subfamily || "")}</span>
        </div>
        <div class="btns">
          <button class="add-btn ${inStack ? "in" : ""}" data-act="toggle-stack">
            ${inStack ? "✓ 已在栈中" : "➕ 加入字体栈"}</button>
          <button class="danger" data-act="delete">删除</button>
        </div>
      </div>
      <div class="tags">
        <span class="tag">${esc(f.sfnt_version)}</span>
        <span class="tag">UPEM ${f.upem}</span>
        <span class="tag">${f.num_glyphs} glyphs</span>
        <span class="tag">cmap ${f.cmap_count}</span>
        ${(f.scripts || []).map((s) => `<span class="tag">${esc(s)}</span>`).join("")}
      </div>
      <div class="hash">${f.sha256} · ${esc(f.filename)}</div>
    </div>`;
  }).join("");
}

$("#font-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const card = e.target.closest(".font-card");
  const digest = card.dataset.d;
  const act = btn.dataset.act;
  if (act === "toggle-stack") {
    if (state.stack.includes(digest)) {
      state.stack = state.stack.filter((d) => d !== digest);
    } else {
      state.stack.push(digest);
    }
    renderFontList();
    renderStack();
  } else if (act === "delete") {
    if (!confirm("从字体库移除该字体的元数据记录？\n（磁盘文件保留，已保存的审查记录仍可复核）")) return;
    try {
      await api(`/api/fonts/${digest}`, { method: "DELETE" });
      state.stack = state.stack.filter((d) => d !== digest);
      await loadFonts();
    } catch (err) { alert(err.message); }
  }
});

$("#upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = $("#font-file").files[0];
  const msg = $("#upload-msg");
  if (!file) return;
  msg.className = "msg";
  msg.textContent = "上传与解析中…";
  const fd = new FormData();
  fd.append("file", file);
  try {
    const meta = await api("/api/fonts", { method: "POST", body: fd });
    msg.className = "msg ok";
    msg.textContent = `已登记：${meta.family} ${meta.subfamily}（${meta.num_glyphs} 字形）`;
    await loadFonts();
  } catch (err) {
    msg.className = "msg error";
    msg.textContent = "失败：" + err.message;
  }
});

/* ------------------------------------------------------------- stack */

function renderStack() {
  const ul = $("#stack-list");
  if (!state.stack.length) {
    ul.innerHTML = "";
    $("#stack-hint").hidden = false;
    return;
  }
  $("#stack-hint").hidden = true;
  ul.innerHTML = state.stack.map((d, i) => {
    const f = fontByDigest(d);
    if (!f) return `<li class="stack-item"><span class="pos">${i + 1}</span>
      <span class="nm">⚠ 字体缺失 <small>${d.slice(0, 12)}…</small></span></li>`;
    return `<li class="stack-item ${i > 0 ? "fb" : ""}" data-d="${d}">
      <span class="pos">${i + 1}</span>
      <span class="nm">${esc(f.family)} <small>${esc(f.subfamily)} · ${f.num_glyphs} glyphs</small></span>
      <span class="ctrls">
        <button data-act="up" ${i === 0 ? "disabled" : ""}>↑</button>
        <button data-act="down" ${i === state.stack.length - 1 ? "disabled" : ""}>↓</button>
        <button data-act="rm">✕</button>
      </span>
    </li>`;
  }).join("");
}

$("#stack-list").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const li = e.target.closest(".stack-item");
  const i = state.stack.indexOf(li.dataset.d);
  const act = btn.dataset.act;
  if (act === "rm") state.stack.splice(i, 1);
  if (act === "up" && i > 0)
    [state.stack[i - 1], state.stack[i]] = [state.stack[i], state.stack[i - 1]];
  if (act === "down" && i < state.stack.length - 1)
    [state.stack[i + 1], state.stack[i]] = [state.stack[i], state.stack[i + 1]];
  renderStack();
  renderFontList();
});

/* ------------------------------------------------------------- features */

function renderFeatures() {
  $("#feature-list").innerHTML = state.features.map((f, i) => `
    <li class="feature-item">
      <span class="ftag">${esc(f.tag)}</span>
      <span class="pill ${f.value}">${f.value === "on" ? "强制开启" : "强制关闭"}</span>
      <button class="small" data-act="remove" data-i="${i}">移除</button>
    </li>`).join("");
  if ($("#feature-presets")) renderFeaturePresets();
}
$("#feature-list").addEventListener("click", (e) => {
  if (e.target.dataset.act === "remove") {
    state.features.splice(+e.target.dataset.i, 1);
    renderFeatures();
  }
});
function addFeature(value) {
  const tag = $("#feature-tag").value.trim().toLowerCase();
  if (!/^[ -~]{4}$/.test(tag)) { alert("特性标签必须恰好为 4 个 ASCII 字符"); return; }
  state.features = state.features.filter((f) => f.tag !== tag);
  state.features.push({ tag, value });
  $("#feature-tag").value = "";
  renderFeatures();
}
$("#feature-add-on").addEventListener("click", () => addFeature("on"));
$("#feature-add-off").addEventListener("click", () => addFeature("off"));
$("#feature-tag").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addFeature("on");
});

// 常用特性快捷开关：点击在 关 / 开 / 不指定 之间循环
function renderFeaturePresets() {
  const cur = Object.fromEntries(state.features.map((f) => [f.tag, f.value]));
  $("#feature-presets").innerHTML = COMMON_FEATURES.map(([tag, label]) => {
    const v = cur[tag];
    const text = v === undefined ? tag : `${tag} ${v === "on" ? "开" : "关"}`;
    const style = v === "on" ? "background:var(--ok-soft);color:var(--ok);border-color:#bfe3cf"
      : v === "off" ? "background:var(--bad-soft);color:var(--bad);border-color:#efc2bb" : "";
    return `<button type="button" style="${style}" title="${label}" data-tag="${tag}">${text}</button>`;
  }).join("");
}
$("#feature-presets").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tag]");
  if (!btn) return;
  const tag = btn.dataset.tag;
  const i = state.features.findIndex((f) => f.tag === tag);
  const cur = i >= 0 ? state.features[i].value : undefined;
  if (cur === undefined) state.features.push({ tag, value: "off" });
  else if (cur === "off") { state.features[i].value = "on"; }
  else state.features.splice(i, 1);
  renderFeatures();
});

/* ------------------------------------------------------------- analyze */

function buildRequest() {
  return {
    text: $("#text-input").value,
    font_stack: state.stack,
    language: $("#cfg-language").value.trim() || "dflt",
    script: $("#cfg-script").value || null,
    direction: $("#cfg-direction").value,
    features: state.features,
    normalize: $("#cfg-normalize").value,
  };
}

$("#btn-analyze").addEventListener("click", async () => {
  const msg = $("#analyze-msg");
  msg.className = "msg";
  if (!$("#text-input").value) { msg.className = "msg error"; msg.textContent = "请输入文本"; return; }
  if (!state.stack.length) { msg.className = "msg error"; msg.textContent = "字体栈为空，请先添加字体"; return; }
  msg.textContent = "整形中…";
  const body = buildRequest();
  const save = $("#save-review").checked;
  const qs = new URLSearchParams();
  if (save) {
    qs.set("save", "true");
    qs.set("title", $("#review-title").value.trim());
  }
  try {
    const payload = await api(`/api/analyze?${qs}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.result = payload.result;
    state.fontSummaries = payload.font_summaries;
    state.request = payload.request;
    state.reviewId = payload.review_id || null;
    state.selectedCluster = null;
    renderResults();
    msg.className = "msg ok";
    msg.textContent = save ? `已保存为审查记录 #${state.reviewId}` : "完成";
    if (save) loadReviews();
    $("#btn-rerun").hidden = !state.reviewId;
  } catch (err) {
    msg.className = "msg error";
    msg.textContent = "失败：" + err.message;
  }
});

/* ------------------------------------------------------------- rendering */

function scriptLabel(tag) { return `${tag}${SCRIPT_LABELS[tag] ? " · " + SCRIPT_LABELS[tag] : ""}`; }

function renderResults() {
  $("#results-empty").hidden = true;
  $("#results-content").hidden = false;
  document.querySelectorAll(".rerun-banner").forEach((el) => el.remove());
  const r = state.result;
  const s = r.stats;
  $("#stats-bar").innerHTML = `
    <div class="stat"><b>${s.num_clusters}</b>字素簇</div>
    <div class="stat ok"><b>${s.covered}</b>已覆盖</div>
    <div class="stat bad"><b>${s.missing}</b>缺失</div>
    <div class="stat" style="border-color:#b6dcc8"><b>${s.served_by_primary}</b>首选字体</div>
    <div class="stat fb"><b>${s.served_by_fallback}</b>回退字体</div>
    <div class="stat lig"><b>${s.clusters_merged_into_ligature}</b>合并入连字的簇</div>`;
  $("#meta-bar").innerHTML = `
    <span>引擎：${esc(r.engine.shaper)} ${esc(r.engine.harfbuzz_version)}</span>
    <span>语言：${esc(r.input.language)}</span>
    <span>脚本：${esc(scriptLabel(r.input.script === "auto" ? "auto" : r.input.script))}</span>
    <span>方向：${esc(r.input.direction_requested)} → 段落基级别 ${r.input.paragraph_base_level}</span>
    <span>规范化：${esc(r.input.normalization)}</span>
    <span>bidi/字符：[${r.input.bidi_levels_per_char.join(",")}]</span>`;

  renderAttempts(r);
  renderClusters(r);
  $("#cluster-detail").hidden = true;
}

function renderAttempts(r) {
  const boxes = r.shaping_attempts.map((a) => {
    const cls = a.result === "covered" ? "" :
      a.result === "partial" ? "partial" : "exhausted";
    const bad = (a.uncovered_clusters || []).length
      ? `<span class="abad">未覆盖簇: ${a.uncovered_clusters.join(", ")}</span>` : "";
    const font = a.result === "font-stack-exhausted"
      ? `<span class="afont">字体栈耗尽</span>`
      : `<span class="afont">[${a.font_position + 1}] ${esc(a.font_family)}</span>`;
    return `<div class="attempt ${cls}">
      <span class="atext">${esc(a.text)}</span>
      ${font}
      <span class="ameta">${esc(a.script)} · ${esc(a.direction || "")} · ${a.num_glyphs ?? ""} glyphs</span>
      ${bad}
    </div>`;
  }).join("");
  $("#attempts").innerHTML = boxes || `<p class="hint">无</p>`;
}

function clusterClass(c) {
  if (!c.covered) return "missing";
  return c.font_position > 0 ? "fallback" : "covered";
}
function clusterDot(c) {
  if (!c.covered) return `<span class="dot none" title="无字体覆盖"></span>`;
  return `<span class="dot ${c.font_position > 0 ? "fb" : "primary"}"
    title="${c.font_position > 0 ? "回退字体" : "首选字体"}"></span>`;
}

function renderClusters(r) {
  $("#cluster-mode-label").textContent = state.tableMode ? "数据表" : "卡片";
  $("#view-toggle").textContent = state.tableMode ? "切换为卡片" : "切换为数据表";
  $("#cluster-strip").hidden = state.tableMode;
  $("#cluster-table-wrap").hidden = !state.tableMode;

  if (!state.tableMode) {
    $("#cluster-strip").innerHTML = r.clusters.map((c) => `
      <div class="cluster-chip ${clusterClass(c)}" data-i="${c.cluster_index}"
           title="${esc(c.fallback_reason)}">
        ${c.merged_into_ligature ? `<span class="ligbadge">连字</span>` : ""}
        ${(c.warnings || []).length ? `<span class="warnmark" title="${esc(c.warnings.join("; "))}">!</span>` : ""}
        <div class="ch">${esc(c.text)}</div>
        <div class="fi">
          ${clusterDot(c)}
          <span>#${c.cluster_index}</span>
          <span>${c.glyphs.map((g) => g.gid).join(",") || "—"}</span>
        </div>
      </div>`).join("");
  } else {
    const tbody = $("#cluster-table tbody");
    tbody.innerHTML = r.clusters.map((c) => `
      <tr class="${clusterClass(c)}" data-i="${c.cluster_index}">
        <td>${c.cluster_index}</td>
        <td style="font-size:15px">${esc(c.text)}</td>
        <td class="mono">${c.codepoints.map((cp) =>
          `<span class="cp">${cp.codepoint}</span>`).join("")}</td>
        <td class="mono">${esc(c.script)}</td>
        <td>${c.bidi_level}</td>
        <td>${c.covered ? "✓" : "✗"}</td>
        <td>${esc(c.font_family || "—")}</td>
        <td>${c.font_position >= 0 ? c.font_position + 1 : "—"}</td>
        <td class="mono">${c.glyphs.map((g) => `${g.name}(${g.gid})`).join(", ") || "—"}</td>
        <td>${c.merged_into_ligature ? "是" : ""}</td>
        <td class="mono" style="font-size:11px">${esc(c.fallback_reason)}
          ${(c.warnings || []).map((w) => `<br>⚠ ${esc(w)}`).join("")}</td>
      </tr>`).join("");
  }
}

$("#cluster-strip").addEventListener("click", (e) => {
  const chip = e.target.closest(".cluster-chip");
  if (chip) selectCluster(+chip.dataset.i);
});
$("#cluster-table-wrap").addEventListener("click", (e) => {
  const tr = e.target.closest("tr");
  if (tr) selectCluster(+tr.dataset.i);
});
$("#view-toggle").addEventListener("click", () => {
  state.tableMode = !state.tableMode;
  renderClusters(state.result);
});

function selectCluster(i) {
  state.selectedCluster = i;
  document.querySelectorAll(".cluster-chip").forEach((el) =>
    el.classList.toggle("sel", +el.dataset.i === i));
  renderDetail();
}

function glyphBox(c, g) {
  const digest = c.font_sha256;
  const svgUrl = digest ? `/api/fonts/${digest}/glyph/${g.gid}.svg` : "";
  const ext = g.extents ? `<div class="metrics">
      <span class="k">x_bearing</span><span>${g.extents.x_bearing}</span>
      <span class="k">y_bearing</span><span>${g.extents.y_bearing}</span>
      <span class="k">width</span><span>${g.extents.width}</span>
      <span class="k">height</span><span>${g.extents.height}</span>
    </div>` : `<span class="hint">无轮廓（空格/控制符）</span>`;
  return `<div class="glyph-box">
    <div class="gname">${esc(g.name)} ${g.merges_grapheme_clusters ? '<span class="ligbadge" style="position:static">连字</span>' : ""}</div>
    <div class="ggid">gid=${g.gid}${g.notdef ? " · .notdef" : ""}
      ${g.visual_order ? " · RTL 视觉序" : ""}</div>
    <div class="glyph-svg ${g.gid === 0 ? "notdef" : ""}">
      ${svgUrl ? `<img alt="${esc(g.name)}" src="${svgUrl}" style="height:100%">` : ""}
    </div>
    <div class="metrics">
      <span class="k">x_advance</span><span>${g.x_advance} (${g.x_advance_em} em)</span>
      <span class="k">y_advance</span><span>${g.y_advance} (${g.y_advance_em} em)</span>
      <span class="k">x_offset</span><span>${g.x_offset} (${g.x_offset_em} em)</span>
      <span class="k">y_offset</span><span>${g.y_offset} (${g.y_offset_em} em)</span>
      <span class="k">nominal adv</span><span>${g.nominal_advance}</span>
      <span class="k">input range</span><span>[${g.input_start}, ${g.input_end})</span>
    </div>
    ${ext}
  </div>`;
}

function renderDetail() {
  const i = state.selectedCluster;
  if (i == null) return;
  const c = state.result.clusters[i];
  const box = $("#cluster-detail");
  box.hidden = false;
  const reasonClass = !c.covered ? "exhausted" :
    c.font_position > 0 ? "fallback" : "primary";
  const cps = c.codepoints.map((cp) =>
    `<div><b>${cp.codepoint}</b> ${esc(cp.name)}
      <span class="hint">[${cp.category}${cp.combining_class ? "/ccc=" + cp.combining_class : ""}]</span></div>`
  ).join("");
  box.innerHTML = `
    <h3>簇 #${c.cluster_index} — <span style="font-size:22px">${esc(c.text)}</span>
      <small>(${esc(KIND_LABELS[c.kind] || c.kind)})</small></h3>
    <dl class="detail-grid">
      <div><dt>实际字体</dt><dd>${esc(c.font_family || "— 无 —")}</dd></div>
      <div><dt>字体栈位置</dt><dd>${c.font_position >= 0 ? "第 " + (c.font_position + 1) + " 位" : "—"}</dd></div>
      <div><dt>字体摘要 SHA-256</dt><dd>${c.font_sha256 || "—"}</dd></div>
      <div><dt>脚本 / Bidi 级别</dt><dd>${esc(c.script)} / L${c.bidi_level}</dd></div>
      <div><dt>字符范围</dt><dd>[${c.char_start}, ${c.char_end})</dd></div>
      <div><dt>字形数量</dt><dd>${c.glyph_count}${c.merged_into_ligature ? "（合并入连字）" : ""}</dd></div>
    </dl>
    <div><dt class="hint">码位明细</dt><dd>${cps}</dd></div>
    <div class="reason-box ${reasonClass}">${esc(c.fallback_reason)}</div>
    ${(c.warnings || []).length ? `<div class="warnings-box">⚠ ${esc(c.warnings.join("；"))}</div>` : ""}
    <h3>字形与定位数据</h3>
    <div class="glyph-boxes">
      ${c.glyphs.length ? c.glyphs.map((g) => glyphBox(c, g)).join("")
        : `<p class="hint">该簇未由任何已配置字体产生字形。</p>`}
    </div>`;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ------------------------------------------------------------- reviews */

async function loadReviews() {
  const list = await api("/api/reviews");
  $("#review-list").innerHTML = list.length ? list.map((r) => `
    <div class="review-card">
      <div>
        <div><b>#${r.id} ${esc(r.title)}</b></div>
        <div class="meta">${esc(r.created_at)}</div>
      </div>
      <div class="btns">
        <button data-act="load" data-id="${r.id}">载入</button>
        <button data-act="export" data-id="${r.id}">导出 JSON</button>
        <button class="danger" data-act="delete" data-id="${r.id}">删除</button>
      </div>
    </div>`).join("") : `<p class="hint">尚无保存的审查记录。</p>`;
}

$("#review-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const id = btn.dataset.id;
  if (btn.dataset.act === "export") {
    window.location.href = `/api/reviews/${id}/export`;
  } else if (btn.dataset.act === "delete") {
    if (!confirm("删除该审查记录？")) return;
    await api(`/api/reviews/${id}`, { method: "DELETE" });
    loadReviews();
  } else if (btn.dataset.act === "load") {
    const rec = await api(`/api/reviews/${id}`);
    // 恢复表单
    $("#text-input").value = rec.request.text;
    state.stack = [...rec.request.font_stack];
    state.features = [...(rec.request.features || [])];
    $("#cfg-language").value = rec.request.language;
    $("#cfg-script").value = rec.request.script || "";
    $("#cfg-direction").value = rec.request.direction;
    $("#cfg-normalize").value = rec.request.normalize || "none";
    await loadFonts();
    renderFeatures();
    state.result = rec.result;
    state.fontSummaries = rec.font_summaries;
    state.request = rec.request;
    state.reviewId = rec.id;
    state.selectedCluster = null;
    renderResults();
    $("#btn-rerun").hidden = false;
    document.querySelector('.tab[data-tab="analyze"]').click();
  }
});

/* ------------------------------------------------------------- rerun / export */

$("#btn-rerun").addEventListener("click", async () => {
  if (!state.reviewId) return;
  try {
    const d = await api(`/api/reviews/${state.reviewId}/rerun`, { method: "POST" });
    const bar = document.createElement("div");
    bar.className = "rerun-banner " + (d.consistent ? "ok" : "bad");
    if (d.consistent) {
      bar.textContent = `✓ 复核一致：以记录固定的参数与字体摘要重新整形，${d.fresh_result.stats.num_clusters} 个簇的字体、字形编号与定位数据完全相同。`;
    } else {
      bar.innerHTML = `✗ 复核不一致：<ul class="diff-list">` +
        [...(d.font_drift || []).map((x) => `<li>字体漂移: ${esc(JSON.stringify(x))}</li>`),
         ...(d.differences || []).map((x) => `<li>簇 ${x.cluster_index} 字段 ${esc(x.field)}: ${esc(JSON.stringify(x.stored))} → ${esc(JSON.stringify(x.current))}</li>`)]
          .join("") + `</ul>`;
    }
    $("#stats-bar").after(bar);
    setTimeout(() => bar.remove(), 12000);
    state.result = d.fresh_result;
    renderResults();
  } catch (err) { alert("复核失败：" + err.message); }
});

$("#btn-export").addEventListener("click", async () => {
  if (state.reviewId) {
    window.location.href = `/api/reviews/${state.reviewId}/export`;
    return;
  }
  // 未保存的即时结果：生成客户端 JSON 报告
  const report = {
    report: "font-fallback-glyph-review",
    schema_version: "1.0",
    review: { id: null, title: "未保存的即时分析",
              created_at: new Date().toISOString() },
    request_parameters: state.request,
    font_summaries: state.fontSummaries,
    result: state.result,
  };
  const blob = new Blob([JSON.stringify(report, null, 2)],
                        { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "glyph-review-unsaved.json";
  a.click();
  URL.revokeObjectURL(a.href);
});

/* ------------------------------------------------------------- init */

async function init() {
  try {
    await api("/api/health");
    $("#health").textContent = "● 后端在线";
  } catch (_) {
    $("#health").textContent = "● 后端离线";
    $("#health").className = "health bad";
  }
  renderFeatures();
  await loadFonts();
  await loadReviews();
}
init();

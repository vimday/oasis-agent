import { $, apiGet, apiPost, esc, short, fmtTime, state, toast, badgeHtml, renderTable } from "./core.js";

let runTriggerPending = false;
async function triggerRun(button) {
  if (button.disabled || runTriggerPending) return;
  runTriggerPending = true;
  button.disabled = true;
  try {
    const r = await apiPost("/api/v1/runs/shadow");
    toast("运行已排队: " + r.run_id);
    loadRuns();
  } catch { /* toast */ }
  finally { runTriggerPending = false; await loadConfiguration(); }
}

async function stopRun(button) {
  if (button.disabled) return;
  button.disabled = true;
  try {
    const result = await apiPost(`/api/v1/runs/${encodeURIComponent(button.dataset.runId)}/stop`);
    toast(result.status === "stopped" ? "观测进程已停止" : "停止请求已接受，等待进程退出");
    await loadRuns();
  } catch { button.disabled = false; }
}

let campaignHead = null;
let campaignLoading = false;
let campaignMutation = false;
let campaignEditorDirty = false;

function campaignKindChanged() {
  const launch = $("campaign-kind").value === "launch";
  const archive = $("campaign-kind").value === "archive";
  const market = $("campaign-kind").value === "market" || archive;
  const spot = $("campaign-kind").value === "spot";
  $("campaign-launch-fields").hidden = !(launch || market);
  $("campaign-wallet").hidden = !launch;
  $("campaign-wallet-label").hidden = !launch;
  $("campaign-lp-fields").hidden = launch || (market && !archive);
  $("campaign-source").hidden = archive || spot;
  $("campaign-source-label").hidden = archive || spot;
}

function updateCampaignOptions(id, items) {
  const select = $(id);
  const selected = select.value;
  const markup = '<option value="">请选择</option>' + items.map(x => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
  if (select.innerHTML !== markup) {
    select.innerHTML = markup;
    select.value = items.includes(selected) ? selected : "";
  }
}

async function loadCampaigns() {
  if (campaignLoading) return;
  campaignLoading = true;
  try {
    const data = await apiGet("/api/v1/campaigns");
    $("campaign-capabilities").textContent = "可运行：LP 市场分析 / 可恢复历史采集 / LP Shadow / Launch Observe / 自主交易影子 · 最多 " + data.max_active
      + " 个并行任务 · 实盘阻断：独立签名服务及目标发布门禁未通过";
    updateCampaignOptions("campaign-primary", data.rpc_profiles.map(x => x.name));
    updateCampaignOptions("campaign-secondary", data.rpc_profiles.map(x => x.name));
    updateCampaignOptions("campaign-source", data.input_sources);
    renderTable("campaign-table", ["任务", "模式", "已保存", "已应用", "本次运行版本", "状态", "编辑"],
      data.items.map(x => [esc(x.name), esc(x.mode), esc(x.desired_revision), esc(x.applied_revision ?? "–"),
        esc(x.run?.revision ?? "–"), badgeHtml(x.status),
        `<button class="action" type="button" data-action="campaign-select" data-name="${esc(x.name)}">查看 / 编辑</button>`]), "尚无独立任务");
    if (campaignHead) {
      const current = data.items.find(x => x.name === campaignHead.name);
      if (current) {
        // Polls update runtime truth, never the editor's optimistic revision.
        campaignHead = {...campaignHead, applied_revision: current.applied_revision, status: current.status, run: current.run};
        $("campaign-revision").textContent = "编辑基于 v" + campaignHead.desired_revision + " · 后端最新 v" + current.desired_revision
          + " · 已应用 " + (current.applied_revision ?? "无") + " · " + current.status;
      }
    }
  } catch { $("campaign-capabilities").textContent = "运行管理不可用，请检查服务状态"; renderTable("campaign-table", [], [], "任务状态未验证"); }
  finally { campaignLoading = false; }
}

async function selectCampaign(name) {
  const head = await apiGet(`/api/v1/campaigns/${encodeURIComponent(name)}`);
  const spec = JSON.parse(head.document); // policy itself remains an unparsed string.
  campaignHead = head;
  campaignEditorDirty = false;
  $("campaign-name").value = head.name;
  $("campaign-kind").value = spec.kind;
  $("campaign-wallet").value = spec.wallet || "";
  $("campaign-primary").value = spec.rpc_primary || "";
  $("campaign-secondary").value = spec.rpc_secondary || "";
  $("campaign-source").value = spec.input_source || "";
  $("campaign-cycles").value = spec.cycles;
  $("campaign-timeout").value = spec.timeout_seconds;
  $("campaign-policy").value = spec.policy;
  $("campaign-discovery").value = spec.discovery_from_block ?? "";
  $("campaign-events").checked = spec.events;
  $("campaign-apply-revision").value = head.desired_revision;
  $("campaign-revision").textContent = "已保存 v" + head.desired_revision + " · 已应用 " + (head.applied_revision ?? "无") + " · " + head.status;
  campaignKindChanged();
  const snapshot = await apiGet(`/api/v1/campaigns/${encodeURIComponent(name)}/snapshot`);
  $("campaign-output").textContent = "运行状态：" + head.status + "\n" + (head.run?.stdout_tail || "") + "\n"
    + (head.run?.stderr_tail || "") + "\n配置计划（不代表实盘许可）：\n"
    + JSON.stringify(head.configuration_plan, null, 2) + "\n" + snapshot.document;
}

function campaignInteger(id, min, max) {
  const raw = $(id).value;
  if (!/^[0-9]+$/.test(raw)) throw new Error("请输入整数：" + id);
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < min || value > max) throw new Error("数值超出范围：" + id);
  return value;
}

async function campaignAction(action) {
  if (campaignMutation) return;
  campaignMutation = true;
  const name = $("campaign-name").value;
  try {
    if (!/^[a-z][a-z0-9_-]{0,63}$/.test(name)) throw new Error("任务 ID 需为小写英文、数字、下划线或短横线，并以字母开头");
    const base = `/api/v1/campaigns/${encodeURIComponent(name)}`;
    if (action === "save") {
      const kind = $("campaign-kind").value;
      const spec = {kind, mode: kind === "lp" || kind === "spot" ? "shadow" : "observe", policy: $("campaign-policy").value,
        timeout_seconds: campaignInteger("campaign-timeout", 10, kind === "spot" ? 345600 : 86400)};
      if (kind === "market" || kind === "archive") {
        Object.assign(spec, {rpc_primary: $("campaign-primary").value, rpc_secondary: $("campaign-secondary").value});
        if (kind === "archive") spec.cycles = campaignInteger("campaign-cycles", 1, 1000);
      } else if (kind === "launch") {
        Object.assign(spec, {wallet: $("campaign-wallet").value.toLowerCase(), rpc_primary: $("campaign-primary").value,
          rpc_secondary: $("campaign-secondary").value, events: $("campaign-events").checked});
        if ($("campaign-discovery").value) spec.discovery_from_block = campaignInteger("campaign-discovery", 0, Number.MAX_SAFE_INTEGER);
      } else if (kind === "spot") {
        // cycles = provider-call budget in thousands (OKX + GMGN, whole run).
        spec.cycles = campaignInteger("campaign-cycles", 1, 200);
      } else Object.assign(spec, {input_source: $("campaign-source").value, cycles: campaignInteger("campaign-cycles", 1, 10000)});
      await apiPost(base + "/configuration", {expected_revision: campaignHead?.name === name ? campaignHead.desired_revision : 0,
        document: JSON.stringify(spec)});
    } else {
      if (!campaignHead || campaignHead.name !== name) throw new Error("请先保存或选择任务");
      if (action !== "stop" && campaignEditorDirty) throw new Error("编辑内容尚未保存，请先保存新版本或重新载入任务");
      if (action === "apply") await apiPost(base + "/apply", {expected_revision: campaignHead.desired_revision,
        revision: campaignInteger("campaign-apply-revision", 1, campaignHead.desired_revision)});
      if (action === "start") {
        if (!campaignHead.applied_revision) throw new Error("请先应用一个已保存版本");
        await apiPost(base + "/start", {revision: campaignHead.applied_revision, request_id: crypto.randomUUID().replaceAll("-", "")});
      }
      if (action === "stop") await apiPost(base + "/stop");
    }
    $("campaign-result").textContent = {save:"配置已保存，需应用后才用于启动", apply:"版本已应用，下次启动将使用该版本",
      start:"启动已受理；历史采集将从该版本的已完成片段和原预算继续", stop:"停止请求已受理，等待进程退出"}[action];
    await loadCampaigns();
    await selectCampaign(name);
  } catch (error) {
    $("campaign-result").textContent = error.detail || error.message || "操作失败，请刷新状态";
  } finally { campaignMutation = false; }
}

async function saveCampaignRpc() {
  const name = $("campaign-rpc-name").value;
  const url = $("campaign-rpc-url").value;
  $("campaign-rpc-url").value = "";
  try {
    await apiPost(`/api/v1/campaigns/rpc/${encodeURIComponent(name)}`, {url});
    await loadCampaigns();
    $("campaign-result").textContent = "RPC 已保存到当前服务内存；已有运行保持原端点";
  } catch { $("campaign-result").textContent = "RPC 未保存，请检查名称和 URL"; }
}

let configurationTemplates = {};
let validatedConfiguration = null;
async function loadConfiguration() {
  const button = document.querySelector('[data-action="trigger-run"]');
  try {
    const info = await apiGet("/api/v1/configuration");
    configurationTemplates = info.templates;
    button.disabled = runTriggerPending || !info.shadow_start_available || !state.token;
    $("configuration-capabilities").textContent = "兼容命令启停：" + (info.shadow_start_available ? "已配置，需操作 Token" : "未配置")
      + " · 此高级入口仅校验/导出；任务配置请使用上方独立任务管理";
  } catch {
    button.disabled = true;
    $("configuration-capabilities").textContent = "后端能力读取失败，启动不可用";
  }
}

function invalidateConfiguration() {
  validatedConfiguration = null;
  $("configuration-download").disabled = true;
  $("configuration-result").textContent = "配置已更改，需要重新校验";
}

async function validateConfiguration(button) {
  if (button.disabled) return;
  button.disabled = true;
  invalidateConfiguration();
  const kind = $("configuration-kind").value;
  const document = $("configuration-document").value;
  try {
    const result = await apiPost("/api/v1/configuration/validate", {kind, document});
    if (kind !== $("configuration-kind").value || document !== $("configuration-document").value) return;
    validatedConfiguration = result;
    $("configuration-download").disabled = false;
    $("configuration-result").textContent = "校验通过，尚未应用。配置摘要：" + result.digest;
  } catch { $("configuration-result").textContent = "校验失败，不能下载已校验配置"; }
  finally { button.disabled = false; }
}

function downloadConfiguration() {
  if (!validatedConfiguration) return;
  const url = URL.createObjectURL(new Blob([validatedConfiguration.document], {type: "text/plain;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = validatedConfiguration.filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function loadRuns() {
  try {
    const r = await apiGet("/api/v1/runs?limit=20");
    const rows = r.items.map((x) => [
      `<button type="button" class="action" data-action="run-detail" data-id="${esc(x.run_id)}">${esc(short(x.run_id, 12))}</button>`,
      badgeHtml(x.status),
      esc(short(x.command_digest, 18)),
      esc(x.exit_code ?? "–"),
      esc(fmtTime(x.finished_at || x.created_at)),
      ["queued", "running", "stopping"].includes(x.status)
        ? `<button type="button" class="action" data-action="stop-run" data-run-id="${esc(x.run_id)}" ${x.status === "stopping" || !state.token ? "disabled" : ""}>停止观测</button>` : "–",
    ]);
    renderTable("runs-table", ["run_id", "状态", "command_digest", "exit", "时间", "控制"], rows, "无运行记录");
  } catch { renderTable("runs-table", [], [], "读取失败，旧结果已清除"); }
}

async function loadOps() {
  try {
    const r = await apiGet("/api/v1/ops?limit=50");
    const rows = r.items.map((o) => [
      esc(o.seq),
      esc(o.action),
      esc(o.actor),
      badgeHtml(o.result),
      esc(short(o.detail, 40)),
      esc(fmtTime(o.ts)),
    ]);
    renderTable("ops-table", ["seq", "action", "actor", "result", "detail", "ts"], rows, "无操作记录");
  } catch { renderTable("ops-table", [], [], "读取失败，旧结果已清除"); }
}

async function requestApproval() {
  const gate = $("approval-gate").value;
  const reason = $("approval-reason").value.trim();
  if (!reason) { toast("请填写审批理由"); return; }
  try {
    await apiPost(`/api/v1/p2/gates/${encodeURIComponent(gate)}/approval?reason=${encodeURIComponent(reason)}`);
    toast("审批请求已记录");
    $("approval-reason").value = "";
    loadApprovals();
  } catch { /* toast */ }
}

async function confirmApproval(gate, id) {
  try {
    await apiPost(`/api/v1/p2/gates/${encodeURIComponent(gate)}/approval/${id}/confirm`);
    toast("已由第二操作者确认");
    loadApprovals();
  } catch { /* toast */ }
}

async function loadApprovals() {
  try {
    const r = await apiGet("/api/v1/p2/gates");
    const rows = r.items.map((a) => [
      esc(a.gate),
      badgeHtml(a.status),
      esc(a.requested_by),
      esc(short(a.requested_reason, 30)),
      esc(a.confirmed_by || "–"),
      a.status === "pending"
        ? `<button type="button" class="action" data-action="confirm-approval" data-gate="${esc(a.gate)}" data-id="${a.id}">确认</button>`
        : "–",
    ]);
    renderTable("approvals-table", ["gate", "状态", "请求人", "理由", "确认人", ""], rows, "无审批记录");
  } catch { renderTable("approvals-table", [], [], "读取失败，旧结果已清除"); }
}


export function wireOperations(registerAction, registerForm) {
registerAction("trigger-run", (button) => { triggerRun(button); });
registerAction("stop-run", (button) => { stopRun(button); });
registerAction("configuration-template", () => {
  $("configuration-document").value = configurationTemplates[$("configuration-kind").value] || "";
  invalidateConfiguration();
});
registerAction("configuration-validate", (button) => { validateConfiguration(button); });
registerAction("configuration-download", downloadConfiguration);
registerAction("campaign-select", (button) => { selectCampaign(button.dataset.name).catch(() => {}); });
registerAction("campaign-template", () => {
  $("campaign-policy").value = configurationTemplates[$("campaign-kind").value] || "";
  campaignEditorDirty = true;
});
for (const action of ["save", "apply", "start", "stop"]) registerAction("campaign-" + action, () => { campaignAction(action); });
registerAction("campaign-rpc-save", saveCampaignRpc);
$("campaign-kind").addEventListener("change", campaignKindChanged);
for (const id of ["campaign-kind", "campaign-wallet", "campaign-primary", "campaign-secondary", "campaign-source", "campaign-cycles", "campaign-timeout", "campaign-policy", "campaign-discovery", "campaign-events"]) {
  $(id).addEventListener("input", () => { campaignEditorDirty = true; });
  $(id).addEventListener("change", () => { campaignEditorDirty = true; });
}

$("configuration-document").addEventListener("input", invalidateConfiguration);
$("configuration-kind").addEventListener("change", invalidateConfiguration);
registerAction("confirm-approval", (el) => { confirmApproval(el.dataset.gate, Number(el.dataset.id)); });

registerForm("request-approval", requestApproval);
}

export { loadCampaigns, loadConfiguration, loadRuns, loadOps, loadApprovals };

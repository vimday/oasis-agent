import { $, esc, gateState, fmtTime } from "./core.js";

// Gate declarations are not raw-evidence replay or execution authority.
export const RELEASE_PATH = Object.freeze([
  ["wp0_correctness", "基础正确性", "核对解码、数值边界与测试证据。", "evidence"],
  ["wp1_pool_access", "池与权限", "核验目标池身份、LP 准入与可报价性。", "evidence"],
  ["wp2_replay_accounting", "回放对账", "逐资产核对真实回放与未解释差额。", "evidence"],
  ["wp3_research", "经济验证", "审阅未触碰样本的扣费后基准比较。", "evidence"],
  ["wp4_hook_executor", "合约验证", "审阅 Hook 与执行器绑定的验证证据。", "evidence"],
  ["wp5_adversarial", "对抗验证", "补齐固定区块分叉攻击测试与独立审查。", "evidence"],
  ["wp6_shadow_routing", "影子与路由", "核验持续影子观察和目标池路由证据。", "ops"],
  ["wp7_canary", "限额试运行", "核验独立批准的 canary 结果与止损、退出证明。", "execution"],
  ["wp8_production", "生产审查", "审阅独立发布结论、监控与撤销方案。", "gates"],
]);

export function readinessModel(assessment) {
  const gates = assessment?.gates;
  const valid = Array.isArray(gates) && gates.length === RELEASE_PATH.length
    && RELEASE_PATH.every(([id]) => gates.filter(g => g?.gate === id).length === 1)
    && Number.isFinite(Date.parse(assessment.as_of));
  const items = RELEASE_PATH.map(([id, label, guidance, view]) => {
    const gate = valid ? gates.find(g => g.gate === id) : null;
    const evaluated = gate ? gateState(gate, assessment.as_of) : "unresolved";
    const status = ["pass", "fail", "expired", "unresolved"].includes(evaluated) ? evaluated : "unresolved";
    return { id, label, guidance, view, status };
  });
  return { valid, items, passed: items.filter(g => g.status === "pass").length,
    next: items.find(g => g.status !== "pass") ?? null };
}

export function renderReadinessPath(assessment) {
  const model = readinessModel(assessment);
  const labels = {pass: "通过", fail: "未通过", expired: "已过期", unresolved: "未验证"};
  $("readiness-summary").textContent = model.valid
    ? `${model.passed} / ${model.items.length} 项门槛声明通过 · 原始证据与执行授权未验证 · 评估时间 ${fmtTime(assessment.as_of)}`
    : "发布证据不可用或不完整 · 当前资格未验证";
  $("readiness-path").innerHTML = model.items.map((item, i) =>
    `<div class="path-step ${item.status}"><span class="path-index">WP${i}</span><strong>${item.label}</strong><span>${labels[item.status]}</span></div>`
  ).join("");
  const next = model.next;
  $("readiness-next").innerHTML = !model.valid
    ? '<div><strong>下一步 · 恢复证据读取</strong><p>检查数据源配置与完整性，再重新评估发布路径。</p></div><button type="button" class="action" data-action="navigate" data-view="system">检查数据源 ↗</button>'
    : next
      ? `<div><strong>首个待核验环节 · ${esc(next.label)}</strong><p>${esc(next.guidance)}</p></div><button type="button" class="action" data-action="navigate" data-view="${next.view}">查看相关工作区 ↗</button>`
      : '<div><strong>门槛声明完整，尚未获得执行资格</strong><p>还需重放原始证据、核验审查身份与发布授权，并通过独立风险门禁和签名策略。</p></div><button type="button" class="action" data-action="navigate" data-view="gates">查看审批记录 ↗</button>';
}

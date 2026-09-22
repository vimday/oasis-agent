import { $, parseJsonExact, FETCH_TIMEOUT_MS } from "./core.js";

// Send original JSON bytes: parsing/re-serializing financial integers in JS loses precision.
export function scoreRequestBody(candidate, policy) {
  if (candidate.length > 1_000_000 || policy.length > 100_000) throw new Error("input too large");
  for (const raw of [candidate, policy]) {
    const value = parseJsonExact(raw);
    if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("object required");
  }
  return `{"candidate":${candidate},"policy":${policy}}`;
}

export function wireResearch() {
  $("score-form").addEventListener("submit", async event => {
    event.preventDefault();
    const button = $("score-submit"), result = $("score-result"), status = $("score-status");
    button.disabled = true;
    result.textContent = "";
    status.textContent = "正在计算研究评分…";
    try {
      const candidate = $("score-candidate").files[0], policy = $("score-policy").files[0];
      if (!candidate || !policy || candidate.size > 1_000_000 || policy.size > 100_000) throw new Error("invalid input");
      const body = scoreRequestBody(await candidate.text(), await policy.text());
      const response = await fetch("/api/v1/research/token-score", {
        method: "POST", headers: {"Content-Type": "application/json"}, body,
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      if (!response.ok) throw new Error("score failed");
      const value = parseJsonExact(await response.text());
      if (value.execution_eligible !== false || value.evidence_level !== "normalized_import_not_raw_attestation") throw new Error("unexpected authority");
      status.textContent = `研究评分 ${value.score_bps} / 10000 · ${value.action} · 不具备交易资格`;
      result.textContent = JSON.stringify(value, null, 2);
    } catch {
      result.textContent = "";
      status.textContent = "评分失败：请检查候选与策略 JSON、文件大小及服务连接。旧结果已清除。";
    } finally {
      button.disabled = false;
    }
  });
}

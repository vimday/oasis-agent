import { $, apiGet, esc, fmtInt, renderTable } from "./core.js";

let pageSize = 50;
export function setPageLimit(maximum) {
  if (!Number.isSafeInteger(maximum) || maximum < 1) throw new Error("无效的后端分页限制");
  pageSize = Math.min(50, maximum);
}

// Each view owns its request generation. A slow earlier filter/page response
// cannot replace a newer selection; failures remove the old verified table.
export function createPagedView(name, path, {query = () => ({}), columns, row, onData = () => {}}) {
  let offset = 0, generation = 0;
  return {
    async load({reset = false, delta = 0} = {}) {
      offset = reset ? 0 : Math.max(0, offset + delta * pageSize);
      const selectedOffset = offset, request = ++generation;
      const params = new URLSearchParams({...query(), limit: pageSize, offset});
      $(name + "-status").textContent = "正在读取并校验…";
      $(name + "-table").setAttribute("aria-busy", "true");
      $(name + "-pager").replaceChildren();
      try {
        const result = await apiGet(path + "?" + params);
        if (request !== generation) return;
        if (!Array.isArray(result.items) || !Number.isSafeInteger(result.total) || result.total < 0) {
          throw new Error("列表响应不完整");
        }
        renderTable(name + "-table", columns, result.items.map(row), "暂无记录");
        onData(result.items);
        $(name + "-status").textContent = "本次读取成功 · " + new Date().toLocaleTimeString("zh-CN");
        const start = result.items.length ? selectedOffset + 1 : 0;
        $(name + "-pager").innerHTML =
          `<button type="button" class="action" data-action="page" data-page="${esc(name)}" data-delta="-1" ${selectedOffset === 0 ? "disabled" : ""}>上一页</button>` +
          `<span>${fmtInt(start)}–${fmtInt(result.items.length ? selectedOffset + result.items.length : 0)} / ${fmtInt(result.total)}</span>` +
          `<button type="button" class="action" data-action="page" data-page="${esc(name)}" data-delta="1" ${selectedOffset + result.items.length >= result.total ? "disabled" : ""}>下一页</button>` +
          `<button type="button" class="action" data-action="page-reset" data-page="${esc(name)}">刷新到首页</button>`;
      } catch {
        if (request !== generation) return;
        renderTable(name + "-table", columns, [], "数据不可用；旧记录已清除");
        onData([]);
        $(name + "-status").textContent = "读取失败、未配置或校验未通过";
        $(name + "-pager").innerHTML = `<button type="button" class="action" data-action="page-reset" data-page="${esc(name)}">重试</button>`;
      } finally {
        if (request === generation) $(name + "-table").setAttribute("aria-busy", "false");
      }
    },
  };
}

let detailGeneration = 0;
export async function showDetail(title, path) {
  const request = ++detailGeneration;
  $("detail-title").textContent = title;
  $("detail-document").textContent = "正在读取…";
  if (!$("record-dialog").open) $("record-dialog").showModal();
  try {
    const record = await apiGet(path);
    if (request !== detailGeneration) return;
    $("detail-document").textContent = record.document || JSON.stringify(record, null, 2);
  } catch {
    if (request === detailGeneration) $("detail-document").textContent = "记录不可用或校验失败";
  }
}

/* 仅操作已渲染文本和本地 diff；不执行报告、源码或 JSON 中的任何代码。 */
(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const fileItems = Array.from(document.querySelectorAll(".file-item"));
  const pathInput = byId("path-filter");
  const bucketInput = byId("bucket-filter");
  const issueInput = byId("issue-filter");
  const announce = (message) => { byId("interaction-status").textContent = message; };

  function filterFiles() {
    const query = pathInput.value.toLocaleLowerCase();
    const bucket = bucketInput.value;
    let shown = 0;
    fileItems.forEach((item) => {
      item.hidden = !item.dataset.path.includes(query)
        || (bucket && item.dataset.bucket !== bucket)
        || (issueInput.checked && item.dataset.hasIssue !== "yes");
      if (!item.hidden) shown += 1;
    });
    byId("filter-result").textContent = `显示 ${shown} / ${fileItems.length} 个文件`;
    byId("empty-filter").hidden = shown !== 0;
  }
  pathInput.addEventListener("input", filterFiles);
  bucketInput.addEventListener("change", filterFiles);
  issueInput.addEventListener("change", filterFiles);
  byId("wrap-code").addEventListener("change", (event) => {
    document.body.classList.toggle("wrap-code", event.target.checked);
  });
  ["expand", "collapse"].forEach((action) => {
    byId(`${action}-all`).addEventListener("click", () => {
      document.querySelectorAll("details[data-review-expand]").forEach((details) => {
        details.open = action === "expand";
      });
    });
  });

  function openTarget(fragment, scroll) {
    let id;
    try { id = decodeURIComponent(fragment.replace(/^#/, "")); } catch (_) { return; }
    const target = byId(id);
    if (!target) return;
    const file = target.closest(".file-item");
    if (file && file.hidden) {
      pathInput.value = "";
      bucketInput.value = "";
      issueInput.checked = false;
      filterFiles();
    }
    let element = target;
    while (element) {
      if (element.tagName === "DETAILS") element.open = true;
      element = element.parentElement;
    }
    if (scroll) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-open-target]");
    if (link) openTarget(link.getAttribute("href"), false);
  });
  window.addEventListener("hashchange", () => openTarget(window.location.hash, true));
  if (window.location.hash) openTarget(window.location.hash, true);

  function rawPatch(button) {
    const data = button.closest(".file-content").querySelector(".raw-patch");
    const bytes = Uint8Array.from(atob(data.dataset.patchBase64), (char) => char.charCodeAt(0));
    return { patch: new TextDecoder("utf-8", { fatal: true }).decode(bytes), filename: data.dataset.filename };
  }
  document.querySelectorAll("[data-copy-patch]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        const { patch } = rawPatch(button);
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(patch);
        } else {
          const textarea = document.createElement("textarea");
          textarea.value = patch;
          document.body.appendChild(textarea);
          textarea.select();
          const copied = document.execCommand("copy");
          textarea.remove();
          if (!copied) throw new Error("copy unavailable");
        }
        announce("已复制此文件完整 diff。");
      } catch (_) {
        announce("当前浏览器未允许复制；可使用下载 diff 按钮。");
      }
    });
  });
  document.querySelectorAll("[data-download-patch]").forEach((button) => {
    button.addEventListener("click", () => {
      try {
        const { patch, filename } = rawPatch(button);
        const url = URL.createObjectURL(new Blob([patch], { type: "text/plain;charset=utf-8" }));
        const link = document.createElement("a");
        link.href = url;
        link.download = filename.replace(/[\\/]/g, "_") + ".diff";
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (_) {
        announce("未能生成本地 diff 下载。");
      }
    });
  });
})();

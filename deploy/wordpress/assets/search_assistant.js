/* 增强已核实的#site-search搜索弹层；不拦截原Search提交。 */
(() => {
  "use strict";
  const config = window.TAYUNE_SEARCH_ASSISTANT;
  const dialog = document.getElementById("site-search");
  if (!config || !dialog || dialog.querySelector("[data-tayune-ai]")) return;
  const form = dialog.querySelector('form[role="search"]');
  const input = form && form.querySelector('input[name="s"]');
  const fields = form && form.querySelector(".search-fields");
  if (!input || !fields) return; // 主题改变时原搜索仍可用。
  const ask = document.createElement("button");
  ask.type = "button";
  ask.textContent = "Ask AI";
  ask.setAttribute("data-tayune-ai", "");
  ask.setAttribute("aria-controls", "tayune-ai-results");
  fields.appendChild(ask);
  const panel = document.createElement("section");
  panel.id = "tayune-ai-results";
  panel.hidden = true;
  panel.setAttribute("aria-label", "Tea assistant conversation");
  const intro = document.createElement("p");
  intro.textContent = "Ask about tea, brewing or shop policies. AI searches across shop information; the category menu applies to ordinary Search. Please do not share private details.";
  const log = document.createElement("div");
  log.className = "tayune-ai-messages";
  log.setAttribute("role", "log");
  log.setAttribute("aria-live", "polite");
  const status = document.createElement("p");
  status.setAttribute("role", "status");
  const contact = document.createElement("a");
  contact.href = config.contactUrl;
  contact.textContent = "Contact the shop";
  const reset = document.createElement("button");
  reset.type = "button";
  reset.textContent = "New AI conversation";
  panel.append(intro, log, status, contact, reset);
  form.after(panel);
  let token = ""; // 仅保存于页面内存，不存localStorage或DOM。
  let pending = false;
  function show(text, role) {
    const item = document.createElement("p");
    item.className = "tayune-ai-message " + role;
    item.textContent = text; // 不用innerHTML执行模型返回的内容。
    log.appendChild(item);
    log.scrollTop = log.scrollHeight;
  }
  async function post(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 160000);
    try {
      const headers = {"Content-Type": "application/json"};
      if (path === "/api/chat") headers.Authorization = "Bearer " + token;
      const response = await fetch(config.apiBase + path, {
        method: "POST", headers, body: JSON.stringify(body), signal: controller.signal,
      });
      if (response.status === 401) token = "";
      if (!response.ok) throw new Error("Assistant unavailable");
      return await response.json();
    } finally { clearTimeout(timer); }
  }
  ask.addEventListener("click", async () => {
    if (pending) return;
    const question = input.value.trim();
    panel.hidden = false;
    if (!question || question.length > 500) {
      status.textContent = "Enter a tea question of up to 500 characters in the search box.";
      input.focus();
      return;
    }
    pending = true;
    ask.disabled = true;
    reset.disabled = true;
    status.textContent = "Looking into your question…";
    show(question, "user");
    try {
      if (!token) token = (await post("/api/session", {})).session_token;
      const result = await post("/api/chat", {message: question});
      show(result.answer, "assistant");
      for (const source of result.sources || []) {
        let url;
        try { url = new URL(source.url); } catch { continue; }
        if (url.protocol !== "https:" || url.hostname !== "tayunetea.com") continue;
        const link = document.createElement("a");
        link.href = url.href;
        link.textContent = source.title;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        const row = document.createElement("p");
        row.appendChild(link);
        log.appendChild(row);
      }
      status.textContent = result.degraded ? "Some information could not be verified. You can still use Search or contact us." : "Ask a follow-up in the same search box. Confirm price and availability at checkout.";
      // 保留原问题，用户随时可以用原Search继续普通搜索。
    } catch {
      status.textContent = "The tea assistant is unavailable. Use Search to browse products and articles, or contact the shop.";
    } finally {
      pending = false;
      ask.disabled = false;
      reset.disabled = false;
    }
  });
  reset.addEventListener("click", () => {
    token = "";
    log.replaceChildren();
    status.textContent = "New conversation. Enter your next question above.";
    input.focus();
  });
})();

const els = {
  roomInput: document.querySelector("#roomInput"),
  keywordInput: document.querySelector("#keywordInput"),
  cookieInput: document.querySelector("#cookieInput"),
  modeSelect: document.querySelector("#modeSelect"),
  guardSelect: document.querySelector("#guardSelect"),
  repeatInput: document.querySelector("#repeatInput"),
  connectBtn: document.querySelector("#connectBtn"),
  disconnectBtn: document.querySelector("#disconnectBtn"),
  shutdownBtn: document.querySelector("#shutdownBtn"),
  saveBtn: document.querySelector("#saveBtn"),
  clearBtn: document.querySelector("#clearBtn"),
  resetOverlayBtn: document.querySelector("#resetOverlayBtn"),
  importBtn: document.querySelector("#importBtn"),
  guardImport: document.querySelector("#guardImport"),
  statusPill: document.querySelector("#statusPill"),
  statusText: document.querySelector("#statusText"),
  roomLine: document.querySelector("#roomLine"),
  queueCount: document.querySelector("#queueCount"),
  guardCount: document.querySelector("#guardCount"),
  ruleText: document.querySelector("#ruleText"),
  queueBody: document.querySelector("#queueBody"),
  guardList: document.querySelector("#guardList"),
  logList: document.querySelector("#logList"),
};

const modeLabels = {
  historical: "曾经上过舰",
  current: "当前在舰",
  all: "不限制",
};

const guardLabels = {
  1: "总督",
  2: "提督及以上",
  3: "舰长及以上",
};

let hydrated = false;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `请求失败：${response.status}`);
  }
  return data;
}

function formSettings() {
  return {
    room: els.roomInput.value.trim(),
    keyword: els.keywordInput.value.trim() || "排队",
    eligibility_mode: els.modeSelect.value,
    required_guard_level: Number(els.guardSelect.value),
    allow_repeat: els.repeatInput.checked,
  };
}

function connectSettings() {
  return {
    ...formSettings(),
    cookie: els.cookieInput.value.trim(),
  };
}

function hydrateSettings(settings) {
  if (hydrated) return;
  els.roomInput.value = settings.room || "";
  els.keywordInput.value = settings.keyword || "排队";
  els.cookieInput.value = settings.cookie || "";
  els.modeSelect.value = settings.eligibility_mode || "historical";
  els.guardSelect.value = String(settings.required_guard_level || 3);
  els.repeatInput.checked = Boolean(settings.allow_repeat);
  syncModeControls();
  hydrated = true;
}

function splitKeywords(value) {
  if (Array.isArray(value)) {
    return value.flatMap((item) => splitKeywords(item));
  }
  return String(value ?? "")
    .replace(/\r/g, "\n")
    .split(/[\n,，;；|]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatKeywords(value) {
  const keywords = splitKeywords(value);
  return keywords.length ? keywords.join("、") : "排队";
}

function renderStatus(status) {
  els.statusPill.classList.toggle("connected", Boolean(status.connected));
  els.statusPill.classList.toggle("running", Boolean(status.running && !status.connected));

  if (status.connected) {
    els.statusText.textContent = "已连接";
  } else if (status.running) {
    els.statusText.textContent = "连接中";
  } else {
    els.statusText.textContent = "未连接";
  }

  const room = status.room || els.roomInput.value.trim();
  const realRoom = status.real_room_id ? ` / ${status.real_room_id}` : "";
  els.roomLine.textContent = room ? `直播间 ${room}${realRoom}` : "未连接";
  els.connectBtn.disabled = Boolean(status.running);
  els.disconnectBtn.disabled = !status.running;
}

function renderMetrics(state) {
  els.queueCount.textContent = `${state.counts.queue} 人`;
  els.guardCount.textContent = String(state.counts.guards);
  const settings = state.settings;
  const mode = modeLabels[settings.eligibility_mode] || "曾经上过舰";
  const guard = settings.eligibility_mode === "all" ? "全部" : guardLabels[settings.required_guard_level];
  const keyword = formatKeywords(settings.keyword);
  const eligibility = settings.eligibility_mode === "current" ? `${mode} / ${guard}` : mode;
  els.ruleText.textContent = `${keyword} / ${eligibility}`;
}

function syncModeControls() {
  const usesGuardLevel = els.modeSelect.value === "current";
  els.guardSelect.disabled = !usesGuardLevel;
}

function renderQueue(rows) {
  if (!rows.length) {
    els.queueBody.innerHTML = `<tr><td colspan="6" class="empty">暂无排队</td></tr>`;
    return;
  }

  els.queueBody.innerHTML = rows
    .map(
      (row) => `
        <tr>
          <td>#${escapeHtml(row.queue_no)}</td>
          <td>
            <strong class="uname">${escapeHtml(row.uname || "未知用户")}</strong>
            <span class="uid">${escapeHtml(row.uid || "")}</span>
          </td>
          <td><span class="badge">${escapeHtml(row.guard_name || "无")}</span></td>
          <td class="message">${escapeHtml(row.message || "")}</td>
          <td>${escapeHtml(row.note || "")}</td>
          <td>${shortTime(row.queued_at)}</td>
        </tr>
      `,
    )
    .join("");
}

function renderGuards(rows) {
  if (!rows.length) {
    els.guardList.innerHTML = `<div class="guard-item"><strong>暂无名单</strong><span></span></div>`;
    return;
  }
  els.guardList.innerHTML = rows
    .slice(0, 80)
    .map(
      (row) => `
        <div class="guard-item">
          <strong>${escapeHtml(row.uname || "未知用户")}</strong>
          <span>${escapeHtml(row.uid)} · 最高 ${escapeHtml(row.best_guard_name)} · 最近 ${escapeHtml(row.last_guard_name)}</span>
        </div>
      `,
    )
    .join("");
}

function renderLogs(rows) {
  if (!rows.length) {
    els.logList.innerHTML = `<div class="log-item"><strong>暂无日志</strong><span></span></div>`;
    return;
  }
  els.logList.innerHTML = rows
    .slice(-120)
    .map(
      (row) => `
        <div class="log-item ${escapeHtml(row.level)}">
          <strong>${escapeHtml(row.message)}</strong>
          <span>${shortTime(row.created_at)}</span>
        </div>
      `,
    )
    .join("");
  els.logList.scrollTop = els.logList.scrollHeight;
}

function shortTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

async function refresh() {
  try {
    const state = await api("/api/state");
    hydrateSettings({
      ...state.settings,
      cookie: state.cookie || "",
    });
    renderStatus(state.status);
    renderMetrics(state);
    renderQueue(state.queue);
    renderGuards(state.guards);
    renderLogs(state.logs);
  } catch (error) {
    els.statusText.textContent = error.message;
  }
}

async function saveSettings() {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify(formSettings()),
  });
  hydrated = false;
  await refresh();
}

els.saveBtn.addEventListener("click", async () => {
  try {
    await saveSettings();
  } catch (error) {
    alert(error.message);
  }
});

els.modeSelect.addEventListener("change", syncModeControls);

els.connectBtn.addEventListener("click", async () => {
  try {
    await api("/api/connect", {
      method: "POST",
      body: JSON.stringify(connectSettings()),
    });
    await refresh();
  } catch (error) {
    alert(error.message);
  }
});

els.disconnectBtn.addEventListener("click", async () => {
  try {
    await api("/api/disconnect", { method: "POST", body: "{}" });
    await refresh();
  } catch (error) {
    alert(error.message);
  }
});

els.shutdownBtn.addEventListener("click", async () => {
  if (!confirm("退出 DanmuQueue？本地监听服务会停止。")) return;
  try {
    await api("/api/shutdown", { method: "POST", body: "{}" });
    els.statusText.textContent = "正在退出";
  } catch (error) {
    alert(error.message);
  }
});

els.clearBtn.addEventListener("click", async () => {
  if (!confirm("清空当前队列？")) return;
  try {
    await api("/api/queue/clear", { method: "POST", body: "{}" });
    await refresh();
  } catch (error) {
    alert(error.message);
  }
});

els.resetOverlayBtn.addEventListener("click", async () => {
  try {
    await api("/api/queue/overlay/reset", { method: "POST", body: "{}" });
    await refresh();
  } catch (error) {
    alert(error.message);
  }
});

els.importBtn.addEventListener("click", async () => {
  try {
    const data = await api("/api/guards/import", {
      method: "POST",
      body: JSON.stringify({ text: els.guardImport.value }),
    });
    els.guardImport.value = "";
    alert(`已导入 ${data.imported} 个`);
    await refresh();
  } catch (error) {
    alert(error.message);
  }
});

refresh();
setInterval(refresh, 1200);

const overlayEls = {
  title: document.querySelector("#overlayTitle"),
  meta: document.querySelector("#overlayMeta"),
  status: document.querySelector("#overlayStatus"),
  queue: document.querySelector("#overlayQueue"),
};

const hidingQueueNos = new Set();

function getParam(name, fallback) {
  const value = new URLSearchParams(window.location.search).get(name);
  return value && value.trim() ? value.trim() : fallback;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function renderStatus(status, count) {
  overlayEls.status.classList.toggle("connected", Boolean(status.connected));
  overlayEls.status.classList.toggle("running", Boolean(status.running && !status.connected));

  if (status.connected) {
    overlayEls.status.textContent = "在线";
  } else if (status.running) {
    overlayEls.status.textContent = "重连";
  } else {
    overlayEls.status.textContent = "离线";
  }

  const room = status.room ? `直播间 ${status.room}` : "未连接";
  overlayEls.meta.textContent = `${room} · ${count} 人排队`;
}

function visibleRows(rows) {
  return rows.filter((row) => !hidingQueueNos.has(Number(row.queue_no)));
}

function renderQueue(rows) {
  const visible = visibleRows(rows);
  if (!visible.length) {
    overlayEls.queue.innerHTML = `<li class="empty">暂无排队</li>`;
    return;
  }

  overlayEls.queue.innerHTML = visible
    .map((row) => {
      const name = row.uname || `UID ${row.uid || "未知"}`;
      const note = String(row.note || "").trim();
      return `
        <li class="queue-item" data-queue-no="${escapeHtml(row.queue_no)}" data-name="${escapeHtml(name)}" data-note="${escapeHtml(note)}">
          <span class="queue-no">${escapeHtml(row.queue_no)}.</span>
          <span class="queue-copy">
            <span class="queue-name">${escapeHtml(name)}</span>
            ${note ? `<span class="queue-note">${escapeHtml(note)}</span>` : ""}
          </span>
          <span class="queue-actions">
            <button class="queue-action note-action" type="button" data-action="note" aria-label="填写备注">&#9998;&#xfe0e;</button>
            <button class="queue-action hide-action" type="button" data-action="hide" aria-label="从 overlay 隐藏">&times;</button>
          </span>
        </li>
      `;
    })
    .join("");
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || "请求失败");
  }
  return data;
}

async function hideOverlayItem(queueNo, name) {
  if (!queueNo || hidingQueueNos.has(queueNo)) return;
  if (!confirm(`从 overlay 移除 #${queueNo} ${name || ""}？\n\n后台队列和导出记录仍会保留。`)) return;

  hidingQueueNos.add(queueNo);
  renderQueue(
    [...overlayEls.queue.querySelectorAll(".queue-item")].map((item) => ({
      queue_no: Number(item.dataset.queueNo),
      uname: item.querySelector(".queue-name")?.textContent || "",
      note: item.dataset.note || "",
    })),
  );

  try {
    await postJson("/api/queue/overlay/hide", { queue_no: queueNo });
    hidingQueueNos.delete(queueNo);
    await refreshOverlay();
  } catch (error) {
    hidingQueueNos.delete(queueNo);
    await refreshOverlay();
    overlayEls.status.textContent = "错误";
    overlayEls.status.classList.remove("connected", "running");
    overlayEls.meta.textContent = error.message;
  }
}

async function editQueueNote(queueNo, name, currentNote) {
  if (!queueNo) return;
  const note = prompt(`#${queueNo} ${name || ""} 的备注/比分`, currentNote || "");
  if (note === null) return;

  try {
    await postJson("/api/queue/note", { queue_no: queueNo, note });
    await refreshOverlay();
  } catch (error) {
    overlayEls.status.textContent = "错误";
    overlayEls.status.classList.remove("connected", "running");
    overlayEls.meta.textContent = error.message;
  }
}

async function refreshOverlay() {
  try {
    const response = await fetch("/api/state");
    const state = await response.json();
    const rows = state.overlay_queue || state.queue || [];
    overlayEls.title.textContent = getParam("title", "排队中");
    renderStatus(state.status, visibleRows(rows).length);
    renderQueue(rows);
  } catch (error) {
    overlayEls.status.textContent = "错误";
    overlayEls.status.classList.remove("connected", "running");
    overlayEls.meta.textContent = "无法读取本地队列";
  }
}

overlayEls.queue.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const item = button.closest(".queue-item");
  if (!item) return;

  const queueNo = Number(item.dataset.queueNo);
  const name = item.dataset.name || "";
  const note = item.dataset.note || "";
  if (button.dataset.action === "note") {
    await editQueueNote(queueNo, name, note);
  } else if (button.dataset.action === "hide") {
    await hideOverlayItem(queueNo, name);
  }
});

refreshOverlay();
setInterval(refreshOverlay, 1000);

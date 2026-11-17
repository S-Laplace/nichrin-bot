"use strict";

const find = (id) => document.getElementById(id);
const repeatNames = { once: "한 번", daily: "매일", weekly: "매주", monthly: "매월" };
const deliveryNames = {
  submitted: "발송 요청",
  uncertain: "결과 불명",
  missed: "시간 초과",
  sending: "발송 중"
};

function notice(message, isError = false) {
  const element = find("notice");
  element.textContent = message;
  element.classList.toggle("error", isError);
}

function showError(error) {
  notice(error.message || String(error), true);
}

async function request(path, method = "GET", body = null) {
  const response = await fetch(path, {
    method,
    cache: "no-store",
    headers: body === null ? {} : { "Content-Type": "application/json" },
    body: body === null ? null : JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "요청 실패 (HTTP " + response.status + ")");
  }
  return data;
}

function dateLabel(value) {
  return value ? String(value).replace("T", " ") : "—";
}

function roomLabel(name, roomId) {
  const title = String(name || roomId);
  return title === String(roomId) ? title + " (이름 확인 중)" : title;
}

function addCell(row, value, className = "") {
  const cell = document.createElement("td");
  cell.textContent = value === null || value === undefined ? "—" : String(value);
  if (className) cell.className = className;
  row.appendChild(cell);
  return cell;
}

function setEmpty(tbody, count, message) {
  tbody.replaceChildren();
  const row = document.createElement("tr");
  const cell = addCell(row, message, "empty");
  cell.colSpan = count;
  tbody.appendChild(row);
}

function addStatus(row, value, active) {
  const cell = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = "status-label" + (active ? "" : " off");
  badge.textContent = value;
  cell.appendChild(badge);
  row.appendChild(cell);
  return badge;
}

function renderJobs(jobs) {
  const tbody = find("jobs");
  const room = find("filter").value;
  const visible = jobs.filter((item) => !room || String(item.room_id) === room);
  tbody.replaceChildren();

  if (!visible.length) {
    setEmpty(tbody, 6, "해당 채팅방에 예약이 없습니다.");
    return;
  }

  for (const job of visible) {
    const row = document.createElement("tr");
    const name = addCell(row, roomLabel(job.room_title, job.room_id));
    const sub = document.createElement("span");
    sub.className = "subtext";
    sub.textContent = "채팅방 ID " + job.room_id;
    name.appendChild(sub);

    addCell(row, dateLabel(job.next_run));
    addCell(row, repeatNames[job.repeat] || job.repeat);
    addCell(row, job.message, "message-cell");
    addStatus(row, job.active ? "예약됨" : "중지 / 완료", Boolean(job.active));

    const controlCell = document.createElement("td");
    if (job.next_run) {
      const button = document.createElement("button");
      const action = job.active ? "disable" : "enable";
      button.type = "button";
      button.className = "action-button" + (job.active ? " danger" : "");
      button.textContent = job.active ? "중지" : "재개";
      button.dataset.job = String(job.id);
      button.dataset.action = action;
      button.setAttribute("aria-label", job.room_title + " 예약 " + job.id + " " + button.textContent);
      controlCell.appendChild(button);
    } else {
      controlCell.textContent = "—";
    }
    row.appendChild(controlCell);
    tbody.appendChild(row);
  }
}

function renderHistory(items) {
  const tbody = find("history");
  tbody.replaceChildren();

  if (!items.length) {
    setEmpty(tbody, 4, "아직 발송 기록이 없습니다.");
    return;
  }

  for (const item of items) {
    const row = document.createElement("tr");
    addCell(row, dateLabel(item.due_at));
    addCell(row, roomLabel(item.room_title, item.room_id));
    const badge = addStatus(row, deliveryNames[item.status] || item.status,
      item.status === "submitted");
    if (item.status === "uncertain" || item.status === "missed") {
      badge.className = "status-label uncertain";
    }
    addCell(row, item.detail || "—");
    tbody.appendChild(row);
  }
}

function updateRooms(rooms) {
  const originalFilter = find("filter").value;
  const roomSelect = find("room");
  const originalRoom = roomSelect.value;
  const filterSelect = find("filter");
  roomSelect.replaceChildren(new Option("채팅방 선택", ""));
  filterSelect.replaceChildren(new Option("전체 채팅방", ""));

  for (const room of rooms) {
    const id = String(room.id);
    const name = roomLabel(room.title, id);
    const label = name.endsWith(" (이름 확인 중)") ? name : name + " (" + id + ")";
    roomSelect.appendChild(new Option(label, id));
    filterSelect.appendChild(new Option(name, id));
  }

  if ([...filterSelect.options].some((option) => option.value === originalFilter)) {
    filterSelect.value = originalFilter;
  }
  if ([...roomSelect.options].some((option) => option.value === originalRoom)) {
    roomSelect.value = originalRoom;
  }
}

async function refresh() {
  const [status, jobs, history] = await Promise.all([
    request("/api/status"),
    request("/api/jobs"),
    request("/api/history")
  ]);
  const mode = find("mode");
  mode.dataset.live = String(Boolean(status.live));
  mode.textContent = status.live ? "실제 발송 작동 중" : "미리보기 모드";
  renderJobs(jobs.jobs);
  renderHistory(history.history);
}

let refreshing = false;

async function reloadDashboard() {
  if (refreshing) return;
  refreshing = true;
  try {
    await refresh();
    notice("로컬 연결 완료");
    try {
      const response = await request("/api/rooms");
      updateRooms(response.rooms);
    } catch (error) {
      notice("예약 조회는 가능하지만 채팅방을 불러오지 못했습니다: " + error.message, true);
    }
  } catch (error) {
    find("mode").textContent = "연결 실패";
    delete find("mode").dataset.live;
    showError(error);
  } finally {
    refreshing = false;
  }
}

find("reload").addEventListener("click", reloadDashboard);
reloadDashboard();
setInterval(() => {
  if (!document.hidden) reloadDashboard();
}, 30000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) reloadDashboard();
});

find("newjob").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = {
    room_id: find("room").value,
    at: find("at").value,
    repeat: find("repeat").value,
    message: find("message").value
  };
  try {
    await request("/api/jobs", "POST", body);
    find("message").value = "";
    await refresh();
    notice("예약이 등록되었습니다.");
  } catch (error) {
    showError(error);
  }
});

find("filter").addEventListener("change", () => {
  refresh().catch(showError);
});

find("jobs").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-job]");
  if (!button) return;
  button.disabled = true;
  try {
    await request("/api/jobs/" + button.dataset.job + "/" + button.dataset.action, "POST", {});
    await refresh();
    notice("예약 상태를 변경했습니다.");
  } catch (error) {
    showError(error);
  } finally {
    button.disabled = false;
  }
});

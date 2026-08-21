const statusBox = document.querySelector("#status");
const listContainer = document.getElementById("reportsListContainer");
const tbody = document.getElementById("reportsTableBody");
const emptyState = document.getElementById("emptyState");

function setStatus(message, type = "idle") {
  statusBox.textContent = message;
  statusBox.className = `status-banner ${type}`;
}

function formatDate(unixSeconds) {
  return new Date(unixSeconds * 1000).toLocaleString("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function formatExpiry(expiresAt) {
  const secondsLeft = expiresAt - Math.floor(Date.now() / 1000);
  if (secondsLeft <= 0) return "Expired";
  const daysLeft = Math.ceil(secondsLeft / 86400);
  return `${daysLeft} day${daysLeft === 1 ? "" : "s"} left`;
}

function renderReports(reports) {
  if (reports.length === 0) {
    listContainer.hidden = true;
    emptyState.hidden = false;
    return;
  }

  emptyState.hidden = true;
  listContainer.hidden = false;
  tbody.innerHTML = "";

  reports.forEach((report) => {
    const row = document.createElement("div");
    row.className = "file-row report-row-grid";
    const basePath = window.__BASE_PATH__ || "";
    const downloadUrl = `${window.__BACKEND_URL__ || ""}${basePath}/download/${report.jobId}?userId=${encodeURIComponent(report.userId)}`;

    row.innerHTML = `
      <div class="col-file"><span class="pdf-badge">XLSX</span> ${report.generatedFilename}</div>
      <div>${report.originalFilenames.join(", ")}</div>
      <div>${formatDate(report.createdAt)}</div>
      <div>${formatExpiry(report.expiresAt)}</div>
      <div><a class="btn-download report-download-btn" href="${downloadUrl}">Download</a></div>
    `;

    tbody.appendChild(row);
  });
}

async function loadReports() {
  const userId = resolveUserId();
  try {
    const response = await fetch(`${window.__BACKEND_URL__ || ""}${window.__BASE_PATH__ || ""}/api/reports?userId=${encodeURIComponent(userId)}`);
    if (!response.ok) {
      throw new Error(`Server error ${response.status}`);
    }
    const data = await response.json();
    renderReports(data.reports || []);
    setStatus(`${data.reports.length} report${data.reports.length === 1 ? "" : "s"} available.`, "success");
  } catch (error) {
    setStatus(`Could not load your reports: ${error.message}`, "error");
  }
}

loadReports();

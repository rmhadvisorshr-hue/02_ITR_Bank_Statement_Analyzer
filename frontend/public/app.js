const form = document.querySelector("#uploadForm");
const input = document.querySelector("#pdfInput");
const dropZone = document.querySelector("#dropZone");
const fileCount = document.querySelector("#fileCount");
const fileNames = document.querySelector("#fileNames");
const generateBtn = document.querySelector("#generateBtn");
const statusBox = document.querySelector("#status");
const result = document.querySelector("#result");
const downloadLink = document.querySelector("#downloadLink");

const statementsMetric = document.querySelector("#statementsMetric");
const transactionsMetric = document.querySelector("#transactionsMetric");
const receiptsMetric = document.querySelector("#receiptsMetric");
const paymentsMetric = document.querySelector("#paymentsMetric");

const loaderOverlay = document.getElementById("loaderOverlay");
const tickerText = document.getElementById("tickerText");
const auditMetricStatus = document.getElementById("auditMetricStatus");
const auditMetricRecon = document.getElementById("auditMetricRecon");

const messages = [
  "Reading your PDF statements...",
  "Detecting transaction tables...",
  "Extracting rows and columns...",
  "Categorizing transactions...",
  "Reconciling balances...",
  "Building your Excel workbook...",
  "Almost done..."
];

let tickerInterval;
let messageIdx = 0;

function startLoader() {
  loaderOverlay.classList.remove("hidden");
  setTimeout(() => loaderOverlay.classList.add("visible"), 10);
  
  messageIdx = 0;
  tickerText.textContent = messages[messageIdx];

  auditMetricStatus.textContent = "Starting";
  auditMetricRecon.textContent = "Standby";

  let ticks = 0;
  tickerInterval = setInterval(() => {
    ticks++;
    if (ticks % 3 === 0) {
      messageIdx = (messageIdx + 1) % messages.length;
      tickerText.textContent = messages[messageIdx];
    }

    if (ticks === 2) auditMetricStatus.textContent = "Extracting";
    if (ticks === 5) auditMetricStatus.textContent = "Analyzing";
    if (ticks === 8) auditMetricStatus.textContent = "Compiling";
    if (ticks > 12) auditMetricStatus.textContent = "Finalizing";

    if (ticks === 4) auditMetricRecon.textContent = "In progress";
    if (ticks === 10) auditMetricRecon.textContent = "Verified";
  }, 600);
}

function stopLoader() {
  clearInterval(tickerInterval);
  loaderOverlay.classList.remove("visible");
  setTimeout(() => loaderOverlay.classList.add("hidden"), 500);
}

function formatMoney(value) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(Number(value || 0));
}

function setStatus(message, type = "idle") {
  statusBox.textContent = message;
  statusBox.className = `status-banner ${type}`;
}

let selectedFiles = [];

function renderFileList() {
  const container = document.getElementById('fileListContainer');
  const tbody = document.getElementById('fileTableBody');
  
  if (selectedFiles.length === 0) {
    container.hidden = true;
    generateBtn.disabled = true;
    fileCount.textContent = "No files selected";
    fileNames.textContent = "";
    setStatus("Ready. Add a statement to begin.", "idle");
    return;
  }

  container.hidden = false;
  generateBtn.disabled = false;
  fileCount.textContent = `${selectedFiles.length} Document${selectedFiles.length === 1 ? "" : "s"} Ready`;
  fileNames.textContent = selectedFiles.map(f => f.name).join(", ");
  setStatus("Ready to analyze.", "idle");
  
  tbody.innerHTML = '';
  selectedFiles.forEach((file, index) => {
    const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
    
    const row = document.createElement('div');
    row.className = 'file-row';
    
    row.innerHTML = `
      <div class="col-id">${index + 1}</div>
      <div class="col-file">
        <span class="pdf-badge">PDF</span>
        ${file.name}
      </div>
      <div class="col-size">${sizeMb} MB</div>
      <div class="col-pwd">
        <input type="text" class="pwd-input" placeholder="Optional" data-index="${index}" />
      </div>
      <div class="col-action">
        <button type="button" class="btn-remove" data-index="${index}" title="Remove file">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </button>
      </div>
    `;
    
    tbody.appendChild(row);
  });
  
  document.querySelectorAll('.btn-remove').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const idx = parseInt(e.currentTarget.dataset.index);
      selectedFiles.splice(idx, 1);
      renderFileList();
    });
  });
}

function updateFiles(files) {
  const pdfs = [...files].filter((file) => file.name.toLowerCase().endsWith(".pdf"));
  pdfs.forEach(newFile => {
     if (!selectedFiles.find(f => f.name === newFile.name && f.size === newFile.size)) {
        selectedFiles.push(newFile);
     }
  });
  renderFileList();
}

input.addEventListener("change", () => updateFiles(input.files));

dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropZone.classList.add("dragover");
});

dropZone.addEventListener("dragleave", () => {
  dropZone.classList.remove("dragover");
});

dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone.classList.remove("dragover");
  updateFiles(event.dataTransfer.files);
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedFiles.length) return;

  const body = new FormData();
  body.append("userId", resolveUserId());
  selectedFiles.forEach((file) => {
    body.append("pdfs", file);
  });

  const pwdInputs = document.querySelectorAll('.pwd-input');
  pwdInputs.forEach(input => {
    const pwd = input.value.trim();
    if (pwd) {
      body.append("statementPassword", pwd);
    }
  });

  const processingMode = document.querySelector('input[name="processingMode"]:checked')?.value || 'normal';
  if (processingMode === 'scanned') {
    body.append("autoOCR", "true");
  }

  generateBtn.disabled = true;
  result.hidden = true;
  setStatus("Processing your statements...", "working");

  loaderOverlay.classList.remove("hidden");
  setTimeout(() => loaderOverlay.classList.add("visible"), 10);

  auditMetricStatus.textContent = "Starting";
  auditMetricRecon.textContent = "Standby";
  tickerText.textContent = "Uploading securely...";

  try {
    const response = await fetch((window.__BACKEND_URL__ || "") + (window.__BASE_PATH__ || "") + "/api/generate", { method: "POST", body });
    if (!response.ok && !response.body) {
       throw new Error("Server error " + response.status);
    }
    
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); 
      
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const data = JSON.parse(line);
          
          if (typeof data.elapsedMs === 'number') {
            const stamp = `[+${(data.elapsedMs / 1000).toFixed(1)}s]`;
            if (data.phase) {
              console.log(`${stamp} ${data.phase} finished in ${(data.phaseDurationMs / 1000).toFixed(1)}s`);
            } else if (data.message) {
              console.log(`${stamp} ${data.message}`);
            }
          }

          if (data.type === 'progress') {
            if (data.message) {
              tickerText.textContent = data.message;
              auditMetricStatus.textContent = "Processing";
            }
            if (data.pdf_progress) {
              const { current, total } = data.pdf_progress;
              const pct = Math.round((current / total) * 100);
              
              const pCont = document.getElementById("pdfProgressContainer");
              const pBar = document.getElementById("pdfProgressBar");
              const pText = document.getElementById("pdfProgressText");
              
              pCont.classList.remove("hidden");
              pBar.style.width = `${pct}%`;
              pText.textContent = `${pct}%`;
            }
            if (data.sheet) {
              auditMetricStatus.textContent = `Building Sheet`;
              auditMetricRecon.textContent = data.sheet;
              
              // Hide progress bar once excel generation starts
              const pCont = document.getElementById("pdfProgressContainer");
              if (pCont) pCont.classList.add("hidden");
            }
          } 
          else if (data.type === 'error' || (data.error && !data.type)) {
            const detail = Array.isArray(data.details)
              ? data.details.map((item) => `${item.file}: ${item.issue}`).join("; ")
              : data.details || data.error;
            throw new Error(detail || "Processing failed.");
          }
          else if (data.type === 'done') {
            const payload = data.payload;
            statementsMetric.textContent = payload.statements;
            transactionsMetric.textContent = payload.transactions;
            receiptsMetric.textContent = formatMoney(payload.total_receipts);
            paymentsMetric.textContent = formatMoney(payload.total_payments);
            downloadLink.href = (window.__BACKEND_URL__ || "") + payload.downloadUrl;
            console.log('Timing breakdown:', payload.timings);

            result.hidden = false;
            setStatus(
              payload.persistent === false
                ? "Analysis complete. Download now - this report is not saved to your report history."
                : "Analysis complete. Your Excel file is ready.",
              "success"
            );
          }
        } catch (e) {
          if (e.message !== "Unexpected end of JSON input") {
             if (e.message !== "Processing failed." && !e.message.includes("failed") && !e.message.includes("Error")) {
                console.error("Parse error:", e);
             } else {
                throw e;
             }
          }
        }
      }
    }
    
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    loaderOverlay.classList.remove("visible");
    setTimeout(() => loaderOverlay.classList.add("hidden"), 500);
    generateBtn.disabled = selectedFiles.length === 0;
  }
});

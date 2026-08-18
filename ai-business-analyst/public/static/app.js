/**
 * DataWise — AI Business Analyst (Vercel Serverless Demo SPA)
 *
 * DEMO-MODE-SPECIFIC:
 * This adapted client runs against Vercel serverless architecture:
 * - CSV uploads and schema introspection run live via /api/upload.py
 * - Question answering evaluates against pre-recorded verified responses
 * - Database connections and live glossary generation show demo notices
 */

// ── DEMO MODE FLAG ─────────────────────────────────────────────
const DEMO_MODE = true;

// ── Pre-loaded example dataset for instant 1-click preview ─────
const SAMPLE_SALES_SCHEMA = {
  connection_id: "demo-sample-sales",
  generated_at: new Date().toISOString(),
  tables: [
    {
      name: "sales",
      kind: "table",
      row_count: 100,
      row_count_is_estimate: false,
      primary_key_columns: [],
      foreign_keys: [],
      columns: [
        { name: "region", type: "text", nullable: false, primary_key: false },
        { name: "country", type: "text", nullable: false, primary_key: false },
        { name: "item_type", type: "text", nullable: false, primary_key: false },
        { name: "sales_channel", type: "text", nullable: false, primary_key: false },
        { name: "order_priority", type: "text", nullable: false, primary_key: false },
        { name: "order_date", type: "date", nullable: false, primary_key: false },
        { name: "order_id", type: "integer", nullable: false, primary_key: false },
        { name: "ship_date", type: "date", nullable: false, primary_key: false },
        { name: "units_sold", type: "integer", nullable: false, primary_key: false },
        { name: "unit_price", type: "float", nullable: false, primary_key: false },
        { name: "unit_cost", type: "float", nullable: false, primary_key: false },
        { name: "total_revenue", type: "float", nullable: false, primary_key: false },
        { name: "total_cost", type: "float", nullable: false, primary_key: false },
        { name: "total_profit", type: "float", nullable: false, primary_key: false }
      ]
    }
  ]
};

// ── State ─────────────────────────────────────────────────────
const state = {
  activeConnectionId: localStorage.getItem("activeConnectionId") || null,
  activeConnectionName: localStorage.getItem("activeConnectionName") || null,
  activeConnectionDialect: localStorage.getItem("activeConnectionDialect") || null,
  conversationId: null,
  messages: [],
  schema: null,
  glossary: null,
  isAsking: false,
  isGeneratingGlossary: false,
  exampleResponses: []
};

// ── DOM refs ───────────────────────────────────────────────────
const elements = {
  connectionStatusBadge: document.getElementById("connection-status-badge"),
  connectionStatusText:  document.getElementById("connection-status-text"),
  newConvBtn:            document.getElementById("new-conv-btn"),
  disconnectBtn:         document.getElementById("disconnect-btn"),
  setupView:             document.getElementById("connection-setup-view"),
  mainView:              document.getElementById("main-app-view"),
  dbConnectForm:         document.getElementById("db-connect-form"),
  csvUploadForm:         document.getElementById("csv-upload-form"),
  existingConnectionsList: document.getElementById("existing-connections-list"),
  sidebarDialectBadge:   document.getElementById("sidebar-dialect-badge"),
  generateGlossaryBtn:   document.getElementById("generate-glossary-btn"),
  glossaryLoading:       document.getElementById("glossary-loading"),
  schemaTreeContainer:   document.getElementById("schema-tree-container"),
  chatMessages:          document.getElementById("chat-messages"),
  chatLoadingIndicator:  document.getElementById("chat-loading-indicator"),
  chatForm:              document.getElementById("chat-form"),
  questionInput:         document.getElementById("question-input"),
  askBtn:                document.getElementById("ask-btn"),
  sidebarToggleBtn:      document.getElementById("sidebar-toggle-btn"),
  sidebar:               document.getElementById("sidebar"),
};

// ── Loading phase cycling ──────────────────────────────────────
const LOADING_PHASES = [
  "Generating SQL…",
  "Validating query…",
  "Running against data…",
  "Interpreting results…"
];

let _loadingPhaseTimer = null;
let _loadingPhaseIdx   = 0;
const _loadingPhaseEl  = () => document.getElementById("loading-phase");

function startLoadingPhases(intervalMs = 450) {
  _loadingPhaseIdx = 0;
  const el = _loadingPhaseEl();
  if (el) el.textContent = LOADING_PHASES[0];
  _loadingPhaseTimer = setInterval(() => {
    _loadingPhaseIdx = (_loadingPhaseIdx + 1) % LOADING_PHASES.length;
    const el2 = _loadingPhaseEl();
    if (el2) el2.textContent = LOADING_PHASES[_loadingPhaseIdx];
  }, intervalMs);
}

function stopLoadingPhases() {
  if (_loadingPhaseTimer) { clearInterval(_loadingPhaseTimer); _loadingPhaseTimer = null; }
}

// ── Init & view switching ──────────────────────────────────────
async function init() {
  bindEvents();
  await loadExampleResponses();
  await loadConnectionsList();

  if (state.activeConnectionId) {
    if (state.activeConnectionId === "demo-sample-sales" && !state.schema) {
      state.schema = SAMPLE_SALES_SCHEMA;
    }
    showMainView();
    renderSchemaTree();
  } else {
    showSetupView();
  }
}

async function loadExampleResponses() {
  try {
    const res = await fetch("/demo-data/example-responses.json");
    if (res.ok) {
      state.exampleResponses = await res.json();
    }
  } catch (err) {
    console.warn("Could not load example responses:", err);
  }
}

function bindEvents() {
  elements.dbConnectForm.addEventListener("submit", handleDbConnect);
  elements.csvUploadForm.addEventListener("submit", handleCsvUpload);
  elements.disconnectBtn.addEventListener("click", handleDisconnect);
  elements.newConvBtn.addEventListener("click", handleNewConversation);
  elements.generateGlossaryBtn.addEventListener("click", handleGenerateGlossary);
  elements.chatForm.addEventListener("submit", handleAskQuestion);

  // Hint chips fill the textarea and submit
  document.addEventListener("click", (e) => {
    if (e.target.classList.contains("hint-chip") && elements.questionInput) {
      const q = e.target.textContent.trim();
      elements.questionInput.value = q;
      elements.questionInput.focus();
    }
  });

  // Enter submits
  elements.questionInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      elements.chatForm.requestSubmit();
    }
  });

  // Mobile sidebar toggle
  if (elements.sidebarToggleBtn && elements.sidebar) {
    elements.sidebarToggleBtn.addEventListener("click", () => {
      elements.sidebar.classList.toggle("open");
    });
  }

  // Hero "Get Started" glowing CTA button smooth scroll
  const getStartedBtn = document.getElementById("hero-get-started-btn");
  if (getStartedBtn) {
    getStartedBtn.addEventListener("click", () => {
      const target = document.getElementById("setup-grid-section");
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
  }

  // Dynamic file name label for CSV upload dropzone
  const csvFileInput = document.getElementById("csv-file");
  const csvFileName  = document.getElementById("csv-file-name");
  if (csvFileInput && csvFileName) {
    csvFileInput.addEventListener("change", () => {
      if (csvFileInput.files && csvFileInput.files.length > 0) {
        csvFileName.textContent = csvFileInput.files[0].name;
        csvFileName.classList.add("file-selected");
      } else {
        csvFileName.textContent = "Choose .csv file or drag here";
        csvFileName.classList.remove("file-selected");
      }
    });
  }
}

function showSetupView() {
  document.body.classList.remove("is-connected");
  document.body.classList.add("is-landing");
  elements.setupView.classList.remove("hidden");
  elements.mainView.classList.add("hidden");
  elements.connectionStatusBadge.classList.add("hidden");
  elements.newConvBtn.classList.add("hidden");
  elements.disconnectBtn.classList.add("hidden");
  loadConnectionsList();
}

function showMainView() {
  document.body.classList.remove("is-landing");
  document.body.classList.add("is-connected");
  elements.setupView.classList.add("hidden");
  elements.mainView.classList.remove("hidden");
  elements.connectionStatusBadge.classList.remove("hidden");
  elements.newConvBtn.classList.remove("hidden");
  elements.disconnectBtn.classList.remove("hidden");
  elements.connectionStatusText.textContent =
    `${state.activeConnectionName || "Sales Records"} · ${state.activeConnectionDialect || "sqlite"}`;
  elements.sidebarDialectBadge.textContent =
    (state.activeConnectionDialect || "sqlite").toLowerCase();

  if (state.messages.length === 0) {
    elements.chatMessages.innerHTML = connectedEmptyStateHtml(state.activeConnectionName);
  }
}

// ── Connection management ──────────────────────────────────────
async function loadConnectionsList() {
  // DEMO-MODE: Provide instant sample connection along with any uploaded session
  const connections = [
    {
      connection_id: "demo-sample-sales",
      name: "Sample Sales Dataset",
      dialect: "sqlite",
      created_at: new Date().toISOString()
    }
  ];

  elements.existingConnectionsList.innerHTML = connections.map((c) => `
    <div class="connection-item" onclick="activateDemoSampleConnection('${c.connection_id}','${escapeHtml(c.name)}','${escapeHtml(c.dialect)}')">
      <div class="connection-left">
        <svg class="connection-glyph" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <ellipse cx="12" cy="5" rx="9" ry="3"/>
          <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/>
          <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>
        </svg>
        <div class="connection-info">
          <div class="connection-title-row">
            <span class="connection-name">${escapeHtml(c.name)}</span>
            <span class="dialect-text">${escapeHtml(c.dialect)}</span>
          </div>
          <span class="connection-meta">Pre-loaded 100-row sample CSV</span>
        </div>
      </div>
      <button class="btn-select-conn">
        <span>Select</span>
        <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 7h8M8 3.5l3.5 3.5-3.5 3.5"/>
        </svg>
      </button>
    </div>
  `).join("");
}

window.activateDemoSampleConnection = function (connectionId, name, dialect) {
  state.schema = SAMPLE_SALES_SCHEMA;
  activateConnection(connectionId, name, dialect);
};

// DEMO-MODE: Database connection form notice
async function handleDbConnect(e) {
  e.preventDefault();
  alert("Live database connections aren't available in this static demo — try uploading a CSV instead or click the Sample Sales Dataset below.");
}

// DEMO-MODE: Upload CSV to Vercel Python serverless endpoint /api/upload
async function handleCsvUpload(e) {
  e.preventDefault();
  const name      = document.getElementById("csv-name").value.trim() || "Uploaded CSV";
  const fileInput = document.getElementById("csv-file");
  const submitBtn = document.getElementById("csv-upload-btn");

  if (!fileInput.files || fileInput.files.length === 0) {
    alert("Please select a CSV file.");
    return;
  }

  const file = fileInput.files[0];
  if (file.size > 4.5 * 1024 * 1024) {
    alert("File size exceeds the 4.5MB serverless demo limit.");
    return;
  }

  const formData = new FormData();
  formData.append("name", name);
  formData.append("file", file);

  try {
    submitBtn.disabled = true;
    submitBtn.textContent = "Introspecting…";

    const res = await fetch("/api/upload", { method: "POST", body: formData });
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || "CSV upload and introspection failed");
    }

    state.schema = data.schema;
    state.glossary = null;
    await activateConnection(data.connection_id || "demo-csv", name, "sqlite");
    elements.csvUploadForm.reset();
  } catch (err) {
    alert(`Upload error: ${err.message}`);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Upload & Analyze";
  }
}

async function activateConnection(connectionId, name, dialect) {
  state.activeConnectionId      = connectionId;
  state.activeConnectionName    = name;
  state.activeConnectionDialect = dialect;
  localStorage.setItem("activeConnectionId",      connectionId);
  localStorage.setItem("activeConnectionName",    name);
  localStorage.setItem("activeConnectionDialect", dialect);

  showMainView();
  renderSchemaTree();
}

function handleNewConversation() {
  state.conversationId = null;
  state.messages       = [];
  elements.chatMessages.innerHTML = connectedEmptyStateHtml(state.activeConnectionName);
}

function handleDisconnect() {
  state.activeConnectionId      = null;
  state.activeConnectionName    = null;
  state.activeConnectionDialect = null;
  state.conversationId          = null;
  state.messages                = [];
  state.schema                  = null;
  state.glossary                = null;
  localStorage.removeItem("activeConnectionId");
  localStorage.removeItem("activeConnectionName");
  localStorage.removeItem("activeConnectionDialect");
  elements.chatMessages.innerHTML = "";
  showSetupView();
}

function connectedEmptyStateHtml(connectionName) {
  const name = connectionName || "your data";
  return `
    <div class="system-welcome">
      <div class="welcome-icon">
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none"
             stroke="currentColor" stroke-width="1.5">
          <circle cx="9" cy="9" r="7.5"/>
          <path d="M6 9h6M9 6v6"/>
        </svg>
      </div>
      <h3>Ask a question about <span class="welcome-conn-name">${escapeHtml(name)}</span></h3>
      <p>Type a business question in the box below. In this static demo, verified pre-recorded examples are ready to query.</p>
      <div class="welcome-hints">
        <span class="hint-chip">total revenue by region</span>
        <span class="hint-chip">break down total profit by sales channel</span>
        <span class="hint-chip">what is the average unit price</span>
        <span class="hint-chip">break down total expenses by product category</span>
        <span class="hint-chip">top 5 countries by units sold</span>
      </div>
    </div>`;
}

// ── Schema & Glossary sidebar ──────────────────────────────────
// DEMO-MODE: Glossary generation notice
async function handleGenerateGlossary() {
  alert("Glossary generation requires a running LLM and isn't available in this static demo.");
}

const CHEVRON_SVG = `<svg class="chevron" viewBox="0 0 12 12" fill="none"
  stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="4 2 8 6 4 10"/>
</svg>`;

function renderSchemaTree() {
  if (!state.schema || !state.schema.tables || !state.schema.tables.length) {
    elements.schemaTreeContainer.innerHTML =
      '<p class="text-tertiary" style="padding:12px 16px;font-size:12px;">No tables found.</p>';
    return;
  }

  elements.schemaTreeContainer.innerHTML = state.schema.tables.map((table) => {
    const colsHtml = table.columns.map((col) => `
      <div class="column-item">
        <div class="column-header">
          <span class="col-name">${escapeHtml(col.name)}</span>
          <span class="badge">${escapeHtml(col.type)}</span>
        </div>
      </div>
    `).join("");

    return `
      <details class="table-node" open>
        <summary>
          ${CHEVRON_SVG}
          <span class="table-name-text">${escapeHtml(table.name)}</span>
          <span class="table-row-count">${table.columns.length}c · ${table.row_count || 100}r</span>
        </summary>
        <div class="table-node-body">
          <div class="columns-list">${colsHtml}</div>
        </div>
      </details>`;
  }).join("");
}

// ── Chat & Analysis (DEMO-MODE Matching) ────────────────────────
async function handleAskQuestion(e) {
  e.preventDefault();
  const question = elements.questionInput.value.trim();
  if (!question || state.isAsking) return;

  appendUserMessage(question);
  elements.questionInput.value    = "";
  elements.questionInput.disabled = true;
  elements.askBtn.disabled        = true;

  state.isAsking = true;
  elements.chatLoadingIndicator.classList.remove("hidden");
  startLoadingPhases(350);
  scrollToBottom();

  const msgId = "msg-" + Date.now();

  // Simulate realistic processing time for demo feel (~1.2s)
  await new Promise((r) => setTimeout(r, 1200));

  try {
    const matched = findMatchingExample(question);

    if (matched) {
      appendAssistantResponse(msgId, question, matched, true);
    } else {
      appendDemoFallbackNotice(question);
    }
  } catch (err) {
    appendErrorMessage(`Demo evaluation error: ${err.message}`);
  } finally {
    state.isAsking = false;
    stopLoadingPhases();
    elements.chatLoadingIndicator.classList.add("hidden");
    elements.questionInput.disabled = false;
    elements.askBtn.disabled        = false;
    elements.questionInput.focus();
    scrollToBottom();
  }
}

/**
 * Fuzzy question matching against demo-data/example-responses.json
 */
function findMatchingExample(query) {
  if (!state.exampleResponses || state.exampleResponses.length === 0) {
    return null;
  }

  const q = query.toLowerCase().trim();
  const cleanQ = q.replace(/[^a-z0-9\s]/g, "");
  const qWords = cleanQ.split(/\s+/).filter((w) => w.length > 2);

  // 1. Exact match on question
  for (const item of state.exampleResponses) {
    if (item.question.toLowerCase().trim() === q) return item;
  }

  // 2. Substring match on question or matches array
  for (const item of state.exampleResponses) {
    if (item.question.toLowerCase().includes(q) || q.includes(item.question.toLowerCase())) {
      return item;
    }
    if (item.matches && Array.isArray(item.matches)) {
      for (const m of item.matches) {
        const cleanM = m.toLowerCase();
        if (cleanM === q || q.includes(cleanM) || cleanM.includes(q)) {
          return item;
        }
      }
    }
  }

  // 3. Keyword overlap score
  let bestItem = null;
  let bestScore = 0;

  for (const item of state.exampleResponses) {
    const targetText = [item.question, ...(item.matches || [])].join(" ").toLowerCase();
    let matchCount = 0;
    for (const word of qWords) {
      if (targetText.includes(word)) matchCount++;
    }
    const score = qWords.length > 0 ? matchCount / qWords.length : 0;
    if (score >= 0.45 && score > bestScore) {
      bestScore = score;
      bestItem = item;
    }
  }

  return bestItem;
}

function appendUserMessage(text) {
  const row = document.createElement("div");
  row.className = "message-row";
  row.innerHTML = `
    <div class="user-query">
      <span class="user-query-label">Query</span>
      <span class="user-query-text">${escapeHtml(text)}</span>
    </div>`;
  elements.chatMessages.appendChild(row);
  scrollToBottom();
}

function appendErrorMessage(errorText) {
  const row = document.createElement("div");
  row.className = "message-row";
  row.innerHTML = `
    <div class="error-card">
      <strong>Query failed</strong>
      <p>${escapeHtml(errorText)}</p>
    </div>`;
  elements.chatMessages.appendChild(row);
  scrollToBottom();
}

/**
 * Fallback notice shown when an un-cached question is asked in the demo
 */
function appendDemoFallbackNotice(question) {
  const row = document.createElement("div");
  row.className = "message-row";
  row.innerHTML = `
    <div class="assistant-card">
      <div class="interpretation-section">
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
          <div class="section-label">Demo Notice</div>
          <span style="font-family:var(--font-mono);font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(230,198,135,0.12);color:#e6c687;border:1px solid rgba(230,198,135,0.25);">Static Deployment</span>
        </div>
        <div class="interpretation-text" style="color:#d1d5db; line-height:1.65;">
          This demo shows <strong>pre-recorded verified examples</strong> for sample queries, since live local SQL generation requires an always-on LLM host.
          <br><br>
          Try one of the working questions below, or clone the full project on GitHub for live inference on your machine:
        </div>
        <div class="welcome-hints" style="margin-top:14px; justify-content:flex-start;">
          <span class="hint-chip">total revenue by region</span>
          <span class="hint-chip">break down total profit by sales channel</span>
          <span class="hint-chip">what is the average unit price</span>
          <span class="hint-chip">break down total expenses by product category</span>
          <span class="hint-chip">top 5 countries by units sold</span>
        </div>
      </div>
    </div>`;
  elements.chatMessages.appendChild(row);
  scrollToBottom();
}

function appendAssistantResponse(msgId, question, data, isDemo = true) {
  const row = document.createElement("div");
  row.className = "message-row";

  const interpretation = data.interpretation || "No interpretation returned.";
  const warningHtml    = data.warning
    ? `<div class="warning-box">
         <svg width="13" height="13" viewBox="0 0 13 13" fill="none"
              stroke="currentColor" stroke-width="1.5" style="flex-shrink:0">
           <path d="M6.5 1.5L11.5 10.5H1.5L6.5 1.5Z"/>
           <line x1="6.5" y1="5.5" x2="6.5" y2="7.5"/>
           <circle cx="6.5" cy="9" r="0.5" fill="currentColor" stroke="none"/>
         </svg>
         ${escapeHtml(data.warning)}
       </div>`
    : "";

  const chartId   = `chart-${msgId}`;
  const chartHtml = data.chart
    ? `<div class="chart-section"><div id="${chartId}" class="chart-container"></div></div>`
    : "";

  const qr      = data.query_result || { columns: [], rows: [], row_count: 0 };
  const rows    = qr.rows    || [];
  const columns = qr.columns || [];
  const tableOpenAttr = rows.length > 0 && rows.length <= 10 ? "open" : "";

  let tableContentHtml = `<p class="text-tertiary" style="padding:10px 16px;font-size:12px;">Empty result set</p>`;
  if (rows.length > 0) {
    const numCols = new Set(
      columns.filter((col) => rows.some((r) => {
        const v = r[col];
        return v !== null && v !== undefined && v !== "" && !isNaN(Number(v));
      }))
    );
    const headerHtml = columns.map((col) =>
      `<th style="${numCols.has(col) ? 'text-align:right' : ''}">${escapeHtml(col)}</th>`
    ).join("");
    const rowsHtml = rows.map((r) =>
      `<tr>${columns.map((col) => {
        const v   = r[col] !== null && r[col] !== undefined ? r[col] : "NULL";
        const num = numCols.has(col);
        return `<td class="${num ? 'num' : ''}">${escapeHtml(String(v))}</td>`;
      }).join("")}</tr>`
    ).join("");
    tableContentHtml = `
      <div class="table-wrapper">
        <table class="data-table">
          <thead><tr>${headerHtml}</tr></thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>`;
  }

  const thumbUpSVG = `<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"
    stroke-linecap="round" stroke-linejoin="round">
    <path d="M4 6.5L6.5 1.5C7.33 1.5 8 2.17 8 3v2.5h3.5a1 1 0 011 1.1l-.7 4a1 1 0 01-1 .9H4"/>
    <line x1="4" y1="6.5" x2="4" y2="13"/>
    <line x1="1.5" y1="6.5" x2="4" y2="6.5"/>
  </svg>`;
  const thumbDownSVG = `<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"
    stroke-linecap="round" stroke-linejoin="round">
    <path d="M10 7.5L7.5 12.5C6.67 12.5 6 11.83 6 11V8.5H2.5a1 1 0 01-1-1.1l.7-4a1 1 0 011-.9H10"/>
    <line x1="10" y1="7.5" x2="10" y2="1"/>
    <line x1="12.5" y1="7.5" x2="10" y2="7.5"/>
  </svg>`;

  const demoBadgeHtml = isDemo
    ? `<span style="font-family:var(--font-mono);font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(230,198,135,0.12);color:#e6c687;border:1px solid rgba(230,198,135,0.25);">Pre-recorded example</span>`
    : "";

  row.innerHTML = `
    <div class="assistant-card" id="${msgId}">
      <!-- Interpretation -->
      <div class="interpretation-section">
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:4px;">
          <div class="section-label">Analysis</div>
          ${demoBadgeHtml}
        </div>
        <div class="interpretation-text">
          ${escapeHtml(interpretation).replace(/\n/g, "<br>")}
        </div>
      </div>

      ${warningHtml}
      ${chartHtml}

      <!-- SQL -->
      <details class="collapsible-block">
        <summary>Generated SQL &nbsp;<span style="font-family:var(--font-mono);font-size:10px;color:var(--text-tertiary);">(click to expand)</span></summary>
        <div class="collapsible-content">
          <div class="sql-code-wrapper">
            <button class="copy-btn" onclick="copySQL('${msgId}')">Copy</button>
            <pre class="sql-code" id="sql-${msgId}"><code>${highlightSQL(data.generated_sql)}</code></pre>
          </div>
        </div>
      </details>

      <!-- Data rows -->
      <details class="collapsible-block" ${tableOpenAttr}>
        <summary>Data &nbsp;<span style="font-family:var(--font-mono);font-size:10px;color:var(--text-tertiary);">${qr.row_count} rows</span></summary>
        <div class="collapsible-content">${tableContentHtml}</div>
      </details>

      <!-- Feedback (Demo Mode) -->
      <div class="feedback-bar" id="feedback-bar-${msgId}">
        <span class="feedback-label">Was this helpful?</span>
        <button class="feedback-btn" id="fb-up-${msgId}" onclick="handleDemoFeedback('${msgId}')">
          ${thumbUpSVG} Yes
        </button>
        <button class="feedback-btn" id="fb-down-${msgId}" onclick="handleDemoFeedback('${msgId}')">
          ${thumbDownSVG} No
        </button>
        <span id="fb-note-${msgId}" class="text-tertiary" style="font-size:10.5px;font-family:var(--font-mono);display:none;">Feedback isn't stored in this demo</span>
      </div>
    </div>`;

  elements.chatMessages.appendChild(row);

  // Dark-themed Plotly render
  if (data.chart && data.chart.plotly_figure_json && window.Plotly) {
    try {
      const figure = JSON.parse(data.chart.plotly_figure_json);

      const darkLayout = {
        paper_bgcolor: "#0b0b0e",
        plot_bgcolor:  "#0b0b0e",
        font:          { family: "'JetBrains Mono', monospace", size: 11, color: "#9ca3af" },
        margin:        { t: 28, b: 40, l: 48, r: 20, pad: 0 },
        xaxis: {
          gridcolor:    "rgba(230,198,135,0.06)",
          linecolor:    "rgba(230,198,135,0.12)",
          tickcolor:    "rgba(230,198,135,0.12)",
          zerolinecolor:"rgba(230,198,135,0.08)",
          tickfont:     { size: 10 },
        },
        yaxis: {
          gridcolor:    "rgba(230,198,135,0.06)",
          linecolor:    "rgba(230,198,135,0.12)",
          tickcolor:    "rgba(230,198,135,0.12)",
          zerolinecolor:"rgba(230,198,135,0.08)",
          tickfont:     { size: 10 },
        },
        legend: {
          bgcolor:     "rgba(0,0,0,0)",
          borderwidth: 0,
          font:        { size: 10 },
        },
        colorway: ["#d4a359","#22c55e","#f59e0b","#a78bfa","#ef4444","#06b6d4"],
      };

      const mergedLayout = Object.assign({}, figure.layout || {}, darkLayout);
      Plotly.newPlot(chartId, figure.data || [], mergedLayout, {
        responsive:     true,
        displayModeBar: false,
      });
    } catch (err) {
      console.warn("Plotly render failed:", err);
    }
  }

  scrollToBottom();
}

window.handleDemoFeedback = function (msgId) {
  const upBtn = document.getElementById(`fb-up-${msgId}`);
  const downBtn = document.getElementById(`fb-down-${msgId}`);
  const note = document.getElementById(`fb-note-${msgId}`);
  if (upBtn) upBtn.disabled = true;
  if (downBtn) downBtn.disabled = true;
  if (note) note.style.display = "inline";
};

// ── SQL syntax highlighter ────────────────────────────────────
function highlightSQL(rawSQL) {
  if (!rawSQL) return "";
  const KEYWORDS = [
    "SELECT","FROM","WHERE","GROUP BY","ORDER BY","HAVING","LIMIT","OFFSET",
    "JOIN","LEFT JOIN","RIGHT JOIN","INNER JOIN","OUTER JOIN","FULL JOIN",
    "CROSS JOIN","ON","AS","AND","OR","NOT","IN","IS","NULL","BETWEEN",
    "LIKE","ILIKE","EXISTS","CASE","WHEN","THEN","ELSE","END","DISTINCT",
    "ALL","UNION","UNION ALL","INSERT","UPDATE","DELETE","CREATE","TABLE",
    "ASC","DESC","WITH","BY","SET"
  ];
  const FUNCTIONS = [
    "SUM","AVG","COUNT","MIN","MAX","ROUND","COALESCE","CAST","DATE",
    "DATETIME","STRFTIME","UPPER","LOWER","TRIM","LENGTH","ABS"
  ];

  let esc = escapeHtml(rawSQL);

  esc = esc.replace(/--[^\n]*/g, (m) => `<span class="sql-cmt">${m}</span>`);
  esc = esc.replace(/'([^'\\]|\\.)*'/g, (m) => `<span class="sql-str">${m}</span>`);

  for (const fn of FUNCTIONS) {
    const re = new RegExp(`\\b(${fn})\\s*\\(`, "gi");
    esc = esc.replace(re, `<span class="sql-fn">$1</span>(`);
  }

  for (const kw of KEYWORDS) {
    const re = new RegExp(`(?<![a-zA-Z0-9_])(${kw})(?![a-zA-Z0-9_])`, "gi");
    esc = esc.replace(re, (match) => {
      return `<span class="sql-kw">${match.toUpperCase()}</span>`;
    });
  }

  esc = esc.replace(/(?<![a-zA-Z0-9_])(\d+(\.\d+)?)(?![a-zA-Z0-9_])/g, `<span class="sql-num">$1</span>`);
  return esc;
}

window.copySQL = function (msgId) {
  const codeEl = document.getElementById(`sql-${msgId}`);
  if (!codeEl) return;
  navigator.clipboard.writeText(codeEl.textContent).then(() => {
    const btn = codeEl.closest(".sql-code-wrapper")?.querySelector(".copy-btn");
    if (btn) {
      btn.textContent = "Copied!";
      btn.classList.add("copied");
      setTimeout(() => {
        btn.textContent = "Copy";
        btn.classList.remove("copied");
      }, 1800);
    }
  });
};

function scrollToBottom() {
  requestAnimationFrame(() => {
    elements.chatMessages.scrollTop = elements.chatMessages.scrollHeight;
  });
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

document.addEventListener("DOMContentLoaded", init);

/**
 * AI Business Analyst — SPA Client
 * Logic, API calls, and state management are unchanged.
 * Rendering functions updated for new design system.
 */

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
  "Running against your data…",
  "Interpreting results…",
];

let _loadingPhaseTimer = null;
let _loadingPhaseIdx   = 0;
const _loadingPhaseEl  = () => document.getElementById("loading-phase");

function startLoadingPhases() {
  _loadingPhaseIdx = 0;
  const el = _loadingPhaseEl();
  if (el) el.textContent = LOADING_PHASES[0];
  _loadingPhaseTimer = setInterval(() => {
    _loadingPhaseIdx = (_loadingPhaseIdx + 1) % LOADING_PHASES.length;
    const el2 = _loadingPhaseEl();
    if (el2) el2.textContent = LOADING_PHASES[_loadingPhaseIdx];
  }, 14000); // ~14 s per phase gives full cycle ~56 s before looping
}

function stopLoadingPhases() {
  if (_loadingPhaseTimer) { clearInterval(_loadingPhaseTimer); _loadingPhaseTimer = null; }
}

// ── Init & view switching ──────────────────────────────────────
async function init() {
  bindEvents();
  await loadConnectionsList();
  if (state.activeConnectionId) {
    await activateConnection(
      state.activeConnectionId,
      state.activeConnectionName || "Database",
      state.activeConnectionDialect || "SQL"
    );
  } else {
    showSetupView();
  }
}

function bindEvents() {
  elements.dbConnectForm.addEventListener("submit", handleDbConnect);
  elements.csvUploadForm.addEventListener("submit", handleCsvUpload);
  elements.disconnectBtn.addEventListener("click", handleDisconnect);
  elements.newConvBtn.addEventListener("click", handleNewConversation);
  elements.generateGlossaryBtn.addEventListener("click", handleGenerateGlossary);
  elements.chatForm.addEventListener("submit", handleAskQuestion);

  // Hint chips fill the textarea
  document.addEventListener("click", (e) => {
    if (e.target.classList.contains("hint-chip") && elements.questionInput) {
      elements.questionInput.value = e.target.textContent.trim();
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
      const target = document.getElementById("setup-grid-section") || document.querySelector(".setup-grid");
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        const firstInput = target.querySelector("input");
        if (firstInput) setTimeout(() => firstInput.focus(), 350);
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
    `${state.activeConnectionName} · ${state.activeConnectionDialect}`;
  elements.sidebarDialectBadge.textContent =
    state.activeConnectionDialect.toLowerCase();

  // Always write the correct connected empty state when entering the main view.
  // This replaces whatever was in #chat-messages (including any stale static HTML
  // from index.html) with a message that is actually correct for the current state.
  if (state.messages.length === 0) {
    elements.chatMessages.innerHTML = connectedEmptyStateHtml(state.activeConnectionName);
  }
}


// ── Connection management ──────────────────────────────────────
async function loadConnectionsList() {
  try {
    const res = await fetch("/connections");
    if (!res.ok) throw new Error("Failed to load connections");
    const connections = await res.json();

    if (!connections || connections.length === 0) {
      elements.existingConnectionsList.innerHTML =
        '<p class="connections-empty">No registered connections yet. Connect a database or upload a CSV above.</p>';
      return;
    }

    elements.existingConnectionsList.innerHTML = connections.map((c) => `
      <div class="connection-item" onclick="activateConnection('${c.connection_id}','${escapeHtml(c.name)}','${escapeHtml(c.dialect)}')">
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
            <span class="connection-meta">${new Date(c.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })}</span>
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
  } catch (err) {
    elements.existingConnectionsList.innerHTML =
      `<p class="connections-empty" style="color:var(--red);">Error: ${err.message}</p>`;
  }
}

async function handleDbConnect(e) {
  e.preventDefault();
  const name     = document.getElementById("db-name").value.trim();
  const connStr  = document.getElementById("db-conn-str").value.trim();
  const submitBtn = document.getElementById("db-connect-btn");
  try {
    submitBtn.disabled = true;
    submitBtn.textContent = "Connecting…";
    const res  = await fetch("/connections/database", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, connection_string: connStr }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Connection failed");
    const listRes = await fetch("/connections");
    const list = await listRes.json();
    const connInfo = list.find((c) => c.connection_id === data.connection_id);
    const dialect = connInfo ? connInfo.dialect : "database";
    await activateConnection(data.connection_id, name, dialect);
    elements.dbConnectForm.reset();
  } catch (err) {
    alert(`Connection error: ${err.message}`);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Connect";
  }
}

async function handleCsvUpload(e) {
  e.preventDefault();
  const name      = document.getElementById("csv-name").value.trim();
  const fileInput = document.getElementById("csv-file");
  const submitBtn = document.getElementById("csv-upload-btn");
  if (!fileInput.files || fileInput.files.length === 0) {
    alert("Please select a CSV file.");
    return;
  }
  const formData = new FormData();
  formData.append("name", name);
  formData.append("file", fileInput.files[0]);
  try {
    submitBtn.disabled = true;
    submitBtn.textContent = "Uploading…";
    const res  = await fetch("/connections/csv", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "CSV upload failed");
    await activateConnection(data.connection_id, name, "sqlite");
    elements.csvUploadForm.reset();
  } catch (err) {
    alert(`Upload error: ${err.message}`);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Upload & connect";
  }
}

async function activateConnection(connectionId, name, dialect) {
  state.activeConnectionId    = connectionId;
  state.activeConnectionName  = name;
  state.activeConnectionDialect = dialect;
  localStorage.setItem("activeConnectionId",      connectionId);
  localStorage.setItem("activeConnectionName",    name);
  localStorage.setItem("activeConnectionDialect", dialect);
  showMainView();
  await loadSchemaAndGlossary();
}

function handleNewConversation() {
  state.conversationId = null;
  state.messages       = [];
  // Show the connected empty state so hint chips remain available
  elements.chatMessages.innerHTML = connectedEmptyStateHtml(state.activeConnectionName);
}

function handleDisconnect() {
  state.activeConnectionId    = null;
  state.activeConnectionName  = null;
  state.activeConnectionDialect = null;
  state.conversationId        = null;
  state.messages              = [];
  state.schema                = null;
  state.glossary              = null;
  localStorage.removeItem("activeConnectionId");
  localStorage.removeItem("activeConnectionName");
  localStorage.removeItem("activeConnectionDialect");
  // Clear the chat panel — user is going back to the setup screen,
  // so this content won't be visible, but clear it so next connect is clean.
  elements.chatMessages.innerHTML = "";
  showSetupView();
}

/**
 * Empty state shown when connected but no messages have been sent yet.
 * Always references the active connection name so it's obviously correct.
 */
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
      <p>Type a business question in the box below. The assistant will generate SQL,
         run it safely against your data, and explain the results in plain English.</p>
      <div class="welcome-hints">
        <span class="hint-chip">total revenue by region</span>
        <span class="hint-chip">top 10 products by sales</span>
        <span class="hint-chip">monthly expense trend</span>
        <span class="hint-chip">break down costs by category</span>
      </div>
    </div>`;
}

/** Generic titled welcome for other transient messages */
function welcomeHtml(title, subtitle) {
  return `
    <div class="system-welcome">
      <div class="welcome-icon">
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none"
             stroke="currentColor" stroke-width="1.5">
          <circle cx="9" cy="9" r="7.5"/>
          <path d="M6 9h6M9 6v6"/>
        </svg>
      </div>
      <h3>${escapeHtml(title)}</h3>
      <p>${escapeHtml(subtitle)}</p>
    </div>`;
}


// ── Schema & Glossary sidebar ──────────────────────────────────
async function loadSchemaAndGlossary() {
  if (!state.activeConnectionId) return;
  elements.schemaTreeContainer.innerHTML =
    '<p class="text-tertiary" style="padding:12px 16px;font-size:12px;">Loading schema…</p>';
  try {
    const schemaRes = await fetch(`/connections/${state.activeConnectionId}/schema`);
    if (!schemaRes.ok) {
      if (schemaRes.status === 404) { handleDisconnect(); return; }
      throw new Error("Failed to load schema");
    }
    state.schema = await schemaRes.json();
    try {
      const glossRes = await fetch(`/connections/${state.activeConnectionId}/semantic`);
      state.glossary = glossRes.ok ? await glossRes.json() : null;
    } catch { state.glossary = null; }
    renderSchemaTree();
  } catch (err) {
    elements.schemaTreeContainer.innerHTML =
      `<p class="text-tertiary" style="padding:12px 16px;font-size:12px;">Error: ${err.message}</p>`;
  }
}

async function handleGenerateGlossary() {
  if (!state.activeConnectionId || state.isGeneratingGlossary) return;
  try {
    state.isGeneratingGlossary = true;
    elements.generateGlossaryBtn.disabled = true;
    elements.glossaryLoading.classList.remove("hidden");
    const res  = await fetch(`/connections/${state.activeConnectionId}/semantic/generate`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Glossary generation failed");
    state.glossary = data;
    renderSchemaTree();
  } catch (err) {
    alert(`Glossary error: ${err.message}`);
  } finally {
    state.isGeneratingGlossary = false;
    elements.generateGlossaryBtn.disabled = false;
    elements.glossaryLoading.classList.add("hidden");
  }
}

// Chevron SVG
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
    const tg        = state.glossary ? state.glossary[table.name] : null;
    const tableDesc = tg?.description || "Add description…";

    const colsHtml = table.columns.map((col) => {
      const cg      = tg?.columns ? tg.columns[col.name] : null;
      const colDesc = cg?.description || "";
      const syns    = cg?.synonyms?.length ? cg.synonyms.join(", ") : "";
      return `
        <div class="column-item">
          <div class="column-header">
            <span class="col-name">${escapeHtml(col.name)}</span>
            <span class="badge">${escapeHtml(col.type)}</span>
          </div>
          ${colDesc ? `
            <div class="column-desc">
              <span class="editable-text"
                title="Click to edit"
                onclick="startEditColumnDesc(event,'${escapeHtml(table.name)}','${escapeHtml(col.name)}','${escapeJs(colDesc)}')"
              >${escapeHtml(colDesc)}</span>
            </div>` : `
            <div class="column-desc">
              <span class="editable-text"
                title="Click to add description"
                onclick="startEditColumnDesc(event,'${escapeHtml(table.name)}','${escapeHtml(col.name)}','')"
              >Add description…</span>
            </div>`}
          ${syns ? `<div class="column-synonyms">${escapeHtml(syns)}</div>` : ""}
        </div>`;
    }).join("");

    return `
      <details class="table-node" open>
        <summary>
          ${CHEVRON_SVG}
          <span class="table-name-text">${escapeHtml(table.name)}</span>
          <span class="table-row-count">${table.columns.length}c</span>
        </summary>
        <div class="table-node-body">
          <div class="table-desc-row">
            <span class="editable-text"
              title="Click to edit table description"
              onclick="startEditTableDesc(event,'${escapeHtml(table.name)}','${escapeJs(tableDesc)}')"
            >${escapeHtml(tableDesc)}</span>
          </div>
          <div class="columns-list">${colsHtml}</div>
        </div>
      </details>`;
  }).join("");
}

// Inline glossary editing
window.startEditTableDesc = function (e, tableName, currentDesc) {
  e.stopPropagation();
  const parent = e.target.parentElement;
  parent.innerHTML = `
    <div class="inline-edit-form">
      <input type="text" id="edit-table-${tableName}"
        value="${escapeHtml(currentDesc === 'Add description…' ? '' : currentDesc)}"
        placeholder="Table description" />
      <button class="btn btn-sm btn-primary" onclick="saveTableDesc('${escapeHtml(tableName)}')">Save</button>
      <button class="btn btn-sm btn-outline" onclick="renderSchemaTree()">Cancel</button>
    </div>`;
  document.getElementById(`edit-table-${tableName}`)?.focus();
};

window.saveTableDesc = async function (tableName) {
  const input = document.getElementById(`edit-table-${tableName}`);
  if (!input) return;
  try {
    const res = await fetch(`/connections/${state.activeConnectionId}/semantic/${tableName}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: input.value.trim() }),
    });
    if (!res.ok) { const e = await res.json(); throw new Error(e.detail || "Failed"); }
    const updated = await res.json();
    if (!state.glossary) state.glossary = {};
    state.glossary[tableName] = updated;
    renderSchemaTree();
  } catch (err) { alert(err.message); renderSchemaTree(); }
};

window.startEditColumnDesc = function (e, tableName, colName, currentDesc) {
  e.stopPropagation();
  const parent = e.target.parentElement;
  parent.innerHTML = `
    <div class="inline-edit-form">
      <input type="text" id="edit-col-${tableName}-${colName}"
        value="${escapeHtml(currentDesc)}"
        placeholder="Column description" />
      <button class="btn btn-sm btn-primary" onclick="saveColumnDesc('${escapeHtml(tableName)}','${escapeHtml(colName)}')">Save</button>
      <button class="btn btn-sm btn-outline" onclick="renderSchemaTree()">Cancel</button>
    </div>`;
  document.getElementById(`edit-col-${tableName}-${colName}`)?.focus();
};

window.saveColumnDesc = async function (tableName, colName) {
  const input = document.getElementById(`edit-col-${tableName}-${colName}`);
  if (!input) return;
  try {
    const res = await fetch(`/connections/${state.activeConnectionId}/semantic/${tableName}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ columns: { [colName]: { description: input.value.trim() } } }),
    });
    if (!res.ok) { const e = await res.json(); throw new Error(e.detail || "Failed"); }
    const updated = await res.json();
    if (!state.glossary) state.glossary = {};
    state.glossary[tableName] = updated;
    renderSchemaTree();
  } catch (err) { alert(err.message); renderSchemaTree(); }
};

// ── Chat & Analysis ────────────────────────────────────────────
async function handleAskQuestion(e) {
  e.preventDefault();
  const question = elements.questionInput.value.trim();
  if (!question || state.isAsking || !state.activeConnectionId) return;

  appendUserMessage(question);
  elements.questionInput.value    = "";
  elements.questionInput.disabled = true;
  elements.askBtn.disabled        = true;

  state.isAsking = true;
  elements.chatLoadingIndicator.classList.remove("hidden");
  startLoadingPhases();
  scrollToBottom();

  const msgId = "msg-" + Date.now();

  try {
    const payload = { question, interpret: true, chart: true };
    if (state.conversationId) payload.conversation_id = state.conversationId;

    const res  = await fetch(`/connections/${state.activeConnectionId}/query/ask`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    });
    const data = await res.json();

    if (!res.ok) {
      appendErrorMessage(data.detail || `Server error (${res.status})`);
      return;
    }
    state.conversationId = data.conversation_id;
    appendAssistantResponse(msgId, question, data);
  } catch (err) {
    appendErrorMessage(`Network error: ${err.message}`);
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

function appendAssistantResponse(msgId, question, data) {
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
    // Detect numeric columns
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

  // Feedback SVG icons
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

  row.innerHTML = `
    <div class="assistant-card" id="${msgId}">
      <!-- Interpretation -->
      <div class="interpretation-section">
        <div class="section-label">Analysis</div>
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

      <!-- Feedback -->
      <div class="feedback-bar" id="feedback-bar-${msgId}">
        <span class="feedback-label">Was this helpful?</span>
        <button class="feedback-btn" id="fb-up-${msgId}"
          onclick="submitFeedback('${msgId}','${escapeJs(question)}','${escapeJs(data.generated_sql)}','up')">
          ${thumbUpSVG} Yes
        </button>
        <button class="feedback-btn" id="fb-down-${msgId}"
          onclick="submitFeedback('${msgId}','${escapeJs(question)}','${escapeJs(data.generated_sql)}','down')">
          ${thumbDownSVG} No
        </button>
      </div>
    </div>`;

  elements.chatMessages.appendChild(row);

  // Plotly dark-themed render
  if (data.chart && data.chart.plotly_figure_json && window.Plotly) {
    try {
      const figure = JSON.parse(data.chart.plotly_figure_json);

      // Dark theme overrides — match design tokens
      const darkLayout = {
        paper_bgcolor: "#141518",
        plot_bgcolor:  "#141518",
        font:          { family: "'JetBrains Mono', monospace", size: 11, color: "#9ca3af" },
        margin:        { t: 28, b: 40, l: 48, r: 20, pad: 0 },
        xaxis: {
          gridcolor:    "rgba(255,255,255,0.05)",
          linecolor:    "rgba(255,255,255,0.08)",
          tickcolor:    "rgba(255,255,255,0.08)",
          zerolinecolor:"rgba(255,255,255,0.06)",
          tickfont:     { size: 10 },
        },
        yaxis: {
          gridcolor:    "rgba(255,255,255,0.05)",
          linecolor:    "rgba(255,255,255,0.08)",
          tickcolor:    "rgba(255,255,255,0.08)",
          zerolinecolor:"rgba(255,255,255,0.06)",
          tickfont:     { size: 10 },
        },
        legend: {
          bgcolor:     "rgba(0,0,0,0)",
          borderwidth: 0,
          font:        { size: 10 },
        },
        colorway: ["#3b82f6","#22c55e","#f59e0b","#a78bfa","#ef4444","#06b6d4"],
      };

      const mergedLayout = Object.assign({}, figure.layout || {}, darkLayout);

      // Also style individual traces if bar/line
      const styledTraces = (figure.data || []).map((trace, i) => {
        const palette = darkLayout.colorway;
        const col     = palette[i % palette.length];
        if (trace.type === "bar")  return { ...trace, marker: { ...trace.marker, color: col } };
        if (trace.type === "scatter" || trace.type === "line")
          return { ...trace, line: { ...trace.line, color: col }, marker: { ...trace.marker, color: col } };
        return trace;
      });

      const chartEl = document.getElementById(chartId);
      if (chartEl) {
        Plotly.newPlot(chartEl, styledTraces, mergedLayout, {
          responsive: true,
          displayModeBar: false,
        });
      }
    } catch (err) {
      console.warn("Plotly render error:", err);
    }
  }

  scrollToBottom();
}

// ── SQL keyword highlighting (lightweight, no external lib) ────
function highlightSQL(sql) {
  if (!sql) return "";
  const escaped = escapeHtml(sql);

  const keywords = [
    "SELECT","FROM","WHERE","GROUP BY","ORDER BY","HAVING","LIMIT",
    "JOIN","LEFT JOIN","RIGHT JOIN","INNER JOIN","OUTER JOIN","ON",
    "AND","OR","NOT","IN","IS","NULL","AS","DISTINCT","UNION","WITH",
    "CASE","WHEN","THEN","ELSE","END","OFFSET","FETCH","OVER","PARTITION",
    "BY","ASC","DESC","INSERT","UPDATE","DELETE",
  ].sort((a, b) => b.length - a.length); // longest first

  const kwPattern = new RegExp(`\\b(${keywords.join("|")})\\b`, "gi");
  const fnPattern = /\b(SUM|COUNT|AVG|MIN|MAX|COALESCE|NULLIF|ROUND|UPPER|LOWER|TRIM|DATE|STRFTIME|EXTRACT|YEAR|MONTH|DAY|CAST|ISNULL|IIF|IF)\s*(?=\()/gi;
  const numPattern = /\b(\d+(\.\d+)?)\b/g;
  const strPattern = /'([^']*)'/g;

  return escaped
    .replace(strPattern,  (_m, s) => `<span class="sql-str">'${s}'</span>`)
    .replace(fnPattern,   (m)     => `<span class="sql-fn">${m}</span>`)
    .replace(kwPattern,   (m)     => `<span class="sql-kw">${m}</span>`)
    .replace(numPattern,  (m)     => `<span class="sql-num">${m}</span>`);
}

// ── Copy SQL button ────────────────────────────────────────────
window.copySQL = function (msgId) {
  const pre = document.getElementById(`sql-${msgId}`);
  if (!pre) return;
  const text = pre.innerText || pre.textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = pre.closest(".sql-code-wrapper")?.querySelector(".copy-btn");
    if (btn) {
      btn.textContent = "Copied!";
      btn.classList.add("copied");
      setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("copied"); }, 2000);
    }
  }).catch(() => {});
};

// ── Feedback ───────────────────────────────────────────────────
window.submitFeedback = async function (msgId, question, sql, rating) {
  const bar = document.getElementById(`feedback-bar-${msgId}`);
  if (!bar || !state.activeConnectionId) return;

  const upBtn   = document.getElementById(`fb-up-${msgId}`);
  const downBtn = document.getElementById(`fb-down-${msgId}`);
  if (upBtn)   { upBtn.disabled   = true; }
  if (downBtn) { downBtn.disabled = true; }

  try {
    const res = await fetch(`/connections/${state.activeConnectionId}/feedback`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ question, generated_sql: sql, rating }),
    });
    if (!res.ok) throw new Error("Failed to record feedback");

    if (rating === "up"   && upBtn)   upBtn.classList.add("active-up");
    if (rating === "down" && downBtn) downBtn.classList.add("active-down");

    const label = bar.querySelector(".feedback-label");
    if (label) {
      label.textContent = "Saved — confirmed answers train future SQL generation.";
      label.classList.add("feedback-thanks");
    }
  } catch (err) {
    if (upBtn)   upBtn.disabled   = false;
    if (downBtn) downBtn.disabled = false;
    console.warn("Feedback error:", err.message);
  }
};

// ── Utilities ──────────────────────────────────────────────────
function scrollToBottom() {
  setTimeout(() => {
    elements.chatMessages.scrollTop = elements.chatMessages.scrollHeight;
  }, 60);
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

function escapeJs(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/\\/g, "\\\\")
    .replace(/'/g, "\\'")
    .replace(/"/g, '\\"')
    .replace(/\n/g, "\\n")
    .replace(/\r/g, "\\r");
}


document.addEventListener("DOMContentLoaded", init);

// ── Hero stat count-up animation ──────────────────────────────
// Runs only on the setup screen; safe to call even if elements don't exist.
// Each .stat-number with data-target counts up when it scrolls into view.
// Respects prefers-reduced-motion.
(function initStatCountUp() {
  const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

  function countUp(el, target, duration, delay) {
    if (prefersReduced) {
      // Skip to final value immediately
      if (!el.classList.contains("stat-zero")) el.textContent = target + (el.classList.contains("stat-pct") ? "" : "");
      return;
    }
    setTimeout(() => {
      const start = performance.now();
      function step(now) {
        const elapsed = now - start;
        const progress = Math.min(elapsed / duration, 1);
        const eased = easeOutCubic(progress);
        const current = Math.round(eased * target);
        // stat-zero stays as "$0", stat-pct appends % via CSS ::after
        if (el.classList.contains("stat-zero")) {
          el.textContent = "$0"; // static — always $0
        } else {
          el.textContent = current;
        }
        if (progress < 1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    }, delay);
  }

  function observeStats() {
    const statNums = document.querySelectorAll(".stat-number[data-target]");
    if (!statNums.length) return;

    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        observer.unobserve(entry.target);
        const el = entry.target;
        const target = parseInt(el.dataset.target, 10);
        // Stagger based on visual order
        const siblings = [...document.querySelectorAll(".stat-number[data-target]")];
        const idx = siblings.indexOf(el);
        countUp(el, target, 900, idx * 120);
      });
    }, { threshold: 0.3 });

    statNums.forEach((el) => observer.observe(el));
  }

  // Run after a tick so the DOM is fully painted
  setTimeout(observeStats, 50);
})();

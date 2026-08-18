# DataWise — Vercel Serverless Demo

This directory contains a **Vercel-deployable version** of the DataWise (AI Business Analyst) application. It packages the exact visual design and user experience of the full project into a static + serverless deployment.

---

## 🚀 Architecture & Live vs Demo Capabilities

| Feature | Status in Vercel Demo | Implementation |
|---|---|---|
| **Visual Design & UI** | 🟢 **100% Identical** | Pixel-for-pixel match of `static/index.html` and `static/style.css` |
| **CSV Upload & Ingestion** | 🟢 **Live Serverless** | Python serverless function (`/api/upload.py`) parses, normalizes dates, and introspects schema via in-memory SQLite |
| **Schema Tree Sidebar** | 🟢 **Live** | Renders columns, detected types, and row counts dynamically from uploaded CSV |
| **Natural Language Queries** | 🟡 **Pre-recorded Real Examples** | Returns real validated SQL, query data, plain-English analysis, and Plotly charts |
| **Database Connections** | ⚪ **Disabled (Notice)** | Live remote database connections require persistent backend networking |
| **LLM Glossary Generation** | ⚪ **Disabled (Notice)** | Requires local Ollama instance / dedicated GPU host |

> For full, unrestricted live LLM SQL generation and database connectivity, see the main [DataWise Repository](../README.md).

---

## 📁 Directory Structure

```text
vercel-demo/
├── api/
│   └── upload.py              # Vercel Python serverless function (CSV parsing & schema introspection)
├── demo-data/
│   └── example-responses.json # Real, pre-baked verified query -> response pairs
├── public/
│   ├── index.html             # Identical copy of static/index.html
│   ├── style.css              # Identical copy of static/style.css
│   ├── app.js                 # Adapted client with DEMO_MODE logic
│   └── demo-data/             # Static servable copy of example responses
├── requirements.txt           # Python dependencies for serverless functions (pandas)
├── vercel.json                # Vercel routing and rewrite rules
└── README.md
```

---

## 🛠️ Testing Locally with `vercel dev`

### 1. Prerequisites
Ensure you have Node.js and the [Vercel CLI](https://vercel.com/docs/cli) installed:
```bash
npm install -g vercel
```

Ensure you have Python 3.9+ installed.

### 2. Run Local Development Server
Navigate into the `vercel-demo` directory:
```bash
cd vercel-demo
vercel dev
```

The CLI will start a local emulation of Vercel's serverless environment, typically at `http://localhost:3000`.

### 3. Verification Steps
1. **Landing Page**: Verify header branding (`DataWise`), transparent hero bar, and atmospheric background.
2. **Instant Preview**: Click on **"Sample Sales Dataset"** under Saved Connections to enter the workspace immediately with pre-loaded 100-row sales data.
3. **Live CSV Upload**: Upload any `.csv` file (or [`sales.csv`](../sales.csv)). The `/api/upload` serverless function will parse column types, dates, and render the schema tree in the sidebar.
4. **Example Queries**: Try asking:
   - `total revenue by region`
   - `break down total profit by sales channel`
   - `what is the average unit price`
   - `break down total expenses by product category`
   - `top 5 countries by units sold`
5. **Fallback Notice**: Type an un-cached question to verify the informative demo notice and clickable suggestion chips.

---

## 🚢 Deploying to Vercel

To deploy to production directly from your terminal:
```bash
cd vercel-demo
vercel --prod
```
Or import the repository in your [Vercel Dashboard](https://vercel.com/new) and set the **Root Directory** to `vercel-demo`.

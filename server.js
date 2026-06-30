/**
 * LCL Ocean Services - WR / Consolidation Picking Tool
 *
 * A mobile checklist for picking customer freight against a consolidation list,
 * with photo documentation posted straight to Slack.
 *
 * Endpoints:
 *   GET  /              -> the picking app (public/index.html)
 *   GET  /api/health    -> quick status + whether Slack is configured
 *   GET  /api/channels  -> channels the picker can post to
 *   POST /api/parse-list -> upload an Excel/CSV/PDF consolidation list, get pick items back
 *   POST /api/post      -> post the pick summary + photos to a Slack channel
 */

const express = require("express");
const multer = require("multer");
const XLSX = require("xlsx");
const { WebClient } = require("@slack/web-api");

const app = express();
const PORT = process.env.PORT || 3000;

// JSON bodies can be large because photos come in as base64 data URLs.
app.use(express.json({ limit: "60mb" }));
app.use(express.static("public"));

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 15 * 1024 * 1024 }, // 15 MB list file
});

const SLACK_TOKEN = process.env.SLACK_BOT_TOKEN || "";
const slack = SLACK_TOKEN ? new WebClient(SLACK_TOKEN) : null;
const ALLOWED_CHANNELS = (process.env.SLACK_ALLOWED_CHANNELS || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

/* ------------------------------------------------------------------ */
/* Health                                                              */
/* ------------------------------------------------------------------ */
app.get("/api/health", (req, res) => {
  res.json({ ok: true, slackConfigured: !!slack });
});

/* ------------------------------------------------------------------ */
/* Channel list                                                        */
/* ------------------------------------------------------------------ */
app.get("/api/channels", async (req, res) => {
  if (!slack) return res.json({ channels: [], slackConfigured: false });
  try {
    const channels = [];
    let cursor;
    do {
      const resp = await slack.conversations.list({
        types: "public_channel,private_channel",
        exclude_archived: true,
        limit: 200,
        cursor,
      });
      for (const c of resp.channels || []) {
        channels.push({ id: c.id, name: c.name, is_private: !!c.is_private });
      }
      cursor = resp.response_metadata && resp.response_metadata.next_cursor;
    } while (cursor);

    let out = channels;
    if (ALLOWED_CHANNELS.length) {
      out = channels.filter((c) => ALLOWED_CHANNELS.includes(c.id));
    }
    out.sort((a, b) => a.name.localeCompare(b.name));
    res.json({ channels: out, slackConfigured: true });
  } catch (err) {
    console.error("channels error:", err.data || err.message);
    res.status(500).json({ error: "Could not load channels", detail: err.message });
  }
});

/* ------------------------------------------------------------------ */
/* Parse an uploaded consolidation list                                */
/* ------------------------------------------------------------------ */

// Map a raw header string to one of our known fields.
function classifyHeader(h) {
  const k = String(h || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  if (!k) return null;
  if (/(wr|warehouserec|receipt|whr|billoflading|bol|ref)/.test(k) || k === "bl") return "wr";
  if (/(desc|item|product|commodity|goods|cargo|style)/.test(k)) return "description";
  if (/(qty|quant|pieces|pcs|cartons|ctns|cases|boxes|units|count)/.test(k)) return "expectedQty";
  if (/(loc|bin|slot|position|aisle|rack|spot)/.test(k)) return "location";
  if (/(customer|consignee|client|account)/.test(k)) return "customer";
  return null;
}

function rowsToItems(rows) {
  if (!rows || !rows.length) return [];

  // Find the header row: the row that maps the most known fields.
  let headerIdx = 0;
  let bestScore = -1;
  const scanLimit = Math.min(rows.length, 15);
  for (let i = 0; i < scanLimit; i++) {
    const score = rows[i].reduce(
      (acc, cell) => acc + (classifyHeader(cell) ? 1 : 0),
      0
    );
    if (score > bestScore) {
      bestScore = score;
      headerIdx = i;
    }
  }

  const header = rows[headerIdx];
  const colMap = {};
  header.forEach((cell, idx) => {
    const field = classifyHeader(cell);
    if (field && colMap[field] === undefined) colMap[field] = idx;
  });

  // If we couldn't recognize anything, fall back to positional columns.
  const recognized = Object.keys(colMap).length > 0;

  const items = [];
  for (let i = headerIdx + 1; i < rows.length; i++) {
    const row = rows[i];
    if (!row || row.every((c) => c === "" || c === null || c === undefined)) continue;

    let wr, description, expectedQty, location, customer;
    if (recognized) {
      wr = colMap.wr !== undefined ? row[colMap.wr] : "";
      description = colMap.description !== undefined ? row[colMap.description] : "";
      expectedQty = colMap.expectedQty !== undefined ? row[colMap.expectedQty] : "";
      location = colMap.location !== undefined ? row[colMap.location] : "";
      customer = colMap.customer !== undefined ? row[colMap.customer] : "";
    } else {
      [wr, description, expectedQty, location] = row;
    }

    const cleanQty = parseInt(String(expectedQty).replace(/[^0-9]/g, ""), 10);
    items.push({
      wr: String(wr || "").trim(),
      description: String(description || "").trim(),
      expectedQty: Number.isFinite(cleanQty) ? cleanQty : "",
      location: String(location || "").trim(),
      customer: String(customer || "").trim(),
    });
  }

  // Drop rows that are completely empty of meaning.
  return items.filter((it) => it.wr || it.description);
}

app.post("/api/parse-list", upload.single("file"), async (req, res) => {
  try {
    if (!req.file) return res.status(400).json({ error: "No file uploaded" });
    const name = (req.file.originalname || "").toLowerCase();

    let rows = [];
    if (name.endsWith(".pdf")) {
      // Best-effort text extraction; works for text-based (not scanned) PDFs.
      const pdfParse = require("pdf-parse");
      const data = await pdfParse(req.file.buffer);
      rows = data.text
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => line.split(/\s{2,}|\t|\|/).map((c) => c.trim()));
    } else {
      // xlsx handles .xlsx, .xls, and .csv
      const wb = XLSX.read(req.file.buffer, { type: "buffer" });
      const sheet = wb.Sheets[wb.SheetNames[0]];
      rows = XLSX.utils.sheet_to_json(sheet, { header: 1, blankrows: false, defval: "" });
    }

    const items = rowsToItems(rows);
    res.json({ items, count: items.length });
  } catch (err) {
    console.error("parse-list error:", err.message);
    res.status(500).json({ error: "Could not read that file", detail: err.message });
  }
});

/* ------------------------------------------------------------------ */
/* Post pick summary + photos to Slack                                 */
/* ------------------------------------------------------------------ */
function dataUrlToBuffer(dataUrl) {
  const m = /^data:(.+?);base64,(.*)$/.exec(dataUrl || "");
  if (!m) return null;
  return { mime: m[1], buffer: Buffer.from(m[2], "base64") };
}

app.post("/api/post", async (req, res) => {
  if (!slack) {
    return res.status(400).json({ error: "Slack is not configured on the server (missing SLACK_BOT_TOKEN)." });
  }
  try {
    const { channel_id, message, photos } = req.body || {};
    if (!channel_id) return res.status(400).json({ error: "No channel selected" });
    if (!message) return res.status(400).json({ error: "No message text" });

    const pics = Array.isArray(photos) ? photos : [];

    if (!pics.length) {
      // No photos -> plain message.
      const r = await slack.chat.postMessage({ channel: channel_id, text: message });
      return res.json({ ok: true, ts: r.ts });
    }

    const file_uploads = [];
    pics.forEach((p, i) => {
      const decoded = dataUrlToBuffer(p);
      if (!decoded) return;
      const ext = (decoded.mime.split("/")[1] || "jpg").replace("jpeg", "jpg");
      file_uploads.push({
        file: decoded.buffer,
        filename: `pick-${Date.now()}-${i + 1}.${ext}`,
      });
    });

    if (!file_uploads.length) {
      const r = await slack.chat.postMessage({ channel: channel_id, text: message });
      return res.json({ ok: true, ts: r.ts });
    }

    const result = await slack.files.uploadV2({
      channel_id,
      initial_comment: message,
      file_uploads,
    });

    res.json({ ok: true, result: "posted", files: file_uploads.length });
  } catch (err) {
    console.error("post error:", err.data || err.message);
    res.status(500).json({ error: "Could not post to Slack", detail: (err.data && err.data.error) || err.message });
  }
});

app.listen(PORT, () => {
  console.log(`WR Pick Tool listening on ${PORT} (Slack ${slack ? "configured" : "NOT configured"})`);
});

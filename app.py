"""
PDF Tools - LCL Ocean Services (single-file web service).

A small hub of PDF utilities for the operations team:
  /            hub landing (cards for each tool)
  /splitter    WR PDF Splitter  - split a multi-invoice PDF into one file per handwritten WR number
  /cleaner     PDF Cleaner      - find & remove duplicate pages, optionally compress for email
  /combiner    PDF Combiner     - merge PDFs in order, auto-name by BL # + customer

Files in this repo: app.py, requirements.txt, render.yaml, logo.png, logo-wordmark.png.
Start command: gunicorn app:app --timeout 300 --workers 1 --threads 4

Environment variables:
    ANTHROPIC_API_KEY   (required) your Anthropic key
    ANTHROPIC_MODEL     (optional) defaults to claude-sonnet-4-6
    APP_PASSWORD        (optional) if set, the site requires a password (HTTP Basic)
"""
import os
import io
import re
import json
import base64
import hashlib
import zipfile

from flask import Flask, request, jsonify, send_file, send_from_directory, Response
import fitz  # PyMuPDF
from pypdf import PdfReader, PdfWriter
from PIL import Image
from anthropic import Anthropic

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
APP_PASSWORD = os.environ.get("APP_PASSWORD")
MAX_MB = 60


@app.before_request
def gate():
    if APP_PASSWORD:
        auth = request.authorization
        if not auth or auth.password != APP_PASSWORD:
            return Response("Authentication required.", 401,
                            {"WWW-Authenticate": 'Basic realm="PDF Tools"'})


def _client():
    if not API_KEY:
        raise RuntimeError("Server is missing ANTHROPIC_API_KEY.")
    return Anthropic(api_key=API_KEY)


def _thumb(page, frac_top=1.0, width=360, quality=70):
    pix = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if frac_top < 1.0:
        img = img.crop((0, 0, img.width, int(img.height * frac_top)))
    img = img.resize((width, int(img.height * width / img.width)))
    b = io.BytesIO()
    img.save(b, format="JPEG", quality=quality)
    return base64.b64encode(b.getvalue()).decode()


# ---------------------------------------------------------------- WR splitter
def render_pages(pdf_bytes, longest_px=1300):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for i, page in enumerate(doc):
        rect = page.rect
        longest = max(rect.width, rect.height) or 1
        zoom = min(longest_px / longest, 2.2)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72)
        jpeg = buf.getvalue()
        w, h = img.size
        top = img.crop((0, 0, w, int(h * 0.45))).resize((360, int(h * 0.45 * 360 / w)))
        tbuf = io.BytesIO()
        top.save(tbuf, format="JPEG", quality=70)
        thumb_b64 = base64.b64encode(tbuf.getvalue()).decode()
        text = re.sub(r"\s+", " ", page.get_text() or "").strip()[:900]
        pages.append({"num": i + 1, "jpeg": jpeg, "text": text, "thumb": thumb_b64})
    return pages


def build_prompt(pages):
    pt = ("There are %d page images, in order (page 1 first). Below is the printed "
          "(machine) text from each page to help you group pages into invoices. "
          "Handwriting is NOT in this text - read it from the images.\n\n" % len(pages))
    for p in pages:
        pt += "PAGE %d printed text: %s\n" % (p["num"], p["text"] or "(none)")
    pt += """
TASK: Split these pages into separate invoices and read the handwritten WR number on each.

Determine invoice boundaries:
- A new invoice begins where the printed invoice/order number changes (strongest signal), or a new vendor logo appears.
- "Page X of Y" footers: "Page 1 of N" is a first page; the page with the totals / "Total Amount Due" block is the last page of that invoice.
- Pages with only line items, subtotals, or boilerplate terms belong to the invoice above them, not a new one.
- Every page from 1 to the last page MUST appear in exactly one invoice's "pages" list - do not skip any. If a page is blank, a duplicate, or you are unsure, still attach it to the most likely adjacent invoice and say so in that invoice's notes. Never leave a page out.

Read the WR number (handwritten in pen, usually near the header, on the FIRST page of a multi-page invoice):
- WR numbers are 4-digit numbers, commonly 3500-3900, written cleanly in blue/black pen, sometimes circled in red.
- A single invoice may have MULTIPLE WRs, often slash-separated like "3682/3758" -> return both.
- IGNORE these look-alikes (NOT WR numbers): long tracking numbers ("52206217 6964","TBA3313...","TIN = ...","UPS 1Z..."); category words ("shoes","bags","Bal/B"); first names in margins ("Chris","Vanessa"); delivery dates ("del May 27","26/5"); "POSTED" stamps in pink/red; initials next to dates ("RB 21/5").

Return ONLY valid JSON, no prose, in this exact shape:
{"invoices":[{"pages":[1,2],"vendor":"Nike","invoice":"1090831105","wr":["3798"],"confidence":"high","notes":"short note on anything uncertain, or empty"}]}
confidence is "high","medium", or "low". If you cannot find a WR, use "wr":[] and explain in notes."""
    return pt


def analyze_pages(pages):
    client = _client()
    content = [{"type": "text", "text": build_prompt(pages)}]
    for p in pages:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.b64encode(p["jpeg"]).decode()}})
    msg = client.messages.create(model=MODEL, max_tokens=8192,
                                 messages=[{"role": "user", "content": content}])
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{[\s\S]*\}", txt)
    if not m:
        raise RuntimeError("Could not parse the model response.")
    return json.loads(m.group(0)).get("invoices", [])


# ---------------------------------------------------------------- PDF cleaner
def _ahash(page):
    pix = page.get_pixmap(matrix=fitz.Matrix(0.35, 0.35), colorspace=fitz.csGRAY, alpha=False)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples).resize((16, 16))
    px = list(img.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for i, v in enumerate(px):
        if v > avg:
            bits |= (1 << i)
    return bits


def _text_sig(page):
    t = re.sub(r"\s+", " ", (page.get_text() or "")).strip().lower()
    return t if len(t) >= 25 else None


def _hamming(a, b):
    return bin(a ^ b).count("1")


def _same_page(a, b):
    # Duplicates only if extracted text matches exactly (digital docs), or -
    # when neither page has usable text (scans) - the image hashes are almost
    # identical. Never merge two pages that have different text.
    if a["text"] is not None and b["text"] is not None:
        return a["text"] == b["text"]
    if a["text"] is None and b["text"] is None:
        return _hamming(a["ah"], b["ah"]) <= 10
    return False


def find_duplicate_groups(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    infos = []
    for i, page in enumerate(doc):
        ts = _text_sig(page)
        infos.append({"num": i + 1, "text": ts,
                      "ah": (None if ts is not None else _ahash(page)), "page": page})
    groups = []
    for info in infos:
        placed = False
        for g in groups:
            if _same_page(g[0], info):
                g.append(info)
                placed = True
                break
        if not placed:
            groups.append([info])
    dup_groups = [g for g in groups if len(g) > 1]
    return doc, len(infos), dup_groups


def compress_pages(pdf_bytes, keep_pages, dpi=150, quality=60):
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()
    for p in keep_pages:
        page = src[p - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        b = io.BytesIO()
        img.save(b, format="JPEG", quality=quality, optimize=True)
        rect = page.rect
        np = out.new_page(width=rect.width, height=rect.height)
        np.insert_image(np.rect, stream=b.getvalue())
    return out.tobytes(deflate=True, garbage=4)


# ---------------------------------------------------------------- PDF combiner
def _safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|]+', " ", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "COMBINED"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def detect_bl_customer(files):
    """files: list of (filename, bytes). Returns {'bl':..,'customer':..}."""
    chunks = []
    for name, b in files:
        try:
            r = PdfReader(io.BytesIO(b))
            t = ""
            for pg in r.pages[:2]:
                t += (pg.extract_text() or "") + "\n"
            if t.strip():
                chunks.append("FILE %s:\n%s" % (name, t[:2500]))
        except Exception:
            pass
    text = "\n\n".join(chunks)[:7000]
    try:
        client = _client()
        if text.strip():
            prompt = ("From this shipping paperwork (bill of lading, consolidation list, invoices), "
                      "find the Bill of Lading number and the customer / consignee company name. "
                      "The BL number often looks like 43546-26. The customer is the consignee business "
                      "name. Return ONLY JSON: {\"bl\":\"...\",\"customer\":\"...\"}. Use empty strings if unsure.\n\n" + text)
            msg = client.messages.create(model=MODEL, max_tokens=300,
                                         messages=[{"role": "user", "content": prompt}])
        else:
            # scanned: vision on first page of first file
            first = files[0]
            doc = fitz.open(stream=first[1], filetype="pdf")
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            bb = io.BytesIO()
            img.save(bb, format="JPEG", quality=75)
            msg = client.messages.create(model=MODEL, max_tokens=300, messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                 "data": base64.b64encode(bb.getvalue()).decode()}},
                {"type": "text", "text": "This is a bill of lading. Return ONLY JSON {\"bl\":\"...\",\"customer\":\"...\"} "
                 "with the Bill of Lading number and the consignee/customer company name."}]}])
        txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{[\s\S]*\}", txt)
        d = json.loads(m.group(0)) if m else {}
        return {"bl": (d.get("bl") or "").strip(), "customer": (d.get("customer") or "").strip()}
    except Exception as e:
        return {"bl": "", "customer": "", "error": str(e)}


# ================================================================ HTML shell
HEAD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__ &mdash; LCL Ocean Services</title>
<link rel="icon" type="image/png" href="/logo.png" />
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@700;800;900&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root{
    --navy:#18294d; --teal:#52b3c0; --teal-dark:#3f9aa7; --coral:#e8623f;
    --bg:#f4f6f8; --card:#ffffff; --text:#2c3a4f; --muted:#8a96a3; --line:#e4e8ec;
    --ok:#1d9e75; --okbg:#e7f6ef; --warn:#9a6800; --warnbg:#fff6e0; --err:#e8623f; --errbg:#fdecec;
    --radius:14px; --shadow:0 6px 20px rgba(24,41,77,.07); --shadow-hover:0 12px 30px rgba(24,41,77,.14);
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:'Inter',system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;min-height:100vh}
  a{color:inherit;text-decoration:none}
  .app{display:flex;min-height:100vh}
  .sidebar{width:230px;flex:0 0 230px;background:#5c939c;color:#fff;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
  .sidebar .brand-cell{background:#fff;height:72px;display:flex;align-items:center;justify-content:center;padding:0 14px}
  .sidebar .brand-cell img{height:30px;width:auto;object-fit:contain}
  .side-title{font-family:'Montserrat';font-weight:800;font-size:12px;letter-spacing:2px;color:#eaf3f4;padding:16px 22px 8px}
  .side-nav{display:flex;flex-direction:column;flex:1}
  .side-nav a{display:flex;align-items:center;gap:11px;padding:12px 22px;font-size:14.5px;font-weight:600;font-family:'Montserrat';color:#eaf3f4;border-left:3px solid transparent}
  .side-nav a svg{width:18px;height:18px;opacity:.95;flex:0 0 18px}
  .side-nav a:hover{background:#517f88}
  .side-nav a.navactive{background:#517f88;border-left-color:#fff;color:#fff}
  .side-foot{border-top:1px solid rgba(255,255,255,.15);padding:14px 22px;display:flex;flex-direction:column;gap:9px}
  .side-foot a{font-size:12.5px;font-weight:600;color:#eaf3f4}.side-foot a:hover{color:#fff}
  .main{flex:1;min-width:0;display:flex;flex-direction:column}
  .topbar{background:#fff;height:64px;display:flex;align-items:center;justify-content:space-between;padding:0 24px;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
  .topbar .crumbs{font-size:13px;color:var(--muted)} .topbar .crumbs b{color:var(--navy);font-family:'Montserrat';font-weight:700}
  .content{padding:26px 30px 44px;max-width:1040px;width:100%}
  .hlinks{display:flex;align-items:center;gap:9px;position:relative}
  .hublink{font-size:12px;font-weight:600;font-family:'Montserrat';color:#fff;background:var(--teal);padding:7px 14px;border-radius:999px;cursor:pointer;border:none;display:inline-flex;align-items:center;gap:6px}
  .hublink:hover{background:var(--teal-dark)}
  .menu{position:relative}
  .dropdown{position:absolute;right:0;top:calc(100% + 8px);background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow-hover);padding:8px;min-width:250px;display:none;z-index:40}
  .dropdown.show{display:block}
  .dropdown a{display:flex;align-items:center;gap:9px;padding:9px 12px;border-radius:9px;font-size:13.5px;font-weight:600;color:var(--navy)}
  .dropdown a:hover{background:var(--bg)}
  .dropdown .dot{width:8px;height:8px;border-radius:50%;background:var(--teal);flex:0 0 8px}
  .dropdown a.coral .dot{background:var(--coral)}
  @media(max-width:860px){
    .app{flex-direction:column}
    .sidebar{width:100%;flex:none;height:auto;position:static}
    .sidebar .brand-cell{height:58px}
    .side-title{display:none}
    .side-nav{flex-direction:row;overflow-x:auto}
    .side-nav a{border-left:none;border-bottom:3px solid transparent;white-space:nowrap;padding:12px 15px}
    .side-nav a.navactive{border-left:none;border-bottom-color:#fff}
    .side-foot{flex-direction:row;gap:16px}
    .content{padding:20px 16px 40px}
    .topbar{padding:0 14px}
  }
  main{max-width:1000px;margin:26px auto 60px;padding:0 22px}
  .lead{margin:0 0 20px}
  .lead h2{font-family:'Montserrat';font-weight:800;font-size:22px;color:var(--navy);margin:0}
  .lead p{color:var(--muted);font-size:14px;margin:6px 0 0}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px}
  .tile{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:24px;
    box-shadow:var(--shadow);transition:transform .15s,box-shadow .15s;display:flex;flex-direction:column;cursor:pointer}
  .tile:hover{transform:translateY(-4px);box-shadow:var(--shadow-hover)}
  .tile .ic{width:50px;height:50px;border-radius:12px;display:flex;align-items:center;justify-content:center;
    background:rgba(82,179,192,.14);color:var(--teal-dark);margin-bottom:16px}
  .tile .ic svg{width:26px;height:26px}
  .tile.coral .ic{background:rgba(232,98,63,.13);color:var(--coral)}
  .tile h3{font-family:'Montserrat';font-weight:700;font-size:17px;color:var(--navy);margin:0 0 6px}
  .tile p{margin:0;font-size:14px;color:var(--muted);line-height:1.5;flex:1}
  .tile .go{margin-top:16px;font-family:'Montserrat';font-weight:600;font-size:13px;color:var(--teal-dark);display:flex;align-items:center;gap:6px}
  .tile.coral .go{color:var(--coral)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:22px;margin-bottom:18px;box-shadow:var(--shadow)}
  .card h3{font-family:'Montserrat';font-weight:700;font-size:16px;color:var(--navy);margin:0 0 4px}
  .card .sub{color:var(--muted);font-size:13px;margin:0 0 14px}
  button{font:inherit;border:0;border-radius:10px;padding:11px 18px;font-weight:600;cursor:pointer}
  .btn{background:var(--teal);color:#fff;font-family:'Montserrat'}.btn:hover{background:var(--teal-dark)}
  .btn:disabled{background:#aab6c6;cursor:not-allowed}
  .btn-ghost{background:#eef2f5;color:var(--navy);border:1px solid var(--line)}.btn-ghost:hover{background:#e4e9ee}
  .drop{border:2px dashed #c4d0e0;border-radius:12px;padding:34px 20px;text-align:center;color:var(--muted);cursor:pointer;transition:.15s;background:#fafcff}
  .drop.drag{border-color:var(--teal);background:#f0f8fa;color:var(--teal-dark)}.drop strong{color:var(--navy)}
  .toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:14px 0 4px}.toolbar .spacer{flex:1}
  .hint{font-size:12px;color:var(--muted)}
  label.fld{display:block;font-size:13px;font-weight:600;margin:10px 0 5px;font-family:'Montserrat';color:var(--navy)}
  input.txt{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;font-size:14px;font-family:inherit}
  input.txt:focus{outline:2px solid #bcd3ff;border-color:var(--teal)}
  .status{font-size:14px;padding:12px 14px;border-radius:10px;margin-top:14px;display:none}
  .status.show{display:block}.status.info{background:#eaf4f6;color:var(--teal-dark)}
  .status.err{background:var(--errbg);color:var(--err)}.status.ok{background:var(--okbg);color:var(--ok)}
  table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
  th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);font-family:'Montserrat';font-weight:700}
  td .nm{width:100%;padding:7px 8px;border:1px solid var(--line);border-radius:7px;font-size:13px;font-family:inherit}
  td.flag .nm{border-color:#e3b341;background:var(--warnbg)}
  .thumb{width:150px;border:1px solid var(--line);border-radius:6px}
  .thumbsm{width:96px;border:1px solid var(--line);border-radius:6px}
  .pill{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px}
  .pill.hi{background:var(--okbg);color:var(--ok)}.pill.med{background:var(--warnbg);color:var(--warn)}.pill.lo{background:var(--errbg);color:var(--err)}
  .note{color:var(--muted);font-size:12px;margin-top:3px}.mono{font-family:ui-monospace,Menlo,monospace}
  .dupwarn{background:var(--warnbg);color:var(--warn);border-radius:8px;padding:10px 12px;font-size:13px;margin:12px 0;display:none}
  .dupwarn.show{display:block}.chk{width:16px;height:16px}
  .spin{display:inline-block;width:15px;height:15px;border:2px solid #cdd8e6;border-top-color:var(--teal);border-radius:50%;animation:s .8s linear infinite;vertical-align:-2px;margin-right:7px}
  @keyframes s{to{transform:rotate(360deg)}}
  .dupgrp{border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
  .dupgrp .row{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}
  .dupgrp .item{text-align:center;font-size:12px;color:var(--muted)}
  .dupgrp .item.keep b{color:var(--ok)} .dupgrp .item.drop b{color:var(--coral)}
  .olist{list-style:none;margin:8px 0 0;padding:0}
  .orow{display:flex;align-items:center;gap:10px;background:#fafcff;border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px}
  .orow .idx{font-family:'Montserrat';font-weight:800;color:var(--teal-dark);width:20px;text-align:center}
  .orow .fn{flex:1;font-size:13.5px;color:var(--navy);word-break:break-all}
  .orow .sz{font-size:12px;color:var(--muted)}
  .orow button{padding:6px 10px;font-size:13px}
  .hist{list-style:none;margin:0;padding:0}
  .hist li{display:flex;gap:12px;align-items:baseline;padding:9px 0;border-bottom:1px solid var(--line);font-size:13px}
  .hist li:last-child{border-bottom:0}
  .hist .when{color:var(--muted);white-space:nowrap;font-size:12px;flex:0 0 auto}
  .hist .cnt{font-weight:700;color:var(--teal-dark);white-space:nowrap}
  .emptyh{color:var(--muted);font-size:13px}
  .foot{color:#8794a8;font-size:12px;text-align:center;margin-top:8px;padding-bottom:20px}
  .foot b{color:var(--navy)}
  .navlbl{font-family:'Montserrat';font-weight:700;font-size:12px;color:#dbe8ea;padding:16px 22px 6px}
  .view{display:none}.view.active{display:block}
  @media(max-width:860px){.navlbl{display:none}}
</style>
</head>
<body>
"""

TOPBAR_HTML = r"""<header class="topbar">
  <div class="crumbs">PDF Tools / <b id="crumb">WR Splitter</b></div>
  <div class="hlinks">
    <div class="menu">
      <button class="hublink" id="toolsBtn">Company Tools &#9662;</button>
      <div class="dropdown" id="toolsMenu">
        <a href="https://warehouse-inventory-app-i6ct.onrender.com/" target="_blank"><span class="dot"></span> Warehouse Inventory App</a>
        <a href="https://rubins-invoice-processor.onrender.com/" target="_blank" class="coral"><span class="dot"></span> Rubins Invoice Processor</a>
        <a href="https://wr-pick-tool.onrender.com/" target="_blank" class="coral"><span class="dot"></span> Inventory Picker Checklist</a>
        <a href="https://lcl-hub.onrender.com/sort-tool.html" target="_blank"><span class="dot"></span> Sports Center Sort</a>
        <a href="https://lcl-hub.onrender.com/air-wr-tool.html" target="_blank"><span class="dot"></span> Air Warehouse Receipt</a>
        <a href="https://lcl-hub.onrender.com/ss-letter-tool.html" target="_blank" class="coral"><span class="dot"></span> Short Shipment Letters</a>
        <a href="https://lcl-hub.onrender.com/accounting-hub.html" target="_blank"><span class="dot"></span> Accounting Tools</a>
        <a href="https://lcl-hub.onrender.com/error-log.html" target="_blank" class="coral"><span class="dot"></span> Error Log</a>
      </div>
    </div>
  </div>
</header>"""

FOOTER_HTML = r"""<p class="foot"><b>LCL Ocean Services</b> &middot; 11701 NW 102nd Rd, Suite 15, Medley, FL 33178 &mdash; files are processed on the server and not stored.</p>"""

DROPDOWN_JS = r"""<script>
(function(){var b=document.getElementById('toolsBtn'),m=document.getElementById('toolsMenu');
if(b){b.onclick=function(e){e.stopPropagation();m.classList.toggle('show');};
document.addEventListener('click',function(){m.classList.remove('show');});}})();
</script>"""

VIEW_JS = r"""<script>
(function(){
  var links=document.querySelectorAll('.side-nav a[data-view]');
  var crumb=document.getElementById('crumb');
  function show(v){
    document.querySelectorAll('.view').forEach(function(el){el.classList.toggle('active', el.id==='view-'+v);});
    links.forEach(function(a){a.classList.toggle('navactive', a.getAttribute('data-view')===v);});
    var act=document.querySelector('.side-nav a[data-view="'+v+'"]');
    if(act&&crumb) crumb.textContent=act.getAttribute('data-label')||v;
    window.scrollTo(0,0);
  }
  links.forEach(function(a){a.addEventListener('click',function(e){e.preventDefault();show(a.getAttribute('data-view'));});});
})();
</script>"""

_TOOLS = [("splitter", "WR Splitter"), ("cleaner", "PDF Cleaner"), ("combiner", "PDF Combiner")]
_ICONS = {
  "splitter": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
  "cleaner": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>',
  "combiner": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/><path d="M9 12h6"/></svg>',
}


def _iife(js):
    js = js.replace('<script>\n"use strict";', '<script>(function(){\n"use strict";', 1)
    return js.rsplit('</script>', 1)[0] + '})();</script>'


def render_page(active="splitter"):
    nav = "".join('<a data-view="%s" data-label="%s" href="#"%s>%s<span>%s</span></a>'
                  % (k, l, (' class="navactive"' if k == active else ''), _ICONS.get(k, ''), l)
                  for k, l in _TOOLS)
    sidebar = ('<aside class="sidebar"><div class="brand-cell">'
               '<img src="/logo-wordmark.png" alt="LCL Ocean Services"></div>'
               '<div class="navlbl">Tools</div><nav class="side-nav">' + nav + '</nav>'
               '<div class="side-foot"><a href="https://lcl-hub.onrender.com/">&#8592; Employee Hub</a>'
               '<a href="https://lcl-hub.onrender.com/guide-pdf-tools.html" target="_blank">Guide &amp; help</a></div></aside>')

    def view(idn, body):
        cls = "view active" if idn == active else "view"
        return '<div id="view-%s" class="%s">%s</div>' % (idn, cls, body)

    views = (view("splitter", SPLITTER_BODY.replace("__MODEL__", MODEL))
             + view("cleaner", CLEANER_BODY) + view("combiner", COMBINER_BODY))
    crumb = dict(_TOOLS).get(active, "WR Splitter")
    topbar = TOPBAR_HTML.replace('id="crumb">WR Splitter', 'id="crumb">' + crumb)
    scripts = DROPDOWN_JS + VIEW_JS + _iife(SPLITTER_JS) + _iife(CLEANER_JS) + _iife(COMBINER_JS)
    return (HEAD_HTML.replace("__TITLE__", "PDF Tools")
            + '<div class="app">' + sidebar + '<div class="main">' + topbar
            + '<div class="content">' + views + '</div>' + FOOTER_HTML
            + '</div></div>' + scripts + "</body></html>")


# ---------------------------------------------------------------- hub landing
HUB_BODY = r"""
<div class="lead">
  <h2>PDF Tools</h2>
  <p>A hub for our PDF utilities. Pick a tool to get started.</p>
</div>
<div class="grid">
  <a class="tile" href="/splitter">
    <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h2M8 17h2M14 13h2M14 17h2"/></svg></span>
    <h3>WR PDF Splitter</h3>
    <p>Split a multi-invoice PDF into separate files, named by the handwritten WR number on each invoice.</p>
    <span class="go">Open tool &#8594;</span>
  </a>
  <a class="tile coral" href="/cleaner">
    <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg></span>
    <h3>PDF Cleaner</h3>
    <p>Scan a combined PDF for duplicate invoices/pages, remove them, and optionally compress the file to email it easily.</p>
    <span class="go">Open tool &#8594;</span>
  </a>
  <a class="tile" href="/combiner">
    <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/><path d="M9 12h6"/></svg></span>
    <h3>PDF Combiner</h3>
    <p>Merge a Bill of Lading, consolidation list, and invoices into one PDF - in order - named by BL # and customer.</p>
    <span class="go">Open tool &#8594;</span>
  </a>
</div>
"""


# ---------------------------------------------------------------- splitter page
SPLITTER_BODY = r"""
<div class="lead">
  <h2>WR PDF Splitter</h2>
  <p>Upload a multi-invoice PDF. It reads the handwritten WR number on each invoice and splits the file into one PDF per WR.</p>
</div>
<div class="card">
  <h3>1. Choose your PDF</h3>
  <p class="sub">A single PDF holding several vendor invoices, each marked with a handwritten WR number.</p>
  <div class="drop" id="drop"><p style="margin:0 0 6px"><strong>Drop a PDF here</strong> or click to browse</p><p style="margin:0;font-size:13px" id="fileName">No file selected</p></div>
  <input type="file" id="fileInput" accept="application/pdf" style="display:none">
  <div class="toolbar"><button class="btn" id="analyzeBtn" disabled>Read WR numbers &amp; propose split</button><span class="hint">Reads with __MODEL__ on the server.</span></div>
  <div class="status" id="status"></div>
</div>
<div class="card" id="reviewCard" style="display:none">
  <h3>2. Review &amp; correct</h3>
  <p class="sub">Check each WR against its scan before splitting - AI reading isn't perfect. Edit any WR, filename, or the Pages each invoice covers. Tick rows and merge them into one file, or add a row for an invoice that was missed. Low-confidence or missing WRs are flagged amber.</p>
  <div class="dupwarn" id="dupwarn"></div>
  <div style="margin:0 0 6px;font-size:13px;color:var(--muted)">Pages to leave out of all files (blank or extra scans), optional:
    <input id="excludePages" class="mono" placeholder="e.g. 35, 36" style="margin-left:6px;padding:6px 8px;border:1px solid var(--line);border-radius:7px;width:140px"></div>
  <div style="overflow-x:auto"><table id="tbl"><thead><tr>
    <th style="width:30px"></th><th style="width:160px">Scan (top)</th><th style="width:90px">Pages</th>
    <th>Vendor</th><th>Invoice #</th><th style="width:110px">WR(s)</th><th style="width:170px">Filename</th><th style="width:80px">Conf.</th>
  </tr></thead><tbody id="tbody"></tbody></table></div>
  <div class="toolbar"><button class="btn-ghost" id="mergeBtn">Merge selected rows</button><button class="btn-ghost" id="addBtn">Add invoice row</button><button class="btn-ghost" id="reanalyzeBtn">Re-read with AI</button><div class="spacer"></div><button class="btn" id="splitBtn">Split &amp; download ZIP</button></div>
  <div class="status" id="status2"></div>
</div>
<div class="card">
  <h3>Recent splits</h3><p class="sub">The last jobs you ran on this device. Stored only in this browser.</p>
  <ul class="hist" id="histList"></ul>
  <div class="toolbar"><div class="spacer"></div><button class="btn-ghost" id="clearHistBtn">Clear history</button></div>
</div>
"""

SPLITTER_JS = r"""<script>
"use strict";
const $ = id => document.getElementById(id);
let currentFile = null, invoices = [], totalPages = 0;
const drop = $("drop");
drop.onclick = () => $("fileInput").click();
["dragover","dragenter"].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add("drag");}));
["dragleave","drop"].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove("drag");}));
drop.addEventListener("drop", ev => { if(ev.dataTransfer.files[0]) pick(ev.dataTransfer.files[0]); });
$("fileInput").onchange = e => { if(e.target.files[0]) pick(e.target.files[0]); };
function pick(f){
  if(!/pdf$/i.test(f.type) && !/\.pdf$/i.test(f.name)){ st("status","err","Please choose a PDF file."); return; }
  currentFile=f; $("fileName").textContent=f.name+"  ("+Math.round(f.size/1024)+" KB)";
  $("analyzeBtn").disabled=false; $("reviewCard").style.display="none"; st("status","info","Ready. Click the read button.");
}
function st(id,kind,msg,spin){ const el=$(id); el.className="status show "+kind; el.innerHTML=(spin?'<span class="spin"></span>':'')+msg; }
function hide(id){ $(id).className="status"; }
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));}
function parsePages(str){const out=[];(str||"").split(",").forEach(part=>{part=part.trim();if(!part)return;const m=part.match(/^(\d+)\s*-\s*(\d+)$/);if(m){for(let n=+m[1];n<=+m[2];n++)out.push(n);}else if(/^\d+$/.test(part))out.push(+part);});return [...new Set(out)].sort((a,b)=>a-b);}
function makeFilename(wr){ return (wr&&wr.length)?("WR"+wr.join("_")+".pdf"):"UNKNOWN.pdf"; }
function assignFilenames(){invoices.forEach(inv=>{if(!inv.filename)inv.filename=makeFilename(inv.wr);});const counts={};invoices.forEach(i=>counts[i.filename]=(counts[i.filename]||0)+1);const seen={};invoices.forEach(inv=>{if(counts[inv.filename]>1){seen[inv.filename]=(seen[inv.filename]||0)+1;inv.filename=inv.filename.replace(/\.pdf$/i,"")+"-"+seen[inv.filename]+".pdf";}});}
function renderTable(){
  const tb=$("tbody"); tb.innerHTML="";
  invoices.forEach((inv,idx)=>{
    const c=(inv.confidence||"").toLowerCase();
    const pill=c==="high"?'<span class="pill hi">high</span>':c==="low"?'<span class="pill lo">low</span>':'<span class="pill med">'+(c||"medium")+'</span>';
    const flag=(c==="low"||!inv.wr||!inv.wr.length)?" flag":"";
    const tr=document.createElement("tr");
    tr.innerHTML='<td><input type="checkbox" class="chk" data-i="'+idx+'"></td>'+
      '<td>'+(inv.thumb?'<img class="thumb" src="data:image/jpeg;base64,'+inv.thumb+'">':'')+'</td>'+
      '<td><input class="nm pg mono" data-i="'+idx+'" value="'+inv.pages.join(", ")+'"></td>'+
      '<td>'+esc(inv.vendor)+'</td><td class="mono">'+esc(inv.invoice)+'</td>'+
      '<td><input class="nm wr" data-i="'+idx+'" value="'+esc((inv.wr||[]).join("/"))+'"></td>'+
      '<td class="'+flag+'"><input class="nm fn" data-i="'+idx+'" value="'+esc(inv.filename)+'"></td>'+
      '<td>'+pill+(inv.notes?'<div class="note">'+esc(inv.notes)+'</div>':'')+'</td>';
    tb.appendChild(tr);
  });
  tb.querySelectorAll(".wr").forEach(el=>el.onchange=e=>{const i=+e.target.dataset.i;invoices[i].wr=e.target.value.split(/[\/,_\s]+/).map(s=>s.trim()).filter(Boolean);invoices[i].filename=makeFilename(invoices[i].wr);assignFilenames();renderTable();});
  tb.querySelectorAll(".fn").forEach(el=>el.onchange=e=>{let v=e.target.value.trim();if(v&&!/\.pdf$/i.test(v))v+=".pdf";invoices[+e.target.dataset.i].filename=v;checkDup();});
  tb.querySelectorAll(".pg").forEach(el=>el.onchange=e=>{invoices[+e.target.dataset.i].pages=parsePages(e.target.value);renderTable();});
  checkDup();
}
function checkDup(){
  const msgs=[]; const exc=new Set(parsePages($("excludePages")?$("excludePages").value:""));
  const seen={}; invoices.forEach(i=>(i.pages||[]).forEach(p=>seen[p]=(seen[p]||0)+1));
  const missing=[]; for(let p=1;p<=totalPages;p++) if(!seen[p]&&!exc.has(p)) missing.push(p);
  const dpages=Object.keys(seen).map(Number).filter(p=>seen[p]>1);
  const both=Object.keys(seen).map(Number).filter(p=>exc.has(p));
  if(missing.length) msgs.push("Pages not yet assigned to any invoice: "+missing.join(", ")+". Add them to the right invoice's Pages box, click Add invoice row, or list them in Pages to leave out if they're blank/extra.");
  if(dpages.length) msgs.push("These pages are in more than one invoice: "+dpages.join(", ")+".");
  if(both.length) msgs.push("These pages are both in an invoice and in Pages to leave out: "+both.join(", ")+".");
  const counts={}; invoices.forEach(i=>counts[i.filename]=(counts[i.filename]||0)+1);
  const dups=Object.keys(counts).filter(k=>counts[k]>1);
  if(dups.length) msgs.push("These filenames repeat ("+dups.join(", ")+").");
  if(invoices.some(i=>/UNKNOWN/.test(i.filename))) msgs.push("One or more invoices have no WR. Type the WR in its row before splitting.");
  const w=$("dupwarn"); if(msgs.length){w.classList.add("show");w.innerHTML=msgs.join("<br>");}else w.classList.remove("show");
}
$("mergeBtn").onclick=()=>{const checked=[...document.querySelectorAll(".chk:checked")].map(c=>+c.dataset.i);if(checked.length<2){st("status2","err","Select at least two rows to merge.");return;}hide("status2");checked.sort((a,b)=>a-b);const base=invoices[checked[0]],pages=[],wr=[];checked.forEach(i=>{pages.push(...invoices[i].pages);(invoices[i].wr||[]).forEach(w=>{if(!wr.includes(w))wr.push(w);});});base.pages=[...new Set(pages)].sort((a,b)=>a-b);base.wr=wr;base.invoice=checked.map(i=>invoices[i].invoice).filter(Boolean).join(", ");base.filename=makeFilename(wr);base.confidence="high";base.notes="merged";invoices=invoices.filter((_,i)=>i===checked[0]||!checked.includes(i));assignFilenames();renderTable();};
$("addBtn").onclick=()=>{invoices.push({pages:[],wr:[],vendor:"",invoice:"",confidence:"low",notes:"added manually"});assignFilenames();renderTable();};
if($("excludePages")) $("excludePages").oninput=()=>checkDup();
function loadHist(){try{return JSON.parse(localStorage.getItem("wr_history")||"[]");}catch(e){return[];}}
function saveHist(h){localStorage.setItem("wr_history",JSON.stringify(h.slice(0,20)));}
function addHist(entry){const h=loadHist();h.unshift(entry);saveHist(h);renderHist();}
function renderHist(){const h=loadHist(),ul=$("histList");if(!h.length){ul.innerHTML='<div class="emptyh">No splits yet on this device.</div>';return;}ul.innerHTML="";h.forEach(e=>{const li=document.createElement("li");li.innerHTML='<span class="when">'+esc(e.when)+'</span><span class="cnt">'+e.count+' file'+(e.count===1?'':'s')+'</span><span>'+esc((e.files||[]).join(", "))+(e.src?' <span style="color:var(--muted)">from '+esc(e.src)+'</span>':'')+'</span>';ul.appendChild(li);});}
$("clearHistBtn").onclick=()=>{localStorage.removeItem("wr_history");renderHist();};
async function analyze(){
  if(!currentFile) return; $("analyzeBtn").disabled=true;
  st("status","info","Reading WR numbers... first run after idle can take ~30s.",true);
  try{
    const fd=new FormData(); fd.append("pdf",currentFile);
    const res=await fetch("/analyze",{method:"POST",body:fd}); const data=await res.json();
    if(!res.ok) throw new Error(data.error||("Error "+res.status));
    totalPages=data.total_pages; invoices=data.invoices;
    invoices.forEach(i=>{i.pages=(i.pages||[]).map(Number);i.wr=i.wr||[];});
    assignFilenames(); hide("status"); $("reviewCard").style.display="block"; renderTable();
    $("reviewCard").scrollIntoView({behavior:"smooth"});
  }catch(err){ st("status","err",err.message||String(err)); } finally{ $("analyzeBtn").disabled=false; }
}
$("analyzeBtn").onclick=analyze; $("reanalyzeBtn").onclick=analyze;
$("splitBtn").onclick=async()=>{
  const exc=parsePages($("excludePages").value); const excSet=new Set(exc);
  const seen={}; invoices.forEach(inv=>inv.pages.forEach(p=>seen[p]=(seen[p]||0)+1));
  const missing=[]; for(let p=1;p<=totalPages;p++) if(!seen[p]&&!excSet.has(p)) missing.push(p);
  const dupes=Object.keys(seen).filter(p=>seen[p]>1);
  if(missing.length||dupes.length){ st("status2","err",(missing.length?"Pages not assigned: "+missing.join(", ")+". ":"")+(dupes.length?"Pages used twice: "+dupes.join(", ")+". ":"")+"Fix page ranges (or list blank pages in Pages to leave out)."); return; }
  const names=invoices.map(i=>i.filename);
  if(new Set(names).size!==names.length){ st("status2","err","Filenames must be unique."); return; }
  st("status2","info","Splitting...",true);
  try{
    const fd=new FormData(); fd.append("pdf",currentFile);
    fd.append("mapping",JSON.stringify(invoices.map(i=>({filename:i.filename,pages:i.pages})))); fd.append("exclude",JSON.stringify(exc));
    const res=await fetch("/split",{method:"POST",body:fd});
    if(!res.ok){ const e=await res.json().catch(()=>({})); throw new Error(e.error||("Error "+res.status)); }
    const blob=await res.blob(); const a=document.createElement("a"); a.href=URL.createObjectURL(blob); a.download="WR_invoices.zip"; a.click(); URL.revokeObjectURL(a.href);
    st("status2","ok","Done - "+invoices.length+" files in WR_invoices.zip.");
    addHist({when:new Date().toLocaleString(),src:currentFile?currentFile.name:"",count:invoices.length,files:invoices.map(i=>i.filename)});
  }catch(err){ st("status2","err",err.message||String(err)); }
};
renderHist();
</script>"""


# ---------------------------------------------------------------- cleaner page
CLEANER_BODY = r"""
<div class="lead">
  <h2>PDF Cleaner</h2>
  <p>Scan a combined PDF for duplicate invoices/pages, remove them, and optionally compress the file so it's easy to email.</p>
</div>
<div class="card">
  <h3>1. Choose your PDF</h3>
  <p class="sub">A combined PDF (e.g. many invoices in one file). We look for pages that repeat.</p>
  <div class="drop" id="cDrop"><p style="margin:0 0 6px"><strong>Drop a PDF here</strong> or click to browse</p><p style="margin:0;font-size:13px" id="cName">No file selected</p></div>
  <input type="file" id="cInput" accept="application/pdf" style="display:none">
  <div class="toolbar"><button class="btn" id="scanBtn" disabled>Scan for duplicates</button></div>
  <div class="status" id="cStatus"></div>
</div>
<div class="card" id="cResult" style="display:none">
  <h3>2. Review duplicates</h3>
  <p class="sub">Each group below is one page that appears more than once. The first copy is kept; later copies are ticked to be removed. Untick anything you want to keep.</p>
  <div id="dupZone"></div>
  <label style="display:flex;align-items:center;gap:9px;margin-top:12px;font-size:14px;font-weight:600;color:var(--navy)">
    <input type="checkbox" id="compressChk" class="chk"> Compress the file for email (smaller size; pages become images, so text is no longer selectable)
  </label>
  <div class="toolbar"><div class="spacer"></div><button class="btn" id="exportBtn">Export cleaned PDF</button></div>
  <div class="status" id="cStatus2"></div>
</div>
"""

CLEANER_JS = r"""<script>
"use strict";
const $=id=>document.getElementById(id);
let cFile=null, cTotal=0, cGroups=[];
function st(id,k,m,sp){const e=$(id);e.className="status show "+k;e.innerHTML=(sp?'<span class="spin"></span>':'')+m;}
function hide(id){$(id).className="status";}
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));}
const cDrop=$("cDrop");
cDrop.onclick=()=>$("cInput").click();
["dragover","dragenter"].forEach(e=>cDrop.addEventListener(e,ev=>{ev.preventDefault();cDrop.classList.add("drag");}));
["dragleave","drop"].forEach(e=>cDrop.addEventListener(e,ev=>{ev.preventDefault();cDrop.classList.remove("drag");}));
cDrop.addEventListener("drop",ev=>{if(ev.dataTransfer.files[0])cPick(ev.dataTransfer.files[0]);});
$("cInput").onchange=e=>{if(e.target.files[0])cPick(e.target.files[0]);};
function cPick(f){if(!/\.pdf$/i.test(f.name)&&!/pdf$/i.test(f.type)){st("cStatus","err","Please choose a PDF.");return;}cFile=f;$("cName").textContent=f.name+"  ("+Math.round(f.size/1024)+" KB)";$("scanBtn").disabled=false;$("cResult").style.display="none";st("cStatus","info","Ready. Click Scan.");}
$("scanBtn").onclick=async()=>{
  if(!cFile)return; $("scanBtn").disabled=true; st("cStatus","info","Scanning for duplicates...",true);
  try{
    const fd=new FormData(); fd.append("pdf",cFile);
    const res=await fetch("/cleaner/scan",{method:"POST",body:fd}); const d=await res.json();
    if(!res.ok) throw new Error(d.error||("Error "+res.status));
    cTotal=d.total_pages; cGroups=d.groups; renderDups(); hide("cStatus"); $("cResult").style.display="block"; $("cResult").scrollIntoView({behavior:"smooth"});
  }catch(err){ st("cStatus","err",err.message||String(err)); } finally{ $("scanBtn").disabled=false; }
};
function renderDups(){
  const z=$("dupZone");
  if(!cGroups.length){ z.innerHTML='<div class="dupwarn show" style="background:var(--okbg);color:var(--ok)">No duplicate pages found across '+cTotal+' pages. You can still compress and export below.</div>'; return; }
  let dupCount=0; cGroups.forEach(g=>dupCount+=g.pages.length-1);
  let html='<div class="dupwarn show">Found '+dupCount+' duplicate page'+(dupCount===1?'':'s')+' in '+cGroups.length+' group'+(cGroups.length===1?'':'s')+'. Ticked copies will be removed.</div>';
  cGroups.forEach((g,gi)=>{
    html+='<div class="dupgrp"><div class="row">';
    g.pages.forEach((pn,pi)=>{
      const thumb=g.thumbs[pi]?'<img class="thumbsm" src="data:image/jpeg;base64,'+g.thumbs[pi]+'">':'';
      if(pi===0){ html+='<div class="item keep">'+thumb+'<div><b>KEEP</b><br>page '+pn+'</div></div>'; }
      else{ html+='<div class="item drop"><label>'+thumb+'<div><b>REMOVE</b><br>page '+pn+' <input type="checkbox" class="chk rm" data-p="'+pn+'" checked></div></label></div>'; }
    });
    html+='</div></div>';
  });
  z.innerHTML=html;
}
$("exportBtn").onclick=async()=>{
  const remove=[...document.querySelectorAll(".rm:checked")].map(c=>+c.dataset.p);
  const compress=$("compressChk").checked;
  if(!remove.length && !compress){ st("cStatus2","err","Nothing to do - no duplicates ticked and compress is off."); return; }
  st("cStatus2","info","Building cleaned PDF...",true);
  try{
    const fd=new FormData(); fd.append("pdf",cFile); fd.append("remove",JSON.stringify(remove)); fd.append("compress",compress?"1":"0");
    const res=await fetch("/cleaner/export",{method:"POST",body:fd});
    if(!res.ok){ const e=await res.json().catch(()=>({})); throw new Error(e.error||("Error "+res.status)); }
    const blob=await res.blob();
    const a=document.createElement("a"); const base=cFile.name.replace(/\.pdf$/i,"");
    a.href=URL.createObjectURL(blob); a.download=base+"_cleaned.pdf"; a.click(); URL.revokeObjectURL(a.href);
    const kb=Math.round(blob.size/1024), okb=Math.round(cFile.size/1024);
    st("cStatus2","ok","Done - removed "+remove.length+" page(s). New size "+kb+" KB (was "+okb+" KB).");
  }catch(err){ st("cStatus2","err",err.message||String(err)); }
};
</script>"""


# ---------------------------------------------------------------- combiner page
COMBINER_BODY = r"""
<div class="lead">
  <h2>PDF Combiner</h2>
  <p>Add your files in order - Bill of Lading, then Consolidation List, then Invoices - and merge them into one PDF. Export the Excel consolidation list to PDF first (in Excel: File &rsaquo; Save As &rsaquo; PDF), then add it here.</p>
</div>
<div class="card">
  <h3>1. Add files in order</h3>
  <p class="sub">Recommended order: Bill of Lading &rarr; Consolidation List (as PDF) &rarr; Invoices. Use the arrows to reorder.</p>
  <div class="drop" id="mDrop"><p style="margin:0 0 6px"><strong>Drop PDFs here</strong> or click to browse (you can add several)</p><p style="margin:0;font-size:13px">They stack in the order you add them.</p></div>
  <input type="file" id="mInput" accept="application/pdf" multiple style="display:none">
  <ul class="olist" id="oList"></ul>
</div>
<div class="card">
  <h3>2. Name the combined file</h3>
  <p class="sub">Auto-detect reads the BL # and customer from your files; check and fix before combining. Final name is ALL CAPS.</p>
  <div class="toolbar"><button class="btn-ghost" id="detectBtn">Auto-detect BL # &amp; customer</button><span class="hint" id="detectHint"></span></div>
  <div style="display:flex;gap:14px;flex-wrap:wrap">
    <div style="flex:1;min-width:160px"><label class="fld" for="blNum">Bill of Lading #</label><input class="txt" id="blNum" placeholder="43546-26"></div>
    <div style="flex:2;min-width:220px"><label class="fld" for="custName">Customer</label><input class="txt" id="custName" placeholder="A &amp; A WHOLESALE"></div>
  </div>
  <p class="sub" style="margin-top:12px">File name preview: <b id="namePrev" class="mono">BL ____ ____</b></p>
  <div class="toolbar"><div class="spacer"></div><button class="btn" id="combineBtn">Combine &amp; download PDF</button></div>
  <div class="status" id="mStatus"></div>
</div>
"""

COMBINER_JS = r"""<script>
"use strict";
const $=id=>document.getElementById(id);
let files=[];
function st(id,k,m,sp){const e=$(id);e.className="status show "+k;e.innerHTML=(sp?'<span class="spin"></span>':'')+m;}
function hide(id){$(id).className="status";}
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));}
const mDrop=$("mDrop");
mDrop.onclick=()=>$("mInput").click();
["dragover","dragenter"].forEach(e=>mDrop.addEventListener(e,ev=>{ev.preventDefault();mDrop.classList.add("drag");}));
["dragleave","drop"].forEach(e=>mDrop.addEventListener(e,ev=>{ev.preventDefault();mDrop.classList.remove("drag");}));
mDrop.addEventListener("drop",ev=>{addFiles(ev.dataTransfer.files);});
$("mInput").onchange=e=>addFiles(e.target.files);
function addFiles(list){ [...list].forEach(f=>{ if(/\.pdf$/i.test(f.name)||/pdf$/i.test(f.type)) files.push(f); }); renderList(); }
function renderList(){
  const ul=$("oList"); ul.innerHTML="";
  files.forEach((f,i)=>{
    const li=document.createElement("li"); li.className="orow";
    li.innerHTML='<span class="idx">'+(i+1)+'</span><span class="fn">'+esc(f.name)+'</span>'+
      '<span class="sz">'+Math.round(f.size/1024)+' KB</span>'+
      '<button class="btn-ghost" data-up="'+i+'">&#8593;</button>'+
      '<button class="btn-ghost" data-dn="'+i+'">&#8595;</button>'+
      '<button class="btn-ghost" data-rm="'+i+'">&times;</button>';
    ul.appendChild(li);
  });
  ul.querySelectorAll("[data-up]").forEach(b=>b.onclick=e=>{const i=+e.target.dataset.up;if(i>0){[files[i-1],files[i]]=[files[i],files[i-1]];renderList();}});
  ul.querySelectorAll("[data-dn]").forEach(b=>b.onclick=e=>{const i=+e.target.dataset.dn;if(i<files.length-1){[files[i+1],files[i]]=[files[i],files[i+1]];renderList();}});
  ul.querySelectorAll("[data-rm]").forEach(b=>b.onclick=e=>{files.splice(+e.target.dataset.rm,1);renderList();});
}
function finalName(){ const bl=($("blNum").value||"").trim(); const c=($("custName").value||"").trim(); return ("BL "+(bl||"____")+" "+(c||"____")).toUpperCase(); }
function updatePrev(){ $("namePrev").textContent=finalName(); }
$("blNum").oninput=updatePrev; $("custName").oninput=updatePrev; updatePrev();
$("detectBtn").onclick=async()=>{
  if(!files.length){ st("mStatus","err","Add your files first."); return; }
  $("detectHint").textContent="detecting..."; 
  try{
    const fd=new FormData(); files.forEach(f=>fd.append("files",f));
    const res=await fetch("/combiner/name",{method:"POST",body:fd}); const d=await res.json();
    if(!res.ok) throw new Error(d.error||("Error "+res.status));
    if(d.bl) $("blNum").value=d.bl; if(d.customer) $("custName").value=d.customer; updatePrev();
    $("detectHint").textContent=(d.bl||d.customer)?"filled in - please double-check":"couldn't read it - type it in";
  }catch(err){ $("detectHint").textContent=""; st("mStatus","err",err.message||String(err)); }
};
$("combineBtn").onclick=async()=>{
  if(files.length<2){ st("mStatus","err","Add at least two files to combine."); return; }
  st("mStatus","info","Combining...",true);
  try{
    const fd=new FormData(); files.forEach(f=>fd.append("files",f)); fd.append("filename",finalName());
    const res=await fetch("/combiner/merge",{method:"POST",body:fd});
    if(!res.ok){ const e=await res.json().catch(()=>({})); throw new Error(e.error||("Error "+res.status)); }
    const blob=await res.blob(); const a=document.createElement("a"); a.href=URL.createObjectURL(blob); a.download=finalName()+".pdf"; a.click(); URL.revokeObjectURL(a.href);
    st("mStatus","ok","Done - combined "+files.length+" files into "+finalName()+".pdf");
  }catch(err){ st("mStatus","err",err.message||String(err)); }
};
</script>"""


# ================================================================ routes
@app.route("/")
def hub():
    return Response(render_page("splitter"), mimetype="text/html")


@app.route("/splitter")
def splitter_page():
    return Response(render_page("splitter"), mimetype="text/html")


@app.route("/cleaner")
def cleaner_page():
    return Response(render_page("cleaner"), mimetype="text/html")


@app.route("/combiner")
def combiner_page():
    return Response(render_page("combiner"), mimetype="text/html")


@app.route("/logo.png")
def logo_icon():
    return send_from_directory(HERE, "logo.png")


@app.route("/logo-wordmark.png")
def logo_wordmark():
    return send_from_directory(HERE, "logo-wordmark.png")


@app.route("/health")
def health():
    return "ok"


# ------- WR splitter backend -------
@app.route("/analyze", methods=["POST"])
def analyze():
    f = request.files.get("pdf")
    if not f:
        return jsonify(error="No PDF uploaded."), 400
    data = f.read()
    if len(data) > MAX_MB * 1024 * 1024:
        return jsonify(error="PDF is larger than %d MB." % MAX_MB), 400
    try:
        pages = render_pages(data)
        invoices = analyze_pages(pages)
        if not invoices:
            return jsonify(error="No invoices detected."), 422
        thumbs = {p["num"]: p["thumb"] for p in pages}
        for inv in invoices:
            inv["pages"] = [int(x) for x in inv.get("pages", [])]
            inv["wr"] = inv.get("wr", []) or []
            first = inv["pages"][0] if inv["pages"] else None
            inv["thumb"] = thumbs.get(first, "")
        return jsonify(total_pages=len(pages), invoices=invoices)
    except Exception as e:  # noqa
        return jsonify(error=str(e)), 500


@app.route("/split", methods=["POST"])
def split():
    f = request.files.get("pdf")
    mapping_raw = request.form.get("mapping")
    if not f or not mapping_raw:
        return jsonify(error="Missing PDF or mapping."), 400
    try:
        mapping = json.loads(mapping_raw)
    except Exception:
        return jsonify(error="Mapping is not valid JSON."), 400
    try:
        exclude = set(int(x) for x in json.loads(request.form.get("exclude") or "[]"))
    except Exception:
        exclude = set()
    data = f.read()
    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    seen = {}
    for entry in mapping:
        for p in entry.get("pages", []):
            seen[p] = seen.get(p, 0) + 1
    missing = [p for p in range(1, total + 1) if p not in seen and p not in exclude]
    dupes = [p for p, c in seen.items() if c > 1]
    extra = [p for p in seen if p < 1 or p > total]
    both = sorted(p for p in seen if p in exclude)
    if missing or dupes or extra or both:
        msg = []
        if missing: msg.append("pages not assigned: %s" % missing)
        if dupes: msg.append("pages used more than once: %s" % dupes)
        if extra: msg.append("pages out of range: %s" % extra)
        if both: msg.append("pages both assigned and excluded: %s" % both)
        return jsonify(error="; ".join(msg)), 400
    names = [e["filename"] for e in mapping]
    if len(set(names)) != len(names):
        return jsonify(error="Filenames must be unique."), 400
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        for entry in mapping:
            writer = PdfWriter()
            for p in entry["pages"]:
                writer.add_page(reader.pages[p - 1])
            out = io.BytesIO()
            writer.write(out)
            z.writestr(entry["filename"], out.getvalue())
    mem.seek(0)
    return send_file(mem, mimetype="application/zip", as_attachment=True, download_name="WR_invoices.zip")


# ------- PDF cleaner backend -------
@app.route("/cleaner/scan", methods=["POST"])
def cleaner_scan():
    f = request.files.get("pdf")
    if not f:
        return jsonify(error="No PDF uploaded."), 400
    data = f.read()
    if len(data) > MAX_MB * 1024 * 1024:
        return jsonify(error="PDF is larger than %d MB." % MAX_MB), 400
    try:
        doc, total, dup_groups = find_duplicate_groups(data)
        groups = []
        for g in dup_groups:
            pages = [info["num"] for info in g]
            thumbs = [_thumb(info["page"], width=150) for info in g]
            groups.append({"pages": pages, "thumbs": thumbs})
        return jsonify(total_pages=total, groups=groups)
    except Exception as e:  # noqa
        return jsonify(error=str(e)), 500


@app.route("/cleaner/export", methods=["POST"])
def cleaner_export():
    f = request.files.get("pdf")
    if not f:
        return jsonify(error="No PDF uploaded."), 400
    try:
        remove = set(int(x) for x in json.loads(request.form.get("remove") or "[]"))
    except Exception:
        remove = set()
    compress = request.form.get("compress") == "1"
    data = f.read()
    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    keep = [p for p in range(1, total + 1) if p not in remove]
    if not keep:
        return jsonify(error="That would remove every page."), 400
    try:
        if compress:
            out_bytes = compress_pages(data, keep)
        else:
            writer = PdfWriter()
            for p in keep:
                writer.add_page(reader.pages[p - 1])
            buf = io.BytesIO()
            writer.write(buf)
            out_bytes = buf.getvalue()
        return send_file(io.BytesIO(out_bytes), mimetype="application/pdf",
                         as_attachment=True, download_name="cleaned.pdf")
    except Exception as e:  # noqa
        return jsonify(error=str(e)), 500


# ------- PDF combiner backend -------
@app.route("/combiner/name", methods=["POST"])
def combiner_name():
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files uploaded."), 400
    pairs = [(f.filename, f.read()) for f in files]
    res = detect_bl_customer(pairs)
    return jsonify(res)


@app.route("/combiner/merge", methods=["POST"])
def combiner_merge():
    files = request.files.getlist("files")
    if len(files) < 2:
        return jsonify(error="Add at least two files."), 400
    filename = _safe_filename(request.form.get("filename") or "COMBINED")
    writer = PdfWriter()
    try:
        for f in files:
            r = PdfReader(io.BytesIO(f.read()))
            for pg in r.pages:
                writer.add_page(pg)
        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)
        return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=filename)
    except Exception as e:  # noqa
        return jsonify(error=str(e)), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)

"""MCP Apps UI for the remote Notion MCP.

Notion's hosted MCP ships an MCP App (``ui://notion/page``), but it embeds
``app.notion.com`` in an iframe that Notion only allows from claude.ai /
chatgpt sandboxes (``frame-ancestors``), and it is not linked from any tool's
``_meta.ui``. So agentic-work renders its own viewer for Notion tool results and
attaches it to the read-only tools below via ``mcp_apps_host.register_tool_app``.
"""
from __future__ import annotations

import logging

import mcp_apps_host

logger = logging.getLogger("notion_app")

SERVER_NAME = "notion-remote"
VIEW_URI = "ui://agentic-work/notion-viewer.html"

# Tools whose results the viewer can render (all read-only).
APP_TOOLS = (
    "notion-search",
    "notion-fetch",
    "notion-list-recent-pages",
    "notion-list-favorite-pages",
    "notion-list-private-pages",
    "notion-list-shared-pages",
)

# Tools the iframe may call back through the host (/api/mcp-apps/call-tool).
# Keep this read-only: the iframe is untrusted page content.
IFRAME_CALLABLE_TOOLS = frozenset(APP_TOOLS)

NOTION_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Notion</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff; --fg: #191918; --muted: #787774; --line: #e7e6e3;
    --card: #ffffff; --card-hover: #f7f6f3; --accent: #2383e2; --chip: #f1f0ee;
    font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo", sans-serif;
  }
  :root[data-theme="dark"] {
    --bg: #1f1f1f; --fg: #f1f0ee; --muted: #9b9a97; --line: #373737;
    --card: #252525; --card-hover: #2f2f2f; --accent: #529cca; --chip: #2f2f2f;
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 14px 16px 16px; background: var(--bg); color: var(--fg); font-size: 14px; }
  header { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
  .brand { font-weight: 700; font-size: 15px; margin-right: 4px; }
  form { display: flex; gap: 6px; flex: 1; min-width: 180px; }
  input[type=search] {
    flex: 1; min-width: 0; padding: 7px 10px; border: 1px solid var(--line); border-radius: 8px;
    background: var(--card); color: var(--fg); font: inherit;
  }
  button {
    padding: 7px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--card);
    color: var(--fg); font: inherit; cursor: pointer;
  }
  button:hover:not(:disabled) { background: var(--card-hover); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button:disabled { opacity: .55; cursor: default; }
  .section { color: var(--muted); font-size: 12px; margin: 4px 0 8px; display: flex; gap: 8px; align-items: center; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
  .card {
    text-align: left; padding: 12px; border: 1px solid var(--line); border-radius: 10px;
    background: var(--card); display: flex; flex-direction: column; gap: 6px; min-height: 92px;
  }
  .card .icon { font-size: 22px; line-height: 1; }
  .card .title { font-weight: 600; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; word-break: break-word; }
  .card .meta { color: var(--muted); font-size: 12px; margin-top: auto; }
  .row {
    display: block; width: 100%; text-align: left; padding: 10px 12px; margin-bottom: 8px;
    border: 1px solid var(--line); border-radius: 10px; background: var(--card);
  }
  .row .title { font-weight: 600; word-break: break-word; }
  .row .hl { color: var(--muted); font-size: 12.5px; margin-top: 4px; word-break: break-word; }
  .chip { display: inline-block; padding: 1px 7px; border-radius: 999px; background: var(--chip); font-size: 11px; color: var(--muted); margin-left: 6px; font-weight: 400; }
  .page h1 { font-size: 22px; margin: 6px 0 4px; word-break: break-word; }
  .page .sub { color: var(--muted); font-size: 12px; margin-bottom: 10px; }
  .page pre {
    white-space: pre-wrap; word-break: break-word; margin: 0; padding: 12px; border: 1px solid var(--line);
    border-radius: 10px; background: var(--card); font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
    max-height: 420px; overflow: auto;
  }
  .empty, .error { padding: 18px 8px; color: var(--muted); }
  .error { color: #e03e3e; }
  .spinner { opacity: .6; }
</style>
</head>
<body>
<header>
  <span class="brand">Notion</span>
  <form id="searchForm">
    <input id="q" type="search" placeholder="페이지 검색" autocomplete="off">
    <button class="primary" type="submit">검색</button>
  </form>
  <button id="recentBtn" type="button">최근 페이지</button>
</header>
<main id="root"><div class="empty spinner">불러오는 중…</div></main>

<script>
(function () {
  "use strict";
  var root = document.getElementById("root");
  var qInput = document.getElementById("q");
  var nextId = 1;
  var pending = {};
  var listState = null; // last list view, for the Back button
  var fetchAvailable = true;

  function post(msg) { parent.postMessage(msg, "*"); }
  function request(method, params) {
    var id = nextId++;
    return new Promise(function (resolve, reject) {
      pending[id] = { resolve: resolve, reject: reject };
      post({ jsonrpc: "2.0", id: id, method: method, params: params });
      setTimeout(function () {
        if (pending[id]) { delete pending[id]; reject(new Error("host response timeout")); }
      }, 60000);
    });
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function safeUrl(u) { return typeof u === "string" && /^https:\/\//i.test(u) ? u : ""; }
  function iconOf(icon) {
    if (!icon) return "📄";
    if (typeof icon === "string") return /^https?:/i.test(icon) ? "📄" : icon;
    if (typeof icon === "object" && icon.emoji) return icon.emoji;
    return "📄";
  }
  function fmtDate(s) {
    if (!s) return "";
    var d = new Date(s);
    return isNaN(d.getTime()) ? "" : d.toLocaleDateString("ko-KR", { month: "short", day: "numeric" });
  }
  function reportSize() {
    var h = Math.ceil(document.documentElement.getBoundingClientRect().height);
    post({ jsonrpc: "2.0", method: "ui/notifications/size-changed",
           params: { height: Math.max(h, 140), width: window.innerWidth } });
  }
  function render(html) { root.innerHTML = html; reportSize(); }

  function parseResult(result) {
    if (!result) return null;
    var blocks = result.content || [];
    for (var i = 0; i < blocks.length; i++) {
      var t = blocks[i] && blocks[i].text;
      if (typeof t !== "string") continue;
      try { return JSON.parse(t); } catch (e) { return { _text: t }; }
    }
    return null;
  }

  function openLink(url) {
    var u = safeUrl(url);
    if (u) request("ui/open-link", { url: u }).catch(function () {});
  }

  function renderList(data, heading) {
    var items = (data && data.results) || [];
    listState = { data: data, heading: heading };
    if (!items.length) { render('<div class="empty">표시할 페이지가 없습니다.</div>'); return; }
    var hasHighlight = items.some(function (r) { return r && r.highlight; });
    var html = '<div class="section">' + esc(heading) + " · " + items.length + "개</div>";
    if (hasHighlight) {
      items.forEach(function (r, i) {
        html += '<button type="button" class="row" data-i="' + i + '">' +
          '<div class="title">' + esc(iconOf(r.icon)) + " " + esc(r.title || "제목 없음") +
          (r.type ? '<span class="chip">' + esc(r.type) + "</span>" : "") + "</div>" +
          (r.highlight ? '<div class="hl">' + esc(String(r.highlight).slice(0, 160)) + "</div>" : "") +
          "</button>";
      });
    } else {
      html += '<div class="grid">';
      items.forEach(function (r, i) {
        html += '<button type="button" class="card" data-i="' + i + '">' +
          '<span class="icon">' + esc(iconOf(r.icon)) + "</span>" +
          '<span class="title">' + esc(r.title || "제목 없음") + "</span>" +
          '<span class="meta">' + esc(r.type || "") + (r.timestamp ? " · " + esc(fmtDate(r.timestamp)) : "") + "</span>" +
          "</button>";
      });
      html += "</div>";
    }
    render(html);
    Array.prototype.forEach.call(root.querySelectorAll("[data-i]"), function (el) {
      el.addEventListener("click", function () {
        var r = items[Number(el.getAttribute("data-i"))];
        if (r) showPage(r);
      });
    });
  }

  function renderPage(data) {
    var url = safeUrl(data.url);
    var text = typeof data.text === "string" ? data.text : (data._text || "");
    var clipped = text.length > 8000;
    var html = '<div class="page">' +
      '<div style="display:flex;gap:8px;margin-bottom:6px">' +
      (listState ? '<button type="button" id="backBtn">← 목록</button>' : "") +
      (url ? '<button type="button" id="openBtn" class="primary">Notion에서 열기</button>' : "") +
      "</div>" +
      "<h1>" + esc(iconOf(data.icon)) + " " + esc(data.title || "제목 없음") + "</h1>" +
      '<div class="sub">' + (data.page_last_edited_at ? "수정 " + esc(fmtDate(data.page_last_edited_at)) : "") + "</div>" +
      "<pre>" + esc(clipped ? text.slice(0, 8000) : text) + "</pre>" +
      (clipped ? '<div class="sub">본문이 길어 앞부분만 표시합니다.</div>' : "") +
      "</div>";
    render(html);
    var back = document.getElementById("backBtn");
    if (back) back.addEventListener("click", function () { if (listState) renderList(listState.data, listState.heading); });
    var open = document.getElementById("openBtn");
    if (open) open.addEventListener("click", function () { openLink(data.url); });
  }

  function callTool(name, args) {
    render('<div class="empty spinner">불러오는 중…</div>');
    return request("tools/call", { name: name, arguments: args || {} }).then(function (res) {
      if (res && res.isError) throw new Error("Notion 도구 오류");
      return parseResult(res);
    });
  }

  function showPage(item) {
    var id = item.url || item.id;
    if (!id) return;
    callTool("notion-fetch", { id: id }).then(function (data) {
      if (data) renderPage(data);
    }).catch(showError);
  }
  function showError(err) {
    render('<div class="error">' + esc(err && err.message ? err.message : err) + "</div>");
  }

  document.getElementById("searchForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var q = qInput.value.trim();
    if (!q) return;
    callTool("notion-search", { query: q }).then(function (data) {
      renderList(data || {}, "검색: " + q);
    }).catch(showError);
  });
  document.getElementById("recentBtn").addEventListener("click", function () {
    callTool("notion-list-recent-pages", {}).then(function (data) {
      renderList(data || {}, "최근 페이지");
    }).catch(showError);
  });

  function applyResult(result) {
    var data = parseResult(result);
    if (!data) { render('<div class="empty">결과가 없습니다.</div>'); return; }
    if (data.results) { renderList(data, data.type ? "검색 결과" : "페이지"); return; }
    if (data.title || data.text) { renderPage(data); return; }
    render('<pre style="white-space:pre-wrap">' + esc(data._text || JSON.stringify(data, null, 2)).slice(0, 4000) + "</pre>");
  }
  window.applyResult = applyResult;

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.jsonrpc !== "2.0") return;
    if (data.method === "ui/notifications/tool-result") { applyResult(data.params || {}); return; }
    if (data.method === "ui/notifications/host-context-changed") { setTheme(data.params); return; }
    if (data.id !== undefined && !data.method && pending[data.id]) {
      var p = pending[data.id]; delete pending[data.id];
      if (data.error) p.reject(new Error(data.error.message || "host error"));
      else p.resolve(data.result);
    }
  });

  function setTheme(ctx) {
    var theme = ctx && ctx.theme;
    if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
  }

  new ResizeObserver(reportSize).observe(document.body);
  request("ui/initialize", {
    appInfo: { name: "notion-viewer", version: "1.0.0" },
    appCapabilities: {},
    protocolVersion: "2026-01-26"
  }).then(function (res) {
    setTheme(res && res.hostContext);
    post({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
    reportSize();
  }).catch(function () {});
})();
</script>
</body>
</html>
"""


def register_notion_apps() -> int:
    """Attach the viewer to the Notion read tools; returns how many were registered."""
    for tool_name in APP_TOOLS:
        mcp_apps_host.register_tool_app(
            tool_name,
            resource_uri=VIEW_URI,
            html=NOTION_HTML,
            mcp_server=SERVER_NAME,
            csp={},
        )
    return len(APP_TOOLS)

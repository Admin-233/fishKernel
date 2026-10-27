"""网页控制台的静态页面（内嵌，零依赖）。

页面本身不含任何业务逻辑：只通过 /api/snapshot 读、往 /api/say 和 /api/ctl 写。
"""

from __future__ import annotations

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>fishKernel 控制台</title>
<style>
  :root {
    --bg: #14161a; --panel: #1b1e24; --line: #2a2f38; --fg: #d8dee9;
    --dim: #7d8695; --user: #6fd3f7; --assistant: #a3e08b; --call: #f2c46b;
    --tool: #7fd1c1; --fail: #ff6b6b; --sys: #8b93a1; --compact: #c792ea;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--fg); display: flex; flex-direction: column;
    font: 13px/1.55 "JetBrains Mono", "Cascadia Code", Consolas, Menlo, monospace;
  }
  header {
    border-bottom: 1px solid var(--line); padding: 8px 12px; background: var(--panel);
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  }
  header h1 { font-size: 13px; margin: 0 8px 0 0; font-weight: 600; letter-spacing: .5px; }
  .chip { color: var(--dim); }
  .chip b { color: var(--fg); font-weight: 500; }
  .spacer { flex: 1; }
  button {
    background: #232830; color: var(--fg); border: 1px solid var(--line);
    border-radius: 4px; padding: 3px 9px; cursor: pointer; font: inherit;
  }
  button:hover { background: #2c323c; }
  button.danger:hover { border-color: var(--fail); color: var(--fail); }
  main { flex: 1; display: flex; min-height: 0; }
  #events { flex: 1; overflow-y: auto; padding: 10px 12px; }
  .ev { margin: 0 0 6px; white-space: pre-wrap; word-break: break-word; }
  .ev .t { color: var(--dim); margin-right: 6px; font-size: 11px; }
  .ev.user { color: var(--user); }
  .ev.assistant { color: var(--assistant); }
  .ev.call { color: var(--call); }
  .ev.tool { color: var(--tool); }
  .ev.fail { color: var(--fail); }
  .ev.system { color: var(--sys); }
  .ev.compact { color: var(--compact); }
  aside {
    width: 320px; border-left: 1px solid var(--line); background: var(--panel);
    overflow-y: auto; padding: 10px; flex-shrink: 0;
  }
  aside h2 { font-size: 11px; text-transform: uppercase; color: var(--dim); margin: 0 0 6px; }
  .job { border: 1px solid var(--line); border-radius: 4px; padding: 5px 7px; margin-bottom: 6px; }
  .job .cmd { color: var(--dim); word-break: break-all; }
  .job .id { color: var(--call); }
  .job.running { border-color: #3a4a5a; }
  footer {
    border-top: 1px solid var(--line); padding: 8px 12px; background: var(--panel);
    display: flex; gap: 8px;
  }
  footer input {
    flex: 1; background: #101216; border: 1px solid var(--line); border-radius: 4px;
    color: var(--fg); padding: 6px 9px; font: inherit;
  }
  footer input:focus { outline: none; border-color: #3d6f8f; }
  #toast {
    position: fixed; right: 14px; bottom: 60px; background: #2a313b; border: 1px solid var(--line);
    border-radius: 4px; padding: 6px 10px; opacity: 0; transition: opacity .2s; pointer-events: none;
  }
  #toast.show { opacity: 1; }
</style>
</head>
<body>
<header>
  <h1>fishKernel</h1>
  <span class="chip">内核 <b id="k-status">-</b></span>
  <span class="chip">轮次 <b id="k-turn">-</b></span>
  <span class="chip">控制 <b id="k-run">-</b></span>
  <span class="chip">事件 <b id="k-events">-</b></span>
  <span class="chip">花费 <b id="k-cost">-</b></span>
  <span class="spacer"></span>
  <button id="b-pause">暂停</button>
  <button id="b-resume">继续</button>
  <button id="b-wake">唤醒</button>
  <button id="b-restart">交班重启</button>
  <button id="b-stop" class="danger">停止</button>
</header>
<main>
  <div id="events"></div>
  <aside>
    <h2>异步任务</h2>
    <div id="jobs"></div>
  </aside>
</main>
<footer>
  <input id="msg" placeholder="给 Agent 发一条消息（回车发送）" autocomplete="off">
  <button id="b-say">发送</button>
</footer>
<div id="toast"></div>
<script>
(function () {
  "use strict";
  var after = 0;
  var first = true;
  var evBox = document.getElementById("events");
  var jobBox = document.getElementById("jobs");

  function toast(text) {
    var t = document.getElementById("toast");
    t.textContent = text;
    t.classList.add("show");
    setTimeout(function () { t.classList.remove("show"); }, 1200);
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function fmtTime(ts) {
    if (!ts) return "";
    var m = /T(\d\d:\d\d:\d\d)/.exec(ts);
    return m ? m[1] : ts;
  }

  function appendEvent(ev) {
    if (!ev || !ev.text) return;
    var atBottom = evBox.scrollTop + evBox.clientHeight >= evBox.scrollHeight - 40;
    var div = document.createElement("div");
    div.className = "ev " + (ev.cls || "");
    div.innerHTML = '<span class="t">' + esc(fmtTime(ev.ts)) + "</span>" + esc(ev.text);
    evBox.appendChild(div);
    if (atBottom || first) evBox.scrollTop = evBox.scrollHeight;
  }

  function renderJobs(jobs) {
    if (!jobs.length) { jobBox.innerHTML = '<div class="chip">（无）</div>'; return; }
    jobBox.innerHTML = jobs.slice().reverse().map(function (j) {
      var status = j.status || "?";
      var cls = status === "running" ? "job running" : "job";
      var exit = (j.exit_code === null || j.exit_code === undefined) ? "-" : j.exit_code;
      return '<div class="' + cls + '">' +
        '<div><span class="id">' + esc(j.id) + "</span> " + esc(j.label || "") + "</div>" +
        '<div class="cmd">' + esc(j.command || "") + "</div>" +
        '<div class="chip">' + esc(status) + " · exit " + esc(exit) + " · " + esc(j.stdout_size || 0) + " B</div>" +
        "</div>";
    }).join("");
  }

  function applyState(s) {
    if (!s) return;
    document.getElementById("k-status").textContent = (s.kernel && s.kernel.status) || "-";
    document.getElementById("k-turn").textContent = (s.kernel && s.kernel.turn) || 0;
    document.getElementById("k-run").textContent = (s.control && s.control.run) || "-";
    document.getElementById("k-events").textContent = s.events || 0;
    var u = s.usage || {};
    document.getElementById("k-cost").textContent =
      (u.calls || 0) + " 次 / ¥" + (u.cost_yuan === undefined ? "-" : u.cost_yuan);
    document.title = (s.name || "fishKernel") + " · " + ((s.kernel && s.kernel.status) || "");
  }

  function poll() {
    var url = "/api/snapshot?after=" + after + (first ? "&tail=200" : "");
    fetch(url, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "bad response");
        applyState(data.state);
        (data.events || []).forEach(appendEvent);
        if (typeof data.last_seq === "number") after = data.last_seq;
        renderJobs(data.jobs || []);
        first = false;
      })
      .catch(function (err) {
        document.getElementById("k-status").textContent = "离线";
      })
      .then(function () { setTimeout(poll, 1000); });
  }

  function ctl(action, value) {
    fetch("/api/ctl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action, value: value })
    }).then(function (r) { return r.json(); }).then(function (d) {
      toast(d.ok ? ("已执行：" + action) : ("失败：" + (d.error || action)));
      first = true; after = 0; evBox.innerHTML = "";
      setTimeout(poll, 150);
    });
  }

  function say() {
    var input = document.getElementById("msg");
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    fetch("/api/say", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text })
    }).then(function (r) { return r.json(); }).then(function (d) {
      toast(d.ok ? "已投递" : ("失败：" + (d.error || "")));
    });
  }

  document.getElementById("b-pause").onclick = function () { ctl("pause"); };
  document.getElementById("b-resume").onclick = function () { ctl("resume"); };
  document.getElementById("b-wake").onclick = function () { ctl("wake"); };
  document.getElementById("b-stop").onclick = function () { ctl("stop"); };
  document.getElementById("b-restart").onclick = function () {
    if (confirm("请求交班重启？内核会启动新进程后退出。")) ctl("restart");
  };
  document.getElementById("b-say").onclick = say;
  document.getElementById("msg").addEventListener("keydown", function (e) {
    if (e.key === "Enter") say();
  });

  poll();
})();
</script>
</body>
</html>
"""

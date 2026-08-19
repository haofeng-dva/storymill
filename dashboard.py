# -*- coding: utf-8 -*-
"""
dashboard.py — 生产线看板（Web 前端 + 人为介入）

用 Python 内置 http.server 起一个本地看板，展示：
  今日产量 / 待采纳推荐 / 产物清单 / 开关状态
支持人为介入：采纳/跳过推荐、切换开关、触发生产、生成报告。

用法:
    py -3 dashboard.py            # 启动看板，浏览器打开 http://localhost:8900
"""
import http.server
import json
import os
import re
import subprocess
import threading
from urllib.parse import urlparse, parse_qs

from state_store import StateStore, today
import progress

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8900
CFG_PATH = os.path.join(HERE, "orchestrator.json")
KEYS_PATH = os.path.join(HERE, "keys.local.json")


def get_keys_status():
    """检查使用者是否已配置 key（keys.local.json，不进 git）。"""
    if not os.path.exists(KEYS_PATH):
        return {"configured": False, "has_gpt": False, "has_gemini": False}
    try:
        d = json.load(open(KEYS_PATH, encoding="utf-8"))
        gpt = bool(d.get("OPENAI_RELAY_KEY"))
        gem = bool(d.get("GEMINI_API_KEY"))
        return {"configured": gpt or gem, "has_gpt": gpt, "has_gemini": gem}
    except Exception:
        return {"configured": False, "has_gpt": False, "has_gemini": False}


def save_keys(gpt_key, gemini_key, proxy):
    """保存使用者自己的 key 到 keys.local.json（仅本地，绝不入库/上传）。"""
    try:
        d = {}
        if os.path.exists(KEYS_PATH):
            try:
                d = json.load(open(KEYS_PATH, encoding="utf-8"))
            except Exception:
                d = {}
        if gpt_key:
            d["OPENAI_RELAY_KEY"] = gpt_key.strip()
        if gemini_key:
            d["GEMINI_API_KEY"] = gemini_key.strip()
        if proxy:
            d["GEMINI_PROXY"] = proxy.strip()
        elif "GEMINI_PROXY" not in d:
            d["GEMINI_PROXY"] = "http://127.0.0.1:7897"
        json.dump(d, open(KEYS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return {"ok": True, "msg": "key 已保存（仅存本机，不会上传）"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def load_cfg():
    return json.load(open(CFG_PATH, encoding="utf-8"))


def save_cfg(cfg):
    json.dump(cfg, open(CFG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def get_status():
    store = StateStore()
    cfg = load_cfg()
    m = store.metrics_today()

    # 产物清单（out/ 下的 story）
    outputs = []
    out_root = os.path.join(HERE, "out")
    if os.path.isdir(out_root):
        for sid in sorted(os.listdir(out_root)):
            d = os.path.join(out_root, sid)
            if not os.path.isdir(d) or sid == "reports":
                continue
            epub = [f for f in os.listdir(d) if f.endswith(".epub")]
            has_manifest = os.path.exists(os.path.join(d, "publish_manifest.json"))
            outputs.append({"storyId": sid, "epub": epub, "manifest": has_manifest})

    # 已生成的故事（shorts/）
    stories = []
    shorts_root = os.path.join(HERE, "shorts")
    if os.path.isdir(shorts_root):
        for sid in sorted(os.listdir(shorts_root)):
            d = os.path.join(shorts_root, sid)
            if os.path.isdir(d):
                chapters = len([f for f in os.listdir(d) if re.match(r"ch\d+\.md$", f)])
                stories.append({"storyId": sid, "chapters": chapters})

    # 方向文件
    directions = []
    dir_root = os.path.join(HERE, "directions")
    if os.path.isdir(dir_root):
        directions = sorted(os.listdir(dir_root))

    status = {
        "metrics": m,
        "limits": cfg["limits"],
        "switches": cfg["switches"],
        "quality": cfg.get("quality", {}),
        "recommendations": store.list_new_recommendations(),
        "outputs": outputs,
        "stories": stories,
        "directions": directions,
        "progress": progress.read(),
    }
    store.close()
    return status


def save_thresholds(qmin, native, engagement):
    """保存质量/质检阈值到 orchestrator.json（前端可调）。"""
    try:
        cfg = load_cfg()
        cfg.setdefault("quality", {})
        cfg["quality"]["quality_min"] = int(qmin)
        cfg["quality"]["native_min"] = int(native)
        cfg["quality"]["engagement_min"] = int(engagement)
        save_cfg(cfg)
        return {"ok": True, "msg": f"thresholds -> quality≥{qmin}, native≥{native}, engagement≥{engagement}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def download_file(story_id, filename):
    """返回 out/{story_id}/ 下文件内容（用于浏览器下载）。"""
    import pathlib
    base = os.path.join(HERE, "out", story_id)
    safe = pathlib.Path(base).resolve()
    target = (safe / filename).resolve()
    if not str(target).startswith(str(safe)) or not target.is_file():
        return None, None
    return open(target, "rb").read(), os.path.basename(target)


def preview_chapter(story_id, ch=1):
    """预览第一章：读 shorts/{story_id}/ch0001.md，转成简单 HTML。"""
    import html as _html
    path = os.path.join(HERE, "shorts", story_id, f"ch{int(ch):04d}.md")
    if not os.path.isfile(path):
        return None
    text = open(path, encoding="utf-8").read()
    paras = [f"<p>{_html.escape(p.strip())}</p>" for p in text.split("\n\n") if p.strip()]
    body = "".join(paras)
    return f"<h3 style='font-family:var(--font-serif);color:var(--text);margin:0 0 12px'>Chapter {int(ch)} 预览</h3><div style='font-family:var(--font-serif);font-size:0.92rem;line-height:1.7;color:var(--text-light)'>{body}</div>"


def adopt(rec_id):
    """采纳推荐：生成方向文件 + 标记 adopted。"""
    store = StateStore()
    from direction_manager import adopt as do_adopt, build_direction
    rec = store.get_recommendation(rec_id)
    if not rec:
        store.close()
        return {"ok": False, "msg": "recommendation not found"}
    do_adopt(store, rec_id)
    store.close()
    return {"ok": True, "msg": f"adopted rec {rec_id}: {rec['genre']}"}


def toggle(key):
    cfg = load_cfg()
    if key in cfg["switches"]:
        cfg["switches"][key] = not cfg["switches"][key]
        save_cfg(cfg)
        return {"ok": True, "msg": f"{key} -> {cfg['switches'][key]}"}
    return {"ok": False, "msg": f"unknown switch {key}"}


PID_FILE = os.path.join(HERE, "logs", "production.pid")


def _record_pid(p):
    """记录生产进程 PID（供暂停/停止）。"""
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    open(PID_FILE, "w", encoding="utf-8").write(str(p.pid))


def stop_production():
    """停止后台生产进程（杀进程树，仅限生产进程）。"""
    if not os.path.exists(PID_FILE):
        return {"ok": False, "msg": "没有正在运行的生产进程"}
    pid = open(PID_FILE, encoding="utf-8").read().strip()
    try:
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True, timeout=15)
        os.remove(PID_FILE)
        return {"ok": True, "msg": f"已停止生产进程 {pid}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def trigger_cycle():
    """后台触发一次全自动生产循环（选品→自动采纳→写作→质检→包装）。"""
    def _run():
        p = subprocess.Popen(
            ["py", "-3", "orchestrator.py", "--cycle"],
            cwd=HERE, stdout=open(os.path.join(HERE, "logs", "cycle.out"), "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        _record_pid(p)
        p.wait()
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    threading.Thread(target=_run).start()
    return {"ok": True, "msg": "已开始全自动生产（选品→自动采纳→写作→质检→包装）"}


def trigger_custom(direction):
    """高级：用自定义方向直接生产（可选功能，主流程不需要）。"""
    if not direction or len(direction.strip()) < 10:
        return {"ok": False, "msg": "方向文本太短（至少 10 字符）"}
    import time as _t
    os.makedirs(os.path.join(HERE, "directions"), exist_ok=True)
    fname = f"custom_{int(_t.time())}.txt"
    fpath = os.path.join(HERE, "directions", fname)
    open(fpath, "w", encoding="utf-8").write(direction.strip())
    story_id = f"custom_{_t.strftime('%Y%m%d_%H%M%S')}"
    qmin = load_cfg().get("quality", {}).get("quality_min", 4)
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    def _run():
        p = subprocess.Popen(
            ["py", "-3", "engine.py", "--direction-file", fpath, "--story-id", story_id,
             "--quality-gate", "--quality-min", str(qmin)],
            cwd=HERE, stdout=open(os.path.join(HERE, "logs", f"{story_id}.out"), "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        _record_pid(p)
        p.wait()
    threading.Thread(target=_run).start()
    return {"ok": True, "msg": f"已触发自定义生产: {story_id}"}


HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>英文短篇生产线看板</title>
<style>
  :root { --bg:#f5efe4; --card:#fbf7ee; --text:#2a2622; --muted:#6b6158;
          --accent:#537d96; --accent-deep:#1b365d; --border:#d8cfbe; --green:#4a6b4a; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:Georgia,'Noto Serif SC',serif; background:var(--bg); color:var(--text); padding:24px; max-width:1000px; margin:0 auto; }
  h1 { font-size:1.5rem; font-weight:500; color:var(--accent-deep); margin-bottom:4px; }
  .sub { color:var(--muted); font-size:0.85rem; margin-bottom:20px; }
  .metrics { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:20px; }
  .metric { background:var(--card); border:1px solid var(--border); border-radius:4px; padding:14px 18px; min-width:140px; }
  .metric .num { font-size:1.7rem; font-weight:600; color:var(--accent); }
  .metric .lbl { font-size:0.75rem; color:var(--muted); }
  .card { background:var(--card); border:1px solid var(--border); border-radius:4px; padding:16px 18px; margin-bottom:16px; }
  .card h2 { font-size:1rem; font-weight:500; color:var(--accent-deep); border-left:2px solid var(--accent); padding-left:8px; margin-bottom:12px; }
  .rec { border-bottom:1px solid var(--border); padding:8px 0; display:flex; justify-content:space-between; align-items:center; gap:12px; }
  .rec:last-child { border-bottom:none; }
  .rec .info { flex:1; }
  .rec .genre { font-size:0.78rem; color:var(--accent); font-weight:500; }
  .rec .concept { font-size:0.85rem; color:var(--text); }
  .btn { border:0.5px solid var(--accent); border-radius:4px; background:transparent; color:var(--accent);
         padding:4px 12px; font-size:0.8rem; cursor:pointer; white-space:nowrap; font-family:inherit; }
  .btn:hover { background:rgba(83,125,150,0.1); }
  .btn.off { border-color:var(--muted); color:var(--muted); }
  .btn.on { border-color:var(--green); color:var(--green); background:rgba(74,107,74,0.08); }
  .row { display:flex; justify-content:space-between; align-items:center; padding:6px 0; font-size:0.85rem; }
  .tag { font-size:0.7rem; color:var(--muted); }
  .empty { color:var(--muted); font-size:0.85rem; }
  .actions { display:flex; gap:10px; margin-top:8px; }
  .dl { color:var(--accent); text-decoration:none; margin-right:8px; font-size:0.78rem; }
  .dl:hover { text-decoration:underline; }
  input[type=text], input[type=number] { border:0.5px solid var(--border); border-radius:4px; padding:6px 8px; font-family:inherit; font-size:0.85rem; background:#fff; color:var(--text); }
  input[type=text] { flex:1; }
</style>
</head>
<body>
<div id="previewOverlay" style="position:fixed;inset:0;background:rgba(42,38,34,0.5);z-index:98;display:none;align-items:center;justify-content:center;">
  <div style="background:var(--card);border:1px solid var(--accent);border-radius:6px;padding:20px 24px;max-width:640px;width:92%;max-height:80vh;display:flex;flex-direction:column;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
      <span id="previewTitle" style="font-weight:500;color:var(--accent-deep);font-size:0.9rem;"></span>
      <button class="btn" onclick="closePreview()">关闭</button>
    </div>
    <div id="previewBody" style="overflow-y:auto;"></div>
  </div>
</div>
<div id="keyOverlay" style="position:fixed;inset:0;background:rgba(245,239,228,0.98);z-index:99;display:none;align-items:center;justify-content:center;">
  <div style="background:var(--card);border:1px solid var(--accent);border-radius:6px;padding:28px 32px;max-width:480px;width:90%;">
    <h2 style="margin-bottom:8px;">配置 API Key</h2>
    <div class="sub" style="margin-bottom:16px;">第一次使用需填入自己的 key。key 只保存在本机（keys.local.json），不会上传到任何地方。</div>
    <div class="row" style="margin-bottom:10px;"><label style="width:110px;font-size:0.85rem;">GPT Key（写作+审查）</label><input id="keyGpt" type="password" placeholder="sk-..." style="flex:1;"></div>
    <div class="row" style="margin-bottom:10px;"><label style="width:110px;font-size:0.85rem;">Gemini Key（可选）</label><input id="keyGemini" type="password" placeholder="AQ...." style="flex:1;"></div>
    <div class="row" style="margin-bottom:16px;"><label style="width:110px;font-size:0.85rem;">代理地址（可选）</label><input id="keyProxy" type="text" placeholder="http://127.0.0.1:7897" value="http://127.0.0.1:7897" style="flex:1;"></div>
    <div class="actions">
      <button class="btn" onclick="saveKeys()" style="padding:8px 24px;">保存并进入</button>
    </div>
    <div id="keyMsg" class="sub" style="margin-top:10px;"></div>
  </div>
</div>
<h1>英文短篇生产线看板</h1>
<div class="sub" id="sub">数据加载中...</div>

<div class="card" id="progCard" style="border-color:var(--accent);">
  <h2>当前进度</h2>
  <div id="progress" style="font-size:0.9rem;color:var(--accent-deep);"></div>
</div>

<div class="metrics" id="metrics"></div>

<div class="card">
  <h2>待采纳推荐</h2>
  <div id="recs"></div>
</div>

<div class="card">
  <h2>产物清单（out/）</h2>
  <div id="outputs"></div>
</div>

<div class="card">
  <h2>已生成故事（shorts/）</h2>
  <div id="stories"></div>
</div>

<div class="card" style="border-color:var(--accent);">
  <h2>生产控制</h2>
  <div class="actions">
    <button class="btn" onclick="act('cycle')" style="font-size:0.95rem;padding:8px 20px;">▶ 开始全自动生产</button>
    <button class="btn off" onclick="act('stop')" style="font-size:0.95rem;padding:8px 20px;">⏸ 暂停生产</button>
    <button class="btn" onclick="act('report')">生成日结报告</button>
    <button class="btn" onclick="load()">刷新</button>
  </div>
  <div class="sub" style="margin-top:8px;">开始 = 自动扒榜选品 → 自动选最佳方向 → 自动写作/质检/包装，全程无需人工；暂停 = 停止当前后台生产。</div>
  <div id="actionMsg" class="sub" style="margin-top:8px"></div>
</div>

<div class="card">
  <h2>高级：自定义方向生产（可选）</h2>
  <div class="row"><input id="direction" type="text" placeholder="跳过选品，直接按这个方向生产（一般不需要）"></div>
  <div class="actions">
    <button class="btn" onclick="produce()">⚡ 按此方向直接生产</button>
  </div>
</div>

<div class="card">
  <h2>质量阈值（可调）</h2>
  <div id="quality"></div>
</div>

<div class="card">
  <h2>开关</h2>
  <div id="switches"></div>
</div>

<script>
async function checkKeys() {
  const r = await fetch('/api/keys');
  const k = await r.json();
  if (!k.configured) {
    document.getElementById('keyOverlay').style.display = 'flex';
  } else {
    document.getElementById('keyOverlay').style.display = 'none';
    load();
  }
}
async function saveKeys() {
  const gpt = document.getElementById('keyGpt').value.trim();
  const gem = document.getElementById('keyGemini').value.trim();
  const proxy = document.getElementById('keyProxy').value.trim();
  if (!gpt && !gem) { document.getElementById('keyMsg').textContent = '至少填一个 key'; return; }
  const r = await fetch('/api/keys?gpt=' + encodeURIComponent(gpt) + '&gemini=' + encodeURIComponent(gem) + '&proxy=' + encodeURIComponent(proxy), {method:'POST'});
  const j = await r.json();
  document.getElementById('keyMsg').textContent = j.msg || JSON.stringify(j);
  if (j.ok) { setTimeout(() => { document.getElementById('keyOverlay').style.display = 'none'; load(); }, 800); }
}
async function load() {
  const r = await fetch('/api/status');
  const s = await r.json();
  document.getElementById('sub').textContent = '最后刷新: ' + new Date().toLocaleTimeString();
  const p = s.progress || {};
  const stepNames = {idle:'空闲', radar:'雷达选品', adopt:'采纳推荐', writing:'写作中', verifying:'质检中', packaging:'包装中', done:'完成'};
  const progEl = document.getElementById('progress');
  if (p.step && p.step !== 'idle') {
    progEl.innerHTML = `<strong>${stepNames[p.step]||p.step}</strong> · ${p.detail||''} · ${p.story_id||''} <span class="tag">${p.updated_at||''}</span>`;
    document.getElementById('progCard').style.display = 'block';
  } else {
    document.getElementById('progCard').style.display = 'none';
  }
  document.getElementById('metrics').innerHTML =
    `<div class="metric"><div class="num">${s.metrics.stories}/${s.limits.max_stories_per_day}</div><div class="lbl">今日产量</div></div>
     <div class="metric"><div class="num">${(s.metrics.tokens/10000).toFixed(0)}万</div><div class="lbl">估算 token / ${(s.limits.daily_token_budget/10000).toFixed(0)}万</div></div>
     <div class="metric"><div class="num">${s.stories.length}</div><div class="lbl">故事目录</div></div>
     <div class="metric"><div class="num">${s.outputs.length}</div><div class="lbl">产物包</div></div>`;
  document.getElementById('recs').innerHTML = s.recommendations.length
    ? s.recommendations.map(r => `<div class="rec"><div class="info"><div class="genre">${r.genre} · conf ${r.confidence}</div><div class="concept">${r.concept}</div></div>
        <div><button class="btn" onclick="act('adopt',${r.id})">采纳</button>
        <button class="btn off" onclick="act('skip',${r.id})">跳过</button></div></div>`).join('')
    : '<div class="empty">无待采纳推荐（先跑 radar）</div>';
  document.getElementById('outputs').innerHTML = s.outputs.length
    ? s.outputs.map(o => `<div class="row"><span>${o.storyId}</span><span class="tag">${
        o.epub.map(f => `<a class="dl" href="/api/download?story=${o.storyId}&file=${f}">⬇ ${f}</a>`).join(' ')
      } ${o.manifest ? `<a class="dl" href="/api/download?story=${o.storyId}&file=publish_manifest.json">⬇ 发布清单</a>` : ''}</span></div>`).join('')
    : '<div class="empty">暂无产物（跑完生产+包装才有）</div>';
  document.getElementById('stories').innerHTML = s.stories.length
    ? s.stories.map(st => `<div class="row"><span>${st.storyId}</span><span class="tag">${st.chapters} 章 <a class="dl" href="javascript:void(0)" onclick="preview('${st.storyId}',1)">预览第一章</a></span></div>`).join('')
    : '<div class="empty">暂无故事</div>';
  document.getElementById('switches').innerHTML = Object.entries(s.switches).map(([k,v]) =>
    `<div class="row"><span>${k}</span><button class="btn ${v?'on':'off'}" onclick="act('toggle','${k}')">${v?'开启':'关闭'}</button></div>`).join('');
  const q = s.quality || {};
  document.getElementById('quality').innerHTML =
    `<div class="row"><span>质量门槛 naturalness（每章盲评）</span><input id="qmin" type="number" min="1" max="5" value="${q.quality_min||4}" style="width:64px"></div>` +
    `<div class="row"><span>质检地道性 native</span><input id="qnative" type="number" min="1" max="5" value="${q.native_min||4}" style="width:64px"></div>` +
    `<div class="row"><span>质检精彩度 engagement</span><input id="qeng" type="number" min="1" max="5" value="${q.engagement_min||3}" style="width:64px"></div>` +
    `<div class="actions"><button class="btn" onclick="saveThresh()">保存阈值</button><span class="tag">保存后对后续生产生效</span></div>`;
}
async function act(op, id) {
  let url = '/api/' + op + (id !== undefined ? '?id=' + id : '');
  const r = await fetch(url, {method:'POST'});
  const j = await r.json();
  document.getElementById('actionMsg').textContent = j.msg || JSON.stringify(j);
  load();
}
async function preview(storyId, ch) {
  const r = await fetch('/api/preview?story=' + storyId + '&ch=' + ch);
  if (!r.ok) { document.getElementById('actionMsg').textContent = '该故事没有第一章（还没写完）'; return; }
  const html = await r.text();
  const ov = document.getElementById('previewOverlay');
  document.getElementById('previewBody').innerHTML = html;
  document.getElementById('previewTitle').textContent = storyId + ' · 预览';
  ov.style.display = 'flex';
}
function closePreview() {
  document.getElementById('previewOverlay').style.display = 'none';
}
async function saveThresh() {
  const r = await fetch('/api/threshold?qmin=' + document.getElementById('qmin').value +
    '&native=' + document.getElementById('qnative').value +
    '&engagement=' + document.getElementById('qeng').value, {method:'POST'});
  const j = await r.json();
  document.getElementById('actionMsg').textContent = j.msg || JSON.stringify(j);
  load();
}
async function produce() {
  const d = document.getElementById('direction').value.trim();
  if (!d || d.length < 10) { document.getElementById('actionMsg').textContent = '方向文本太短（至少 10 字符）'; return; }
  const r = await fetch('/api/produce?direction=' + encodeURIComponent(d), {method:'POST'});
  const j = await r.json();
  document.getElementById('actionMsg').textContent = j.msg || JSON.stringify(j);
  load();
}
checkKeys();
setInterval(() => { const o = document.getElementById('keyOverlay'); if (!o || o.style.display === 'none') load(); }, 15000);
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body, code=200, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            self._send(HTML, ctype="text/html; charset=utf-8")
        elif u.path == "/api/status":
            self._send(json.dumps(get_status(), ensure_ascii=False))
        elif u.path == "/api/keys":
            self._send(json.dumps(get_keys_status(), ensure_ascii=False))
        elif u.path == "/api/download":
            q = parse_qs(u.query)
            data, name = download_file(q.get("story", [""])[0], q.get("file", [""])[0])
            if data is None:
                self._send(json.dumps({"error": "file not found"}), 404)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.end_headers()
                self.wfile.write(data)
        elif u.path == "/api/preview":
            q = parse_qs(u.query)
            body = preview_chapter(q.get("story", [""])[0], q.get("ch", [1])[0])
            if body is None:
                self._send(json.dumps({"error": "not found"}), 404)
            else:
                self._send(body, ctype="text/html; charset=utf-8")
        else:
            self._send(json.dumps({"error": "not found"}), 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/adopt":
            self._send(json.dumps(adopt(int(q.get("id", [0])[0])), ensure_ascii=False))
        elif u.path == "/api/keys":
            self._send(json.dumps(save_keys(
                q.get("gpt", [""])[0], q.get("gemini", [""])[0], q.get("proxy", [""])[0]), ensure_ascii=False))
        elif u.path == "/api/skip":
            store = StateStore()
            store.mark_skipped(int(q.get("id", [0])[0]))
            store.close()
            self._send(json.dumps({"ok": True, "msg": "skipped"}, ensure_ascii=False))
        elif u.path == "/api/toggle":
            self._send(json.dumps(toggle(q.get("id", [""])[0]), ensure_ascii=False))
        elif u.path == "/api/threshold":
            self._send(json.dumps(save_thresholds(
                q.get("qmin", [4])[0], q.get("native", [4])[0], q.get("engagement", [3])[0]), ensure_ascii=False))
        elif u.path == "/api/produce":
            self._send(json.dumps(trigger_custom(q.get("direction", [""])[0]), ensure_ascii=False))
        elif u.path == "/api/stop":
            self._send(json.dumps(stop_production(), ensure_ascii=False))
        elif u.path == "/api/cycle":
            self._send(json.dumps(trigger_cycle(), ensure_ascii=False))
        elif u.path == "/api/report":
            subprocess.Popen(["py", "-3", "orchestrator.py", "--report"], cwd=HERE)
            self._send(json.dumps({"ok": True, "msg": "report triggered"}, ensure_ascii=False))
        else:
            self._send(json.dumps({"error": "not found"}), 404)

    def log_message(self, *args):
        pass  # 静音请求日志


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"看板已启动: http://localhost:{PORT}")
    print("Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

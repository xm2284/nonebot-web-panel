"""管理面板：密码登录 + 全功能管理。

访问：http://<服务器IP>:6200/  （密码见 .env 的 PANEL_PASSWORD）
"""

import asyncio
import json
import os
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from nonebot import get_bots, get_driver
from nonebot.log import logger
from nonebot_plugin_pxchat.manager import chat_manager

PANEL_PORT = 6200
TOKEN: str = str(getattr(get_driver().config, "panel_token", "") or "")
PASSWORD: str = str(getattr(get_driver().config, "panel_password", "123456") or "123456")

DATA_DIR = Path("/app/data")
POINTS_FILE = DATA_DIR / "points.json"
PROACTIVE_FILE = DATA_DIR / "proactive.json"
BANK_FILE = DATA_DIR / "nonebot_data" / "nonebot_plugin_word_bank2" / "bank.json"

START_TIME = time.time()
_group_cache = {"ts": 0.0, "count": -1, "online": False}

app = FastAPI()


def _check(request: Request) -> bool:
    if not TOKEN:
        return False
    t = (
        request.query_params.get("token")
        or request.headers.get("x-token")
        or request.cookies.get("panel_token")
    )
    return t == TOKEN


def _deny():
    return JSONResponse({"error": "auth"}, status_code=403)


def _meminfo():
    try:
        with open("/proc/meminfo") as f:
            info = {}
            for line in f:
                k, v = line.split(":", 1)
                info[k.strip()] = int(v.strip().split()[0])
        return info.get("MemTotal", 0) // 1024, info.get("MemAvailable", 0) // 1024
    except Exception:
        return 0, 0


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


async def _group_info():
    now = time.time()
    if now - _group_cache["ts"] < 60 and _group_cache["count"] >= 0:
        return _group_cache["online"], _group_cache["count"]
    bots = get_bots()
    online = bool(bots)
    count = -1
    if bots:
        try:
            bot = list(bots.values())[0]
            groups = await bot.get_group_list()
            count = len(groups)
        except Exception:
            pass
    _group_cache.update({"ts": now, "count": count, "online": online})
    return online, count


def _bank_stats():
    bank = _read_json(BANK_FILE, {})
    stats = {}
    for t in ("congruence", "include", "regex"):
        idx = bank.get(t, {}) if isinstance(bank, dict) else {}
        stats[t] = sum(len(v) for v in idx.values()) if isinstance(idx, dict) else 0
    return stats


def _points_top():
    data = _read_json(POINTS_FILE, {})
    if not isinstance(data, dict):
        return []
    top = sorted(data.items(), key=lambda kv: -(kv[1].get("points", 0) if isinstance(kv[1], dict) else 0))[:10]
    return [
        {"name": (rec.get("name") or uid), "points": rec.get("points", 0), "streak": rec.get("streak", 0)}
        for uid, rec in top
        if isinstance(rec, dict)
    ]


def _proactive_info():
    data = _read_json(PROACTIVE_FILE, {})
    return {
        "tracked": len(data.get("last_seen", {})) if isinstance(data, dict) else 0,
        "greeted_today": data.get("greet_count", 0) if isinstance(data, dict) else 0,
        "greet_date": data.get("greet_count_date", "") if isinstance(data, dict) else "",
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})


QR_FILE = Path("/app/data/qr.png")


@app.get("/qr", response_class=HTMLResponse)
async def qr_page():
    return HTMLResponse(QR_HTML, headers={"Cache-Control": "no-store"})


@app.get("/qr.png")
async def qr_img():
    if QR_FILE.exists():
        return FileResponse(str(QR_FILE), media_type="image/png",
                            headers={"Cache-Control": "no-store"})
    return JSONResponse({"error": "no qr"}, status_code=404)


@app.post("/api/login")
async def login(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if str(data.get("password", "")) != PASSWORD:
        return JSONResponse({"ok": False, "error": "密码错误"}, status_code=401)
    resp = JSONResponse({"ok": True, "token": TOKEN})
    resp.set_cookie("panel_token", TOKEN, max_age=30 * 24 * 3600, httponly=True)
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("panel_token")
    return resp


@app.post("/api/login_qrcode")
async def get_login_qrcode(request: Request):
    if not _check(request):
        return _deny()
    import hashlib, httpx
    try:
        token = os.environ.get("NAPCAT_WEBUI_TOKEN", "")
        h = hashlib.sha256(f"{token}.napcat".encode()).hexdigest()
        async with httpx.AsyncClient(timeout=8.0) as client:
            login_resp = await client.post("http://127.0.0.1:6099/api/auth/login", json={"hash": h, "totpCode": ""})
            cred = login_resp.json().get("data", {}).get("Credential", "")
            headers = {"Authorization": f"Bearer {cred}"}
            # 先尝试 RefreshQRcode，如果没有则调用 GetQQLoginQrcode
            qr_resp = await client.post("http://127.0.0.1:6099/api/QQLogin/RefreshQRcode", headers=headers)
            url = qr_resp.json().get("data", {}).get("qrcodeurl", "")
            if not url:
                qr2 = await client.post("http://127.0.0.1:6099/api/QQLogin/GetQQLoginQrcode", headers=headers)
                url = qr2.json().get("data", {}).get("qrcode", "")
            return {"ok": True, "qrcodeurl": url}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/state")
async def state(request: Request):
    if not _check(request):
        return _deny()
    online, group_count = await _group_info()
    total_mem, avail_mem = _meminfo()
    try:
        load = round(os.getloadavg()[0], 2)
    except Exception:
        load = -1
    uptime = int(time.time() - START_TIME)
    return {
        "online": online,
        "group_count": group_count,
        "load": load,
        "mem_total": total_mem,
        "mem_avail": avail_mem,
        "uptime": uptime,
        "chat_enabled": chat_manager.is_chat_enabled(),
        "prob": chat_manager.get_group_chat_probability(),
        "image_enabled": chat_manager.is_image_recognition_enabled(),
        "personality": chat_manager.get_personality(),
        "current_model": chat_manager.get_current_ai_config().get("name", ""),
        "models": [
            {"name": c.get("name", ""), "model": c.get("model", ""), "url": c.get("api_url", "")}
            for c in chat_manager.get_ai_configs()
        ],
        "enabled_groups": chat_manager.get_enabled_groups(),
        "group_probabilities": chat_manager.get_group_probabilities(),
        "group_personalities": chat_manager.get_group_personalities(),
        "all_groups": [],
        "bank": _bank_stats(),
        "points_top": _points_top(),
        "proactive": _proactive_info(),
    }


@app.get("/api/groups")
async def groups(request: Request):
    if not _check(request):
        return _deny()
    result = []
    bots = get_bots()
    if bots:
        try:
            bot = list(bots.values())[0]
            gs = await bot.get_group_list()
            result = [
                {"id": str(g.get("group_id", "")), "name": g.get("group_name", ""), "members": g.get("member_count", 0)}
                for g in gs
            ]
        except Exception:
            pass
    return {"groups": result}


@app.post("/api/chat")
async def chat_toggle(request: Request):
    if not _check(request):
        return _deny()
    data = await request.json()
    chat_manager.set_chat_enabled(bool(data.get("enabled")))
    return {"ok": True}


@app.post("/api/prob")
async def prob_set(request: Request):
    if not _check(request):
        return _deny()
    data = await request.json()
    try:
        v = float(data.get("value"))
    except (TypeError, ValueError):
        return {"ok": False}
    if not 0 <= v <= 1:
        return {"ok": False}
    chat_manager.set_group_chat_probability(v)
    return {"ok": True}


@app.post("/api/image")
async def image_toggle(request: Request):
    if not _check(request):
        return _deny()
    data = await request.json()
    chat_manager.set_image_recognition_enabled(bool(data.get("enabled")))
    return {"ok": True}


@app.post("/api/group_personality")
async def set_group_personality(request: Request):
    if not _check(request):
        return _deny()
    data = await request.json()
    gid = str(data.get("group_id", "")).strip()
    pers = str(data.get("personality", "")).strip()
    if not gid:
        return {"ok": False}
    chat_manager.set_group_personality(gid, pers)
    return {"ok": True}


@app.post("/api/personality")
async def set_personality(request: Request):
    if not _check(request):
        return _deny()
    data = await request.json()
    chat_manager.set_personality(str(data.get("personality", "")))
    return {"ok": True}


@app.post("/api/model")
async def model_switch(request: Request):
    if not _check(request):
        return _deny()
    data = await request.json()
    ok = chat_manager.switch_ai_config(str(data.get("name", "")))
    return {"ok": ok}


@app.post("/api/model/add")
async def model_add(request: Request):
    if not _check(request):
        return _deny()
    data = await request.json()
    name = str(data.get("name", "")).strip()
    api_key = str(data.get("api_key", "")).strip()
    api_url = str(data.get("api_url", "")).strip()
    model = str(data.get("model", "")).strip()
    if not (name and api_key and api_url and model):
        return {"ok": False, "error": "参数不完整"}
    ok = chat_manager.add_ai_config(name, api_key, api_url, model)
    return {"ok": ok}


@app.post("/api/model/del")
async def model_del(request: Request):
    if not _check(request):
        return _deny()
    data = await request.json()
    result = chat_manager.remove_ai_config(str(data.get("name", "")))
    ok = result[0] if isinstance(result, tuple) else bool(result)
    return {"ok": ok}


@app.post("/api/group")
async def group_action(request: Request):
    if not _check(request):
        return _deny()
    data = await request.json()
    gid = str(data.get("group_id", "")).strip()
    if not gid:
        return {"ok": False}
    if data.get("action") == "add":
        chat_manager.enable_group(gid)
    else:
        chat_manager.disable_group(gid)
    return {"ok": True}


@app.post("/api/group_prob")
async def group_prob_set(request: Request):
    if not _check(request):
        return _deny()
    data = await request.json()
    gid = str(data.get("group_id", "")).strip()
    if not gid:
        return {"ok": False}
    value = data.get("value")
    if value is None or value == "" or value == "default":
        chat_manager.set_group_probability(gid, None)
        return {"ok": True, "cleared": True}
    try:
        v = float(value)
    except (TypeError, ValueError):
        return {"ok": False}
    if not 0 <= v <= 1:
        return {"ok": False}
    chat_manager.set_group_probability(gid, v)
    return {"ok": True}


QR_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>扫码登录</title></head>
<body style="margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#0f1115;color:#e8e8e8;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif">
<h2 style="font-weight:600;margin:0 0 4px">扫码登录 10001</h2>
<img id="q" src="/qr.png" style="width:min(72vw,300px);height:min(72vw,300px);background:#fff;padding:12px;border-radius:14px;margin:16px 0">
<p style="color:#8b93a7;font-size:13px">用手机 QQ 扫描 · 每 20 秒自动刷新</p>
<script>setInterval(function(){document.getElementById('q').src='/qr.png?t='+Date.now()},20000)</script>
</body></html>"""


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>小球球管理面板</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  html { -webkit-text-size-adjust: 100%; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: #f3f5f9; color: #1f2937; font-size: clamp(13px, 0.85rem + 0.25vw, 14px); line-height: 1.55; }
  .header { background: #1e3a5f; color: #fff; padding: clamp(10px, 2.5vw, 14px) clamp(12px, 3vw, 20px); display: flex; align-items: center; justify-content: space-between; gap: 10px; position: sticky; top: 0; z-index: 10; }
  .header h1 { font-size: clamp(14px, 4vw, 17px); font-weight: 600; letter-spacing: 1px; }
  .header button { background: rgba(255,255,255,.15); color: #fff; border: 0; border-radius: 6px; padding: 8px 12px; cursor: pointer; font-size: clamp(12px, 3vw, 13px); min-height: 38px; }
  .wrap { max-width: 1080px; margin: clamp(10px, 2vw, 18px) auto; padding: 0 clamp(10px, 2.5vw, 14px) 60px; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 440px), 1fr)); gap: clamp(10px, 2vw, 14px); }
  .card { background: #fff; border-radius: 10px; padding: clamp(12px, 3vw, 18px); box-shadow: 0 1px 3px rgba(15,23,42,.08); min-width: 0; }
  .card.full { grid-column: 1 / -1; }
  .card h2 { font-size: clamp(14px, 3.6vw, 15px); color: #1e3a5f; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #eef1f6; }
  .kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 190px), 1fr)); gap: 0 clamp(10px, 3vw, 20px); }
  .kv div { display: flex; justify-content: space-between; gap: 10px; padding: 8px 0; border-bottom: 1px dashed #eef1f6; min-width: 0; }
  .kv span:first-child { color: #6b7280; flex-shrink: 0; }
  .kv span:last-child { font-weight: 600; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }
  button.btn { border: 0; border-radius: 6px; padding: 9px 14px; cursor: pointer; font-size: clamp(12px, 3vw, 13px); background: #2563eb; color: #fff; min-height: 40px; white-space: nowrap; }
  button.btn:active { opacity: .8; }
  button.btn.gray { background: #64748b; }
  button.btn.green { background: #15803d; }
  button.btn.red { background: #b91c1c; }
  button.btn.small { padding: 8px 12px; font-size: 12px; min-height: 36px; }
  input[type=text], input[type=number], input[type=password], textarea { border: 1px solid #d3dae6; border-radius: 6px; padding: 10px 12px; font-size: 13px; font-family: inherit; outline: none; width: 100%; min-width: 0; background: #fff; }
  input:focus, textarea:focus { border-color: #2563eb; }
  textarea { min-height: 140px; resize: vertical; line-height: 1.6; }
  .fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 150px), 1fr)); gap: 8px; margin-top: 12px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; background: #eef4ff; color: #1e3a5f; border-radius: 16px; padding: 5px 12px; font-size: 12px; margin: 3px 2px; max-width: 100%; }
  .chip span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chip button { border: 0; background: #b91c1c; color: #fff; border-radius: 50%; width: 18px; height: 18px; line-height: 18px; font-size: 12px; cursor: pointer; padding: 0; flex-shrink: 0; }
  .list { max-height: 46vh; overflow: auto; margin-top: 8px; border: 1px solid #eef1f6; border-radius: 8px; }
  .lrow { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; padding: 10px 12px; border-bottom: 1px solid #f4f6fa; font-size: clamp(12px, 3vw, 13px); }
  .lrow:last-child { border-bottom: 0; }
  .lrow .name { flex: 1 1 100%; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
  .lrow .meta { color: #94a3b8; font-size: 12px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lrow .actions { margin-left: auto; display: flex; gap: 6px; flex-shrink: 0; }
  .rank { display: flex; align-items: center; gap: 8px; padding: 9px 4px; border-bottom: 1px solid #f4f6fa; font-size: 13px; }
  .rank:last-child { border-bottom: 0; }
  .rank .no { width: 22px; color: #94a3b8; flex-shrink: 0; }
  .rank .rname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rank .pts { font-weight: 600; color: #1e3a5f; flex-shrink: 0; }
  .rank .streak { color: #94a3b8; font-size: 12px; flex-shrink: 0; }
  .toast { position: fixed; left: 50%; bottom: calc(24px + env(safe-area-inset-bottom)); transform: translateX(-50%); background: #1f2937; color: #fff; padding: 10px 18px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .25s; pointer-events: none; max-width: 86vw; text-align: center; }
  .toast.show { opacity: .95; }
  .muted { color: #94a3b8; font-size: 12px; }
  .login { max-width: 360px; margin: 12vh auto; padding: 0 20px; }
  .login h1 { text-align: center; font-size: clamp(17px, 5vw, 20px); color: #1e3a5f; margin-bottom: 6px; }
  .login p { text-align: center; color: #94a3b8; font-size: 13px; margin-bottom: 22px; }
  .login input { margin-bottom: 12px; padding: 12px; }
  .login button { width: 100%; padding: 12px; font-size: 15px; }
  .hidden { display: none !important; }
</style>
</head>
<body>

<div id="loginPage" class="login">
  <h1>小球球管理面板</h1>
  <p>请输入管理密码</p>
  <input type="password" id="pw" placeholder="密码" onkeydown="if(event.key==='Enter')doLogin()">
  <button class="btn" onclick="doLogin()">登录</button>
</div>

<div id="app" class="hidden">
  <div class="header">
    <h1>小球球管理面板</h1>
    <button onclick="doLogout()">退出登录</button>
  </div>
  <div class="wrap">

    <div class="card full" style="border: 2px solid #3b82f6; background: #f0f7ff;">
      <h2 style="color: #1d4ed8; display:flex; align-items:center; justify-content:space-between">
        <span>📱 QQ 登录与安全中心</span>
        <span id="login-badge" style="font-size:13px; font-weight:normal; padding:2px 8px; border-radius:12px; background:#e2e8f0; color:#475569">检测中...</span>
      </h2>
      <p style="margin: 6px 0 12px 0; font-size: 13px; color: #475569; line-height: 1.5;">
        当手机提示外挂异常、被踢下线或遇到滑块/短信验证时，可点击下方按钮直接在手机或电脑浏览器中完成滑块验证、短信接收或扫码重登。
      </p>
      <div class="row" style="flex-wrap: wrap; gap: 10px;">
        <a id="btn-webui-login" href="http://127.0.0.1:6099/webui/" target="_blank" class="btn green" style="text-decoration:none; display:inline-flex; align-items:center; padding:10px 18px; font-weight:bold; font-size:15px">
          📲 打开手机滑块/扫码/密码登录页
        </a>
        <button class="btn" onclick="fetchLiveQR()" style="padding:10px 16px;">
          📷 在本页刷新二维码
        </button>
      </div>
      <div id="live-qr-box" style="display:none; margin-top:15px; padding:12px; background:#fff; border-radius:8px; border:1px solid #bfdbfe; text-align:center;">
        <div style="font-size:13px; color:#1e3a8a; margin-bottom:8px; font-weight:bold;">请使用登录了机器人 QQ (10001) 的手机扫码授权：</div>
        <img id="live-qr-img" src="" alt="登录二维码" style="width:180px; height:180px; border:1px solid #ddd; border-radius:4px; display:inline-block;">
        <div style="margin-top:6px;">
          <a id="live-qr-link" href="#" target="_blank" style="font-size:12px; color:#2563eb; text-decoration:underline;">点此直达二维码链接</a>
        </div>
      </div>
    </div>

    <div class="card full">
      <h2>系统状态</h2>
      <div class="kv">
        <div><span>机器人状态</span><span id="s-online">-</span></div>
        <div><span>所在群数</span><span id="s-groups">-</span></div>
        <div><span>运行时长</span><span id="s-uptime">-</span></div>
        <div><span>系统负载</span><span id="s-load">-</span></div>
        <div><span>内存</span><span id="s-mem">-</span></div>
        <div><span>当前模型</span><span id="s-model">-</span></div>
      </div>
    </div>

    <div class="card">
      <h2>聊天设置</h2>
      <div class="row">
        <button class="btn green" onclick="setChat(true)">开启聊天</button>
        <button class="btn red" onclick="setChat(false)">关闭聊天</button>
        <button class="btn gray" onclick="setImage(!state.image_enabled)" id="btn-image">图片识别</button>
      </div>
      <div class="row">
        <span class="muted">群聊参与概率</span>
        <input type="number" id="prob-input" step="0.1" min="0" max="1" style="max-width:110px">
        <button class="btn" onclick="setProb()">设置</button>
      </div>
      <div class="row">
        <button class="btn gray small" onclick="setProbValue(0.1)">10%</button>
        <button class="btn gray small" onclick="setProbValue(0.2)">20%</button>
        <button class="btn gray small" onclick="setProbValue(0.3)">30%</button>
        <button class="btn gray small" onclick="setProbValue(0.5)">50%</button>
        <button class="btn gray small" onclick="setProbValue(0.9)">90%</button>
      </div>
    </div>

    <div class="card">
      <h2>数据统计</h2>
      <div class="kv">
        <div><span>精确词条</span><span id="b-cong">-</span></div>
        <div><span>模糊词条</span><span id="b-incl">-</span></div>
        <div><span>正则词条</span><span id="b-reg">-</span></div>
        <div><span>主动私聊对象</span><span id="p-tracked">-</span></div>
        <div><span>今日已问候</span><span id="p-greet">-</span></div>
        <div><span>已启用 AI 群</span><span id="g-enabled">-</span></div>
      </div>
    </div>

    <div class="card full">
      <h2>人设配置（支持全局与按群定制）</h2>
      <div class="row" style="margin-bottom:8px">
        <span class="muted">切换人设目标：</span>
        <select id="persona-target" onchange="switchPersonaTarget()" style="padding:6px;border-radius:4px;border:1px solid #ddd;font-size:14px">
          <option value="global">🌐 全局默认人设（私聊/其他群）</option>
        </select>
        <span class="muted" id="persona-tip" style="font-size:12px;color:#888"></span>
      </div>
      <textarea id="persona" style="min-height:160px;font-family:monospace;font-size:13px;line-height:1.5"></textarea>
      <div class="row">
        <button class="btn" onclick="savePersona()">保存当前人设</button>
        <button class="btn gray small" onclick="resetGroupPersona()" id="btn-reset-gpers" style="display:none">恢复使用全局人设</button>
      </div>
    </div>

    <div class="card full">
      <h2>模型配置</h2>
      <div class="list" id="models"></div>
      <div class="fields">
        <input type="text" id="m-name" placeholder="名称（如 susu-opus）">
        <input type="text" id="m-key" placeholder="API Key">
        <input type="text" id="m-url" placeholder="API 地址（如 https://susu.wiki/v1）">
        <input type="text" id="m-model" placeholder="模型名（如 opus4.8）">
      </div>
      <div class="row"><button class="btn" onclick="addModel()">添加模型</button></div>
    </div>

    <div class="card">
      <h2>已启用 AI 的群</h2>
      <div id="enabled"></div>
    </div>

    <div class="card">
      <h2>积分排行</h2>
      <div id="points"></div>
    </div>

    <div class="card full">
      <h2>全部群聊（点击启用 / 关闭 AI）</h2>
      <input type="text" id="search" placeholder="搜索群名或群号" style="margin-bottom:8px" oninput="renderGroups()">
      <div id="groups" class="list"></div>
    </div>

  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let state = {};
let allGroups = [];

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1800);
}

async function api(path, body) {
  const res = await fetch('/api/' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 403 || res.status === 401) { showLogin(); throw new Error('auth'); }
  return res.json();
}

function showLogin() { document.getElementById('loginPage').classList.remove('hidden'); document.getElementById('app').classList.add('hidden'); }
function showApp() { document.getElementById('loginPage').classList.add('hidden'); document.getElementById('app').classList.remove('hidden'); }

async function doLogin() {
  const pw = document.getElementById('pw').value;
  try {
    const res = await fetch('/api/login', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: pw}) });
    const d = await res.json();
    if (d.ok) { showApp(); load(); } else { toast('密码错误'); }
  } catch (e) { toast('网络错误，请重试'); }
}

async function doLogout() { try { await fetch('/api/logout', {method: 'POST'}); } catch (e) {} showLogin(); }

async function load() {
  state = await api('state');
  document.getElementById('s-online').textContent = state.online ? '在线' : '离线';
  document.getElementById('s-groups').textContent = state.group_count >= 0 ? state.group_count : '未知';
  const up = state.uptime; const h = Math.floor(up/3600), mm = Math.floor((up%3600)/60);
  document.getElementById('s-uptime').textContent = h + ' 小时 ' + mm + ' 分';
  document.getElementById('s-load').textContent = state.load;
  document.getElementById('s-mem').textContent = state.mem_avail + ' / ' + state.mem_total + ' MB';
  document.getElementById('s-model').textContent = state.current_model;
  document.getElementById('prob-input').value = state.prob;
  document.getElementById('persona').value = state.personality;
  const bi = document.getElementById('btn-image');
  bi.textContent = state.image_enabled ? '图片识别：开' : '图片识别：关';
  bi.className = 'btn ' + (state.image_enabled ? 'green' : 'gray');
  document.getElementById('b-cong').textContent = state.bank.congruence;
  document.getElementById('b-incl').textContent = state.bank.include;
  document.getElementById('b-reg').textContent = state.bank.regex;
  document.getElementById('p-tracked').textContent = state.proactive.tracked;
  document.getElementById('p-greet').textContent = state.proactive.greeted_today;
  document.getElementById('g-enabled').textContent = state.enabled_groups.length;

  const modelsBox = document.getElementById('models');
  modelsBox.innerHTML = '';
  for (const c of state.models) {
    const cur = c.name === state.current_model;
    const row = document.createElement('div');
    row.className = 'lrow';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = c.name + (cur ? '（当前）' : '');
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = c.model + '  ' + (c.url || '');
    const actions = document.createElement('div');
    actions.className = 'actions';
    const b1 = document.createElement('button');
    b1.className = 'btn small' + (cur ? ' gray' : '');
    b1.textContent = cur ? '使用中' : '切换';
    b1.onclick = async () => { await api('model', {name: c.name}); toast('已切换'); load(); };
    actions.appendChild(b1);
    if (!cur) {
      const b2 = document.createElement('button');
      b2.className = 'btn small red';
      b2.textContent = '删除';
      b2.onclick = async () => { await api('model/del', {name: c.name}); toast('已删除'); load(); };
      actions.appendChild(b2);
    }
    row.appendChild(name);
    row.appendChild(meta);
    row.appendChild(actions);
    modelsBox.appendChild(row);
  }

  renderEnabled();

  const pts = document.getElementById('points');
  pts.innerHTML = '';
  if (!state.points_top.length) { pts.innerHTML = '<span class="muted">还没有人签到</span>'; }
  state.points_top.forEach((r, i) => {
    const row = document.createElement('div');
    row.className = 'rank';
    const no = document.createElement('span'); no.className = 'no'; no.textContent = i + 1;
    const nm = document.createElement('span'); nm.className = 'rname'; nm.textContent = r.name;
    const pt = document.createElement('span'); pt.className = 'pts'; pt.textContent = r.points + ' 分';
    const st = document.createElement('span'); st.className = 'streak'; st.textContent = '连签 ' + r.streak + ' 天';
    row.appendChild(no); row.appendChild(nm); row.appendChild(pt); row.appendChild(st);
    pts.appendChild(row);
  });

  const g = await api('groups');
  allGroups = g.groups || [];
  renderGroups();
  updatePersonaSelect();
}

function renderEnabled() {
  const el = document.getElementById('enabled');
  el.innerHTML = '';
  if (!state.enabled_groups.length) { el.innerHTML = '<span class="muted">暂无</span>'; return; }
  const map = {};
  for (const g of allGroups) map[g.id] = g.name;
  const gp = state.group_probabilities || {};
  for (const gid of state.enabled_groups) {
    const c = document.createElement('span');
    c.className = 'chip';
    const label = document.createElement('span');
    const cur = (gid in gp) ? gp[gid] : state.prob;
    label.textContent = (map[gid] || gid) + ' · ' + Math.round(cur * 100) + '%' + ((gid in gp) ? '＊' : '');
    const btn = document.createElement('button');
    btn.textContent = '×';
    btn.onclick = async () => { await api('group', {group_id: gid, action: 'del'}); toast('已关闭'); load(); };
    c.appendChild(label);
    c.appendChild(btn);
    el.appendChild(c);
  }
}

function renderGroups() {
  const el = document.getElementById('groups');
  const q = document.getElementById('search').value.trim().toLowerCase();
  const gp = state.group_probabilities || {};
  el.innerHTML = '';
  for (const g of allGroups) {
    if (q && !(g.name.toLowerCase().includes(q) || g.id.includes(q))) continue;
    const on = state.enabled_groups.includes(g.id);
    const row = document.createElement('div');
    row.className = 'lrow';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = g.name;
    const meta = document.createElement('span');
    meta.className = 'meta';
    const cur = (g.id in gp) ? gp[g.id] : state.prob;
    meta.textContent = g.id + ' / ' + g.members + ' 人 / 概率 ' + Math.round(cur * 100) + '%' + ((g.id in gp) ? '（单独）' : '');
    const actions = document.createElement('div');
    actions.className = 'actions';
    const pin = document.createElement('input');
    pin.type = 'number'; pin.step = '0.1'; pin.min = '0'; pin.max = '1';
    pin.style.maxWidth = '84px'; pin.placeholder = '概率';
    pin.value = (g.id in gp) ? gp[g.id] : '';
    const pbtn = document.createElement('button');
    pbtn.className = 'btn small gray';
    pbtn.textContent = '设概率';
    pbtn.onclick = async () => {
      const v = parseFloat(pin.value);
      if (isNaN(v) || v < 0 || v > 1) { toast('请输入 0~1'); return; }
      await api('group_prob', {group_id: g.id, value: v}); toast('已设 ' + Math.round(v * 100) + '%'); load();
    };
    actions.appendChild(pin);
    actions.appendChild(pbtn);
    if (g.id in gp) {
      const cbtn = document.createElement('button');
      cbtn.className = 'btn small red';
      cbtn.textContent = '恢复默认';
      cbtn.onclick = async () => { await api('group_prob', {group_id: g.id, value: 'default'}); toast('已恢复默认'); load(); };
      actions.appendChild(cbtn);
    }
    const b = document.createElement('button');
    b.className = 'btn small ' + (on ? 'red' : 'green');
    b.textContent = on ? '关闭AI' : '启用AI';
    b.onclick = async () => { await api('group', {group_id: g.id, action: on ? 'del' : 'add'}); toast('已更新'); load(); };
    actions.appendChild(b);
    row.appendChild(name);
    row.appendChild(meta);
    row.appendChild(actions);
    el.appendChild(row);
  }
}

async function setChat(enabled) { await api('chat', {enabled}); toast('已更新'); load(); }
async function setImage(enabled) { await api('image', {enabled}); toast('已更新'); load(); }
async function setProbValue(v) { await api('prob', {value: v}); toast('参与概率已设为 ' + Math.round(v*100) + '%'); load(); }
async function setProb() {
  const v = parseFloat(document.getElementById('prob-input').value);
  if (isNaN(v) || v < 0 || v > 1) { toast('请输入 0 到 1 之间的数字'); return; }
  await api('prob', {value: v}); toast('已设置'); load();
}
let curPersonaTarget = 'global';
function updatePersonaSelect() {
  const sel = document.getElementById('persona-target');
  if (!sel) return;
  const savedVal = sel.value || 'global';
  sel.innerHTML = '<option value="global">🌐 全局默认人设（私聊/其他群）</option>';
  const map = {};
  for (const g of allGroups) map[g.id] = g.name;
  for (const gid of (state.enabled_groups || [])) {
    const opt = document.createElement('option');
    opt.value = gid;
    const hasCustom = state.group_personalities && state.group_personalities[gid];
    opt.textContent = '👥 群 ' + (map[gid] || gid) + (hasCustom ? ' [已定制]' : '');
    sel.appendChild(opt);
  }
  sel.value = savedVal;
  switchPersonaTarget();
}

function switchPersonaTarget() {
  const sel = document.getElementById('persona-target');
  curPersonaTarget = sel ? sel.value : 'global';
  const ta = document.getElementById('persona');
  const tip = document.getElementById('persona-tip');
  const resetBtn = document.getElementById('btn-reset-gpers');
  if (curPersonaTarget === 'global') {
    ta.value = state.personality || '';
    tip.textContent = '当前正在编辑全局人设';
    if (resetBtn) resetBtn.style.display = 'none';
  } else {
    const custom = (state.group_personalities || {})[curPersonaTarget];
    ta.value = custom || state.personality || '';
    tip.textContent = custom ? '当前正在编辑该群独立人设' : '当前群暂无独立人设（显示为全局人设模板）';
    if (resetBtn) resetBtn.style.display = custom ? 'inline-block' : 'none';
  }
}

async function savePersona() {
  const val = document.getElementById('persona').value;
  if (curPersonaTarget === 'global') {
    await api('personality', {personality: val});
    toast('全局人设已保存');
  } else {
    await api('group_personality', {group_id: curPersonaTarget, personality: val});
    toast('群独立人设已保存');
  }
  load();
}

async function fetchLiveQR() {
  toast('正在向 NapCat 请求最新登录二维码...');
  try {
    const res = await api('login_qrcode');
    if (res.ok && res.qrcodeurl) {
      const box = document.getElementById('live-qr-box');
      const img = document.getElementById('live-qr-img');
      const link = document.getElementById('live-qr-link');
      box.style.display = 'block';
      img.src = 'https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=' + encodeURIComponent(res.qrcodeurl);
      link.href = res.qrcodeurl;
      toast('二维码已生成，请扫码');
    } else {
      toast('获取二维码失败: ' + (res.error || '无返回'));
    }
  } catch (e) {
    toast('请求失败: ' + e);
  }
}

async function resetGroupPersona() {
  if (curPersonaTarget === 'global') return;
  await api('group_personality', {group_id: curPersonaTarget, personality: ''});
  toast('已恢复为继承全局人设');
  load();
}
async function addModel() {
  const name = document.getElementById('m-name').value.trim();
  const api_key = document.getElementById('m-key').value.trim();
  const api_url = document.getElementById('m-url').value.trim();
  const model = document.getElementById('m-model').value.trim();
  if (!(name && api_key && api_url && model)) { toast('请填写完整'); return; }
  const r = await api('model/add', {name, api_key, api_url, model});
  if (r.ok) {
    toast('已添加');
    document.getElementById('m-name').value = '';
    document.getElementById('m-key').value = '';
    document.getElementById('m-url').value = '';
    document.getElementById('m-model').value = '';
    load();
  } else { toast(r.error || '添加失败'); }
}

(async () => {
  try {
    const res = await fetch('/api/state');
    if (res.status === 403 || res.status === 401) { showLogin(); return; }
    showApp(); load();
  } catch (e) { showLogin(); }
})();
</script>
</body>
</html>
"""


@get_driver().on_startup
async def _start_panel():
    if not TOKEN:
        logger.warning("未配置 PANEL_TOKEN，管理面板未启动")
        return
    config = uvicorn.Config(app, host="0.0.0.0", port=PANEL_PORT, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    asyncio.create_task(server.serve())
    logger.info(f"管理面板已启动: http://0.0.0.0:{PANEL_PORT} (密码登录)")

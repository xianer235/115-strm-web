import os, json, sqlite3, asyncio, re, hashlib, time, urllib.parse
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from datetime import datetime

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="115-strm-v7-final")

CONFIG_PATH = "/app/config/settings.json"
DB_PATH = "/app/config/data.db"
TREE_FILE = "/app/config/目录树.txt"
RAW_TEMP = "/app/config/tree.raw"

def get_config():
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        default = {
            "username": "admin", "password": "admin123",
            "alist_url": "", "alist_user": "", "alist_pass": "",
            "tree_url": "", "mount_path": "/115", "exclude_levels": 2,
            "extensions": "mp4,mkv,avi,mov,ts,iso,rmvb,wmv,m4v,mpg,flac,mp3,ass,srt",
            "sync_mode": "incremental", "sync_clean": True, "check_hash": True,
            "cron_hour": "", "last_hash": ""
        }
        with open(CONFIG_PATH, 'w') as f: json.dump(default, f)
    with open(CONFIG_PATH, 'r') as f: return json.load(f)

task_status = {
    "running": False, 
    "next_run": None,
    "logs": ["系统已就绪"], 
    "progress": {"step": "空闲", "percent": 0, "detail": "等待指令"}
}

async def update_progress(step, percent, detail):
    task_status["progress"].update({"step": step, "percent": int(percent), "detail": detail})
    await asyncio.sleep(0) # 关键：交出控制权，让 FastAPI 能响应前端请求

async def write_log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    task_status["logs"].append(f"[{ts}] {msg}")
    if len(task_status["logs"]) > 500: task_status["logs"].pop(0)

async def run_sync(use_local=False, force_full=False):
    if task_status["running"]: return
    task_status["running"] = True
    cfg = get_config()
    try:
        if not use_local:
            await update_progress("正在下载", 2, "启动异步下载任务...")
            # 使用异步子进程下载
            curl_args = ['-fL', cfg['tree_url'], '-o', RAW_TEMP]
            if cfg['alist_user'] and cfg['alist_pass']:
                curl_args += ['-u', f"{cfg['alist_user']}:{cfg['alist_pass']}"]
            
            proc = await asyncio.create_subprocess_exec('curl', *curl_args)
            await proc.wait()
            if proc.returncode != 0: raise Exception("目录树下载失败，请检查URL或网络")

            await update_progress("正在转码", 10, "正在进行字符集转换(UTF-16 -> UTF-8)...")
            proc = await asyncio.create_subprocess_exec('iconv', '-f', 'UTF-16LE', '-t', 'UTF-8//IGNORE', RAW_TEMP, '-o', TREE_FILE)
            await proc.wait()

            new_hash = hashlib.md5(open(TREE_FILE, 'rb').read()).hexdigest()
            if cfg.get('check_hash') and new_hash == cfg.get('last_hash') and not force_full:
                await write_log("✨ MD5校验一致，任务跳过")
                await update_progress("已完成", 100, "目录树无变化")
                return
            cfg['last_hash'] = new_hash
            with open(CONFIG_PATH, 'w') as f: json.dump(cfg, f)

        await update_progress("准备解析", 15, "正在初始化数据库连接...")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS local_files (path_hash TEXT PRIMARY KEY, relative_path TEXT)")
        cursor.execute("CREATE TEMPORARY TABLE current_scan (path_hash TEXT PRIMARY KEY, relative_path TEXT)")

        user_exts = {e.strip().lower() for e in cfg['extensions'].split(',')}
        path_stack = {}
        scan_results = []
        
        # 优化点：获取文件行数以便计算百分比，但不读取内容
        total_lines = sum(1 for _ in open(TREE_FILE, 'r', encoding='utf-8', errors='ignore'))
        
        await update_progress("正在解析", 20, "流式读取目录结构...")
        with open(TREE_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f):
                level = line.count('|')
                clean_name = re.sub(r'^[|\s—-]+', '', line).strip()
                if not clean_name: continue
                path_stack[level] = clean_name
                if '.' in clean_name and clean_name.split('.')[-1].lower() in user_exts:
                    full_parts = [path_stack[l] for l in range(level + 1) if l in path_stack]
                    rel_parts = full_parts[int(cfg.get('exclude_levels', 2)):]
                    if rel_parts:
                        scan_results.append("/".join(rel_parts))
                
                if i % 3000 == 0:
                    await update_progress("解析中", 20 + (i/total_lines*20), f"已处理 {i} 行...")

        total_files = len(scan_results)
        await write_log(f"📋 结构解析完成，待处理媒体: {total_files}")

        for i, r_path in enumerate(scan_results):
            target = os.path.join("/app/strm", r_path + ".strm")
            if not os.path.exists(target) or cfg['sync_mode'] == "full" or force_full:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                encoded = urllib.parse.quote(f"/{cfg['mount_path'].strip('/')}/{r_path}")
                with open(target, 'w') as sf:
                    sf.write(f"{cfg['alist_url'].rstrip('/')}/d{encoded}")
            
            path_h = hashlib.md5(r_path.encode()).hexdigest()
            cursor.execute("INSERT OR IGNORE INTO current_scan VALUES (?, ?)", (path_h, r_path))
            
            if i % 500 == 0:
                await update_progress("生成文件", 40 + (i/total_files*50), f"进度: {i}/{total_files}")

        if cfg['sync_clean']:
            await update_progress("同步清理", 95, "正在比对并删除失效STRM...")
            cursor.execute("SELECT relative_path FROM local_files WHERE path_hash NOT IN (SELECT path_hash FROM current_scan)")
            for (d_path,) in cursor.fetchall():
                p = os.path.join("/app/strm", d_path + ".strm")
                if os.path.exists(p): os.remove(p)
            cursor.execute("DELETE FROM local_files WHERE path_hash NOT IN (SELECT path_hash FROM current_scan)")

        cursor.execute("INSERT OR REPLACE INTO local_files SELECT * FROM current_scan")
        conn.commit()
        conn.close()
        await update_progress("任务完成", 100, f"同步成功，累计管理 {total_files} 个文件")
        await write_log("✅ 所有操作已圆满完成")
    except Exception as e:
        await write_log(f"❌ 运行故障: {str(e)}")
        await update_progress("任务中止", 0, "错误详见日志")
    finally:
        task_status["running"] = False

@app.on_event("startup")
async def startup():
    async def scheduler():
        await asyncio.sleep(5)
        last_run = time.time()
        while True:
            cfg = get_config()
            interval = cfg.get('cron_hour')
            if interval and str(interval).isdigit():
                interval_min = int(interval)
                next_ts = last_run + (interval_min * 60)
                task_status["next_run"] = datetime.fromtimestamp(next_ts).strftime("%H:%M:%S")
                if time.time() >= next_ts and not task_status["running"]:
                    last_run = time.time()
                    asyncio.create_task(run_sync())
            else:
                task_status["next_run"] = None
            await asyncio.sleep(5)
    asyncio.create_task(scheduler())

@app.get("/")
async def index(request: Request):
    if not request.session.get("logged_in"): return RedirectResponse("/login")
    with open("app/templates/index.html") as f: return HTMLResponse(f.read())

@app.get("/get_settings")
async def gs(): return get_config()

@app.post("/save_settings")
async def ss(data: dict):
    cfg = get_config()
    cfg.update(data)
    with open(CONFIG_PATH, 'w') as f: json.dump(cfg, f)
    return {"ok": True}

@app.post("/start")
async def st(data: dict, bt: BackgroundTasks):
    if not task_status["running"]:
        bt.add_task(run_sync, use_local=data.get("use_local", False), force_full=data.get("force_full", False))
        return {"status": "started"}
    return {"status": "busy"}

@app.get("/logs")
async def lg(): return task_status

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")

@app.get("/login", response_class=HTMLResponse)
async def login_p():
    return """<body style="background:#0f172a;color:white;display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif;">
    <form action="/login" method="post" style="background:#1e293b;padding:2rem;border-radius:1rem;width:320px;box-shadow:0 10px 25px rgba(0,0,0,0.5);">
    <h2 style="text-align:center;color:#38bdf8;font-weight:bold;font-size:1.5rem;margin-bottom:1.5rem;">115-STRM 系统</h2>
    <input name="username" placeholder="用户名" style="display:block;margin:1rem 0;padding:0.8rem;width:100%;border-radius:0.5rem;background:#334155;color:white;border:1px solid #475569;outline:none;">
    <input name="password" type="password" placeholder="密码" style="display:block;margin:1rem 0;padding:0.8rem;width:100%;border-radius:0.5rem;background:#334155;color:white;border:1px solid #475569;outline:none;">
    <button style="width:100%;padding:0.8rem;background:#0284c7;color:white;border:none;border-radius:0.5rem;cursor:pointer;font-weight:bold;transition:background 0.3s;" onmouseover="this.style.background='#0369a1'" onmouseout="this.style.background='#0284c7'">进入控制台</button>
    </form></body>"""

@app.post("/login")
async def do_l(request: Request):
    form = await request.form()
    cfg = get_config()
    if form.get("username") == cfg['username'] and form.get("password") == cfg['password']:
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=302)
    return RedirectResponse("/login")

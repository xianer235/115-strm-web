import os, json, sqlite3, subprocess, urllib.parse, asyncio, re, hashlib, time
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
    "logs": ["系统已就绪"], 
    "progress": {"step": "空闲", "percent": 0, "detail": "等待指令"}
}

async def update_progress(step, percent, detail):
    task_status["progress"].update({"step": step, "percent": int(percent), "detail": detail})

async def write_log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    task_status["logs"].append(f"[{ts}] {msg}")
    if len(task_status["logs"]) > 500: task_status["logs"].pop(0)

async def run_sync(use_local=False, force_full=False):
    task_status["running"] = True
    cfg = get_config()
    try:
        # --- 阶段 1: 准备/下载 (权重 0-5%) ---
        if not use_local:
            await update_progress("下载中", 2, "正在从服务器拉取最新目录树...")
            curl_cmd = ['curl', '-fL', cfg['tree_url'], '-o', RAW_TEMP]
            if cfg['alist_user'] and cfg['alist_pass']:
                curl_cmd += ['-u', f"{cfg['alist_user']}:{cfg['alist_pass']}"]
            
            if subprocess.run(curl_cmd).returncode != 0: raise Exception("目录树下载失败，请检查设置")
            subprocess.run(['iconv', '-f', 'UTF-16LE', '-t', 'UTF-8//IGNORE', RAW_TEMP, '-o', TREE_FILE])
            
            new_hash = hashlib.md5(open(TREE_FILE, 'rb').read()).hexdigest()
            if cfg.get('check_hash') and new_hash == cfg.get('last_hash') and not force_full:
                await write_log("✨ MD5校验一致，内容无变动，同步取消")
                await update_progress("已完成", 100, "目录树无变化，无需更新")
                return
            cfg['last_hash'] = new_hash
            with open(CONFIG_PATH, 'w') as f: json.dump(cfg, f)
        await update_progress("下载中", 5, "下载完成，准备解析")

        # --- 阶段 2: 解析路径 (权重 5-30%) ---
        await update_progress("解析中", 6, "正在重构目录层级...")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS local_files (path_hash TEXT PRIMARY KEY, relative_path TEXT)")
        cursor.execute("CREATE TEMPORARY TABLE current_scan (path_hash TEXT PRIMARY KEY, relative_path TEXT)")

        user_exts = {e.strip().lower() for e in cfg['extensions'].split(',')}
        path_stack = {}
        scan_results = []
        
        with open(TREE_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            total_l = len(lines)
            for i, line in enumerate(lines):
                level = line.count('|')
                clean_name = re.sub(r'^[|\s—-]+', '', line).strip()
                if not clean_name: continue
                path_stack[level] = clean_name
                
                if '.' in clean_name and clean_name.split('.')[-1].lower() in user_exts:
                    full_parts = [path_stack[l] for l in range(level + 1) if l in path_stack]
                    skip = int(cfg.get('exclude_levels', 2))
                    rel_parts = full_parts[skip:]
                    if rel_parts:
                        rel_path = "/".join(rel_parts)
                        scan_results.append(rel_path)
                
                if i % 1000 == 0:
                    p = 5 + (i / total_l * 25)
                    await update_progress("解析中", p, f"已解析 {i}/{total_l} 行")
                    await asyncio.sleep(0.001)

        # --- 阶段 3: 生成 STRM (权重 30-90%) ---
        total_files = len(scan_results)
        await write_log(f"🔍 扫描到目标文件: {total_files}")
        gen_count = 0
        for i, r_path in enumerate(scan_results):
            target = os.path.join("/app/strm", r_path + ".strm")
            if not os.path.exists(target) or cfg['sync_mode'] == "full" or force_full:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                encoded = urllib.parse.quote(f"/{cfg['mount_path'].strip('/')}/{r_path}")
                with open(target, 'w') as sf:
                    sf.write(f"{cfg['alist_url'].rstrip('/')}/d{encoded}")
                gen_count += 1
            
            # 记录到临时表
            path_h = hashlib.md5(r_path.encode()).hexdigest()
            cursor.execute("INSERT OR IGNORE INTO current_scan VALUES (?, ?)", (path_h, r_path))

            if i % 200 == 0 or i == total_files - 1:
                p = 30 + (i / total_files * 60)
                await update_progress("生成中", p, f"正在生成 STRM: {i}/{total_files}")
                await asyncio.sleep(0.001)

        # --- 阶段 4: 清理与提交 (权重 90-100%) ---
        await update_progress("清理中", 92, "正在检查失效文件...")
        clean_count = 0
        if cfg['sync_clean']:
            cursor.execute("SELECT relative_path FROM local_files WHERE path_hash NOT IN (SELECT path_hash FROM current_scan)")
            to_del = cursor.fetchall()
            for (d_path,) in to_del:
                p = os.path.join("/app/strm", d_path + ".strm")
                if os.path.exists(p): os.remove(p); clean_count += 1
            cursor.execute("DELETE FROM local_files WHERE path_hash NOT IN (SELECT path_hash FROM current_scan)")

        await update_progress("提交中", 98, "正在写入索引数据库...")
        cursor.execute("INSERT OR REPLACE INTO local_files SELECT * FROM current_scan")
        conn.commit()
        conn.close()
        
        await write_log(f"✅ 同步圆满完成！新增/更新: {gen_count}, 清理: {clean_count}")
        await update_progress("已完成", 100, f"同步成功: 处理了 {total_files} 个文件")

    except Exception as e:
        await write_log(f"❌ 运行报错: {str(e)}")
        await update_progress("错误", 0, str(e))
    finally:
        task_status["running"] = False

@app.on_event("startup")
async def startup():
    async def scheduler():
        while True:
            cfg = get_config()
            if cfg.get('cron_hour') and str(datetime.now().hour) == str(cfg['cron_hour']) and datetime.now().minute == 0:
                await run_sync()
            await asyncio.sleep(60)
    asyncio.create_task(scheduler())

@app.get("/")
async def index(request: Request):
    if not request.session.get("logged_in"): return RedirectResponse("/login")
    with open("app/templates/index.html") as f: return HTMLResponse(f.read())

@app.get("/get_settings")
async def gs(): return get_config()

@app.post("/save_settings")
async def ss(data: dict):
    cfg = get_config(); cfg.update(data)
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

@app.get("/login", response_class=HTMLResponse)
async def login_p():
    return """<body style="background:#0f172a;color:white;display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif;">
    <form action="/login" method="post" style="background:#1e293b;padding:2rem;border-radius:1rem;width:320px;box-shadow:0 25px 50px -12px rgba(0,0,0,0.5);">
    <h2 style="text-align:center;color:#38bdf8;margin-bottom:1.5rem;font-weight:bold;">115-STRM 登录</h2>
    <input name="username" placeholder="用户名" style="display:block;margin:1rem 0;padding:0.8rem;width:100%;border-radius:0.5rem;background:#334155;color:white;border:none;">
    <input name="password" type="password" placeholder="密码" style="display:block;margin:1rem 0;padding:0.8rem;width:100%;border-radius:0.5rem;background:#334155;color:white;border:none;">
    <button style="width:100%;padding:0.8rem;background:#0284c7;color:white;border:none;border-radius:0.5rem;font-weight:bold;cursor:pointer;">进入系统</button>
    </form></body>"""

@app.post("/login")
async def do_l(request: Request):
    form = await request.form(); cfg = get_config()
    if form.get("username") == cfg['username'] and form.get("password") == cfg['password']:
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=302)
    return RedirectResponse("/login")

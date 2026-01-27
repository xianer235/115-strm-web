import os, json, sqlite3, asyncio, re, hashlib, time, urllib.parse
from fastapi import FastAPI, Request, BackgroundTasks,Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from datetime import datetime

app = FastAPI()

app.add_middleware(SessionMiddleware, secret_key="115-strm-v7-final")

def check_login(request: Request):
    return request.session.get("logged_in") == True

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
    await asyncio.sleep(0)

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
            await update_progress("正在下载", 0, "正在连接服务器...")
            curl_args = ['-fL', cfg['tree_url'], '-o', RAW_TEMP]
            if cfg['alist_user'] and cfg['alist_pass']:
                curl_args += ['-u', f"{cfg['alist_user']}:{cfg['alist_pass']}"]
            
            proc = await asyncio.create_subprocess_exec(
                'curl', *curl_args,
                stderr=asyncio.subprocess.PIPE
            )

            # 改进：更健壮的 curl 进度解析逻辑
            last_p = 0
            while True:
                # curl 进度通常以 \r 结尾
                line_bytes = await proc.stderr.read(512)
                if not line_bytes: break
                
                output = line_bytes.decode('utf-8', errors='ignore')
                
                # 匹配百分比、总大小、已下载大小
                # 兼容多种格式: " 10 50.5M" 或 "100 391k"
                p_match = re.search(r'(\d+)\s+([\d\.]+[kMGbB]?)\s+(\d+)\s+([\d\.]+[kMGbB]?)', output)
                
                if p_match:
                    try:
                        p_val = int(p_match.group(1))
                        total_sz = p_match.group(2)
                        recv_sz = p_match.group(4)
                        
                        # 只有当进度真的变化时才更新，防止跳回 0
                        if p_val >= last_p:
                            last_p = p_val
                            await update_progress("正在下载", p_val * 0.15, f"进度: {recv_sz} / {total_sz} ({p_val}%)")
                    except:
                        continue

            await proc.wait()
            if proc.returncode != 0: raise Exception("下载失败，请检查网络或URL")

            await update_progress("正在转码", 15, "转换字符集 (UTF-16 -> UTF-8)...")
            proc = await asyncio.create_subprocess_exec('iconv', '-f', 'UTF-16LE', '-t', 'UTF-8//IGNORE', RAW_TEMP, '-o', TREE_FILE)
            await proc.wait()

            new_hash = hashlib.md5(open(TREE_FILE, 'rb').read()).hexdigest()
            if cfg.get('check_hash') and new_hash == cfg.get('last_hash') and not force_full:
                await write_log("✨ MD5一致，无需更新")
                await update_progress("已完成", 100, "目录树无变化")
                return
            cfg['last_hash'] = new_hash
            with open(CONFIG_PATH, 'w') as f: json.dump(cfg, f)

        await update_progress("准备解析", 18, "统计文件规模...")
        total_lines = sum(1 for _ in open(TREE_FILE, 'r', encoding='utf-8', errors='ignore'))
        
        path_stack = {}
        scan_results = []
        user_exts = {e.strip().lower() for e in cfg['extensions'].replace('，', ',').split(',')}

        with open(TREE_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f):
                level = line.count('|')
                clean_name = re.sub(r'^[|\s—-]+', '', line).strip()
                if not clean_name: continue
                path_stack[level] = clean_name
                if '.' in clean_name and clean_name.split('.')[-1].lower() in user_exts:
                    full_parts = [path_stack[l] for l in range(level + 1) if l in path_stack]
                    rel_parts = full_parts[int(cfg.get('exclude_levels', 2)):]
                    if rel_parts: scan_results.append("/".join(rel_parts))
                
                if i % 3000 == 0:
                    await update_progress("解析结构", 20 + (i/total_lines*20), f"已扫描 {i} 行")

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS local_files (path_hash TEXT PRIMARY KEY, relative_path TEXT)")
        cursor.execute("CREATE TEMPORARY TABLE current_scan (path_hash TEXT PRIMARY KEY, relative_path TEXT)")

        total_files = len(scan_results)
        for i, r_path in enumerate(scan_results):
            target = os.path.join("/app/strm", r_path + ".strm")
            if not os.path.exists(target) or cfg['sync_mode'] == "full" or force_full:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                encoded = urllib.parse.quote(f"/{cfg['mount_path'].strip('/')}/{r_path}")
                with open(target, 'w') as sf: sf.write(f"{cfg['alist_url'].rstrip('/')}/d{encoded}")
            
            path_h = hashlib.md5(r_path.encode()).hexdigest()
            cursor.execute("INSERT OR IGNORE INTO current_scan VALUES (?, ?)", (path_h, r_path))
            
            if i % 500 == 0:
                await update_progress("生成STRM", 40 + (i/total_files*50), f"处理中: {i}/{total_files}")

        if cfg['sync_clean']:
            await update_progress("清理失效", 95, "正在移除多余文件...")
            cursor.execute("SELECT relative_path FROM local_files WHERE path_hash NOT IN (SELECT path_hash FROM current_scan)")
            for (d_path,) in cursor.fetchall():
                p = os.path.join("/app/strm", d_path + ".strm")
                if os.path.exists(p): os.remove(p)
            cursor.execute("DELETE FROM local_files WHERE path_hash NOT IN (SELECT path_hash FROM current_scan)")

        cursor.execute("INSERT OR REPLACE INTO local_files SELECT * FROM current_scan")
        conn.commit()
        conn.close()
        await update_progress("任务完成", 100, f"同步成功: {total_files} 文件")
        await write_log("✅ 任务结束")
    except Exception as e:
        await write_log(f"❌ 运行故障: {str(e)}")
        await update_progress("任务中止", 0, str(e))
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
            else: task_status["next_run"] = None
            await asyncio.sleep(5)
    asyncio.create_task(scheduler())

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not check_login(request):
        return RedirectResponse(url="/login")
    
    # 增加健壮性检查
    path = "templates/index.html"
    if not os.path.exists(path):
        return HTMLResponse(f"<h3>错误：找不到模板文件 {path}</h3>", status_code=404)
        
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

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
    path = "templates/login.html"
    if not os.path.exists(path):
        return HTMLResponse(f"<h3>错误：找不到登录模板 {path}</h3>", status_code=404)
        
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.post("/login")
async def do_l(request: Request):
    form = await request.form()
    cfg = get_config()
    # 确保从 Form 中获取数据
    u = form.get("username")
    p = form.get("password")
    
    if u == cfg.get('username') and p == cfg.get('password'):
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=303) # 建议使用 303 防止表单重提
    return RedirectResponse("/login")
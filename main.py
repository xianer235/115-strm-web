import os, json, sqlite3, asyncio, re, hashlib, time, urllib.parse
from fastapi import FastAPI, Request, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from datetime import datetime

app = FastAPI()

app.add_middleware(
    SessionMiddleware, 
    secret_key="115-strm-v7-final-distinguished",
    https_only=False,
    same_site="lax"
)

# 路径定义
CONFIG_PATH = "/app/config/settings.json"
DB_PATH = "/app/config/data.db"
TREE_FILE = "/app/config/目录树.txt"  # 最终合并解析用的文件

# 区分命名的文件路径
RAW_1 = "/app/config/tree1.raw"
RAW_2 = "/app/config/tree2.raw"
TXT_1 = "/app/config/tree1.txt"
TXT_2 = "/app/config/tree2.txt"

def get_config():
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        default = {
            "username": "admin", "password": "admin123",
            "alist_url": "", "alist_user": "", "alist_pass": "",
            "tree_url": "", "tree_url_2": "", "mount_path": "/115", "exclude_levels": 2,
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
        url1 = cfg.get('tree_url')
        url2 = cfg.get('tree_url_2')

        if not use_local:
            if not url1 and not url2:
                raise Exception("未配置任何目录树 URL")

            # 下载任务
            dl_tasks = [
                ("目录树1", url1, RAW_1, 0),
                ("目录树2", url2, RAW_2, 7.5)
            ]
            
            for name, url, save_path, base_p in dl_tasks:
                if not url:
                    if os.path.exists(save_path): os.remove(save_path)
                    continue
                
                await update_progress("正在下载", base_p, f"正在获取 {name}...")
                curl_args = ['-fL', url, '-o', save_path]
                if cfg['alist_user'] and cfg['alist_pass']:
                    curl_args += ['-u', f"{cfg['alist_user']}:{cfg['alist_pass']}"]
                
                proc = await asyncio.create_subprocess_exec('curl', *curl_args, stderr=asyncio.subprocess.PIPE)
                while True:
                    line_bytes = await proc.stderr.read(512)
                    if not line_bytes: break
                    output = line_bytes.decode('utf-8', errors='ignore')
                    p_match = re.search(r'(\d+)\s+([\d\.]+[kMGbB]?)\s+(\d+)\s+([\d\.]+[kMGbB]?)', output)
                    if p_match:
                        p_val = int(p_match.group(1))
                        await update_progress("正在下载", base_p + (p_val * 0.075), f"{name}: {p_match.group(4)} / {p_match.group(2)}")
                await proc.wait()
                if proc.returncode != 0: raise Exception(f"{name} 下载失败")

        # --- 转码与区分处理 ---
        await update_progress("正在转码", 15, "正在转换字符集...")
        valid_txts = []

        # 转码 1
        if os.path.exists(RAW_1):
            p1 = await asyncio.create_subprocess_exec('iconv', '-f', 'UTF-16LE', '-t', 'UTF-8//IGNORE', RAW_1, '-o', TXT_1)
            await p1.wait()
            if os.path.exists(TXT_1): valid_txts.append(TXT_1)

        # 转码 2
        if os.path.exists(RAW_2):
            p2 = await asyncio.create_subprocess_exec('iconv', '-f', 'UTF-16LE', '-t', 'UTF-8//IGNORE', RAW_2, '-o', TXT_2)
            await p2.wait()
            if os.path.exists(TXT_2): valid_txts.append(TXT_2)

        # --- 合并阶段 ---
        if not valid_txts:
            raise Exception("转码后无有效文件，请检查 URL 配置或原始文件编码")
        
        await update_progress("正在合并", 17, "生成最终名单...")
        with open(TREE_FILE, 'w', encoding='utf-8') as outfile:
            for i, fname in enumerate(valid_txts):
                with open(fname, 'r', encoding='utf-8') as infile:
                    outfile.write(infile.read())
                    if i < len(valid_txts) - 1: outfile.write("\n") # 只有在中间添加换行
        await write_log(f"已成功合并 {len(valid_txts)} 个目录树文件")

        # --- MD5 校验 ---
        new_hash = hashlib.md5(open(TREE_FILE, 'rb').read()).hexdigest()
        if cfg.get('check_hash') and new_hash == cfg.get('last_hash') and not force_full:
            await write_log("✨ 内容无变化，跳过同步")
            await update_progress("已完成", 100, "无需更新")
            return
        cfg['last_hash'] = new_hash
        with open(CONFIG_PATH, 'w') as f: json.dump(cfg, f)

        # --- 解析逻辑 ---
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

        # --- 生成 STRM ---
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

        # --- 自动清理 ---
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

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("logged_in"):
        return RedirectResponse(url="/", status_code=303)
    path = "templates/login.html"
    with open(path, "r", encoding="utf-8") as f: return f.read()

@app.post("/login")
async def do_login(request: Request):
    data = await request.json()
    cfg = get_config()
    if data.get("username") == cfg.get('username') and data.get("password") == cfg.get('password'):
        request.session["logged_in"] = True
        return {"ok": True}
    return JSONResponse(status_code=401, content={"ok": False, "msg": "密码错误"})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not request.session.get("logged_in"): return RedirectResponse(url="/login", status_code=303)
    path = "templates/index.html"
    with open(path, "r", encoding="utf-8") as f: return f.read()

@app.get("/get_settings")
async def gs(request: Request): 
    if not request.session.get("logged_in"): return JSONResponse(status_code=401, content={"err": 1})
    return get_config()

@app.post("/save_settings")
async def ss(request: Request, data: dict):
    if not request.session.get("logged_in"): return JSONResponse(status_code=401, content={"err": 1})
    cfg = get_config()
    cfg.update(data)
    with open(CONFIG_PATH, 'w') as f: json.dump(cfg, f)
    return {"ok": True}

@app.post("/start")
async def st(request: Request, data: dict, bt: BackgroundTasks):
    if not request.session.get("logged_in"): return JSONResponse(status_code=401, content={"err": 1})
    if not task_status["running"]:
        bt.add_task(run_sync, use_local=data.get("use_local", False), force_full=data.get("force_full", False))
        return {"status": "started"}
    return {"status": "busy"}

@app.get("/logs")
async def lg(request: Request): 
    if not request.session.get("logged_in"): return JSONResponse(status_code=401, content={"err": 1})
    return task_status

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
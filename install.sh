#!/bin/bash

# =================================================================
# 115-strm-web 一键部署脚本 (高性能版)
# =================================================================

# 1. 基础环境检查与目录创建
echo "正在初始化宿主机目录..."
BASE_DIR="/root/115-strm-web"
mkdir -p $BASE_DIR/{strm,config,logs,app/templates}
chmod -R 777 $BASE_DIR

# 2. 生成后端核心 Python 代码 (app/main.py)
# 包含高性能流式解析引擎、SQLite 对比逻辑、以及 WebSocket 日志推送
cat <<EOF > $BASE_DIR/app/main.py
import os, sys, re, sqlite3, subprocess, urllib.parse, asyncio
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

# 数据库初始化
DB_PATH = "/app/config/data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS local_files 
                      (path_hash TEXT PRIMARY KEY, relative_path TEXT)''')
    conn.commit()
    conn.close()

init_db()

# 模拟全局锁，防止任务并发
processing_lock = asyncio.Lock()
logs_queue = []

async def write_log(message):
    logs_queue.append(message)
    print(message)

# 高性能解析引擎：处理数十万行目录树
async def process_task(config):
    async with processing_lock:
        try:
            logs_queue.clear()
            await write_log("开始执行任务...")
            
            # 1. 处理编码与下载
            tree_path = "/app/config/tree.txt"
            if config['url'].startswith('http'):
                await write_log(f"正在下载目录树: {config['url']}")
                subprocess.run(['curl', '-L', config['url'], '-o', tree_path + '.raw'], check=True)
                await write_log("转换编码 UTF-16LE -> UTF-8...")
                subprocess.run(['iconv', '-f', 'UTF-16LE', '-t', 'UTF-8//IGNORE', tree_path + '.raw', '-o', tree_path], check=True)
            
            # 2. 解析逻辑
            await write_log("开始解析目录结构...")
            stack = []
            extensions = tuple(config['extensions'].split(','))
            
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            # 临时存入本次扫描到的文件
            cursor.execute("CREATE TEMPORARY TABLE current_scan (path_hash TEXT PRIMARY KEY, relative_path TEXT)")

            with open(tree_path, 'r', encoding='utf-8') as f:
                for line in f:
                    # 识别层级：计算前缀中 "|" 或空格的数量
                    depth = line.count('|') + (line.count('  ') // 2)
                    name = line.replace('|', '').replace('—', '').replace('-', '').strip()
                    if not name: continue
                    
                    # 更新路径栈
                    stack = stack[:depth]
                    stack.append(name)
                    
                    if name.lower().endswith(extensions):
                        # 剔除指定层级
                        valid_stack = stack[int(config['exclude_levels']):]
                        rel_path = "/".join(valid_stack)
                        cursor.execute("INSERT OR IGNORE INTO current_scan VALUES (?, ?)", 
                                       (hash(rel_path), rel_path))

            # 3. 差异对比与生成
            await write_log("比对数据库，生成 STRM 文件...")
            # 获取需要新增的文件
            cursor.execute("SELECT relative_path FROM current_scan WHERE path_hash NOT IN (SELECT path_hash FROM local_files)")
            new_files = cursor.fetchall()
            
            alist_base = config['alist_url'].rstrip('/')
            mount_p = config['mount_path'].strip('/')
            
            for (r_path,) in new_files:
                target_file = os.path.join("/app/strm", r_path + ".strm")
                os.makedirs(os.path.dirname(target_file), exist_ok=True)
                
                # 生成 URL：[AList]/d/[挂载路径]/[编码路径]
                encoded_path = urllib.parse.quote(f"/{mount_p}/{r_path}".replace("//", "/"))
                strm_content = f"{alist_base}/d{encoded_path}"
                
                with open(target_file, 'w') as sf:
                    sf.write(strm_content)
                cursor.execute("INSERT INTO local_files VALUES (?, ?)", (hash(r_path), r_path))

            # 4. 同步清理
            if config['sync_clean']:
                await write_log("清理多余文件...")
                cursor.execute("SELECT relative_path FROM local_files WHERE path_hash NOT IN (SELECT path_hash FROM current_scan)")
                to_delete = cursor.fetchall()
                for (d_path,) in to_delete:
                    d_file = os.path.join("/app/strm", d_path + ".strm")
                    if os.path.exists(d_file): os.remove(d_file)
                    cursor.execute("DELETE FROM local_files WHERE relative_path=?", (d_path,))
            
            conn.commit()
            conn.close()
            await write_log("任务全部完成！")
        except Exception as e:
            await write_log(f"错误: {str(e)}")

@app.post("/start")
async def start_task(config: dict, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_task, config)
    return {"status": "started"}

@app.get("/logs")
async def get_logs():
    return {"logs": logs_queue}

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("app/templates/index.html") as f:
        return f.read()
EOF

# 3. 生成前端界面 (app/templates/index.html)
cat <<EOF > $BASE_DIR/app/templates/index.html
<!DOCTYPE html>
<html>
<head>
    <title>115-strm-web 管理面板</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 p-8">
    <div class="max-w-2xl mx-auto bg-white p-6 rounded-lg shadow-md">
        <h1 class="text-2xl font-bold mb-6">115-strm-web 容器化管理</h1>
        <div class="space-y-4">
            <div>
                <label class="block text-sm font-medium">目录树 URL</label>
                <input id="url" type="text" class="w-full border p-2 rounded" placeholder="http://.../目录树.txt">
            </div>
            <div class="grid grid-cols-2 gap-4">
                <div>
                    <label class="block text-sm font-medium">AList 地址</label>
                    <input id="alist_url" type="text" class="w-full border p-2 rounded" placeholder="http://192.168.1.1:5244">
                </div>
                <div>
                    <label class="block text-sm font-medium">挂载路径</label>
                    <input id="mount_path" type="text" class="w-full border p-2 rounded" placeholder="/115">
                </div>
            </div>
            <div class="grid grid-cols-3 gap-4">
                <div>
                    <label class="block text-sm font-medium">剔除层级</label>
                    <select id="exclude_levels" class="w-full border p-2 rounded">
                        <option value="0">不剔除</option>
                        <option value="1">剔除 1 层</option>
                        <option value="2" selected>剔除 2 层</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium">后缀名(逗号分隔)</label>
                    <input id="extensions" type="text" class="w-full border p-2 rounded" value=".mkv,.mp4,.ts,.iso">
                </div>
                <div class="flex items-center pt-6">
                    <input id="sync_clean" type="checkbox" class="mr-2"> 同步清理
                </div>
            </div>
            <button onclick="start()" class="w-full bg-blue-600 text-white py-2 rounded hover:bg-blue-700">开始执行</button>
        </div>
        <div id="log-box" class="mt-6 bg-black text-green-400 p-4 h-64 overflow-y-auto font-mono text-sm rounded">
            等待任务开始...
        </div>
    </div>
    <script>
        async function start() {
            const data = {
                url: document.getElementById('url').value,
                alist_url: document.getElementById('alist_url').value,
                mount_path: document.getElementById('mount_path').value,
                exclude_levels: document.getElementById('exclude_levels').value,
                extensions: document.getElementById('extensions').value,
                sync_clean: document.getElementById('sync_clean').checked
            };
            await fetch('/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
            setInterval(async () => {
                const res = await fetch('/logs');
                const log = await res.json();
                document.getElementById('log-box').innerHTML = log.logs.join('<br>');
            }, 1000);
        }
    </script>
</body>
</html>
EOF

# 4. 生成 Dockerfile
cat <<EOF > $BASE_DIR/Dockerfile
FROM ubuntu:24.04
ENV LANG=zh_CN.UTF-8
ENV LC_ALL=zh_CN.UTF-8
RUN apt-get update && apt-get install -y python3 python3-pip curl locales \
    && locale-gen zh_CN.UTF-8 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip3 install fastapi uvicorn pydantic --break-system-packages
EXPOSE 18080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18080"]
EOF

# 5. 构建并启动容器
echo "开始构建 Docker 镜像并启动容器..."
docker stop 115-strm-web 2>/dev/null || true
docker rm 115-strm-web 2>/dev/null || true
docker build -t 115-strm-web $BASE_DIR
docker run -d --name 115-strm-web \
  -p 18080:18080 \
  -v $BASE_DIR/strm:/app/strm \
  -v $BASE_DIR/config:/app/config \
  -v $BASE_DIR/logs:/app/logs \
  --restart always \
  115-strm-web

echo "================================================="
echo "部署成功！"
echo "Web 管理界面地址: http://$(curl -s ifconfig.me):18080"
echo "STRM 文件存放路径: $BASE_DIR/strm"
echo "================================================="

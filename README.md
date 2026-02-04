115-STRM-WEB (多目录树增强版)

本项目在[suixing8/115-strm](https://github.com/suixing8/115-strm)核心解析逻辑的基础上，进行了全方位的架构升级。使用GEMINI协助开发，通过 FastAPI 构建了可视化的 Web 管理后台，并引入了双目录树合并机制，专为拥有海量资源的 115 网盘用户设计。

核心功能：1.基于115网盘目录树文件生成本地strm文件，配合openlist可实现视频302播放1.生成速度极快，每秒生成数千近万个strm 文件；2.支持自定义过滤文件类型； 3.支持openlist登录下载目录树； 4.支持定时任务、增/全量更新量、目录树文件哈希校验判断是否跳过更新、清理失效文件；5.支持WEB管理，这样可以方便修改与触发手动更新，而且进度条还很丝滑；6.最新版支持两个目录树（可选），可以大库（存的大包）、小库（新增加、连载更新）分开生成目录树，这样可以在更新新内容时减少目录树生成的等待时间；


🌟 核心功能详解
1. 丝滑的 Web 交互体验实时进度条：不同于传统的后台脚本，本项目通过 WebSocket 实时推送进度，你可以清晰看到“下载 -> 转码 -> 合并 -> 解析 -> 生成”的每一个阶段及具体文件数。配置即时生效：所有参数（AList 链接、URL、过滤后缀等）均可在网页端修改并保存，无需手动编辑 json 文件。
2. 双目录树智能合并 (冷热分离)多源同步：支持同时填入两个目录树下载链接。场景方案：URL 1 (冷库)：对应网盘中长期不动的庞大影视库（如“电影总库”）。URL 2 (热库)：对应网盘中频繁更新的小文件夹（如“正在追剧”）。性能优势：你只需频繁更新体积微小的“热库”目录树，工具会自动将其与“冷库”数据合并，极大节省 AList 导出及数据解析的时间开销。
3. 高级同步策略哈希校验 (MD5)：程序会比对合并后的 目录树.txt 哈希值。如果网盘内容没有变动，程序将自动跳过解析，节省系统资源。自动清理 (失效移除)：开启后，若网盘删除了某个文件，本地对应的 .strm 文件会在下次同步时被物理删除，确保本地库与云端实时一致。

📂 内部处理流程与文件规范
为了方便进阶用户排查问题，config 挂载目录下采用如下处理链路：阶段文件名编码格式作用说明下载原始态tree1.raw / tree2.rawUTF-16LEcurl 下载后的原始字节流，直接查看通常为乱码。转码中间态tree1.txt / tree2.txtUTF-8经过 iconv 处理后的中文文本，可直接打开检查内容。最终解析态目录树.txtUTF-8由上述 TXT 文件合并而成的总表，是生成 .strm 的唯一依据。

⚙️ 参数配置指南
AList 路径相关：AList URL: 填入 AList 的访问地址（如 http://192.168.1.5:5244）。
AList 用户/密码: 用于获取目录树文件的下载权限（Basic Auth）。
挂载路径: 115 在 AList 中的挂载位置（如 /115）。
解析逻辑相关：排除层级: 非常关键。如果目录树从“电影”文件夹开始，而该文件夹在 AList 的路径是 /115/电影，请调整排除层级（默认 1）以确保生成的 .strm 内地址完全正确。
文件后缀: 设置你想生成 strm 的文件类型，建议只包含主流视频及音频格式。
🚀 部署与使用1. Docker Compose 部署 (推荐)将以下内容保存为 docker-compose.yml，执行 docker-compose up -d：YAMLversion: '3.8'
services:
  115-strm-web:
    image: my-115-strm-web:latest
    container_name: 115-strm-web
    restart: always
    ports:
      - "18080:18080"
    volumes:
      - ./strm:/app/strm      # 生成的 strm 文件存放地
      - ./config:/app/config  # 配置文件、数据库及目录树存放地
      - ./logs:/app/logs      # 日志存放地
    environment:
      - TZ=Asia/Shanghai

📸 运行预览


<img width="846" height="902" alt="image" src="https://github.com/user-attachments/assets/141a6989-a212-4e54-b35f-db67a1ea73f2" />
<img width="2560" height="1373" alt="image" src="https://github.com/user-attachments/assets/1b4dab2c-b591-4013-8933-49bb653543ba" />
<img width="2560" height="1366" alt="image" src="https://github.com/user-attachments/assets/1e67ac9f-0b0f-4c15-aaa5-10d458d3fb48" />




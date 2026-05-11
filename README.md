# 音乐 MV 生成工具项目文档

这是一个基于 Flask 的音乐 MV 工具项目，主要用于：

- 调用 RunningHub / ComfyUI 工作流生成音乐视频
- 长音频自动分段并发生成，降低单任务超时风险
- 给已有视频添加 LRC 同步歌词字幕
- 提供用户登录、管理员后台、账号管理和使用记录统计

当前主入口文件是 `app.py`，默认运行在 `http://127.0.0.1:5000`。

## 1. 核心功能

### 1.1 用户登录与数据隔离

项目内置 SQLite 用户体系。

普通用户登录后才能访问：

- 首页
- 歌词字幕工具
- RunningHub 视频生成器
- 视频生成 API
- 任务状态查询 API
- 本地结果预览与下载

数据隔离规则：

- 使用记录会绑定当前登录用户名
- 普通用户只能查询自己创建的分段任务
- 普通用户只能访问自己生成的本地视频结果
- 管理员可以查看全部用户和全部使用记录

### 1.2 管理员后台

管理员可以访问后台：

- 查看谁使用了哪个功能
- 查看功能调用次数
- 查看最近调用记录
- 管理用户账号
- 新增账号
- 重置密码
- 切换普通用户 / 管理员角色
- 启用 / 禁用账号
- 删除账号

保护规则：

- 不能禁用当前登录管理员自己
- 不能取消自己的管理员权限
- 不能删除当前登录账号
- 至少保留一个启用的管理员账号

### 1.3 RunningHub 单段视频生成

用户上传：

- 参考图片
- 完整音频
- RunningHub API Key
- Workflow ID
- 图片节点 ID
- 音频节点 ID

后端会：

1. 上传图片到 RunningHub
2. 上传音频到 RunningHub
3. 写入对应工作流节点
4. 创建 RunningHub 任务
5. 前端轮询任务结果

### 1.4 自动分段 MV 生成

用于较长音频，例如 3 分钟左右的歌曲。

流程：

1. 上传图片、完整音频、可选 LRC 歌词
2. 自动在音频中点附近寻找低能量 / 无歌词切点
3. 切成两段带 overlap 的音频
4. 并发创建两个 RunningHub 任务
5. 下载两段生成视频
6. 使用原始完整音频重新铺底
7. 将两段视频硬切或交叉淡化合并

优点：

- 降低单个 RunningHub 任务超时风险
- 并发运行可缩短等待时间
- 某一段失败时只需要重跑对应段

注意：

- RunningHub 计费通常按所有任务运行时长累计计算
- 并发不一定省钱，主要是减少等待和超时风险

### 1.5 LRC 歌词字幕合成

已有视频 + LRC 文件可以合成带歌词字幕的视频。

脚本：

```text
add_lyrics_to_video.py
```

页面入口：

```text
/lyrics
```

## 2. 目录结构

```text
yapie-musicMV/
├─ app.py                         # Flask 主服务
├─ split_runninghub_mv.py          # 自动分段、并发 RunningHub、视频合并脚本
├─ add_lyrics_to_video.py          # 视频添加 LRC 歌词字幕脚本
├─ video_generator_app.py          # 早期 RunningHub 调用服务，当前主入口不用它
├─ README.md                       # 当前项目说明文档
├─ templates/
│  ├─ home.html                    # 首页
│  ├─ lyrics.html                  # 歌词字幕工具页面
│  ├─ ai-video.html                # RunningHub 视频生成页面
│  ├─ login.html                   # 普通用户登录页
│  ├─ register.html                # 普通用户注册页
│  ├─ admin_login.html             # 管理员登录页
│  ├─ admin_usage.html             # 后台使用记录页
│  └─ admin_users.html             # 后台账号管理页
├─ uploads/                        # 上传文件目录，已被 .gitignore 忽略
├─ outputs/                        # 输出文件和 SQLite 数据库目录，已被 .gitignore 忽略
├─ mc/
│  ├─ 隔街的灯灭了.lrc
│  ├─ XiangJiaoChengXingLingGanTi-2.ttf
│  └─ AI唱歌数字人对口型RCM+Infinite Talk_api (1).json
├─ ruuninghub_api/
│  └─ 默认模块.md                  # RunningHub API 文档资料
├─ mv流程.txt
└─ mv制作提示词.txt
```

## 3. 运行环境

### 3.1 Python

建议使用 Python 3.10+。

项目用到的主要 Python 包：

- Flask
- requests
- moviepy
- numpy
- Pillow

如果环境缺包，可以安装：

```powershell
pip install flask requests moviepy numpy pillow
```

### 3.2 FFmpeg

项目需要 `ffmpeg` 和 `ffprobe`。

检查命令：

```powershell
ffmpeg -version
ffprobe -version
```

自动分段和视频合并依赖 FFmpeg。

## 4. 启动项目

进入项目目录：

```powershell
cd C:\Users\20859\Desktop\yapie-musicMV
```

启动 Flask：

```powershell
python app.py
```

默认地址：

```text
http://127.0.0.1:5000
```

## 5. 环境变量配置

### 5.1 管理员账号

默认管理员账号：

```text
用户名：admin
密码：admin
```

建议正式使用前设置：

```powershell
$env:ADMIN_USERNAME="admin"
$env:ADMIN_PASSWORD="你的强密码"
$env:FLASK_SECRET_KEY="一串随机密钥"
python app.py
```

说明：

- 首次启动会自动创建管理员账号
- 如果已存在同名管理员，并且设置了 `ADMIN_PASSWORD`，启动时会更新该管理员密码

### 5.2 是否允许注册

默认允许普通用户注册。

关闭注册：

```powershell
$env:ALLOW_REGISTRATION="0"
python app.py
```

### 5.3 RunningHub 代理

默认禁用系统代理，避免 RunningHub 请求走错误代理。

如需使用代理：

```powershell
$env:RUNNINGHUB_USE_PROXY="1"
python app.py
```

## 6. 页面入口

```text
/login              普通用户登录
/register           普通用户注册
/logout             退出登录
/                    首页
/lyrics             歌词字幕工具
/ai-video           RunningHub 视频生成器
/admin/login        管理员登录
/admin/usage        后台使用记录
/admin/users        后台账号管理
```

## 7. 后台管理

### 7.1 登录后台

打开：

```text
http://127.0.0.1:5000/admin/login
```

登录后访问：

```text
http://127.0.0.1:5000/admin/usage
```

### 7.2 账号管理

打开：

```text
http://127.0.0.1:5000/admin/users
```

可管理：

- 用户名
- 密码重置
- 角色
- 启用 / 禁用状态
- 使用次数
- 最近使用时间

### 7.3 使用记录

使用记录存储在：

```text
outputs/usage.db
```

表：

```text
usage_events
users
```

`outputs/` 已在 `.gitignore` 中忽略，不会提交到 Git。

## 8. RunningHub 页面使用

打开：

```text
http://127.0.0.1:5000/ai-video
```

页面有两个模式：

### 8.1 单段生成

适合短音频或不容易超时的任务。

需要填写：

- API Key
- Workflow ID
- 图片节点 ID
- 音频节点 ID

需要上传：

- 图片
- 音频

默认参数：

```text
Workflow ID: 2025258518208737281
图片节点 ID: 343
图片字段名: image
音频节点 ID: 243
音频字段名: audio
```

### 8.2 自动分段

适合较长音频。

额外支持：

- LRC 文件
- overlap 秒数
- 交叉淡化秒数
- 搜索窗口
- 最短分段秒数

默认参数：

```text
overlapSeconds: 1.0
xfadeSeconds: 0.5
searchWindow: 45
minPartSeconds: 30
```

## 9. API 接口

所有生成相关 API 都需要先登录。

如果从浏览器页面调用，会自动带 session。

如果从外部脚本调用，需要先登录拿 Cookie，再请求接口。

### 9.1 登录

```bash
curl -c cookie.txt -X POST http://127.0.0.1:5000/login \
  -d "username=你的用户名" \
  -d "password=你的密码"
```

后续请求带 Cookie：

```bash
curl -b cookie.txt ...
```

### 9.2 自动分段 MV 生成接口

```text
POST /api/mv/generate
```

`multipart/form-data` 参数：

| 参数 | 必填 | 说明 |
|---|---:|---|
| apiKey | 是 | RunningHub API Key |
| image | 是 | 图片文件 |
| audio | 是 | 音频文件 |
| lrc | 是 | LRC 文件 |
| lrcText | 否 | 如果不传 lrc 文件，可传 LRC 文本 |
| workflowId | 否 | 默认 `2025258518208737281` |
| imageNodeId | 否 | 默认 `343` |
| audioNodeId | 否 | 默认 `243` |
| nodeInfoList | 否 | 额外 RunningHub 节点 JSON 数组 |
| overlapSeconds | 否 | 默认 `1.0` |
| xfadeSeconds | 否 | 默认 `0.5` |
| searchWindow | 否 | 默认 `45` |
| minPartSeconds | 否 | 默认 `30` |

示例：

```bash
curl -b cookie.txt -X POST http://127.0.0.1:5000/api/mv/generate \
  -F "apiKey=你的RunningHubKey" \
  -F "workflowId=2025258518208737281" \
  -F "image=@cover.png" \
  -F "audio=@song.wav" \
  -F "lrc=@song.lrc"
```

返回：

```json
{
  "success": true,
  "jobId": "xxx",
  "message": "Split generation started",
  "statusUrl": "/api/mv/status/xxx",
  "legacyStatusUrl": "/api/split-job-status/xxx"
}
```

### 9.3 查询自动分段任务状态

```text
GET /api/mv/status/<jobId>
```

示例：

```bash
curl -b cookie.txt http://127.0.0.1:5000/api/mv/status/xxx
```

成功后返回：

```json
{
  "success": true,
  "status": "SUCCESS",
  "progress": 100,
  "preview_url": "/preview/xxx_split_output.mp4",
  "download_url": "/download/xxx_split_output.mp4"
}
```

### 9.4 单段 RunningHub 生成接口

```text
POST /api/generate-video
```

参数：

| 参数 | 必填 | 说明 |
|---|---:|---|
| apiKey | 是 | RunningHub API Key |
| workflowId | 是 | RunningHub Workflow ID |
| image | 是 | 图片文件 |
| audio | 是 | 音频文件 |
| imageNodeId | 否 | 图片节点 ID |
| audioNodeId | 否 | 音频节点 ID |
| nodeInfoList | 否 | 额外节点 JSON 数组 |

示例：

```bash
curl -b cookie.txt -X POST http://127.0.0.1:5000/api/generate-video \
  -F "apiKey=你的RunningHubKey" \
  -F "workflowId=2025258518208737281" \
  -F "image=@cover.png" \
  -F "audio=@song.wav"
```

### 9.5 查询单段任务状态

```text
GET /api/task-status/<taskId>?apiKey=你的RunningHubKey
```

示例：

```bash
curl -b cookie.txt "http://127.0.0.1:5000/api/task-status/任务ID?apiKey=你的RunningHubKey"
```

### 9.6 后台使用记录 API

管理员登录后调用：

```text
GET /api/admin/usage
```

示例：

```bash
curl -b admin_cookie.txt http://127.0.0.1:5000/api/admin/usage
```

## 10. 命令行分段脚本

脚本：

```text
split_runninghub_mv.py
```

只检测切点并切音频，不调用 RunningHub：

```powershell
python split_runninghub_mv.py `
  --image uploads\cover.png `
  --audio uploads\song.wav `
  --lrc mc\隔街的灯灭了.lrc `
  --dry-run
```

完整调用 RunningHub：

```powershell
python split_runninghub_mv.py `
  --api-key 你的RunningHubKey `
  --image uploads\cover.png `
  --audio uploads\song.wav `
  --lrc mc\隔街的灯灭了.lrc `
  --output outputs\final_mv.mp4
```

常用参数：

```text
--overlap-seconds 1.0
--xfade-seconds 0.5
--search-window 45
--min-part-seconds 30
--node-info-list "[]"
--node-info-file nodes.json
```

## 11. 歌词字幕脚本

脚本：

```text
add_lyrics_to_video.py
```

示例：

```powershell
python add_lyrics_to_video.py `
  -v input.mp4 `
  -l song.lrc `
  -o output.mp4 `
  -fs 56 `
  -fc white `
  -pos bottom
```

主要参数：

```text
-v / --video          输入视频
-l / --lrc            LRC 文件
-o / --output         输出视频
-fs / --fontsize      字号
-fc / --fontcolor     字体颜色
-pos / --position     top / center / bottom
-m / --margin         边距
-notitle              不显示开场标题
-td                   标题显示秒数
```

## 12. RunningHub 计费理解

根据 RunningHub 页面说明：

- 任务实际运行时计费
- 多任务并发时，按所有任务累计运行时长计费
- 不是按并发等待时间统一计费

例子：

```text
两个任务并发：
任务 A 运行 90 秒
任务 B 运行 90 秒

计费运行时长 = 90 + 90 = 180 秒
```

分段并发主要解决：

- 超时
- 等待时间长
- 失败重跑成本高

不一定降低费用。

## 13. 常见问题

### 13.1 修改代码后页面还是旧的

Flask 当前不是 debug 热更新模式，改 `app.py` 后需要重启：

```powershell
python app.py
```

如果 5000 端口被旧进程占用，可以查看：

```powershell
Get-NetTCPConnection -LocalPort 5000 -State Listen
```

### 13.2 后台 404

通常是旧 Flask 进程未重启。

确认源码里有路由：

```powershell
python -c "from app import app; print(app.url_map)"
```

然后重启服务。

### 13.3 GitHub 推送失败

如果出现：

```text
Failed to connect to github.com port 443
```

说明当前网络无法连接 GitHub。

网络恢复后执行：

```powershell
git push origin main
```

### 13.4 RunningHub 请求失败

检查：

- API Key 是否正确
- Workflow ID 是否正确
- 图片节点 ID 是否正确
- 音频节点 ID 是否正确
- RunningHub 账号是否允许并发
- 是否需要设置 `RUNNINGHUB_USE_PROXY=1`

### 13.5 自动分段不理想

可以调整：

```text
searchWindow
minPartSeconds
overlapSeconds
xfadeSeconds
lyricGuardSeconds
analysisWindowSeconds
```

如果有 LRC，建议上传 LRC；切点会更倾向避开歌词段。

## 14. Git 远端

当前项目有两个远端：

```text
origin: GitHub
gitlab: GitLab
```

查看：

```powershell
git remote -v
```

推送 GitHub：

```powershell
git push origin main
```

推送 GitLab：

```powershell
git push gitlab main
```

## 15. 后续建议

建议后续补充：

- `requirements.txt`
- 更安全的管理员初始化流程
- API Token 认证，方便外部系统调用
- 任务持久化表，避免服务重启后内存中的分段任务状态丢失
- 文件清理任务，定期删除过期上传和输出文件
- 更细粒度的用户权限，例如每个用户的可用次数、并发限制、RunningHub Key 绑定

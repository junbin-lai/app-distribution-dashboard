# Web 看板使用手册

看板是日常最直观的操作界面，支持选择应用、查看版本、勾选平台一键发布。

## 启动

`appstore` 命令装在项目 `.venv/bin/` 里，**不激活虚拟环境直接敲 `appstore` 会提示 command not found**。推荐用一键脚本（自动切到项目根目录，不依赖 PATH）：

```bash
./start-dashboard.sh          # 前台运行（Ctrl+C 停止，仅本机访问）
./start-dashboard.sh bg       # 后台运行（关终端不停，日志 tmpdoc/web-8090.log）
./start-dashboard.sh lan      # 局域网模式前台运行（绑 0.0.0.0，须先配置登录账号）
./start-dashboard.sh lanbg    # 局域网模式后台运行
./start-dashboard.sh stop     # 停止看板
./start-dashboard.sh status   # 查看运行状态
```

或者直接调虚拟环境里的命令（效果等同）：

```bash
.venv/bin/appstore web --port 8090 --credentials config/credentials.json --catalog apps/catalog.json --board-config config/board.json
```

（也可先 `source .venv/bin/activate` 再敲 `appstore web ...`，用完 `deactivate` 退出虚拟环境）

打开浏览器 → `http://127.0.0.1:8090`

## 登录与权限（config/board.json）

看板默认**无登录、仅本机 127.0.0.1 访问**（和以前一样，零配置可用）。配置 `config/board.json`（模板
`config/example.board.json`，已 gitignore 不入库）后开启**账号登录 + 两种角色**：

```json
{
  "users": [
    {"username": "laijunbin", "password": "改成你的密码", "role": "admin"},
    {"username": "xiaowang",  "password": "同事的密码",   "role": "viewer"}
  ],
  "session_days": 14
}
```

| 角色 | 能做什么 |
| --- | --- |
| **admin** | 全部操作：发布、上传安装包、停止任务、清空历史、华为立即上线、查询 |
| **viewer** | 只读：看板/版本/发布历史/分享图、查询状态、下载签名 APK；**发布类按钮全部禁用**（后端同样拦截） |

- 会话 Cookie 有效期默认 14 天（`session_days` 可调，1~90），访问自动续期；重启看板后需重新登录。
- 不想在文件里放明文密码的，可以存 `"password_sha256": "<sha256(明文)>"`（有它时优先校验哈希）。
- 登录失败有 0.6s 延迟，增加暴力尝试成本；会话为 HttpOnly + SameSite=Lax Cookie，写接口额外做 Origin 同源校验。
- `users` 留空/文件不存在 = 鉴权关闭（本机直连模式，行为与旧版一致）。

### 登录/操作审计日志

账号的登录与敏感操作会追加记录到 `config/login_log.jsonl`（JSONL 一行一条，已 gitignore）：

- **记录内容**：登录成功/失败（错密码也记，能看出暴力尝试）、退出登录、发布应用（含应用/平台/版本/dry-run 标记）、上传安装包、修改应用配置、停止发布任务、清空发布历史、华为立即上线；viewer 越权尝试写操作被拒也会记
- **查看**：`cat config/login_log.jsonl`（或 `grep 失败 config/login_log.jsonl`、`grep 发布 config/login_log.jsonl`）
- 普通浏览/查询不记录，避免日志噪音；测试隔离可用环境变量 `APPSTORE_AUDIT_LOG` 指向临时文件

### 发布历史的安全保护

发布历史（`config/tasks_history.json`）清空前有**两道防护**：

1. **必须显式确认**：调 `POST /api/tasks/clear` 要带 `{"confirm": true}`，否则返回 400 不执行（前端没有清空按钮，这个接口只给脚本用，多一步确认不影响日常）
2. **自动多份备份**：清空前写 `tasks_history.json.bak`（最近一次）+ `tasks_history.json.bak-YYYYmmdd-HHMMSS`（保留最近 5 份，多次清空不会覆盖上一次的备份）

万一误清，恢复办法是把备份条目和当前文件**按 id 合并**（不要直接覆盖，否则会丢掉清空后新产生的记录）：

```python
import json
cur = json.load(open("config/tasks_history.json"))
bak = json.load(open("config/tasks_history.json.bak"))
seen = {t.get("id") for t in cur}
merged = cur + [t for t in bak if t.get("id") not in seen]
merged.sort(key=lambda x: x.get("finished_at") or "", reverse=True)
json.dump(merged, open("config/tasks_history.json", "w"), ensure_ascii=False, indent=1)
```

（改完重启看板生效；dry-run 校验任务不会进历史）

## 局域网访问（同事共用看板）

1. 先按上一节配置好登录账号（**没有账号时局域网模式会拒绝启动**，这是故意的安全闸）。
2. 启动：`./start-dashboard.sh lan`（前台）或 `./start-dashboard.sh lanbg`（后台）。
3. 启动日志会打印本机局域网地址，例如：

   ```
   🖥  AppStore 发布看板已启动（局域网模式，监听 0.0.0.0:8090）
      同网段访问: http://192.168.x.x:8090/
      看板配置: config/board.json（登录鉴权: 已开启）
      账号: laijunbin(admin), xiaowang(viewer)
   ```

4. 同事用浏览器打开该地址 → 输入 viewer 账号 → 只能看和查询，不能发布。

注意事项：

- **首次局域网启动 macOS 会弹防火墙授权**（允许 python 接受传入连接），点「允许」。
- 局域网 IP 是 DHCP 分配的，路由器重启/换网可能变化；要固定可在路由器里给这台 Mac 绑定静态 IP。
- **不要把端口直接映射到公网**（路由器端口转发/DMZ）。看板走 HTTP 明文 + 会话 Cookie，公网暴露有被嗅探/劫持风险；
  确需外网访问请走 VPN（如 Tailscale/WireGuard）或带 HTTPS+鉴权的反向代理。

## 飞书群机器人（发布通知 + @查询）

### 一、为什么是「自建应用机器人」而不是群 webhook 机器人

群里「添加机器人 → 自定义机器人」拿到的那种 webhook 机器人**只能往群里发文字/卡片**：
收不到 @消息（没法做「@机器人查状态」），也不能传图片（发图要先调开放平台接口拿 image_key，需要应用身份）。
所以这里用的是**企业自建应用 + 机器人能力**，配合**长连接**接收事件——不需要公网 IP、不需要端口映射，内网 Mac 直接可用。

### 二、创建步骤（一次性，约 10 分钟）

1. 打开 [飞书开放平台](https://open.feishu.cn/) → 开发者后台 → **创建企业自建应用**；
   应用名称填你想要的机器人名（如「App 发布助手」）、上传头像——**这就是群里显示的机器人名字/头像**。
2. 左侧「添加应用能力」→ 添加 **机器人**。
3. 左侧「权限管理」→ 开通以下权限（搜索后逐个开通）：
   - `im:message`（获取与发送单聊、群组消息）
   - `im:message.group_at_msg`（获取群组中所有 @机器人 的消息）
   - `im:resource`（上传图片或文件）
   （在「事件与回调」里订阅事件时，控制台也会提示所需权限，可一键开通。）
4. 左侧「事件与回调」→ 订阅方式选 **使用长连接接收事件** → 添加事件 **接收消息 im.message.receive_v1**。
5. 「凭证与基础信息」页复制 **App ID / App Secret**。
6. 「版本管理与发布」→ 创建版本 → 申请线上发布；可用范围按需（全员或指定同事）；管理员在飞书管理后台审核通过。
   > 机器人名字/头像以后想改：回「凭证与基础信息」改应用名称/头像，已发布的应用改完需要**发布一个新版本**才对全部人生效。
7. 把 App ID / App Secret 填进 `config/board.json`：

   ```json
   "feishu": {
     "app_id": "cli_xxxx",
     "app_secret": "xxxx",
     "notify_chat_id": "",
     "notify_on_publish": true
   }
   ```

8. 重启看板（`./start-dashboard.sh stop` 再 `bg` / `lanbg`），日志出现「飞书机器人: 长连接启动中」即接入成功；
   看板顶栏也会显示「飞书已连接」。
9. 群设置 → 群机器人 → 添加机器人 → 选刚创建的应用机器人进群。
10. 在群里 **@机器人 发送「绑定通知群」** → 它回复确认，发布完成通知就会自动发到这个群
    （也可以手动把群的 `chat_id` 填进 `notify_chat_id`，两种方式等价）。

### 三、机器人用法（群成员）

| 指令 | 作用 |
| --- | --- |
| `@机器人 查询 <应用名>` | 查该应用各平台状态，回复**状态分享图**（同看板「分享」那张） |
| `@机器人 查询 全部` | 逐个应用查询（较慢，每个应用查完即回图） |
| `@机器人 绑定通知群` | 把当前群设为发布完成通知群（写回 board.json，立即生效） |
| `@机器人 解绑通知群` | 取消通知群绑定 |
| `@机器人`（或「帮助」） | 回复用法说明 + 可查询应用列表 |

应用名支持模糊匹配（中文名、英文名、应用 ID、包名都行，还能容错错别字如 `KKHOEM`→`KKhome`）；命中多个会列出来让你说得更具体。

**其他行为约定**：

- **回复会 @ 回提问的人**：群里谁 @机器人，机器人的每条回复（含查询结果）都会 @上他；结果消息用富文本把「@提问者 + 状态分享图」放在同一条里（飞书的图片消息本身带不了 @）。私聊则不带 @。
- **@所有人不触发**：消息里 @所有人（飞书标记 `@_all`）时机器人直接忽略，不会响应，避免群里发通知时误触发查询。
- **口语化指令**：`凯迪仕版本多少`、`帮我查一下 homeaccess`、`目前显示KKHOEM 版本` 这类都能识别。

### 四、发布完成通知长什么样

**默认只有「正式发布 + 勾选的平台全部提审成功」才会通知**（dry-run 校验、部分失败、被手动停止、只存草稿
等场景一律静默）。判定口径：国内平台提交成功即提审；Google 须勾选「自动送审」且提交成功才算送审，
仅存草稿不算。

以后想扩展通知范围，改 `config/board.json` 的 `feishu.notify_on` 字段即可（改完重启）：

```json
"notify_on": {
  "all_success": true,      // 勾选平台全部提审成功（默认唯一开启）
  "partial_success": false, // 部分平台成功（其余失败/仅草稿）
  "failed": false,          // 全部失败
  "killed": false,          // 被手动停止
  "draft_only": false       // 只存草稿未送审（如 Google 未勾自动送审）
}
```

满足条件时，机器人往通知群发一张卡片：

- 标题：✅ 发布完成 · 应用名 + 版本
- 字段：应用、平台、操作者（登录账号）、结束时间
- **状态分享图**：发布后自动查一次各平台状态，渲染与看板同款的分享图嵌在卡片里
- 「打开看板」按钮：直接跳回看板页面

机器人私聊也支持同样的指令（不用 @）。


## 界面布局

左侧栏：应用列表（按分类分组）
右侧主区：

1. **顶部**：当前选中应用信息（名称 / 包名 / 版本）
2. **三列卡片**
   - **应用信息**：包名、versionName、versionCode（未选包时版本显示 "-"）
   - **构建产物**：AAB/APK **选择文件上传**（区分 .aab / .apk，选后自动解析版本/包名）
   - **发布**：版本名、版本号（选包后自动回填）、**平台多选复选框**、更新说明、**定时上线时间选择**、发布/查询按钮
3. **平台状态**：各平台已上架版本、审核状态、草稿版本（三行）
4. **日志**：发布任务进度与结果

## 发布操作步骤

1. 左侧选择目标 App → 右侧加载应用信息
2. 在「构建产物」点击 **"选择…"** 上传 APK/AAB → 自动解析版本名、versionCode、包名校验并回填发布表单
3. 确认：
   - **勾选发布平台**（可多选，不含 Apple）
   - **更新说明**
   - **定时上线**（选时间；留空=立即上线）
4. 点 **"发布"** → 后台异步执行
5. 右侧日志区显示每个平台的进度
6. 点 **"查询状态"** 刷新版本信息

### 平台多选注意

- 复选框全选/清空按钮在平台列表下方
- Apple 不参与发布（仅查询）
- 如果你勾选了多个平台，后台会**依次**执行发布
- 「查询全部平台」是**并发**执行的，总耗时约等于最慢的那个平台（不再是各平台相加）；
  苹果的商店页面版本按包名缓存 5 分钟，避免每次查询都抓一次大页面
- 荣耀、应用宝上传 APK 遇到连接中断/超时会**自动重试**（每次从头重传，最多 3 次，指数退避）

### Google Play 状态口径（重要）

Google Play Developer API **没有审核状态字段**（track release 的 status 只有 draft / inProgress / halted / completed，
没有「审核中 / 待发布 / 被拒」）。因此看板用 **Play 商店页面显示的版本号** 复核「已上架」，
其余状态按下面口径归类：

- 商店页面版本 == 提交版本 → 已上架
- 商店页面版本 < 提交版本（提交方式是「全量发布」，status=completed）→ 审核中（尚未在商店生效）
- 提交方式是「分阶段发布」（status=inProgress + 百万分之一）→ **待发布**（不是「审核中」）

### 为什么「分阶段发布中」显示成「待发布」

Google API 的 inProgress 既可能是「审核中」，也可能是「审核通过后按比例灰度中」，**两者无法区分**。
我们的分阶段发布比例是百万分之一，约等于没人收得到更新，语义上更接近「还没正式发布」，
所以统一显示为「待发布」，并在审核状态里写明「X 分阶段发布中（百万分之一，点「全量发布」正式上线）」。

这是一个有意的近似：**「待发布」阶段实际可能仍在审核、也可能已审核通过**；如果 Google 后续在 API 里
提供真实的「待发布」和「拒绝」字段，再把这里改成准确归类。当前被拒版本也无法可靠判断，
只能人工到 Play Console 查看。

**自管式发布（Managed Publishing）是 Play Console 里按应用开的全局开关**，不是发布接口能按次传的参数，
所以「一次分阶段、一次自管式」这种混用做不到。看板能按次选的是**发布方式**（都在标准 / 非自管式账户下）：

- **分阶段发布**：审核通过后先放百万分之一，再在看板点「全量发布」→ 100%（发布表单「Google 发布方式」选「分阶段发布」）
- **全量发布**：审核通过后 Google 自动 100% 上线（选「全量发布」）

如果要 Play Console 意义上的自管式（审核通过后任何版本都不自动上架、必须去 Play Console 手动点发布），
去 Play Console 把该应用的「发布管理 → 自管式发布」打开，并把 credentials.json 的 google.managed_publishing 设为 true，
看板会在审核状态里提示「需在 Play Console 手动发布」。注意它是全局的：分阶段 / 全量都会变成手动发布。

### 小米应用商店状态口径（重要）

小米 dev/query 接口**同样没有审核状态字段**，只返回 packageInfo 里的两个版本号：

- versionName / versionCode：**最新（提审）版本**
- onlineVersionCode：**线上版本号**（商店已上架；接口不返回线上版本名）

看板用这两个版本号的大小关系推断状态：

- onlineVersionCode < versionCode → 审核中（提审版本还没上线）
- onlineVersionCode == versionCode → 已上架

这是一个有意的近似：**被拒时接口也不会告诉我们**，所以被拒版本会继续显示成「审核中」；
等小米后续开放审核状态/拒绝字段后再改成准确归类。审核中时「已上架版本」行为空，
线上版本号会补进审核状态备注里（例如「6.19.1 审核中（线上版本号 202605181）」）。

## HTTP API

看板也提供 JSON API，供脚本/CI 集成。

### 查询状态

```http
POST /api/status
Content-Type: application/json

{"app_id": "example-app"}
```

返回：

```json
{
  "ok": true,
  "package": "com.example.app",
  "statuses": [
    {"platform": "google", "state": "published", "live_version_names": ["100 (1.0.0)"], "draft_version_names": ["101 (1.0.1)"], "reviewing_version_names": [], "review_message": "production:草稿未送审"},
    {"platform": "xiaomi", "state": "published", "live_version_names": ["4.6.0"]},
    ...
  ]
}
```

### 发布

```http
POST /api/publish
Content-Type: application/json

{
  "app_id": "philps-easykey-plus",
  "platform": "xiaomi,oppo",
  "dry_run": false,
  "version_name": "1.0.1",
  "version_code": 101,
  "release_notes": "修复问题",
  "online_time": "2026-09-01T10:00"
}
``

- `platform`：逗号分隔多个；`"all"` = 全部已配平台（不含 Apple）
- `online_time`：ISO 格式时间（`YYYY-MM-DDTHH:MM`），留空=立即

返回 `{"ok": true, "task_id": "xxx"}`，然后 GET `/api/tasks/<id>` 轮询进度。

### 获取发布任务进度

```http
GET /api/tasks/{task_id}
```

返回：

```json
{
  "status": "running",
  "progress": 45,
  "stage": "上传文件",
  "steps": ["...", "..."],
  "errors": []
}
```

### 获取应用列表

```http
GET /api/apps
```

### 获取平台列表

```http
GET /api/platforms
```

### 校验凭证

```http
POST /api/validate
```

### 列出目录可检测的构建文件

```http
POST /api/files
Content-Type: application/json

{"app_id": "example-app"}
```

### 登录 / 会话

```http
POST /api/login
Content-Type: application/json

{"username": "laijunbin", "password": "..."}
```

返回 `{"ok": true, "username": "...", "role": "admin"}` 并设置会话 Cookie（`board_session`，HttpOnly）。
开启鉴权后其余 API 未登录一律 401；viewer 调发布类接口返回 403。

```http
GET  /api/session    # 当前登录态 {authenticated, auth_enabled, username, role}
POST /api/logout     # 退出登录
```

### 状态分享图（服务端渲染）

```http
GET /api/share-image?app_id=example-app
```

返回 PNG（与看板「分享」按钮出的图同款设计）。会实时查询各平台状态（约 10-60 秒），
飞书机器人的通知图就是它出的；CI/脚本也可以直接取图。

### 看板配置信息

```http
GET /api/config
```

返回凭证/目录路径、登录开关、当前用户角色、飞书机器人连接状态。
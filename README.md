# TG 群 Topic 桥接机器人

一个 Python Telegram 机器人，把「群内某一个 Topic」和「群外用户」双向桥接：

- 群外用户私聊机器人，消息自动转发到群内指定 Topic；
- 群内该 Topic 中任意成员发的消息，自动转发给所有已连接的外部用户；
- 只处理「指定群 + 指定 Topic」，其他 Topic、其他群的消息一律不转发，实现消息隔离。

## 隔离原理

机器人会收到更新，但代码只匹配 `GROUP_CHAT_ID + message_thread_id` 都正确的消息，其余全部忽略。即使机器人因为权限原因能看到群内所有消息，也不会把它们转发出去。

## 项目结构

| 文件 | 作用 |
|---|---|
| `bot.py` | 主程序，全部逻辑都在这里（配置、存储、命令、双向转发） |
| `requirements.txt` | Python 依赖：python-telegram-bot、python-dotenv |
| `.env.example` | 配置模板，复制为 `.env` 后填写 token 和 ID |
| `Dockerfile` | 把项目打包成 Docker 镜像 |
| `docker-compose.yml` | 一条命令启动容器，自动重启，数据目录挂载 |
| `deploy/install.sh` | VPS 一键部署脚本：自动装 Docker、建 swap、检查配置、启动 |
| `deploy/tg-topic-bridge.service` | 不用 Docker 时的 systemd 服务模板 |
| `README.md` | 使用和部署文档 |
| `使用手册.md` | 傻瓜操作手册（含架构图、部署流程、命令速查、故障排查） |
| `.gitignore` / `.dockerignore` | 排除 `.env`、`data/` 等敏感/临时文件 |
| `data/bridge.json` | 运行时自动生成：默认 Topic、订阅者、分流绑定、白名单、群记录（不要手动改） |

## 一、准备工作

1. 私聊 [@BotFather](https://t.me/BotFather)，用 `/newbot` 创建机器人，拿到 token。
2. 关键一步：在 BotFather 中 `/mybots` → 选择机器人 → Bot Settings → Group Privacy → **Turn off**（关闭隐私模式）。或者把机器人设为群管理员并勾选“读取消息”权限。二选一，否则机器人收不到群内普通成员的消息。
3. 在 Telegram 群里开启 Topics（话题），创建一个目标 Topic，把机器人拉进群。建议设为管理员（至少勾选“读取消息”和“发送消息”）。
4. 在目标 Topic 里发送 `/topic`，机器人会返回群 ID 和 Topic ID；也可以直接发 `/set_topic` 把当前 Topic 保存为桥接主题。

## 二、配置

复制 `.env.example` 为 `.env` 并填写：

| 变量 | 必填 | 说明 |
|---|---|---|
| `BOT_TOKEN` | 是 | BotFather 给的 token |
| `GROUP_CHAT_ID` | 是 | 群数字 ID，如 `-1001234567890` |
| `TOPIC_THREAD_ID` | 否 | 目标 Topic ID；不填则在群内用 `/set_topic` 保存 |
| `ADMIN_USER_IDS` | 否 | 额外管理员 ID（可选）。不填时，群主/群管理员即可执行管理命令；填了之后这些 ID 始终视为管理员 |
| `ALLOWED_USER_IDS` | 否 | 初始白名单（可选）。留空表示默认不允许任何人私聊使用（管理员除外），必须用 `/allow` 添加后才能使用；运行中可用 `/allow`、`/disallow` 动态增删 |
| `BRIDGE_MODE` | 否 | `copy`（默认，匿名，隐藏群痕迹）或 `forward`（保留转发来源） |
| `PROTECT_CONTENT` | 否 | `true` 时接收方无法再转发/保存收到的消息 |
| `DATA_FILE` | 否 | 状态文件位置，默认 `data/bridge.json` |

管理员数字 ID 可通过 [@userinfobot](https://t.me/userinfobot) 或 [@RawDataBot](https://t.me/RawDataBot) 获取。

## 三、运行

本地运行（Python 3.10+）：

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

Docker 运行：

```bash
docker compose up -d --build
```

订阅者列表和 Topic ID 保存在 `data/bridge.json`，升级容器不会丢失。

### VPS 部署（Docker 推荐）

1. SSH 登录 VPS（Ubuntu/Debian）后安装 Docker：

   ```bash
   curl -fsSL https://get.docker.com | sh
   systemctl enable --now docker
   ```

2. 把项目上传到 VPS（在本地 Windows PowerShell 执行，`root@你的IP` 换成实际账号和地址）：

   ```powershell
   scp -r "C:\Users\XOS\Documents\Codex\2026-08-13\new-chat\outputs\tg-topic-bridge" root@你的VPS_IP:/opt/
   ```

   或用 WinSCP 上传。注意 `.env` 也要一起上传；如果用了 git，`.env` 不会被提交，需在服务器上重新创建。

3. 在 VPS 上启动：

   ```bash
   cd /opt/tg-topic-bridge
   bash deploy/install.sh
   ```

   `deploy/install.sh` 会自动安装 Docker（如果没有）、检查 `.env`、构建并启动容器。也可以手动执行 `docker compose up -d --build`。

4. 查看日志确认启动成功：

   ```bash
   docker compose logs -f
   ```

   看到 `机器人启动：群=...，Topic=...` 即正常。容器已配置 `restart: unless-stopped`，VPS 重启后会自动拉起，不需要开放任何入站端口。

### VPS 部署（无 Docker，systemd）

```bash
cd /opt/tg-topic-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

然后把 `deploy/tg-topic-bridge.service` 复制到 `/etc/systemd/system/`，按需修改 `WorkingDirectory` 和 `ExecStart` 路径，再执行：

```bash
sudo cp deploy/tg-topic-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-topic-bridge
journalctl -u tg-topic-bridge -f
```

修改 `.env` 后重启服务：`sudo systemctl restart tg-topic-bridge`。

## 四、使用方式

群外用户：

- 直接私聊机器人即可：发的任何消息都会进入群内目标 Topic，并自动成为订阅者；
- `/stop` 断开，`/start` 重新连接，`/status` 查看状态。
- 任何人私聊发送 `/id`，机器人会返回对方的数字 ID（未放行的用户也可以用来查 ID）。

群内成员：

- 在目标 Topic 里正常发言，机器人会自动转发给所有订阅者；
- 其他 Topic 的消息不会被转发。
- 回复引用：`copy` 模式下，群内回复会以原生引用形式转发给外部用户；外部用户在私聊里回复也会以原生引用形式进入群话题（消息映射只保留最近 2000 条，超出后旧消息无法引用）。`forward` 模式不支持引用，会退化为普通转发。

### 多用户分流（绑定到不同群/Topic/频道）

默认情况下所有用户都走同一个默认 Topic。如果需要把不同用户分流到不同目标，使用以下命令（管理员在群内发送）：

| 命令 | 说明 |
|---|---|
| `/bind <用户ID或@用户名>` | 在当前群/Topic 中绑定该用户，之后该用户的消息只和这个目标互通 |
| `/bind <用户ID或@用户名> <群ID> [TopicID]` | 绑定到指定群；TopicID 留空表示整个群/频道 |
| `/unbind <用户ID或@用户名>` | 解除分流绑定，用户恢复为默认 Topic 订阅 |
| `/add <用户ID或@用户名>` | 把用户加入默认 Topic 订阅（同时清除分流绑定） |
| `/remove <用户ID或@用户名>` | 从所有绑定中彻底移除该用户 |
| `/status` | 查看默认订阅者和全部分流绑定 |

绑定后，该用户在目标群/Topic 里的消息只转发给 TA，其他用户看不到，实现用户级消息隔离。

用 @用户名 绑定更方便，但前提是对方必须先私聊过机器人（任意一条消息即可），机器人会记录他的用户名。用户名全局唯一但可以随时改名、也可能被他人注册，数字 ID 永久不变，所以内部始终以数字 ID 为准。

绑定频道时，把机器人设为频道管理员并勾选「发布消息」。可以在频道里直接发送：

```
/allow @对方用户名
```

机器人会一步完成两件事：把对方加入白名单，并绑定到当前频道。之后频道里的新消息会转发给 TA；TA 私聊机器人的消息会以机器人名义发布到频道（双向）。频道内只有管理员能发帖，因此这条命令天然只有频道管理员能触发。

管理员命令（群内使用，群主/群管理员均可执行）：

| 命令 | 说明 |
|---|---|
| `/topic` | 查看群 ID 和当前 Topic ID |
| `/set_topic` | 把当前 Topic 设为默认桥接主题 |
| `/allow <用户ID或@用户名>` | 把用户加入外部用户白名单 |
| `/disallow <用户ID或@用户名>` | 把用户移出白名单，并同时解除他/她的全部绑定（所有群/Topic/频道） |
| `/leave`（群内） | 让机器人退出当前群 |
| `/leave <群ID>`（私聊管理员） | 远程让机器人退出指定群，并清理该群绑定 |
| `/groups`（私聊管理员） | 查看机器人记录到的所有群（群 ID + 群名），可配合 `/leave` 使用 |
| `/addadmin <用户ID或@用户名>`（私聊全局管理员） | 添加新的全局管理员 |
| `/removeadmin <用户ID或@用户名>`（私聊全局管理员） | 移除全局管理员（不能移除自己） |
| `/admins`（私聊全局管理员） | 查看当前全局管理员列表 |
| `/setdefaultgroup <群ID>`（私聊全局管理员） | 更改默认群 |
| `/setdefaulttopic <TopicID>`（私聊全局管理员） | 更改默认 Topic（配合上面命令远程迁移默认目标） |

以上分流命令和默认订阅命令一起构成了完整的管理方式，`/bind`、`/unbind`、`/add`、`/remove`、`/status` 的说明见上面的多用户分流表格。

注意：Telegram 没有“查询机器人加入了哪些群”的接口，`/groups` 只能列出**机器人收到过消息/入群通知**的群；新部署后，需要群里有人发消息才会被记录。

## 五、常见问题

- **群内消息转发不出来**：基本是隐私模式没关，或机器人没有“读取消息”管理员权限。
- **外部用户的消息没进目标 Topic**：确认 Topic 已通过 `/set_topic` 设置；确认机器人有发送权限。
- **群开启了“禁止转发/保存内容”**：受保护的消息无法用 `forward` 或 `copy` 转发，该条消息会失败并提示。
- **`copy` 模式**：外部用户身份不会暴露，群内看到的是机器人发送的内容。
- **机器人重复转发自己的消息**：代码已自动跳过机器人自己发的消息，不会形成回环。
- **订阅者收不到消息**：对方必须先主动私聊过机器人（Telegram 限制机器人不能主动发起会话）；如果对方屏蔽了机器人，会自动从订阅列表移除。
- **单实例部署**：`data/bridge.json` 文件存储方式只适合单实例，不要同时跑多个进程。

## 六、从零部署教程（傻瓜版）

全程约 30 分钟。跟着做就能把机器人跑起来。

### 你需要准备

- 一台 VPS（推荐 1 核 / 512MB 以上 / Ubuntu 22.04 或 24.04，机房选日本、新加坡、美国等能访问 Telegram 的地区）
- 一个 Telegram 账号
- 本地电脑（Windows 或 Mac 都行）

### 第 1 阶段：创建机器人（约 5 分钟）

1. Telegram 搜索 `@BotFather`，点开并发送 `/start`；
2. 发送 `/newbot`，按提示给机器人起一个显示名称；
3. 再给它一个 `@用户名`（必须以 `bot` 结尾，例如 `@my_bridge_bot`）；
4. BotFather 会返回 token，形如 `123456789:AAHf...`，复制保存好；
5. 发送 `/mybots` → 选择机器人 → Bot Settings → Group Privacy → **Turn off**（关闭隐私模式）。

> 第 5 步也可以不做，改成：之后把机器人设为群管理员并勾选「读取消息」。二选一即可，否则机器人收不到群里普通成员的消息。

### 第 2 阶段：准备群和话题（约 10 分钟）

1. 创建一个新群（或使用现有群）；
2. 群设置里开启 **Topics（话题）**；
3. 在群里创建一个目标 Topic；
4. 把机器人拉进群：群信息 → 添加成员 → 搜索 `@你的机器人用户名` → 确认；
5. 把机器人设为管理员：群信息 → 管理员 → 添加管理员 → 选机器人 → 勾选「读取消息」「发送消息」（建议顺手勾「管理话题」）→ 保存；
6. 获取群 ID：在群里随便发一条消息，然后浏览器打开：

   ```
   https://api.telegram.org/bot<你的TOKEN>/getUpdates
   ```

   在返回的 JSON 里找 `"chat": {"id": -100xxxxxxxxxx, ...}`，这个 `-100` 开头的数字就是群 ID。如果消息发在某个 Topic 里，同一段 JSON 里还有 `"message_thread_id": 2`，就是 Topic ID。

### 第 3 阶段：本地准备项目（约 5 分钟）

1. 解压部署包，得到 `tg-topic-bridge` 文件夹；
2. 进入文件夹，把 `.env.example` 复制一份，改名为 `.env`（用记事本打开）；
3. 填写：

   ```
   BOT_TOKEN=第1阶段拿到的token
   GROUP_CHAT_ID=-100xxxxxxxxxx（换成你的）
   ADMIN_USER_IDS=（可以不填）
   ```

   `TOPIC_THREAD_ID`、`ALLOWED_USER_IDS` 都可以留空，后面用命令设置。

### 第 4 阶段：上传到 VPS（约 5 分钟）

方法 A：本地 PowerShell 执行（把 IP 换成你的 VPS IP，输入密码）：

```powershell
scp -r "本地路径\tg-topic-bridge" root@你的IP:/opt/
```

方法 B：用 WinSCP 图形界面，登录后把 `tg-topic-bridge` 文件夹拖到 `/opt/` 目录。

### 第 5 阶段：在 VPS 上一键部署（约 5 分钟）

1. 本地 PowerShell 登录 VPS：

   ```powershell
   ssh root@你的IP
   ```

2. 依次执行：

   ```bash
   cd /opt/tg-topic-bridge
   bash deploy/install.sh
   ```

   脚本会自动：创建 swap（内存不足时）→ 安装 Docker → 构建镜像 → 启动机器人 → 配置开机自启。

3. 看日志确认：

   ```bash
   docker compose logs -f
   ```

   看到 `机器人启动：默认群=...` 就成功了。按 `Ctrl+C` 退出日志，不影响机器人运行。

### 第 6 阶段：让桥接生效（约 5 分钟）

1. 去群里目标 Topic 发送 `/set_topic`（管理员）；
2. 发 `/status` 确认状态；
3. 添加外部用户：
   - 对方先私聊机器人发任意一条消息（会被拒绝，但用户名已被记录）；
   - 管理员在群里发 `/allow @对方用户名` 放行；
   - 群 Topic 场景：在目标 Topic 里发 `/bind @对方用户名` 绑定；
   - 频道场景：在频道里发 `/allow @对方用户名`，自动完成白名单 + 绑定频道。
4. 测试：群里 Topic 发消息 → 外部用户收到；外部用户私聊 → 消息进入 Topic。

### 部署后常见问题

- **群里消息转发不出来**：隐私模式没关，或机器人没勾「读取消息」。
- **token 格式错误**：完整 URL 是 `https://api.telegram.org/bot<TOKEN>/getUpdates`，token 两边不能有尖括号、空格。
- **两个实例抢 token**：本地和 VPS 不能同时运行同一个机器人。
- **新用户一直说没权限**：白名单默认全拒，必须先让用户私聊一次，再 `/allow`。
- **VPS 连不上 Telegram**：机房要选海外，国内大陆机房直连不通。
- **更新代码/重启**：`cd /opt/tg-topic-bridge && docker compose up -d --build`。
- **`git pull` 提示本地文件会被覆盖**：说明 VPS 上手动改过文件，先执行 `git checkout -- bot.py` 再 `git pull`。
- **备份**：定期备份 `data/bridge.json`（订阅和绑定都在里面）。

## 安全建议

- 白名单默认全拒：只有通过 `ALLOWED_USER_IDS` 或 `/allow` 添加的用户才能私聊使用，避免任何人拿到 bot 后都能读取 Topic 内容。
- 不要泄露 token；泄露后在 BotFather 中 `/revoke` 重新生成。
- 日志已自动对 token 脱敏（显示为 `***`），查看 `docker compose logs` 不会暴露 token。
- 想要隐藏 Topic 所属群的痕迹，把 `BRIDGE_MODE` 设为 `copy`。
- 任何人知道 @用户名都可以把机器人拉进自己的群：建议在 BotFather 中关闭 **Allow Groups**（需要时再打开）；已拉入的群可用私聊 `/groups` 查看、`/leave <群ID>` 踢出。

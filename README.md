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
| `.gitignore` / `.dockerignore` | 排除 `.env`、`data/` 等敏感/临时文件 |
| `data/bridge.json` | 运行时自动生成：默认 Topic、订阅者、分流绑定（不要手动改） |

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
| `ALLOWED_USER_IDS` | 否 | 外部用户白名单；留空表示任何人都能连接（谨慎） |
| `BRIDGE_MODE` | 否 | `forward`（默认，保留转发来源）或 `copy`（匿名，以机器人名义发送） |
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

群内成员：

- 在目标 Topic 里正常发言，机器人会自动转发给所有订阅者；
- 其他 Topic 的消息不会被转发。

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

绑定频道时机器人需要是频道管理员，且频道只能单向推送给用户（用户私聊消息无法进入真正的频道，只能进入绑定的群/Topic）。

管理员命令（群内使用，群主/群管理员均可执行）：

| 命令 | 说明 |
|---|---|
| `/topic` | 查看群 ID 和当前 Topic ID |
| `/set_topic` | 把当前 Topic 设为默认桥接主题 |

以上分流命令和默认订阅命令一起构成了完整的管理方式，`/bind`、`/unbind`、`/add`、`/remove`、`/status` 的说明见上面的多用户分流表格。

## 五、常见问题

- **群内消息转发不出来**：基本是隐私模式没关，或机器人没有“读取消息”管理员权限。
- **外部用户的消息没进目标 Topic**：确认 Topic 已通过 `/set_topic` 设置；确认机器人有发送权限。
- **群开启了“禁止转发/保存内容”**：受保护的消息无法用 `forward` 或 `copy` 转发，该条消息会失败并提示。
- **`copy` 模式**：外部用户身份不会暴露，群内看到的是机器人发送的内容。
- **机器人重复转发自己的消息**：代码已自动跳过机器人自己发的消息，不会形成回环。
- **订阅者收不到消息**：对方必须先主动私聊过机器人（Telegram 限制机器人不能主动发起会话）；如果对方屏蔽了机器人，会自动从订阅列表移除。
- **单实例部署**：`data/bridge.json` 文件存储方式只适合单实例，不要同时跑多个进程。

## 安全建议

- 生产环境务必配置 `ALLOWED_USER_IDS` 白名单，避免任何人拿到 bot 后都能读取 Topic 内容。
- 不要泄露 token；泄露后在 BotFather 中 `/revoke` 重新生成。
- 想要隐藏 Topic 所属群的痕迹，把 `BRIDGE_MODE` 设为 `copy`。

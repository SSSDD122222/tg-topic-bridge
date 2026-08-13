"""Telegram 群 Topic ↔ 外部用户桥接机器人（支持多用户分流）。

核心逻辑：
- 每个外部用户可以绑定到自己的目标（群 + Topic，或频道）；
- 用户私聊机器人，消息转发到 TA 绑定的目标；
- 目标里任何成员发送的消息，转发给绑定到该目标的用户；
- 未绑定的用户走默认目标（GROUP_CHAT_ID + TOPIC_THREAD_ID）；
- 其他 Topic、其他群、非目标消息一律不转发，实现消息隔离。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- 环境变量配置 ----------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("缺少 BOT_TOKEN，请在 .env 中配置。")

GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0") or 0)
if not GROUP_CHAT_ID:
    raise SystemExit("缺少 GROUP_CHAT_ID，请在 .env 中配置（默认目标群，例如 -1001234567890）。")

TOPIC_THREAD_ID = int(os.getenv("TOPIC_THREAD_ID", "0") or 0)

ADMIN_USER_IDS = {
    int(part)
    for part in os.getenv("ADMIN_USER_IDS", "").split(",")
    if part.strip().lstrip("-").isdigit()
}
ALLOWED_USER_IDS = {
    int(part)
    for part in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if part.strip().lstrip("-").isdigit()
}

BRIDGE_MODE = os.getenv("BRIDGE_MODE", "copy").strip().lower()
if BRIDGE_MODE not in {"forward", "copy"}:
    raise SystemExit("BRIDGE_MODE 只能是 forward（保留转发来源）或 copy（匿名转发）。")

PROTECT_CONTENT = os.getenv("PROTECT_CONTENT", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

DATA_FILE = Path(os.getenv("DATA_FILE", "data/bridge.json")).expanduser()

HELP_TEXT = (
    "🔗 已连接到桥接机器人。\n\n"
    "在这里发送的消息会转发到你绑定的群/Topic；"
    "绑定目标里的新消息也会自动转发到这里。\n\n"
    "可用命令：\n"
    "/start - 连接默认主题\n"
    "/stop - 断开连接\n"
    "/status - 查看当前状态\n"
    "/help - 显示本帮助"
)

PRIVATE_FILTER = filters.ChatType.PRIVATE
GROUP_FILTER = filters.ChatType.GROUP | filters.ChatType.SUPERGROUP
CHANNEL_FILTER = filters.ChatType.CHANNEL


async def _can_use(store: BridgeStore, user_id: int) -> bool:
    """未配置白名单时允许所有人；配置后仅白名单成员（或管理员）可用。"""
    if user_id in ADMIN_USER_IDS:
        return True
    return await store.is_allowed(user_id)


async def _is_group_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """群管理员（或 ADMIN_USER_IDS 白名单中的用户）才能执行管理命令。"""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return False
    if user.id in ADMIN_USER_IDS:
        return True
    try:
        member = await context.bot.get_chat_member(
            chat_id=message.chat_id, user_id=user.id
        )
    except TelegramError as exc:
        logger.warning("查询成员 %s 的管理权限失败：%s", user.id, exc)
        return False
    return member.status in ("administrator", "creator")


class BridgeStore:
    """用 JSON 文件保存默认 Topic、默认订阅者和用户分流映射（单实例部署）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self.data: dict = {
            "topic_thread_id": TOPIC_THREAD_ID,
            "subscribers": [],
            "mappings": {},
            "users": {},
            "allowed_users": list(ALLOWED_USER_IDS),
            "groups": {},
        }
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except (OSError, ValueError) as exc:
                logger.warning("无法读取 %s，将重新创建：%s", self.path, exc)
        self.data.setdefault("topic_thread_id", TOPIC_THREAD_ID)
        self.data.setdefault("subscribers", [])
        self.data.setdefault("mappings", {})
        self.data.setdefault("users", {})
        self.data.setdefault("allowed_users", list(ALLOWED_USER_IDS))
        self.data.setdefault("groups", {})
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get_topic(self) -> int:
        return int(self.data.get("topic_thread_id") or 0)

    async def set_topic(self, thread_id: int) -> None:
        async with self._lock:
            self.data["topic_thread_id"] = int(thread_id)
            self._save()

    # ---------- 默认订阅者 ----------

    async def add_subscriber(self, user_id: int) -> bool:
        """把用户加入默认订阅者（同时清除其分流映射）。"""
        async with self._lock:
            if user_id in self.data["subscribers"]:
                self.data["mappings"].pop(str(user_id), None)
                self._save()
                return False
            self.data["mappings"].pop(str(user_id), None)
            self.data["subscribers"].append(user_id)
            self._save()
            return True

    async def remove_subscriber(self, user_id: int) -> bool:
        async with self._lock:
            if user_id not in self.data["subscribers"]:
                return False
            self.data["subscribers"].remove(user_id)
            self._save()
            return True

    async def is_subscribed(self, user_id: int) -> bool:
        async with self._lock:
            return user_id in self.data["subscribers"]

    async def list_subscribers(self) -> list[int]:
        async with self._lock:
            return list(self.data["subscribers"])

    # ---------- 用户资料（用于 @用户名 绑定） ----------

    async def remember_user(self, user_id: int, username: Optional[str]) -> None:
        """记录用户最近一次私聊时看到的 @用户名。"""
        if not username:
            return
        async with self._lock:
            entry = self.data["users"].setdefault(str(user_id), {})
            if entry.get("username") != username:
                entry["username"] = username
                self._save()

    async def get_username(self, user_id: int) -> Optional[str]:
        async with self._lock:
            return self.data["users"].get(str(user_id), {}).get("username")

    async def resolve_user_id(self, key: str) -> Optional[int]:
        """把数字 ID 或 @用户名解析为用户数字 ID（用户名需已私聊过机器人）。"""
        key = key.strip().lstrip("@").lower()
        if key.isdigit():
            return int(key)
        async with self._lock:
            for uid_str, info in self.data["users"].items():
                if str(info.get("username", "")).lower() == key:
                    return int(uid_str)
        return None

    # ---------- 外部用户白名单（运行时管理） ----------

    async def is_allowed(self, user_id: int) -> bool:
        """白名单模式：只放行名单内成员（空名单 = 不允许任何人）。"""
        async with self._lock:
            allowed = self.data.get("allowed_users") or []
            return user_id in allowed

    async def add_allowed(self, user_id: int) -> bool:
        async with self._lock:
            allowed = self.data.setdefault("allowed_users", [])
            if user_id in allowed:
                return False
            allowed.append(user_id)
            self._save()
            return True

    async def remove_allowed(self, user_id: int) -> bool:
        async with self._lock:
            allowed = self.data.get("allowed_users") or []
            if user_id not in allowed:
                return False
            allowed.remove(user_id)
            self._save()
            return True

    async def list_allowed(self) -> list[int]:
        async with self._lock:
            return list(self.data.get("allowed_users") or [])

    # ---------- 群记录（用于 /groups 查看机器人所在群） ----------

    async def remember_group(self, chat_id: int, title: Optional[str]) -> None:
        """记录机器人收到过消息的群（Telegram 没有查询机器人所在群的 API）。"""
        async with self._lock:
            groups = self.data.setdefault("groups", {})
            key = str(chat_id)
            entry = groups.get(key)
            title = title or ""
            if entry is None:
                groups[key] = {"title": title}
                self._save()
            elif entry.get("title") != title:
                entry["title"] = title
                self._save()

    async def list_groups(self) -> dict[int, dict]:
        async with self._lock:
            return {int(k): dict(v) for k, v in self.data.get("groups", {}).items()}

    # ---------- 用户分流映射 ----------

    async def set_mapping(self, user_id: int, chat_id: int, thread_id: int = 0) -> None:
        """把用户绑定到指定群/Topic（thread_id=0 表示整个群/频道）。"""
        async with self._lock:
            self.data["mappings"][str(user_id)] = {
                "chat_id": int(chat_id),
                "thread_id": int(thread_id),
            }
            if user_id in self.data["subscribers"]:
                self.data["subscribers"].remove(user_id)
            self._save()

    async def clear_mapping(self, user_id: int) -> bool:
        """解除分流映射，用户回到默认订阅者。"""
        async with self._lock:
            if str(user_id) not in self.data["mappings"]:
                return False
            del self.data["mappings"][str(user_id)]
            if user_id not in self.data["subscribers"]:
                self.data["subscribers"].append(user_id)
            self._save()
            return True

    async def get_mapping(self, user_id: int) -> Optional[dict]:
        async with self._lock:
            return dict(self.data["mappings"].get(str(user_id)) or {})

    async def list_mappings(self) -> dict[int, dict]:
        async with self._lock:
            return {int(k): dict(v) for k, v in self.data["mappings"].items()}

    async def remove_user(self, user_id: int) -> bool:
        """从默认订阅者和分流映射中彻底移除用户。"""
        async with self._lock:
            removed = False
            if str(user_id) in self.data["mappings"]:
                del self.data["mappings"][str(user_id)]
                removed = True
            if user_id in self.data["subscribers"]:
                self.data["subscribers"].remove(user_id)
                removed = True
            if removed:
                self._save()
            return removed

    async def remove_chat_mappings(self, chat_id: int) -> int:
        """移除绑定到指定群/频道的所有映射，返回清理的用户数。"""
        async with self._lock:
            removed = []
            for key, dest in list(self.data["mappings"].items()):
                if int(dest["chat_id"]) == chat_id:
                    removed.append(int(key))
                    del self.data["mappings"][key]
            if removed:
                self._save()
            return len(removed)

    async def get_destination(self, user_id: int) -> Optional[tuple[int, int]]:
        """返回用户消息应该转发到的 (chat_id, thread_id)；无映射时返回默认目标。"""
        async with self._lock:
            mapping = self.data["mappings"].get(str(user_id))
            if mapping:
                return int(mapping["chat_id"]), int(mapping["thread_id"])
            topic = int(self.data.get("topic_thread_id") or 0)
            if topic:
                return GROUP_CHAT_ID, topic
            return None

    async def recipient_user_ids(self, chat_id: int, thread_id: int) -> list[int]:
        """返回一条群消息应该转发给的所有用户。"""
        async with self._lock:
            recipients: set[int] = set()
            default_topic = int(self.data.get("topic_thread_id") or 0)
            if (
                chat_id == GROUP_CHAT_ID
                and default_topic
                and thread_id == default_topic
            ):
                recipients.update(self.data["subscribers"])
            for key, dest in self.data["mappings"].items():
                if int(dest["chat_id"]) == chat_id and (
                    int(dest["thread_id"]) == 0
                    or int(dest["thread_id"]) == thread_id
                ):
                    recipients.add(int(key))
            return sorted(recipients)


# ---------- 私聊命令 ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    store: BridgeStore = context.bot_data["store"]
    await store.remember_user(user.id, user.username)
    if not await _can_use(store, user.id):
        await message.reply_text(
            "⛔ 你没有使用该桥接机器人的权限。\n"
            "你的用户名已被记录，请联系管理员执行 /allow @你的用户名 放行后重试。"
        )
        return
    if await store.get_mapping(user.id) is None and not store.get_topic():
        await message.reply_text(
            "⚠️ 桥接目标尚未设置：请联系管理员在目标群/Topic 内发送 /bind <你的ID>，"
            "或先在默认 Topic 内发送 /set_topic。"
        )
        return
    if await store.get_mapping(user.id) is None:
        await store.add_subscriber(user.id)
    await message.reply_text(HELP_TEXT)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    store: BridgeStore = context.bot_data["store"]
    if await store.remove_user(user.id):
        await message.reply_text("👋 已断开连接，不再接收消息。发送 /start 可重新连接。")
    else:
        await message.reply_text("你当前未连接。")


async def cmd_private_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    store: BridgeStore = context.bot_data["store"]
    mapping = await store.get_mapping(user.id)
    if mapping:
        target = f"群 {mapping['chat_id']} / Topic {mapping['thread_id'] or '全部'}"
    elif store.get_topic():
        target = f"默认群 {GROUP_CHAT_ID} / Topic {store.get_topic()}"
    else:
        target = "未绑定"
    await message.reply_text(
        "📊 当前状态：\n"
        f"• 你的目标：{target}\n"
        f"• 转发模式：{BRIDGE_MODE}"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text(HELP_TEXT)


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text("未知命令，发送 /help 查看可用命令。")


async def cmd_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """私聊命令：返回发送者自己的数字 ID（同时记录用户名）。"""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    store: BridgeStore = context.bot_data["store"]
    await store.remember_user(user.id, user.username)
    username = f"（@{user.username}）" if user.username else ""
    await message.reply_text(
        f"你的数字 ID：{user.id} {username}\n"
        "把这个数字发给群管理员，就能通过 /allow 和 /bind 添加你。"
    )


# ---------- 私聊消息：外部用户 -> 目标群/Topic/频道 ----------

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.from_user is None:
        return
    store: BridgeStore = context.bot_data["store"]
    await store.remember_user(user.id, user.username)
    if not await _can_use(store, user.id):
        await message.reply_text(
            "⛔ 你没有使用该桥接机器人的权限。\n"
            "你的用户名已被记录，请联系管理员执行 /allow @你的用户名 放行后重试。"
        )
        return

    destination = await store.get_destination(user.id)
    if destination is None:
        await message.reply_text(
            "⚠️ 你还没有绑定目标。请联系管理员在目标群/Topic 内发送 /bind <你的ID>。"
        )
        return
    target_chat_id, target_thread_id = destination

    if await store.get_mapping(user.id) is None:
        first_time = await store.add_subscriber(user.id)
        if first_time:
            await message.reply_text(
                "✅ 已连接默认主题，之后该 Topic 的新消息会自动转发到这里。回复 /stop 可断开。"
            )

    try:
        if BRIDGE_MODE == "forward":
            kwargs = {
                "chat_id": target_chat_id,
                "from_chat_id": user.id,
                "message_id": message.message_id,
                "protect_content": PROTECT_CONTENT,
            }
            if target_thread_id:
                kwargs["message_thread_id"] = target_thread_id
            await context.bot.forward_message(**kwargs)
        else:
            kwargs = {
                "chat_id": target_chat_id,
                "from_chat_id": user.id,
                "message_id": message.message_id,
                "protect_content": PROTECT_CONTENT,
            }
            if target_thread_id:
                kwargs["message_thread_id"] = target_thread_id
            await context.bot.copy_message(**kwargs)
    except Forbidden as exc:
        logger.warning("无法把用户 %s 的消息发到 %s：%s", user.id, target_chat_id, exc)
        await message.reply_text("❌ 消息未能送达目标（目标可能禁止转发，或机器人没有发送权限）。")
    except TelegramError as exc:
        logger.error("把用户 %s 的消息发到 %s 失败：%s", user.id, target_chat_id, exc)
        await message.reply_text("❌ 消息转发失败，请稍后重试。")


# ---------- 群消息：目标群/Topic -> 绑定用户 ----------

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    store: BridgeStore = context.bot_data["store"]
    await store.remember_group(chat.id, chat.title)

    if message.from_user is None and message.sender_chat is None:
        return  # 服务消息（成员进出等）不转发
    if message.from_user is not None and message.from_user.id == context.bot.id:
        return  # 机器人自己发/转发的消息，避免回环

    recipients = await store.recipient_user_ids(chat.id, message.message_thread_id or 0)
    if not recipients:
        return

    for user_id in recipients:
        try:
            if BRIDGE_MODE == "forward":
                await context.bot.forward_message(
                    chat_id=user_id,
                    from_chat_id=chat.id,
                    message_id=message.message_id,
                    protect_content=PROTECT_CONTENT,
                )
            else:
                await context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=chat.id,
                    message_id=message.message_id,
                    protect_content=PROTECT_CONTENT,
                )
        except Forbidden as exc:
            logger.warning("用户 %s 无法接收消息（可能已屏蔽机器人），已自动移除：%s", user_id, exc)
            await store.remove_user(user_id)
        except TelegramError as exc:
            logger.error("向用户 %s 转发失败：%s", user_id, exc)


# ---------- 群管理命令 ----------

async def cmd_set_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _is_group_admin(update, context):
        await message.reply_text("⛔ 只有配置的管理员可以设置默认 Topic。")
        return
    if message.message_thread_id is None:
        await message.reply_text("⚠️ 请进入群内的目标 Topic，再发送 /set_topic。")
        return
    store: BridgeStore = context.bot_data["store"]
    await store.set_topic(message.message_thread_id)
    await message.reply_text(f"✅ 已将当前 Topic 设为默认桥接 Topic（thread_id={message.message_thread_id}）。")


async def cmd_topic_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _is_group_admin(update, context):
        await message.reply_text("⛔ 只有配置的管理员可以查看。")
        return
    await message.reply_text(
        f"群 ID（GROUP_CHAT_ID）：{message.chat_id}\n"
        f"当前 Topic ID（TOPIC_THREAD_ID）：{message.message_thread_id or 1}\n\n"
        "发送 /set_topic 把当前 Topic 设为默认桥接 Topic；\n"
        "发送 /bind <用户ID> 把某个用户绑定到当前 Topic。"
    )


async def cmd_bind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _is_group_admin(update, context):
        await message.reply_text("⛔ 只有配置的管理员可以绑定用户。")
        return
    if not context.args:
        await message.reply_text(
            "用法：\n"
            "/bind <用户ID或@用户名> —— 在当前群/Topic 中绑定该用户\n"
            "/bind <用户ID或@用户名> <群ID> [TopicID] —— 绑定到指定目标（TopicID 留空=整个群/频道）\n\n"
            "用 @用户名 绑定前，对方必须先私聊过机器人。"
        )
        return
    store: BridgeStore = context.bot_data["store"]
    target_user = await store.resolve_user_id(context.args[0])
    if target_user is None:
        await message.reply_text(
            "❌ 找不到该用户。数字 ID 可以直接用；@用户名 要求对方先私聊过机器人（发任意消息即可）。"
        )
        return
    try:
        if len(context.args) >= 2:
            target_chat = int(context.args[1])
            target_thread = int(context.args[2]) if len(context.args) >= 3 else 0
        else:
            target_chat = message.chat_id
            target_thread = message.message_thread_id or 0
    except ValueError:
        await message.reply_text("❌ 参数格式不正确，用户 ID 和群 ID 必须是数字。")
        return

    username = await store.get_username(target_user)
    await store.set_mapping(target_user, target_chat, target_thread)
    await message.reply_text(
        f"✅ 已把用户 {target_user}{' (@' + username + ')' if username else ''} 绑定到：\n"
        f"• 群/频道 ID：{target_chat}\n"
        f"• Topic ID：{target_thread if target_thread else '全部（不区分 Topic）'}\n\n"
        "注意：对方必须先私聊过机器人，否则机器人无法主动给 TA 发消息。"
    )


async def cmd_unbind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _is_group_admin(update, context):
        await message.reply_text("⛔ 只有配置的管理员可以操作。")
        return
    if not context.args:
        await message.reply_text("用法：/unbind <用户ID>")
        return
    store: BridgeStore = context.bot_data["store"]
    target_user = await store.resolve_user_id(context.args[0])
    if target_user is None:
        await message.reply_text("❌ 找不到该用户。数字 ID 或已私聊过的 @用户名均可。")
        return
    if await store.clear_mapping(target_user):
        await message.reply_text(f"✅ 已解除用户 {target_user} 的分流绑定，恢复为默认 Topic 订阅。")
    else:
        await message.reply_text("ℹ️ 该用户没有分流绑定。")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _is_group_admin(update, context):
        await message.reply_text("⛔ 只有配置的管理员可以操作。")
        return
    if not context.args:
        await message.reply_text("用法：/add <用户数字ID>")
        return
    store: BridgeStore = context.bot_data["store"]
    target = await store.resolve_user_id(context.args[0])
    if target is None:
        await message.reply_text("❌ 找不到该用户。数字 ID 或已私聊过的 @用户名均可。")
        return
    first = await store.add_subscriber(target)
    if first:
        await message.reply_text(
            f"✅ 已把用户 {target} 加入默认 Topic 订阅。注意：对方必须先私聊过机器人，否则机器人无法主动给 TA 发消息。"
        )
    else:
        await message.reply_text("ℹ️ 该用户已经是默认订阅者。")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _is_group_admin(update, context):
        await message.reply_text("⛔ 只有配置的管理员可以操作。")
        return
    if not context.args:
        await message.reply_text("用法：/remove <用户数字ID>")
        return
    store: BridgeStore = context.bot_data["store"]
    target = await store.resolve_user_id(context.args[0])
    if target is None:
        await message.reply_text("❌ 找不到该用户。数字 ID 或已私聊过的 @用户名均可。")
        return
    removed = await store.remove_user(target)
    await message.reply_text(f"✅ 已移除用户 {target} 的全部绑定。" if removed else "ℹ️ 该用户不在任何绑定中。")


async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _is_group_admin(update, context):
        await message.reply_text("⛔ 只有群管理员可以操作。")
        return
    if not context.args:
        await message.reply_text("用法：/allow <用户ID或@用户名>")
        return
    store: BridgeStore = context.bot_data["store"]
    target = await store.resolve_user_id(context.args[0])
    if target is None:
        await message.reply_text(
            "❌ 找不到该用户。数字 ID 可以直接用；@用户名 要求对方先私聊过机器人（发任意消息即可）。"
        )
        return
    username = await store.get_username(target)
    added = await store.add_allowed(target)
    await message.reply_text(
        f"✅ 已把 {target}{' (@' + username + ')' if username else ''} 加入白名单。"
        if added
        else "ℹ️ 该用户已经在白名单里。"
    )


async def cmd_channel_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """频道内使用：/allow <用户ID或@用户名> = 加入白名单 + 绑定到当前频道。

    频道里只有管理员能发帖，因此收到这条命令即视为频道管理员操作。
    """
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if not context.args:
        await message.reply_text("用法：/allow <用户ID或@用户名>")
        return
    store: BridgeStore = context.bot_data["store"]
    target = await store.resolve_user_id(context.args[0])
    if target is None:
        await message.reply_text(
            "❌ 找不到该用户。数字 ID 可以直接用；@用户名 要求对方先私聊过机器人（发任意消息即可）。"
        )
        return
    username = await store.get_username(target)
    await store.add_allowed(target)
    await store.set_mapping(target, chat.id, 0)
    await message.reply_text(
        f"✅ 已把 {target}{' (@' + username + ')' if username else ''} 加入白名单，"
        f"并绑定到本频道（{chat.id}）。\n"
        "之后：频道新消息会转发给 TA；TA 私聊机器人的消息会以机器人名义发布到本频道。"
    )


async def cmd_disallow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _is_group_admin(update, context):
        await message.reply_text("⛔ 只有群管理员可以操作。")
        return
    if not context.args:
        await message.reply_text("用法：/disallow <用户ID或@用户名>")
        return
    store: BridgeStore = context.bot_data["store"]
    target = await store.resolve_user_id(context.args[0])
    if target is None:
        await message.reply_text(
            "❌ 找不到该用户。数字 ID 可以直接用；@用户名 要求对方先私聊过机器人（发任意消息即可）。"
        )
        return
    username = await store.get_username(target)
    removed = await store.remove_allowed(target)
    await store.remove_user(target)
    await message.reply_text(
        f"✅ 已把 {target}{' (@' + username + ')' if username else ''} 移出白名单，并解除了他/她的全部绑定（订阅和分流映射）。"
        if removed
        else f"ℹ️ {target} 不在白名单里，但已解除他/她的全部绑定。"
    )


async def cmd_group_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not await _is_group_admin(update, context):
        await message.reply_text("⛔ 只有配置的管理员可以查看。")
        return
    store: BridgeStore = context.bot_data["store"]
    subs = await store.list_subscribers()
    mappings = await store.list_mappings()
    lines = [
        "📊 桥接状态：",
        f"• 默认群：{GROUP_CHAT_ID}",
        f"• 默认 Topic：{store.get_topic() or '未设置'}",
        f"• 转发模式：{BRIDGE_MODE}",
    ]
    sub_names = []
    for uid in subs:
        name = await store.get_username(uid)
        sub_names.append(f"{uid}{' (@' + name + ')' if name else ''}")
    lines.append(f"• 默认订阅者（{len(subs)}）：{', '.join(sub_names) if sub_names else '无'}")
    allowed = await store.list_allowed()
    allowed_names = []
    for uid in allowed:
        name = await store.get_username(uid)
        allowed_names.append(f"{uid}{' (@' + name + ')' if name else ''}")
    lines.append(f"• 白名单（{len(allowed)}）：{', '.join(allowed_names) if allowed_names else '空=不允许任何人，需用 /allow 添加'}")
    lines.append(f"• 分流绑定（{len(mappings)}）：")
    if mappings:
        for uid, dest in sorted(mappings.items()):
            name = await store.get_username(uid)
            lines.append(
                f"  {uid}{' (@' + name + ')' if name else ''} → 群/频道 {dest['chat_id']} / Topic {dest['thread_id'] or '全部'}"
            )
    else:
        lines.append("  无")
    await message.reply_text("\n".join(lines))


async def cmd_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """让机器人退出某个群/频道。

    - 私聊（管理员）：/leave <群ID>，可远程让机器人退出任何它所在的群；
    - 群内：/leave，群管理员可直接让机器人退出当前群。
    """
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return

    store: BridgeStore = context.bot_data["store"]
    target_chat: Optional[int] = None

    if chat.type == "private":
        if user.id not in ADMIN_USER_IDS:
            await message.reply_text("⛔ 只有配置的管理员可以远程让我退群。")
            return
        if not context.args:
            await message.reply_text("用法（私聊）：/leave <群ID>")
            return
        try:
            target_chat = int(context.args[0])
        except ValueError:
            await message.reply_text("❌ 群 ID 格式不正确（例如 -1001234567890）。")
            return
    else:
        if not await _is_group_admin(update, context):
            await message.reply_text("⛔ 只有群管理员可以让我退群。")
            return
        target_chat = chat.id

    try:
        await context.bot.leave_chat(chat_id=target_chat)
    except TelegramError as exc:
        await message.reply_text(f"❌ 退群失败：{exc}")
        return

    cleared = await store.remove_chat_mappings(target_chat)
    await message.reply_text(
        f"✅ 已退出群 {target_chat}，并清理了 {cleared} 个绑定关系。"
    )


async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """私聊管理员：查看机器人记录到的所有群。"""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if user.id not in ADMIN_USER_IDS:
        await message.reply_text("⛔ 只有配置的管理员可以查看。")
        return
    store: BridgeStore = context.bot_data["store"]
    groups = await store.list_groups()
    if not groups:
        await message.reply_text(
            "📋 目前没有记录到任何群。\n"
            "机器人只会记录它收到过消息的群；新功能部署后，需要群里有人发消息（或机器人刚被拉进群）才会出现。"
        )
        return
    lines = ["📋 机器人所在群："]
    for chat_id, info in sorted(groups.items()):
        title = info.get("title") or "(无标题)"
        lines.append(f"• {title}（{chat_id}）")
    lines.append("\n用 /leave <群ID> 可让机器人退出任意一个群。")
    await message.reply_text("\n".join(lines))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("处理更新时出错：%s", context.error, exc_info=context.error)


def main() -> None:
    store = BridgeStore(DATA_FILE)
    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["store"] = store

    app.add_handler(CommandHandler("start", cmd_start, filters=PRIVATE_FILTER))
    app.add_handler(CommandHandler("stop", cmd_stop, filters=PRIVATE_FILTER))
    app.add_handler(CommandHandler("status", cmd_private_status, filters=PRIVATE_FILTER))
    app.add_handler(CommandHandler("help", cmd_help, filters=PRIVATE_FILTER))
    app.add_handler(CommandHandler("id", cmd_my_id, filters=PRIVATE_FILTER))
    app.add_handler(MessageHandler(PRIVATE_FILTER & ~filters.COMMAND, handle_private_message))

    app.add_handler(CommandHandler("set_topic", cmd_set_topic, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("topic", cmd_topic_info, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("bind", cmd_bind, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("unbind", cmd_unbind, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("add", cmd_add, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("remove", cmd_remove, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("allow", cmd_allow, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("disallow", cmd_disallow, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("allow", cmd_channel_allow, filters=CHANNEL_FILTER))
    app.add_handler(CommandHandler("status", cmd_group_status, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("leave", cmd_leave, filters=PRIVATE_FILTER))
    app.add_handler(CommandHandler("leave", cmd_leave, filters=GROUP_FILTER))
    app.add_handler(CommandHandler("groups", cmd_groups, filters=PRIVATE_FILTER))
    app.add_handler(MessageHandler(GROUP_FILTER & ~filters.COMMAND, handle_group_message))
    app.add_handler(MessageHandler(PRIVATE_FILTER & filters.COMMAND, cmd_unknown))

    app.add_error_handler(error_handler)

    logger.info(
        "机器人启动：默认群=%s，默认Topic=%s，模式=%s，绑定用户=%d",
        GROUP_CHAT_ID,
        store.get_topic() or "未设置",
        BRIDGE_MODE,
        len(store.data.get("mappings", {})),
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

import argparse
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import ExitStack, closing, contextmanager
from itertools import chain
from pathlib import Path
from threading import Event

from cryptography.fernet import InvalidToken

from archive import Archive, load_key, start_new_archive
from formatting import author_link, diff_parts, format_time, media_author, notice_parts
from media_vault import MediaVault, download_worker
from telegram_api import Telegram, TelegramError


ROOT = Path(__file__).resolve().parent
LOVE_HEART = "\n".join((
    "🤍❤️❤️🤍🤍🤍❤️❤️🤍",
    "❤️❤️❤️❤️🤍❤️❤️❤️❤️",
    "❤️❤️❤️❤️❤️❤️❤️❤️❤️",
    "❤️❤️❤️❤️❤️❤️❤️❤️❤️",
    "🤍❤️❤️❤️❤️❤️❤️❤️🤍",
    "🤍🤍❤️❤️❤️❤️❤️🤍🤍",
    "🤍🤍🤍❤️❤️❤️🤍🤍🤍",
    "🤍🤍🤍🤍❤️🤍🤍🤍🤍",
))
LOVE_FRAMES = (
    "🤍",
    "🩷",
    "🤍🩷🤍🩷🤍\n🩷🩷🩷🩷🩷\n🤍🩷🩷🩷🤍\n🤍🤍🩷🤍🤍",
    LOVE_HEART.replace("❤️", "🩷"),
    LOVE_HEART,
    LOVE_HEART.replace("❤️", "💗"),
    f"{LOVE_HEART}\n\nЯ люблю тебя",
    f"{LOVE_HEART.replace('❤️', '🩷')}\n\nЯ люблю тебя ❤️",
)
LOVE_FRAME_DELAY = 1.2
UPDATES = [
    "message", "business_connection", "business_message",
    "edited_business_message", "deleted_business_messages",
]
MEDIA = {
    "animation": ("sendAnimation", "GIF"),
    "live_photo": ("sendLivePhoto", "живое фото"),
    "photo": ("sendPhoto", "фото"),
    "video": ("sendVideo", "видео"),
    "video_note": ("sendVideoNote", "видеосообщение"),
    "voice": ("sendVoice", "голосовое сообщение"),
    "audio": ("sendAudio", "аудио"),
    "document": ("sendDocument", "файл"),
    "sticker": ("sendSticker", "стикер"),
}
EXTENSIONS = {"photo": ".jpg", "live_photo": ".mp4", "video": ".mp4", "video_note": ".mp4",
              "voice": ".ogg", "audio": ".mp3"}
REPLY_MEDIA = ("photo", "video", "voice", "video_note")
TIMER_FIELDS = ("ttl_seconds", "view_once", "is_view_once", "is_one_time", "one_time", "self_destruct")
HELP = ("Аккаунт привязан. Подключите бота через Настройки → Автоматизация чатов "
        "и выберите нужные личные чаты.\n\n"
        "Сюда будут приходить уведомления о правках и удалениях.\n\n"
        "Для «бомбочки» свайпните по ней до открытия и отправьте любой ответ: слово, эмодзи, стикер или другое сообщение. "
        "Собеседник увидит ваш ответ. "
        "Файл и уведомление придут только сюда. Команда в исходном чате не нужна.\n\n"
        "/status — статус подключения\n/last — последнее изменение или удаление\n"
        "/recent — последние сообщения в архиве\n"
        "/history номер — полная история сообщения\n"
        "/media номер [версия] — файл из локального архива\n\n"
        ".luv — отправьте в подключённом чате, чтобы заменить команду анимацией «Я люблю тебя ❤️».")


def read_config():
    config = {}
    if (ROOT / ".env").is_file():
        for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator:
                config[name.strip()] = value.strip()
    token = os.environ.get("BOT_TOKEN", config.get("BOT_TOKEN", "")).strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise RuntimeError("Укажите BOT_TOKEN из @BotFather в файле .env. См. README.md.")
    default_base = "https://api.telegram.org"
    base = os.environ.get("BOT_API_URL", config.get("BOT_API_URL", "")).strip().rstrip("/") or default_base
    if base != default_base and not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", base):
        raise RuntimeError("BOT_API_URL должен указывать на локальный сервер, например http://127.0.0.1:8081.")
    return token, base


@contextmanager
def single_instance(path):
    with path.open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Этот архив уже использует другой процесс.") from None
        yield


def display_name(user):
    name = " ".join(user[key] for key in ("first_name", "last_name") if user.get(key))
    if user.get("username"):
        return f"{name} (@{user['username']})" if name else f"@{user['username']}"
    return name or str(user.get("id", "неизвестно"))


def restricted_message(message):
    return any(message.get(field) for field in (
        "ephemeral_message_id", "receiver_user", "has_protected_content", "paid_media", *TIMER_FIELDS,
    ))


def largest_photo(variants):
    return max(reversed(variants), key=lambda item: item.get("width", 0) * item.get("height", 0))


def message_attachment(message, fields=MEDIA):
    for field in fields:
        media = message.get(field)
        if not media:
            continue
        variants = media if isinstance(media, list) else [media]
        best = largest_photo(variants) if field == "photo" else variants[0]
        if field == "live_photo" and not best.get("photo"):
            continue
        if best.get("file_id"):
            attachment = {
                "type": field,
                "file_id": best["file_id"],
                "identity": best.get("file_unique_id", best["file_id"]),
                "name": best.get("file_name"),
                "size": best.get("file_size"),
                "spoiler": bool(message.get("has_media_spoiler")),
            }
            if field == "live_photo":
                still = message_attachment({"photo": best["photo"]}, ("photo",))
                if not still:
                    continue
                attachment["photo_id"] = still["file_id"]
                attachment["still"] = still
            return attachment
    return None


def snapshot_fields(message, owner_id, attachment):
    text = message.get("text", message.get("caption", ""))
    if not text and attachment is None:
        return None
    sender = message.get("from", {})
    reply = message.get("reply_to_message") or {}
    reply_text = reply.get("text") if not restricted_message(reply) else None
    return {
        "author": "Вы" if sender.get("id") == owner_id else display_name(sender),
        "author_id": sender.get("id"),
        "author_username": sender.get("username"),
        "sent_at": message.get("date"),
        "reply": reply_text[:240] if reply_text else None,
        "text": text,
        "entities": message.get("entities", message.get("caption_entities", [])),
        "attachment": attachment,
    }


def message_snapshot(message, owner_id):
    if restricted_message(message):
        return None
    for field in MEDIA:
        media = message.get(field)
        variants = media if isinstance(media, list) else [media]
        if any(item and any(item.get(flag) for flag in TIMER_FIELDS) for item in variants):
            return None
    return snapshot_fields(message, owner_id, message_attachment(message))


def attachment_identity(attachment):
    if attachment:
        return tuple(attachment[field] for field in ("type", "identity", "name", "spoiler"))
    return None


def content(snapshot):
    return snapshot["text"], snapshot["entities"], attachment_identity(snapshot["attachment"])


def describe(snapshot):
    if snapshot is None:
        return "Сохранённой версии нет."
    attachment = snapshot["attachment"]
    lines = [snapshot["text"]] if snapshot["text"] else []
    if attachment:
        label = MEDIA[attachment["type"]][1]
        if attachment["name"]:
            label += f" · {attachment['name']}"
        lines.append(f"[{label}]")
    return "\n".join(lines)


def upload_name(attachment, first_chunk):
    if attachment.get("name"):
        return attachment["name"]
    kind = attachment["type"]
    extension = EXTENSIONS.get(kind, "")
    if kind == "sticker":
        if first_chunk.startswith(b"\x1f\x8b"):
            extension = ".tgs"
        elif first_chunk.startswith(b"\x1a\x45\xdf\xa3"):
            extension = ".webm"
        else:
            extension = ".webp"
    elif kind == "animation":
        extension = ".gif" if first_chunk.startswith(b"GIF8") else ".mp4"
    elif kind in ("voice", "audio"):
        if first_chunk[4:8] == b"ftyp":
            extension = ".m4a"
        elif first_chunk.startswith(b"ID3") or (len(first_chunk) > 1 and first_chunk[0] == 255 and first_chunk[1] & 224 == 224):
            extension = ".mp3"
    return f"{kind}{extension}"


class DialogSpy:
    def __init__(self, api, archive, vault=None, *, bot_id=None, bot_username=None):
        self.api = api
        self.archive = archive
        self.vault = vault
        self.bot_id = bot_id
        self.bot_username = bot_username
        self.pair_code = secrets.token_urlsafe(24)

    def queue_text(self, owner_id, text):
        # Telegram limits the length of a message. Allow for two UTF-16 units per character.
        for start in range(0, len(text), 2000):
            self.archive.queue("sendMessage", {
                "chat_id": owner_id,
                "text": text[start:start + 2000],
                "link_preview_options": {"is_disabled": True},
            })

    def queue_attachment(self, owner_id, attachment, label, *, reply_ref=None, archive_ref=None, local=False):
        field = attachment["type"]
        parameters = {"chat_id": owner_id, field: attachment["file_id"]}
        if field == "live_photo":
            parameters["photo"] = attachment["photo_id"]
        if label and field not in ("sticker", "video_note"):
            parameters["caption"] = label
            if field in ("photo", "live_photo", "video", "animation"):
                parameters["show_caption_above_media"] = True
        elif label:
            self.queue_text(owner_id, label)
        if attachment["spoiler"] and field in ("animation", "live_photo", "photo", "video"):
            parameters["has_spoiler"] = True
        if self.vault:
            parameters["_backup"] = attachment
        if reply_ref:
            parameters["_reply_ref"] = reply_ref
        if archive_ref:
            parameters["_archive_ref"] = archive_ref
        if local:
            parameters["_local"] = True
        self.archive.queue(MEDIA[field][0], parameters)

    def can_download(self, attachment):
        size = attachment.get("size")
        return self.api.local or size is None or size <= 20 * 1024 ** 2

    def queue_download(self, attachment, *, priority=False):
        if attachment and self.vault:
            for part in (attachment, attachment.get("still")):
                if part and self.can_download(part):
                    key = self.archive.media_key(part)
                    if not self.vault.exists(key):
                        self.archive.queue_download(key, part["file_id"], priority=priority)

    def capture_reply(self, message, owner_id):
        if message.get("from", {}).get("id") != owner_id or message.get("sender_business_bot"):
            return
        reply = message.get("reply_to_message")
        if not reply or reply.get("paid_media"):
            return
        chat = message["chat"]
        message_id = reply.get("message_id", 0)
        if message_id <= 0 or reply.get("chat", {}).get("id") != chat["id"]:
            return
        if reply.get("from", {}).get("id") == owner_id:
            return
        attachment = message_attachment(reply, REPLY_MEDIA)
        if not attachment or self.archive.last_message_event(chat["id"], message_id):
            return

        snapshot = snapshot_fields(reply, owner_id, attachment)
        event = {"kind": "reply", "chat": display_name(chat), "message_id": message_id,
                 "before": None, "after": snapshot, "time": reply.get("date")}
        reference = self.archive.save_event(chat["id"], event)
        self.queue_download(attachment, priority=True)
        label = media_author(snapshot, event["chat"])
        self.queue_attachment(owner_id, attachment, label, reply_ref=reference)

    def queue_diff(self, owner_id, before, after):
        if before and after and before["text"] != after["text"]:
            self.queue_html(owner_id, diff_parts(before["text"], after["text"]))

    def queue_html(self, owner_id, parts):
        for part in parts:
            self.archive.queue("sendMessage", {
                "chat_id": owner_id, "text": part, "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            })

    def queue_notice(self, owner_id, event):
        before, after = event["before"], event["after"]
        deleted = event["kind"] == "delete"
        known = after or before
        author = author_link(known, event["chat"])
        if deleted:
            header = f"{author} — сообщение удалено:"
            sections = [("", describe(before))]
        else:
            header = f"{author} изменил(а) сообщение:"
            sections = [("Old", describe(before)), ("New", describe(after))]
        photo_only = known and known["attachment"] and known["attachment"]["type"] in ("photo", "live_photo") and not (
            (before and before["text"]) or (after and after["text"])
        )
        if not photo_only:
            self.queue_html(owner_id, notice_parts(header, sections, self.bot_username))
        reference = event.get("ref")

        old_media = before and before["attachment"]
        new_media = after and after["attachment"]
        if deleted or attachment_identity(old_media) != attachment_identity(new_media):
            if old_media:
                self.queue_attachment(owner_id, old_media, media_author(before, event["chat"]),
                                      archive_ref=reference)
            if new_media:
                self.queue_attachment(owner_id, new_media, media_author(after, event["chat"]), archive_ref=reference)

    def show_history(self, owner_id, history):
        first = history[0]
        self.queue_text(owner_id, f"История сообщения №{first['message_id']}\nЧат: {first['chat']}")
        version = 0
        for event in history:
            before, after = event["before"], event["after"]
            if after is None:
                label = "Сообщение удалено"
            else:
                version += 1
                label = f"Версия {version} · {after['author']}"
            if event.get("time"):
                label += f"\n{format_time(event['time'])}"
            if after:
                label += "\n\n" + describe(after)
                if after["attachment"]:
                    label += f"\nЛокальный файл: /media {first['ref']} {version}"
            self.queue_text(owner_id, label)
            if after:
                self.queue_diff(owner_id, before, after)

    def archive_command(self, owner_id, command, argument):
        args = argument.split()
        if len(args) not in ((1,) if command == "/history" else (1, 2)) or not all(
            re.fullmatch(r"[1-9][0-9]{0,17}", value) for value in args
        ):
            suffix = " [версия]" if command == "/media" else ""
            self.queue_text(owner_id, f"Формат команды — {command} номер_из_уведомления{suffix}")
            return
        history = self.archive.history(int(args[0]))
        if not history:
            self.queue_text(owner_id, "В архиве нет сообщения с таким номером.")
        elif command == "/history":
            self.show_history(owner_id, history)
        else:
            versions = [event["after"] for event in history if event["after"]]
            version = int(args[1]) if len(args) == 2 else len(versions)
            if 1 <= version <= len(versions) and versions[version - 1]["attachment"]:
                snapshot = versions[version - 1]
                attachment = snapshot["attachment"]
                key = self.archive.media_key(attachment)
                if self.vault and self.vault.exists(key):
                    self.queue_attachment(owner_id, attachment, media_author(snapshot, history[0]["chat"]), local=True)
                else:
                    self.queue_text(owner_id, "Локальной копии пока нет. Для облачного Bot API доступны файлы до 20 МБ.")
            else:
                self.queue_text(owner_id, "У этой версии нет сохранённого вложения.")

    def command(self, message, owner_id):
        sender = message.get("from", {})
        chat = message["chat"]
        if chat["type"] != "private" or chat["id"] != sender.get("id"):
            return
        command, _, argument = message.get("text", "").partition(" ")
        command = command.split("@", 1)[0]
        if owner_id is None:
            if command == "/start" and secrets.compare_digest(argument.strip().encode(), self.pair_code.encode()):
                self.archive.set_setting("owner", sender["id"])
                self.queue_text(sender["id"], HELP)
            return
        if sender["id"] != owner_id:
            return
        if command in ("/start", "/help"):
            self.queue_text(owner_id, HELP)
        elif command == "/status":
            if self.archive.setting("connection"):
                self.queue_text(owner_id, "Telegram подтвердил подключение. Проверьте выбранные чаты в настройках Telegram.")
            else:
                self.queue_text(owner_id, "Подключение пока не подтверждено. Добавьте бота в разделе «Автоматизация чатов» и проверьте новый входящий текст в одном из выбранных чатов.")
        elif command == "/last":
            event = self.archive.last_notice()
            if event:
                self.queue_notice(owner_id, event)
            else:
                self.queue_text(owner_id, "В архиве пока нет правок или удалений.")
        elif command == "/recent":
            entries = []
            for event in self.archive.recent():
                summary = describe(event["after"] or event["before"])[:120].replace("\n", " ")
                marker = " · удалено" if event["kind"] == "delete" else ""
                entries.append(f"/history {event['ref']} · {event['chat']}{marker}\n{summary}")
            self.queue_text(owner_id, "\n\n".join(entries) or "Архив пока пуст.")
        elif command in ("/history", "/media"):
            self.archive_command(owner_id, command, argument)

    def accepts_connection(self, connection_id, owner_id):
        if not connection_id:
            return False
        if self.archive.setting("connection") == connection_id:
            return True
        try:
            connection = self.api.call("getBusinessConnection", business_connection_id=connection_id)
        except TelegramError as error:
            if error.code in (400, 403, 404):
                return False
            raise
        if connection["user"]["id"] != owner_id or not connection["is_enabled"]:
            return False
        self.archive.set_setting("connection", connection_id)
        return True

    def handle_message(self, message, owner_id, kind):
        chat = message["chat"]
        message_id = message["message_id"]
        if chat["type"] != "private" or chat["id"] == self.bot_id or message_id == 0:
            return
        if not self.accepts_connection(message.get("business_connection_id"), owner_id):
            return
        if kind == "new":
            self.capture_reply(message, owner_id)
            if self.love_command(message, owner_id):
                return
        after = message_snapshot(message, owner_id)
        if after is None:
            return
        previous = self.archive.last_message_event(chat["id"], message_id)
        before = previous["after"] if previous else None
        if previous and (before is None or kind == "new" or content(before) == content(after)):
            return
        timestamp = message.get("edit_date") if kind == "edit" else message.get("date")
        event = {"kind": kind, "chat": display_name(chat), "message_id": message_id,
                 "before": before, "after": after, "time": timestamp}
        event["ref"] = self.archive.save_event(chat["id"], event)
        self.queue_download(after["attachment"])
        if kind == "edit":
            self.queue_notice(owner_id, event)

    def handle_deletions(self, update, owner_id):
        chat = update["chat"]
        if chat["type"] != "private" or chat["id"] == self.bot_id:
            return
        if not self.accepts_connection(update["business_connection_id"], owner_id):
            return
        for message_id in update["message_ids"]:
            previous = self.archive.last_message_event(chat["id"], message_id)
            if previous and previous["kind"] in ("delete", "command"):
                continue
            event = {"kind": "delete", "chat": display_name(chat), "message_id": message_id,
                     "before": previous["after"] if previous else None, "after": None,
                     "time": int(time.time()), "context": self.archive.context(chat["id"], message_id)}
            event["ref"] = self.archive.save_event(chat["id"], event)
            self.queue_notice(owner_id, event)

    def handle_update(self, update):
        with self.archive.db:
            if not self.archive.accept_update(update["update_id"]):
                return
            owner_id = self.archive.setting("owner")
            if "message" in update:
                self.command(update["message"], owner_id)
            elif owner_id is None:
                return
            elif "business_connection" in update:
                connection = update["business_connection"]
                if connection["user"]["id"] != owner_id:
                    return
                if connection["is_enabled"]:
                    self.archive.set_setting("connection", connection["id"])
                    self.queue_text(owner_id, "Бот подключён. Сюда будут приходить уведомления о правках и удалениях.")
                elif self.archive.setting("connection") == connection["id"]:
                    self.archive.set_setting("connection", None)
                    self.queue_text(owner_id, "Telegram отключил бота от личных чатов.")
            elif "business_message" in update:
                self.handle_message(update["business_message"], owner_id, "new")
            elif "edited_business_message" in update:
                self.handle_message(update["edited_business_message"], owner_id, "edit")
            elif "deleted_business_messages" in update:
                self.handle_deletions(update["deleted_business_messages"], owner_id)

    def send_pending(self):
        for _ in range(10):
            item = self.archive.pending()
            if item is None:
                return
            item_id, payload = item
            method, parameters = payload["method"], payload["parameters"]
            owner_id = self.archive.setting("owner")
            if method == "loveAnimation":
                self.deliver_love(item_id, parameters, owner_id)
                continue
            if owner_id is None or parameters.get("chat_id") != owner_id or "business_connection_id" in parameters:
                logging.error("Очередь содержит отправку за пределы личного чата владельца")
                with self.archive.db:
                    self.archive.delivered(item_id)
                continue
            reply_ref = parameters.get("_reply_ref")
            archive_ref = parameters.get("_archive_ref")
            local = parameters.get("_local")
            try:
                self.deliver(method, parameters)
            except TelegramError as error:
                if error.temporary or error.code == 401:
                    raise
                logging.warning("Telegram отклонил отправку (%s)", error)
                if method != "sendMessage" and error.code in (400, 404):
                    with self.archive.db:
                        if reply_ref:
                            text = f"Telegram не выдал доступный файл из ответа. Проверить локальную копию: /media {reply_ref}"
                        elif archive_ref:
                            text = f"Вложение пока недоступно. Проверить локальную копию: /media {archive_ref}"
                            if parameters.get("caption"):
                                text = f"{parameters['caption']}\n\n{text}"
                        elif method == "archivedDocument" or local:
                            text = "Не удалось отправить локальную копию вложения."
                        else:
                            text = "Вложение пока недоступно. Проверьте командой /media номер_из_уведомления."
                        self.queue_text(parameters["chat_id"], text)
                        self.archive.delivered(item_id)
                    continue
            with self.archive.db:
                self.archive.delivered(item_id)

    def love_command(self, message, owner_id):
        if message.get("from", {}).get("id") != owner_id or message.get("sender_business_bot"):
            return False
        if message.get("text", "").strip() != ".luv":
            return False
        chat = message["chat"]
        if self.archive.last_message_event(chat["id"], message["message_id"]):
            return True
        self.archive.save_event(chat["id"], {
            "kind": "command", "chat": display_name(chat), "message_id": message["message_id"],
            "before": None, "after": None, "time": message.get("date"),
        })
        self.archive.queue("loveAnimation", {
            "chat_id": chat["id"], "business_connection_id": message["business_connection_id"],
            "command_id": message["message_id"], "owner_id": owner_id, "chat": display_name(chat),
        })
        return True

    def love_failed(self, item_id, owner_id, text):
        with self.archive.db:
            self.archive.delivered(item_id)
            if owner_id is not None:
                self.queue_text(owner_id, text)

    def deliver_love(self, item_id, parameters, owner_id):
        connection_id = parameters["business_connection_id"]
        if owner_id is None or parameters.get("owner_id") != owner_id or self.archive.setting("connection") != connection_id:
            self.love_failed(item_id, owner_id, "Для .luv нужно действующее подключение бота к вашему аккаунту.")
            return
        try:
            self.advance_love(item_id, parameters, owner_id)
        except TelegramError as error:
            if error.temporary or error.code == 401:
                raise
            logging.warning("Telegram отклонил команду .luv (%s)", error)
            text = "Telegram прервал анимацию .luv. Проверьте права бота в настройках подключения." if parameters.get("message_id") else (
                "Telegram не разрешил отправить .luv. Проверьте права бота и что собеседник писал в этом чате за последние 24 часа."
            )
            self.love_failed(item_id, owner_id, text)
            return

    def advance_love(self, item_id, parameters, owner_id):
        connection_id = parameters["business_connection_id"]
        target = {"business_connection_id": connection_id, "chat_id": parameters["chat_id"]}
        if not parameters.get("message_id"):
            connection = self.api.call("getBusinessConnection", business_connection_id=connection_id)
            rights = connection.get("rights") or {}
            if connection["user"]["id"] != owner_id or not connection["is_enabled"]:
                self.love_failed(item_id, owner_id, "Telegram отключил бота. Подключите его заново для команды .luv.")
                return
            if not rights.get("can_delete_all_messages") or (not parameters.get("sent") and not rights.get("can_reply")):
                self.love_failed(item_id, owner_id, "Для .luv включите права «Отвечать» и «Удалять все сообщения» "
                                 "в настройках подключения бота. Затем отправьте .luv заново.")
                return
            if parameters.get("sent"):
                # Finish deletion for a previously sent animation from an older version.
                self.api.call("deleteBusinessMessages", business_connection_id=connection_id,
                              message_ids=[parameters["command_id"]])
                with self.archive.db:
                    self.archive.delivered(item_id)
                return
            sent = self.api.call("sendMessage", **target, text=LOVE_FRAMES[0])
            parameters.update(message_id=sent["message_id"], frame=0)
            with self.archive.db:
                self.archive.save_event(parameters["chat_id"], {
                    "kind": "command", "chat": parameters.get("chat", str(parameters["chat_id"])),
                    "message_id": sent["message_id"], "before": None, "after": None, "time": int(time.time()),
                })
                self.archive.update_pending(item_id, "loveAnimation", parameters)
        if not parameters.get("deleted"):
            self.api.call("deleteBusinessMessages", business_connection_id=connection_id,
                          message_ids=[parameters["command_id"]])
            parameters["deleted"] = True
            with self.archive.db:
                self.archive.update_pending(item_id, "loveAnimation", parameters, LOVE_FRAME_DELAY)
            return
        frame = parameters["frame"] + 1
        try:
            self.api.call("editMessageText", **target, message_id=parameters["message_id"], text=LOVE_FRAMES[frame])
        except TelegramError as error:
            if not error.message_not_modified:
                raise
        parameters["frame"] = frame
        with self.archive.db:
            if frame == len(LOVE_FRAMES) - 1:
                self.archive.delivered(item_id)
            else:
                self.archive.update_pending(item_id, "loveAnimation", parameters, LOVE_FRAME_DELAY)

    def deliver(self, method, parameters):
        backup = parameters.pop("_backup", None)
        reply_ref = parameters.pop("_reply_ref", None)
        archive_ref = parameters.pop("_archive_ref", None)
        local = parameters.pop("_local", False) or method == "archivedDocument"
        if not local:
            try:
                sent = self.api.call(method, **parameters)
                if reply_ref and backup and isinstance(sent, dict):
                    copy = message_attachment(sent)
                    if copy and self.can_download(copy):
                        key = self.archive.media_key(backup)
                        if not self.vault.exists(key):
                            with self.archive.db:
                                self.archive.queue_download(key, copy["file_id"], priority=True)
                return
            except TelegramError as error:
                if not backup or error.code not in (400, 404):
                    raise

        if not self.vault or not backup:
            raise TelegramError("archivedDocument", 400)
        field = backup["type"]
        method = MEDIA[field][0]
        parameters.pop(field, None)
        if field == "document":
            parameters["disable_content_type_detection"] = True
        if field == "live_photo":
            parameters["photo"] = backup["photo_id"]
        try:
            with ExitStack() as stack:
                files = [self.upload_part(stack, backup, bool(reply_ref or archive_ref))]
                still = backup.get("still")
                if still and self.vault.exists(self.archive.media_key(still)):
                    parameters.pop("photo", None)
                    files.append(self.upload_part(stack, still, False))
                self.api.upload(method, parameters, files)
        except (InvalidToken, OSError):
            raise TelegramError(method, 400) from None

    def upload_part(self, stack, attachment, fetch):
        key = self.archive.media_key(attachment)
        if fetch and not self.vault.exists(key) and self.can_download(attachment):
            with self.api.open_file(attachment["file_id"]) as (source, expected):
                self.vault.capture(key, source, expected, Event())
        size, chunks = stack.enter_context(self.vault.read(key))
        first_chunk = next(chunks, b"")
        return attachment["type"], upload_name(attachment, first_chunk), size, chain([first_chunk], chunks)

    def run(self):
        offset = None
        last_poll = time.monotonic()
        send_after = 0
        while True:
            try:
                if time.monotonic() >= send_after:
                    try:
                        self.send_pending()
                    except TelegramError as error:
                        if not error.temporary:
                            raise
                        logging.warning("Повторная отправка уведомлений отложена (%s)", error)
                        send_after = time.monotonic() + (error.retry_after or 5)

                if time.monotonic() - last_poll >= 86400:
                    offset = None
                timeout = 1 if self.archive.has_pending() else 25
                parameters = {"timeout": timeout, "allowed_updates": UPDATES}
                if offset is not None:
                    parameters["offset"] = offset
                updates = self.api.call("getUpdates", **parameters)
                last_poll = time.monotonic()
                if not updates:
                    offset = None
                for update in updates:
                    self.handle_update(update)
                    offset = update["update_id"] + 1
            except TelegramError as error:
                if not error.temporary:
                    raise
                logging.warning("Повторный запрос к Telegram отложен (%s)", error)
                time.sleep(error.retry_after or 5)


def main(*, new_archive=False):
    api = Telegram(*read_config())
    me = api.call("getMe")
    if not me.get("can_connect_to_business"):
        logging.warning("Включите Secretary Mode для @%s в @BotFather, чтобы подключить личные чаты.",
                        me["username"])
    if api.call("getWebhookInfo")["url"]:
        raise RuntimeError("У бота уже настроен webhook. Для этого проекта используйте отдельного бота или отключите прежний webhook.")

    directory = ROOT / "data"
    directory.mkdir(exist_ok=True)
    database = directory / f"{me['id']}.sqlite3"
    with single_instance(database.with_suffix(".lock")):
        reset_request = ROOT / "new-archive.once"
        if new_archive or reset_request.is_file():
            backup = start_new_archive(me["id"], database)
            reset_request.unlink(missing_ok=True)
            print(f"Резервная копия архива и медиа сохранена в {backup}\nНовый архив создан.", flush=True)
            if new_archive:
                print("Теперь запустите python bot.py и откройте ссылку привязки.", flush=True)
                return
        key = load_key(me["id"], database)
        archive = Archive(database, key)
        with closing(archive.db):
            vault = MediaVault(database.with_suffix(".media"), archive.cipher)
            bot = DialogSpy(api, archive, vault, bot_id=me["id"], bot_username=me["username"])
            if archive.setting("owner") is None:
                print(f"Откройте ссылку в своём Telegram и нажмите «Запустить»:\n"
                      f"https://t.me/{me['username']}?start={bot.pair_code}", flush=True)
            else:
                print(f"@{me['username']} запущен. Для остановки нажмите Ctrl+C.", flush=True)
            with download_worker(api, vault, database, key):
                bot.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Личный архив Telegram")
    parser.add_argument("--new-archive", action="store_true",
                        help="сохранить резервную копию архива и медиа, создать новый архив и выйти")
    arguments = parser.parse_args()
    try:
        main(new_archive=arguments.new_archive)
    except KeyboardInterrupt:
        print("Бот остановлен.")
    except TelegramError as error:
        if error.code == 401:
            raise SystemExit("Telegram отклонил токен. Проверьте BOT_TOKEN в .env.") from None
        if error.method == "getUpdates" and error.code == 409:
            raise SystemExit("Этот бот уже запущен в другом процессе или использует webhook.") from None
        raise SystemExit(f"Ошибка Telegram ({error}). Проверьте подключение и перезапустите бота.") from None
    except InvalidToken:
        raise SystemExit("Ключ не подходит к архиву. Восстановите исходный ключ.") from None
    except (RuntimeError, OSError, sqlite3.Error, ValueError) as error:
        raise SystemExit(str(error)) from None

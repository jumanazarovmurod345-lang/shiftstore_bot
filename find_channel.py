"""Диагностический скрипт для автоматического определения CHANNEL_ID.

Скрипт читает BOT_TOKEN из .env, опрашивает Telegram Bot API и выводит
ID каналов и групп, где состоит бот или куда публикуются посты.
Токены и конфиденциальные данные не выводятся.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


def get_bot_token() -> str:
    env_file = Path(".env")
    if not env_file.exists():
        print("❌ Ошибка: файл .env не найден в текущей директории.")
        sys.exit(1)
    content = env_file.read_text(encoding="utf-8")
    m = re.search(r"^BOT_TOKEN=([^\r\n]+)", content, re.MULTILINE)
    if not m or not m.group(1).strip():
        print("❌ Ошибка: BOT_TOKEN не задан в .env файле.")
        sys.exit(1)
    return m.group(1).strip().strip("'\"")


def main() -> None:
    token = get_bot_token()
    base_url = f"https://api.telegram.org/bot{token}"

    print("🔍 Подключение к Telegram Bot API...")
    try:
        me_resp = json.loads(urllib.request.urlopen(f"{base_url}/getMe", timeout=10).read())
        bot_user = me_resp.get("result", {})
        print(f"🤖 Бот авторизован: @{bot_user.get('username')} (ID: {bot_user.get('id')})")
    except Exception as exc:
        print(f"❌ Не удалось подключиться к Telegram: {exc}")
        sys.exit(1)

    # Проверяем webhook
    try:
        wh_resp = json.loads(urllib.request.urlopen(f"{base_url}/getWebhookInfo", timeout=10).read())
        wh_url = wh_resp.get("result", {}).get("url")
        if wh_url:
            print(f"ℹ️ Снимаем временный вебхук для чтения getUpdates...")
            del_url = f"{base_url}/deleteWebhook?drop_pending_updates=false"
            urllib.request.urlopen(del_url, timeout=10)
    except Exception as exc:
        print(f"⚠️ Ошибка проверки вебхука: {exc}")

    allowed_updates = json.dumps(["channel_post", "edited_channel_post", "my_chat_member", "chat_member", "message"])
    params = urllib.parse.urlencode({
        "allowed_updates": allowed_updates,
        "limit": 100,
        "timeout": 5,
    })

    print("📡 Проверка доступных обновлений бота (поиск каналов)...")
    try:
        upd_resp = json.loads(urllib.request.urlopen(f"{base_url}/getUpdates?{params}", timeout=15).read())
    except Exception as exc:
        print(f"❌ Ошибка вызова getUpdates: {exc}")
        sys.exit(1)

    results = upd_resp.get("result", [])
    found_channels: dict[int, dict] = {}

    for u in results:
        chat = None
        event_type = ""
        if "channel_post" in u:
            chat = u["channel_post"].get("chat")
            event_type = "Пост в канале"
        elif "edited_channel_post" in u:
            chat = u["edited_channel_post"].get("chat")
            event_type = "Редактирование поста"
        elif "my_chat_member" in u:
            chat = u["my_chat_member"].get("chat")
            event_type = "Добавление бота в канал/чат"
        elif "chat_member" in u:
            chat = u["chat_member"].get("chat")
            event_type = "Событие участника"
        elif "message" in u:
            c = u["message"].get("chat", {})
            if c.get("type") in ("channel", "supergroup", "group"):
                chat = c
                event_type = f"Сообщение в {c.get('type')}"

        if chat and chat.get("id"):
            cid = chat["id"]
            if cid not in found_channels:
                found_channels[cid] = {
                    "id": cid,
                    "title": chat.get("title") or "Без названия",
                    "username": chat.get("username"),
                    "type": chat.get("type"),
                    "event": event_type,
                }

    if found_channels:
        print("\n✅ НАЙДЕНЫ КАНАЛЫ / ЧАТЫ:")
        for cid, info in found_channels.items():
            uname = f"@{info['username']}" if info['username'] else "нет (приватный)"
            print(f"  • Название: {info['title']}")
            print(f"    Тип:      {info['type']}")
            print(f"    Username: {uname}")
            print(f"    Событие:  {info['event']}")
            print(f"    👉 CHANNEL_ID={info['id']}\n")
    else:
        print("\n⏳ В очереди обновлений пока нет сохранённых событий из канала.")
        print("💡 Что нужно сделать:")
        print("   1. Убедитесь, что бот добавлен в канал как администратор.")
        print("   2. Опубликуйте любой пост в канале (или запустите диагностику с флагом --watch).")

    if "--watch" in sys.argv:
        print("👀 Включён режим онлайн-наблюдения (--watch).")
        print("Опубликуйте пост в канале прямо сейчас, и ID сразу появится на экране. Для выхода нажмите Ctrl+C.\n")
        offset = results[-1]["update_id"] + 1 if results else 0
        try:
            while True:
                p = urllib.parse.urlencode({
                    "allowed_updates": allowed_updates,
                    "offset": offset,
                    "timeout": 20,
                })
                try:
                    r = json.loads(urllib.request.urlopen(f"{base_url}/getUpdates?{p}", timeout=25).read())
                    for item in r.get("result", []):
                        offset = max(offset, item["update_id"] + 1)
                        c = None
                        ev = "Обновление"
                        if "channel_post" in item:
                            c = item["channel_post"].get("chat")
                            ev = "Новый пост в канале"
                        elif "edited_channel_post" in item:
                            c = item["edited_channel_post"].get("chat")
                            ev = "Редактирование поста"
                        elif "my_chat_member" in item:
                            c = item["my_chat_member"].get("chat")
                            ev = "Статус бота в канале"
                        if c:
                            print(f"[{time.strftime('%H:%M:%S')}] 📢 {ev}:")
                            print(f"    Название:   {c.get('title')}")
                            print(f"    CHANNEL_ID: {c.get('id')}\n")
                except Exception as loop_exc:
                    time.sleep(2)
        except KeyboardInterrupt:
            print("\nНаблюдение остановлено.")


if __name__ == "__main__":
    main()


import os
import json
import asyncio
import subprocess
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from groq import Groq

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
client = Groq(api_key=os.getenv("GROQ_API_KEY"))
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", 0))
NOTES_DIR = os.path.expanduser("~/server-assistant/notes")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

ROUTER_PROMPT = """
Ти розумний асистент. Визнач намір користувача.
Поверни ТІЛЬКИ валідний JSON у форматі: {"intent": "НАМІР", "response": "текст"}

Доступні наміри (НАМІР):
1. "SYSTEM": Загальний стан сервера. response: "".
2. "DOCKER": Статус Docker. response: "".
3. "TODO": Завдання. response: "коротке формулювання завдання (1 рядок)".
4. "NOTE": Збереження довгої ідеї, конспекту. response: "чистий Markdown-код нотатки з YAML-frontmatter (title, date, tags). Теги АНГЛІЙСЬКОЮ. Дата: {CURRENT_DATE}".
5. "DAILY": Короткі думки, події для щоденника. response: "текст для щоденника".
6. "LINK": Збереження посилання, якщо повідомлення містить URL. response: "сама URL-адреса".
7. "SEARCH": Користувач шукає інформацію у своїх нотатках (питає "який", "де", "нагадай", "що"). response: "2-3 ключових слова для пошуку (тільки іменники/терміни, наприклад: планшет область)".
8. "CHAT": Звичайна розмова, якщо жоден інший намір не підходить. response: "твоя розгорнута відповідь".
"""

def sync_git():
    try:
        cmds = [
            ["git", "pull"],
            ["git", "add", "."],
            ["git", "commit", "-m", "Бот: оновлено дані"],
            ["git", "push"]
        ]
        for cmd in cmds:
            res = subprocess.run(cmd, cwd=NOTES_DIR, capture_output=True, text=True)
            if res.returncode != 0 and cmd[1] == 'commit' and 'nothing to commit' in str(res.stdout + res.stderr):
                continue
            elif res.returncode != 0:
                print(f"❌ Помилка Git ({cmd[1]}): {res.stderr}")
                return
    except Exception as e:
        print(f"❌ Помилка Python (git): {e}")

def get_system_metrics():
    try:
        uptime = subprocess.check_output("uptime", shell=True, text=True).strip()
        ram = subprocess.check_output("free -h", shell=True, text=True).strip()
        temp = subprocess.check_output("sensors", shell=True, text=True).strip()
        return f"```bash\n[UPTIME]\n{uptime}\n\n[MEMORY]\n{ram}\n\n[TEMP]\n{temp}\n```"
    except Exception as e:
        return f"```bash\nПомилка метрик: {e}\n```"

def get_docker_status():
    try:
        status = subprocess.check_output("docker ps --format 'table {{.Names}}\t{{.Status}}'", shell=True, text=True).strip()
        return f"```bash\n[DOCKER]\n{status}\n```"
    except Exception as e:
        return f"```bash\nПомилка Docker: {e}\n```"

async def process_intent(message: types.Message, raw_text: str):
    msg = await message.answer("🔄 Аналізую...")
    try:
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        system_prompt = ROUTER_PROMPT.replace("{CURRENT_DATE}", current_date)

        api_response = await asyncio.to_thread(
            client.chat.completions.create,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": raw_text}
            ],
            model="llama-3.3-70b-versatile",
            temperature=0.1,
            response_format={"type": "json_object"}
        )

        data = json.loads(api_response.choices[0].message.content)
        intent = data.get("intent", "CHAT")
        response_text = data.get("response", "")

        if intent == "SYSTEM":
            await msg.edit_text(get_system_metrics(), parse_mode="MarkdownV2")

        elif intent == "DOCKER":
            await msg.edit_text(get_docker_status(), parse_mode="MarkdownV2")

        elif intent == "TODO":
            filepath = os.path.join(NOTES_DIR, "Tasks.md")
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"- [ ] {response_text} (Додано: {datetime.now().strftime('%Y-%m-%d')})\n")
            sync_git()
            await msg.edit_text(f"✅ Додано до справ:\n`{response_text}`")

        elif intent == "NOTE":
            filename = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_Note.md")
            filepath = os.path.join(NOTES_DIR, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(response_text)
            sync_git()
            await msg.edit_text(f"✅ Нотатку збережено як `{filename}`")

        elif intent == "DAILY":
            filename = f"{datetime.now().strftime('%Y-%m-%d')}_Daily.md"
            filepath = os.path.join(NOTES_DIR, filename)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"- [{datetime.now().strftime('%H:%M')}] {response_text}\n")
            sync_git()
            await msg.edit_text(f"📝 Додано у щоденник:\n{response_text}")

        elif intent == "LINK":
            url = response_text.strip()
            try:
                res = requests.get(url, timeout=5)
                soup = BeautifulSoup(res.text, 'html.parser')
                title = soup.title.string.strip() if soup.title else "Збережене посилання"
                md_link = f"[{title}]({url})"
            except:
                md_link = f"[Посилання]({url})"
            
            filename = f"{datetime.now().strftime('%Y-%m-%d')}_Daily.md"
            filepath = os.path.join(NOTES_DIR, filename)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"- [{datetime.now().strftime('%H:%M')}] {md_link}\n")
            sync_git()
            await msg.edit_text(f"🔗 Посилання збережено у щоденник:\n`{md_link}`")

        elif intent == "SEARCH":
            await msg.edit_text(f"🔍 Шукаю в базі за словами: `{response_text}`...")
            keywords = response_text.split()
            context_text = ""
            
            # Скануємо всі .md файли
            for root, _, files in os.walk(NOTES_DIR):
                for file in files:
                    if file.endswith(".md"):
                        try:
                            with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                                text = f.read()
                                if any(kw.lower() in text.lower() for kw in keywords):
                                    context_text += f"\n--- Файл: {file} ---\n{text}\n"
                        except:
                            pass

            if not context_text.strip():
                await msg.edit_text("❌ В базі знань не знайдено інформації за цим запитом.")
                return

            # Формуємо відповідь на основі знайденого
            qa_prompt = f"Відповідай коротко. Спирайся ТІЛЬКИ на цей контекст з моїх нотаток:\n{context_text[:15000]}"
            qa_response = await asyncio.to_thread(
                client.chat.completions.create,
                messages=[
                    {"role": "system", "content": qa_prompt},
                    {"role": "user", "content": raw_text}
                ],
                model="llama-3.3-70b-versatile",
                temperature=0.1
            )
            await msg.edit_text(f"🧠 **Знайдено в нотатках:**\n\n{qa_response.choices[0].message.content}")

        else: # CHAT
            await msg.edit_text(response_text)

    except Exception as e:
        await msg.edit_text(f"❌ Помилка: {e}")

@dp.message(Command("start"), F.from_user.id == ALLOWED_USER_ID)
async def cmd_start(message: types.Message):
    await message.answer("Привіт! Я ServerAssistantBot. Готовий робити нотатки, вести список справ та шукати інформацію.")

@dp.message((F.photo | F.video | F.document), F.from_user.id == ALLOWED_USER_ID)
async def handle_media(message: types.Message):
    msg = await message.answer("🔄 Зберігаю файл...")
    try:
        attach_dir = os.path.join(NOTES_DIR, "attachments")
        os.makedirs(attach_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        
        if message.photo:
            file_id, ext, tag = message.photo[-1].file_id, "jpg", "photo"
        elif message.video:
            file_id, ext, tag = message.video.file_id, "mp4", "video"
        else:
            file_id = message.document.file_id
            ext = message.document.file_name.split('.')[-1] if message.document.file_name and '.' in message.document.file_name else "file"
            tag = "document"

        filename = f"{tag}_{timestamp}.{ext}"
        filepath = os.path.join(attach_dir, filename)

        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, filepath)

        caption = message.caption if message.caption else f"Медіафайл: {filename}"
        note_name = f"{timestamp}_{tag.capitalize()}.md"
        with open(os.path.join(NOTES_DIR, note_name), "w", encoding="utf-8") as f:
            f.write(f"---\ntitle: {tag.capitalize()} {timestamp}\ndate: {datetime.now().strftime('%Y-%m-%d %H:%M')}\ntags: [media, {tag}]\n---\n\n{caption}\n\n![[attachments/{filename}]]\n")

        sync_git()
        await msg.edit_text(f"✅ Файл збережено: `{note_name}`")
    except Exception as e:
        await msg.edit_text(f"❌ Помилка збереження: {e}")

@dp.message(F.text, F.from_user.id == ALLOWED_USER_ID)
async def handle_text(message: types.Message):
    await process_intent(message, message.text)

@dp.message(F.voice, F.from_user.id == ALLOWED_USER_ID)
async def handle_voice(message: types.Message):
    msg = await message.answer("🎧 Розпізнаю...")
    try:
        file = await bot.get_file(message.voice.file_id)
        file_path = f"{message.voice.file_id}.ogg"
        await bot.download_file(file.file_path, file_path)

        with open(file_path, "rb") as audio:
            transcription = await asyncio.to_thread(
                client.audio.transcriptions.create,
                file=(file_path, audio.read()),
                model="whisper-large-v3",
                response_format="text"
            )
        os.remove(file_path)
        await msg.delete()
        await process_intent(message, transcription)
    except Exception as e:
        await msg.edit_text(f"❌ Помилка голосу: {e}")
        if os.path.exists(file_path): os.remove(file_path)

async def main():
    os.makedirs(NOTES_DIR, exist_ok=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

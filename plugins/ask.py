from pyrogram import Client, filters
from core.ai_client import ask_ai
from core.ai_client import ask_groq
from song import BANNED_USERS


@Client.on_message(filters.command("ask") & filters.text)
async def ask_handler(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    if len(message.command) < 2:
        await message.reply_text("Say it properly.")
        return

    query = " ".join(message.command[1:])
    reply = await ask_ai(message.chat.id, query)
    await message.reply_text(reply)


@Client.on_message(filters.mentioned & filters.text)
async def mention_handler(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    
    query = message.text.replace("@BestFreakingBot", "").strip()
    if not query:
        query = "Hello"

    reply = await ask_groq(message.chat.id, query)
    await message.reply_text(reply)


@Client.on_message(filters.text)
async def name_call_handler(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    if message.text.startswith("/"):
        return

    if "waguri" in message.text.lower():
        reply = await ask_groq(message.chat.id, message.text)
        await message.reply_text(reply)


@Client.on_message(filters.reply & filters.text)
async def reply_handler(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    replied = message.reply_to_message

    if replied and replied.from_user and replied.from_user.id == client.me.id:
        reply = await ask_groq(message.chat.id, message.text)
        await message.reply_text(reply)



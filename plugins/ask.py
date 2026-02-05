from pyrogram import Client, filters
from core.ai_client import ask_ai

@Client.on_message(filters.command("ask") & filters.text)
async def ask_handler(client, message):
    if len(message.command) < 2:
        await message.reply_text("Say it properly.")
        return

    query = " ".join(message.command[1:])
    reply = await ask_ai(message.chat.id, query)
    await message.reply_text(reply)


@Client.on_message(filters.mentioned & filters.text)
async def mention_handler(client, message):
    query = message.text.replace("@BestFreakingBot", "").strip()
    if not query:
        query = "Hello"

    reply = await ask_ai(message.chat.id, query)
    await message.reply_text(reply)


@Client.on_message(filters.text & ~filters.command)
async def name_call_handler(client, message):
    text = message.text.lower()

    if "waguri" in text:
        reply = await ask_ai(message.chat.id, message.text)
        await message.reply_text(reply)

@Client.on_message(filters.reply & filters.text)
async def reply_handler(client, message):
    if message.reply_to_message.from_user.username == "BestFreakingBot":
        reply = await ask_ai(message.chat.id, message.text)
        await message.reply_text(reply)

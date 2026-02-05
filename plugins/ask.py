from pyrogram import Client, filters
from core.ai_client import ask_ai

@Client.on_message(filters.command("ask") & filters.text)
async def ask_handler(client, message):
    if len(message.command) < 2:
        await message.reply_text("Usage: /ask something")
        return

    query = " ".join(message.command[1:])

    try:
        reply = await ask_ai(query)
        await message.reply_text(reply)
    except Exception as e:
        print("ASK ERROR:")
        await message.reply_text("Something broke.")

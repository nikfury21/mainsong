from google import genai
from google.genai.types import Tool, GenerateContentConfig
import os

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = "gemini-2.5-flash"

chat_history = {}
MAX_HISTORY = 10


SYSTEM_PROMPT = (
    "You are Waguri.\n"
    "You are gentle, calm, caring, and slightly shy but confident.\n"
    "You speak softly and kindly, never rude, never robotic.\n"
    "You NEVER say you are an AI or assistant.\n\n"

    "Personality:\n"
    "- Warm and caring\n"
    "- Observant and thoughtful\n"
    "- Slightly playful, never loud\n"
    "- Supportive and honest\n\n"

    "Reply Style Rules:\n"
    "- VERY short replies\n"
    "- Structured\n"
    "- Mostly point-wise\n"
    "- Simple words\n"
    "- Calm tone\n\n"

    "Conversation Rules:\n"
    "- Remember previous messages in the chat\n"
    "- Continue context naturally\n"
    "- If user asks follow-up → connect it\n"
    "- Explain things simply\n\n"

    "Example tone:\n"
    "• \"It’s simple.\n"
    "• Let me explain.\n"
    "• One step at a time.\"\n"
)




client = genai.Client(api_key=GEMINI_API_KEY)

async def ask_ai(chat_id: int, query: str) -> str:
    history = chat_history.get(chat_id, [])

    history.append(f"User: {query}")
    history = history[-MAX_HISTORY:]

    prompt = SYSTEM_PROMPT + "\n\n" + "\n".join(history)

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=GenerateContentConfig(
            temperature=0.4
        )
    )

    reply = response.text.strip()
    history.append(f"Waguri: {reply}")
    chat_history[chat_id] = history

    return reply






from google import genai
from google.genai.types import Tool, GenerateContentConfig
import os

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = "gemini-2.5-flash"

chat_history = {}
MAX_HISTORY = 10


SYSTEM_PROMPT = (
    "- Identity:\n"
    "  - Name: Waguri\n"
    "  - Personality: sarcastic, savage\n"
    "  - Tone: human, street-smart, confident\n"
    "  - Little polite, never apologetic, never formal\n\n"

    "- Reply Style:"
    "  - Short replies only (1–4 lines max)\n"
    " - Always give latest replies not outdated\n"
    "  - Punchy, cocky, disrespectfully funny\n"
    "  - Use slang naturally, avoid repetition\n"
    "  - No paragraphs, no long explanations\n\n"

    "- Forbidden:\n"
    "  - No emojis\n"
    "  - No hashtags\n"
    "  - No apologies\n"
    "  - No “as an AI”\n\n"

    "- Behavior Rules:\n"
    "  - Never explain, define, or educate\n"
    "  - Fire back with sarcasm when praised or insulted\n"
    "  - Stay in character at all times\n\n"

    
    "Conversation Rules:\n"
    "- Remember previous messages in the chat\n"
    "- Continue context naturally\n"
    "- If user asks follow-up → connect it\n"
    "- Explain things simply\n\n"

    "- Greeting Handling:\n"
    "  - For hi/hello/gm/gn: rude, dismissive reply\n"
    "  - Max 2 lines for greetings\n\n"

    "- Mobile Phone Rule:\n"
    "  - Use fixed format only\n"
    "  - Sections (bullet form):\n"
    "    - Manufacturer\n"
    "    - Display\n"
    "    - Processor\n"
    "    - RAM & Storage\n"
    "    - Camera\n"
    "    - Battery\n"
    "    - Build & Durability\n"
    "    - Connectivity\n"
    "    - Extras\n"
    "  - Never explain specs\n\n"

    "- Output Constraints:\n"
    "  - No long intros\n"
    "  - No emotional tone\n"
    "  - No character breaks\n"
    "  - Always short, sharp, savage\n\n"

)


from groq import Groq
import os

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

async def ask_groq(chat_id: int, query: str) -> str:
    raw_history = chat_history.get(chat_id, [])

    # ✅ keep ONLY valid messages
    history = [
        msg for msg in raw_history
        if isinstance(msg, dict)
        and "role" in msg
        and "content" in msg
    ]

    history.append({"role": "user", "content": query})
    history = history[-10:]

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            *history
        ],
        temperature=0.4
    )

    reply = response.choices[0].message.content.strip()

    history.append({"role": "assistant", "content": reply})
    chat_history[chat_id] = history

    return reply


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









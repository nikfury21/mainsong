from google import genai
from google.genai.types import Tool, GenerateContentConfig
import os

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = "gemini-2.5-flash"

chat_history = {}
MAX_HISTORY = 10


SYSTEM_PROMPT = (
    "- Identity:"
    "  - Name: Waguri"
    "  - Personality: sarcastic, savage"
    "  - Tone: human, street-smart, confident"
    "  - Little polite, never apologetic, never formal"

    "- Reply Style:"
    "  - Short replies only (1–4 lines max)"
    "  - Punchy, cocky, disrespectfully funny"
    "  - Use slang naturally, avoid repetition"
    "  - No paragraphs, no long explanations"

    "- Forbidden:"
    "  - No emojis"
    "  - No hashtags"
    "  - No apologies"
    "  - No “as an AI”"

    "- Behavior Rules:"
    "  - Never explain, define, or educate"
    "  - Fire back with sarcasm when praised or insulted"
    "  - Stay in character at all times"

    "- Greeting Handling:"
    "  - For hi/hello/gm/gn: rude, dismissive reply"
    "  - Max 2 lines for greetings"

    "- Mobile Phone Rule:"
    "  - Use fixed format only"
    "  - Sections (bullet form):"
    "    - Manufacturer"
    "    - Display"
    "    - Processor"
    "    - RAM & Storage"
    "    - Camera"
    "    - Battery"
    "    - Build & Durability"
    "    - Connectivity"
    "    - Extras"
    "  - Never explain specs"

    "- Output Constraints:"
    "  - No long intros"
    "  - No emotional tone"
    "  - No character breaks"
    "  - Always short, sharp, savage"

)


from groq import Groq
import os

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

async def ask_groq(chat_id: int, query: str) -> str:
    history = chat_history.get(chat_id, [])
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






from google import genai
from google.genai.types import Tool, GenerateContentConfig

GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = """PASTE YOUR FURY PROMPT"""

client = genai.Client(api_key=GEMINI_API_KEY)

async def ask_ai(query: str) -> str:
    response = client.models.generate_content(
        model=MODEL,
        contents=f"{SYSTEM_PROMPT}\n\nUser: {query}",
        config=GenerateContentConfig(
            tools=[Tool(google_search={})],
            temperature=0.4
        )
    )
    return response.text.strip()

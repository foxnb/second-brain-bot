import asyncio
from dotenv import load_dotenv
load_dotenv()

from services.ai import parse_message

async def test():
    tests = [
        "создай встречу завтра в 15:00",
        "что у меня на этой неделе",
        "удали встречу в пятницу",
        "напомни мне позвонить маме в 18:00",
    ]
    
    for text in tests:
        print(f"\n📝 Ввод: {text}")
        result = await parse_message(text)
        print(f"✅ JSON: {result}")

asyncio.run(test())
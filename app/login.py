"""One-time Telegram login:  python -m app.login

Asks for your phone number, the code Telegram sends you, and your 2FA password (if set).
The session is saved in data/telegram.session - keep that file private!
"""
import asyncio

from telethon import TelegramClient

from .config import Settings


async def main() -> None:
    s = Settings()
    if not s.api_id or not s.api_hash:
        print("חסרים TG_API_ID / TG_API_HASH בקובץ .env (מקבלים ב-https://my.telegram.org)")
        return
    client = TelegramClient(str(s.session_path), s.api_id, s.api_hash)
    print("התחברות לטלגרם. הכנס מספר טלפון בפורמט בינלאומי, לדוגמה +972501234567")
    await client.start()
    me = await client.get_me()
    print(f"\nמחובר בהצלחה בתור: {me.first_name} (@{me.username})")
    print("טוען את רשימת הקבוצות...")
    count = 0
    async for d in client.iter_dialogs():
        if d.is_group or d.is_channel:
            count += 1
    print(f"נמצאו {count} קבוצות/ערוצים. עכשיו הפעל את האתר: python -m app")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

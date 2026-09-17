# Telegram Quiz Bot — Render + Gemini Free Tier

Bu versiya OpenAI o‘rniga **Google Gemini API** ishlatadi.

Google Gemini API ayrim modellar uchun bepul tier beradi, lekin bepul tierda ham rate limitlar mavjud. Bot savollarni batch qilib yuboradi, shuning uchun bitta-bitta API so‘rov yubormaydi.

## Required Environment Variables

- `BOT_TOKEN` — Telegram BotFather tokeni
- `GEMINI_API_KEY` — Google AI Studio'dan olingan Gemini API key
- `ADMIN_ID` — adminning Telegram numeric ID raqami
- `PORT` — Render avtomatik beradi; kod `10000` fallback ishlatadi

## Optional Environment Variables

- `GEMINI_MODEL` — default: `gemini-3.8-flash`
- `GEMINI_BATCH_SIZE` — default: `40`, maksimal: `50`
- `GEMINI_MAX_RETRIES` — default: `3`
- `GEMINI_MAX_RETRY_WAIT` — default: `60` soniya
- `GEMINI_TIMEOUT` — default: `90` soniya
- `GEMINI_MIN_REQUEST_INTERVAL` — default: `1.3` soniya

## Gemini API key olish

Google AI Studio orqali API key yarating:

https://aistudio.google.com/apikey

Keyni GitHub'ga yozmang. Render → Environment → `GEMINI_API_KEY` sifatida qo‘ying.

## Render

Service type: **Web Service**

Build Command:
```bash
pip install -r requirements.txt
```

Start Command:
```bash
python main.py
```

Health Check Path:
```text
/health
```

## Features

- Faqat admin test yaratadi
- `.txt` va `.docx` fayllar
- Separator formatidagi savollarni o‘qish
- Fayldagi savol va variantlarni saqlash
- Gemini orqali to‘g‘ri javobni aniqlash
- Batch AI checking
- 429/5xx/timeout holatlariga nazoratli retry
- Uzoq rate-limit kutishida botni qotirib qo‘ymaslik
- Savollarni 20/40/50 kabi qismlarga bo‘lish
- Test vaqti: `13s`, `1m`, `5m`, `1h` yoki oddiy raqam (daqiqalar)
- Telegram deep-link orqali ulashish
- Foydalanuvchi natijasini avtomatik hisoblash
- SQLite
- Telegram polling + aiohttp `/health`

## Muhim

Bepul Gemini API ham cheksiz emas. Rate limit yoki quota tugasa, bot tushunarli xabar beradi va cheksiz kutib qolmaydi.

# Telegram Quiz Bot — Render

## Required Environment Variables

- `BOT_TOKEN` — Telegram bot token from BotFather
- `OPENAI_API_KEY` — OpenAI API key
- `ADMIN_ID` — your numeric Telegram user ID
- `PORT` — Render supplies this automatically; the code reads it and defaults to 10000
- Optional: `OPENAI_MODEL` — defaults to `gpt-5.6-luna`
- Optional: `OPENAI_BATCH_SIZE` — default `40`; one API request checks many questions
- Optional: `OPENAI_MAX_RETRIES` — default `3`
- Optional: `OPENAI_MAX_RETRY_WAIT` — default `75` seconds; prevents the bot from hanging for 20–30 minutes on a hard 429
- Optional: `OPENAI_TIMEOUT` — default `90` seconds

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

- Admin-only test creation
- `.txt` and `.docx` question files
- Parser for the separator format shown in the supplied sample
- Keeps question/option text from the file
- Uses OpenAI to determine the correct option
- Batch AI checking to greatly reduce API request count
- Built-in SDK retries disabled so 429 errors do not silently multiply requests
- Short temporary 429/5xx errors retry automatically; long 429 waits fail cleanly instead of hanging
- Splits a large question bank into configurable test sizes
- Configurable test duration
- Shareable Telegram deep links
- User attempts and automatic scoring
- SQLite persistence
- Telegram polling + aiohttp health server in the same process

## Important

Set `ADMIN_ID` to your own Telegram numeric ID. You can temporarily run the bot and use `/myid` to see it, then put that number in Render Environment Variables.

Do not commit API keys or bot tokens to GitHub.

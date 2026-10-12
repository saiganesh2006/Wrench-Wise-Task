# Deploy BankMind as a public website

The app is one container (FastAPI serves both the API and the web UI), so any host that runs Docker works.
You will get a public **https://** link to put in your submission email.

## Option A – Render (recommended, free, ~5 min)
1. Push the project to GitHub (public or private repo).
2. Go to **render.com → New → Blueprint**, pick the repo. Render reads `render.yaml`.
3. When asked, paste your **`ANTHROPIC_API_KEY`** (the other variables are filled in automatically).
   *Using OpenAI/Groq instead?* After deploy add `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `LLM_MODEL`.
4. Wait for the build (~3 min). Your site is live at `https://bankmind-xxxx.onrender.com`. Sign in with the demo accounts.

Manual alternative: **New → Web Service → Docker**, set the env vars above, health check path `/api/health`.

## Option B – Hugging Face Spaces (free, no sleep on CPU basic)
1. Create a Space → SDK **Docker**. Push the repo files to it.
2. Add a *Secret* `ANTHROPIC_API_KEY` in Settings.
3. In the Space's README header add `app_port: 7860` and set variable `PORT=7860`.

## Option C – Railway / Fly.io / any VPS
`Procfile` and `Dockerfile` are included. Railway: *New Project → Deploy from GitHub*, add the env vars. VPS:
`docker build -t bankmind . && docker run -d -p 80:8000 --env-file .env bankmind` (put Caddy/Nginx in front for HTTPS).

## Things to know
* **Data resets on redeploy / free-tier sleep.** The SQLite file lives in `/tmp`, and the app re-creates and re-seeds it on
  start, which keeps the fraud demo data fresh. For permanent data attach a disk (Render paid plan) and set
  `BANKMIND_DB=/var/data/bankmind.db`.
* **Free tiers sleep when idle** – the first visit after a pause takes ~30–50 s. Open the site just before the reviewer does,
  or use a free uptime pinger on `/api/health`.
* **Costs:** every chat uses your LLM key. Set a spending limit with your provider, and consider a cheaper model via `LLM_MODEL`.
* **Security for a public site:** passwords are hashed, sessions are signed, logins lock after 5 failures, chat is rate-limited,
  and security headers are set. The three demo accounts are intentionally public - they only hold fake data.
* **HTTPS is provided by the host**; SSE streaming works through their proxy (`X-Accel-Buffering: no` is already sent).

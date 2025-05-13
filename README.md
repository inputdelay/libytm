# LIBYTM
A Flask-based YouTube Music API clone for InputDelay Music.

## Setup
1. Clone: `git clone https://github.com/your-username/libytm.git`
2. Install: `pip install -r requirements.txt`
3. Create `.env` with `COOKIES=/app/cookies.txt` or provide `oauth.json`.
4. Run locally: `python app.py`
5. Deploy to Railway (see below).

## Deployment
### Railway
1. Create a Service on [Railway](https://railway.app).
2. Connect GitHub repository.
3. Add Environment Variables: `COOKIES` or upload `cookies.txt`.
4. Deploy with `Procfile`: `web: gunicorn app:app`.

## Powered By
- [ytmusicapi](https://github.com/sigma67/ytmusicapi)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
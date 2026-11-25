# Private hosted copy (just you)

The app can run on a server so you reach it from any device. It is protected by one shared password.
It needs a **persistent disk** (the history and fund theses live in a SQLite file) and a **long-running process**,
so serverless hosts (Vercel, Netlify) do not fit. Render, Fly.io or Railway do.

## Render (about 10 minutes)

1. Push this project to GitHub (it is already there). If it sits in a subfolder, add `rootDir: <folder>` under the service in `render.yaml`.
2. In Render: **New > Blueprint**, pick the repo. It reads `render.yaml` (Docker, 1 GB disk at `/data`).
3. Optionally set an AI key (e.g. `GROQ_API_KEY`). The site has no login by default.
4. Open the URL Render gives you, sign in, then add AI keys on the **AI keys** page if you did not set them in step 3.

A persistent disk needs a paid Render plan (a few dollars a month). On a free plan the database is wiped on each restart.

## Good to know

- **There is no login by default:** anyone who finds the web address can run screenings with your AI keys, read your reports and change or delete your keys. To lock it, set both `REQUIRE_LOGIN=1` and `APP_PASSWORD=<passphrase>`. A leftover `APP_PASSWORD` alone does nothing.
- Sign-in is a single password, rate-limited to 5 wrong tries per 5 minutes, with a 14-day cookie. Always use the https URL.
- Keys added on the AI keys page are stored in the database on the server's disk (not encrypted at rest). Keys set as environment variables stay in the host's dashboard.
- Screenings run one at a time. A restart during a screening loses that run (nothing else).
- To back up: copy `/data/screening.db`.
- Locally nothing changes: `python app.py` still serves only `localhost`.

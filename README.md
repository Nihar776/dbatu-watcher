# DBATU Watcher (Phase 1)

Checks DBATU pages daily (exam timetables, academic calendar, syllabus) and sends an
alert to **Discord** (and/or Telegram) for new documents, plus silently replaced PDFs.
Runs free on GitHub Actions.

## Setup (about 10 minutes)

1. **Discord webhook**
   - Create a server (or use your class/committee one) and a channel like `#dbatu-alerts`.
   - Channel settings (gear icon) -> Integrations -> Webhooks -> New Webhook -> **Copy Webhook URL**.
   - Treat that URL like a password: anyone with it can post to your channel.
   - *(Optional)* To ping people on real alerts, set `DISCORD_MENTION` to `@everyone`,
     or a role like `<@&ROLE_ID>` (enable Developer Mode in Discord -> right-click the role -> Copy Role ID).
2. **GitHub repo**
   - Create a **private** repo and push this folder to it.
   - Settings -> Secrets and variables -> Actions -> add `DISCORD_WEBHOOK_URL`
     (and `DISCORD_MENTION` if you want pings).
   - Settings -> Actions -> General -> Workflow permissions -> **Read and write**.
3. **First run**: Actions tab -> "DBATU watcher" -> Run workflow.
   It records a baseline silently and posts one "set up" message. After that, only changes alert you.

## Test locally first (recommended)

    pip install -r requirements.txt
    python watcher.py --dry-run

Shows how many document links each page yields, without alerting or saving anything.
To test the Discord message itself, do a real run locally with the webhook set:

    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    python watcher.py

(Delete `state/seen.json` afterwards if you want the first GitHub run to start from a clean baseline.)

## Channels

Any channel with credentials set is used; you can enable several.

| Channel | Env vars |
|---|---|
| Discord | `DISCORD_WEBHOOK_URL`, optional `DISCORD_MENTION` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

Discord attaches PDFs up to ~8 MB (larger ones are link-only).

## Config (`config.yaml`)

- `relevant_regex` - matching docs get a star and the PDF itself is attached.
- `include_regex` / `exclude_regex` - filter noise.
- `follow_pages_regex` - also open linked sub-pages (use if the syllabus page links to per-branch pages).

## Notes

- State lives in `state/seen.json`, committed back by the workflow each run.
- GitHub pauses scheduled workflows in repos with no activity for ~60 days. The daily state
  commit usually keeps it alive; if alerts stop, re-enable it in the Actions tab.
- Be polite: the script waits between requests. Don't lower `POLITE_DELAY` much.
- Phase 2 (Drive upload) and the library module plug into `check_page()`.

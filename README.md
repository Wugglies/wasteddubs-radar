# WastedDubs trending-clip radar

Every 10 minutes, checks Twitch's official API for the fastest-rising clips in a
game category and posts the best new ones to Discord. Free: runs on GitHub
Actions in this public repo, standard-library Python only.

## How a clip is ranked
1. Pull the last 6 h of clips for the category (`/helix/clips`, 1-hour windows).
2. Merge clips of the same moment (same streamer, same VOD within 45 s, or created
   within 90 s). More viewers clipping it = small boost.
3. Score = views/hour (since creation, or since the last run if faster), divided by
   √(1 + live viewers / 1000) so small streamers can surface.
4. Post the top 5 new clips with ≥ 50 views, English only. Each clip alerts once.

## Setup (all doable from a phone browser)
1. **Twitch app:** dev.twitch.tv/console/apps → Register Your Application →
   name `wasteddubs-radar`, OAuth Redirect URL `http://localhost`, category
   *Application Integration*, client type *Confidential* → Create → Manage →
   copy **Client ID**, click **New Secret**, copy it.
2. **Discord webhook:** your server → channel settings (gear) → Integrations →
   Webhooks → New Webhook → Copy Webhook URL.
3. **GitHub secrets:** this repo → Settings → Secrets and variables → Actions →
   New repository secret, three times: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`,
   `DISCORD_WEBHOOK_URL`.
4. **Test run:** Actions tab → *Trending clip radar* → Run workflow → game
   `Grand Theft Auto V`, dry_run `1` → check the log. Then run again with
   dry_run `0` to get a real Discord alert.

## Tuning (Settings → Secrets and variables → Actions → Variables)
`GAME_NAME`, `LOOKBACK_HOURS`, `MIN_VIEWS`, `TOP_N`, `LANGUAGES` (comma list, `*` = any language).

## Notes
- Twitch app tokens are fetched fresh each run; nothing to renew.
- GitHub pauses scheduled workflows in public repos after 60 days without repo
  activity. If alerts stop, open the Actions tab and re-enable the workflow.
- Only clips you have permission to use (clipping campaign or streamer OK) should
  be posted.

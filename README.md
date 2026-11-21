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
4. Check the top ~20 candidates' thumbnails (free OpenCV, no AI service):
   a real face near the left/right edge = streamer facecam → score x0.35
   (sinks, still alerts if huge); black bars top and bottom = 🎬 cutscene tag
   (only dub early, non-story ones: Rockstar's policy bans story/cutscene uploads).
5. Vehicles first: planes, boats, cars, bikes and helicopters rank x2.0, other
   clips x0.4 (a truly viral clip can still win). Found by title keywords (several
   languages) and a free image detector (YOLOX from the OpenCV model zoo,
   Apache-2.0), tested on real GTA VI clips: 6 of 7 vehicle frames caught.
6. Post the top 5 new clips with ≥ 50 views, any language (voiceover replaces the talk). Each clip alerts once.

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
`GAME_NAME`, `LOOKBACK_HOURS`, `MIN_VIEWS`, `TOP_N`, `LANGUAGES` (default `*` = any language; or a comma list like `en,es`), `VEHICLES` (`0` turns vehicles-first off).

## Notes
- Twitch app tokens are fetched fresh each run; nothing to renew.
- GitHub pauses scheduled workflows in public repos after 60 days without repo
  activity. If alerts stop, open the Actions tab and re-enable the workflow.
- Only clips you have permission to use (clipping campaign or streamer OK) should
  be posted.

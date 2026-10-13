# Crypto bot

Buys and sells crypto on Robinhood at least 10 times a day. GitHub runs it every 30 minutes (see the **Actions** tab), so nothing has to stay on.

- **Settings:** edit `settings.env`. It starts with `DRY_RUN=true`, which simulates trades only. Change it to `false` for real orders.
- **API keys:** these go in **Settings → Secrets and variables → Actions** as `RH_API_KEY` and `RH_PRIVATE_KEY`. Never put them in a file.
- **Activity:** `bot.log` shows every trade, and `bot_state.json` shows today's trade count and P&L.
- **Pause it:** go to **Actions → Trade → ⋯ → Disable workflow**.

# yt-remote

Control YouTube on your laptop from your phone.

- **Laptop app** (`controller.py`): opens YouTube in a real browser window (Chrome, Edge, or Playwright's Chromium) and carries out commands.
- **Phone app** (`web.py` + `templates/remote.html`): a mobile web remote. The laptop serves it.
- **Messaging**: the two apps talk over a free public [ntfy.sh](https://ntfy.sh) topic. No account or API key is needed.

```
 phone browser ──POST──▶ ntfy.sh/<topic> ──stream──▶ controller.py ──▶ YouTube tab
       ▲                                                   │
       └──── SSE ◀── ntfy.sh/<topic>-status ◀──POST────────┘
```

## Run

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python run.py          # starts both apps
```

The laptop opens a **pairing page** at http://127.0.0.1:8000/pair with the QR code and the link (with a Copy button), served only to the laptop itself. The terminal also prints them, such as `http://192.168.1.20:8000/`. Open it on your phone while the phone is on the same Wi-Fi. The first time it runs, Windows asks to let Python through the firewall. Allow it on **private** networks.

To run the two apps separately, use `uv run python controller.py` and `uv run python web.py`.

If neither Chrome nor Edge is installed, run `uv run playwright install chromium` once.

## Remote controls

The remote has a tactical-HUD theme in the style of a sci-fi shooter command console. Its controls are play/pause, previous and next, seek ±10 s, volume, mute, captions, playback speed, **Fullscreen** (toggles the player, even during ads), **Skip Ad** (clicks YouTube's Skip button once it appears; it glows red while an ad plays), and home (**Base**). You can also search: tap a result to play it, or paste a YouTube link.

**D-pad (TV-style navigation):** the arrows move an amber highlight between video thumbnails on any page, like a TV app. **OK** opens the highlighted video and **Back** goes back. On a video page, ◀ ▶ seek 10 s until you press ▼ to move into the recommended videos; ▲ from the top tile returns to the player. Quick repeated taps in one direction are sent as a single message, to stay within ntfy.sh's free quota.

## Config and security

On first run the app creates `config.json` with a random topic such as `ytr-…`. **The topic name is the password.** Anyone who knows it can send commands, so don't share it. `config.json` is git-ignored. To get a new topic, delete `config.json`.

The laptop never sends the topic over the network: apart from the loopback-only `/pair` page, it goes only in the `#fragment` of the printed link and QR code, which browsers never send to a server, and the phone keeps it in local storage. Someone who scans your Wi-Fi only sees a remote that isn't linked to anything. Commands are posted with `cache=no`, so ntfy.sh doesn't store them.

Commands are limited to a fixed set of player actions. `open` only accepts YouTube URLs, so a stranger who learns the topic can only control YouTube, not your machine.

You can override settings with environment variables: `NTFY_SERVER` (for a self-hosted ntfy) and `YTR_TOPIC`.

The browser profile is kept in `.browser-profile/` (git-ignored), so a YouTube sign-in stays saved between runs. Google can block sign-in inside automated browsers. If that happens, use the app signed out.

ntfy.sh's free tier rate-limits each IP address. Normal use stays well below the limit, but tapping a button very fast can return HTTP 429 for a few seconds.

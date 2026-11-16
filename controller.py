"""Laptop app: opens YouTube in a real browser window and executes commands
received from the phone over an ntfy.sh topic. Publishes player status back."""
import json
import queue
import sys
import threading
import time
import urllib.parse

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from common import PROFILE_DIR, load_config

YOUTUBE = "https://www.youtube.com/"
# Search result titles: legacy layout, and the newer lockup layout YouTube A/B-serves to some profiles.
RESULT_SEL = "ytd-video-renderer a#video-title, ytd-search yt-lockup-view-model a.ytLockupMetadataViewModelTitle"
NAV = {"wait_until": "domcontentloaded", "timeout": 15_000}
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
MAX_CMD_AGE = 60  # seconds; ignore commands that arrive very late (e.g. after a reconnect)
STATUS_POLL = 2.0  # seconds between local status checks
MIN_PUBLISH_GAP = 4.0  # seconds; ntfy.sh's free tier refills ~1 request / 5 s per IP
RATE_LIMIT_BACKOFF = 30.0  # seconds to pause status publishing after an HTTP 429

# Windows consoles/redirects default to a legacy code page; never crash on an emoji search.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PLAYER_JS = """([action, value]) => {
  const p = document.getElementById('movie_player');
  if (!p || typeof p.getPlayerState !== 'function') return 'no player';
  switch (action) {
    case 'toggle': p.getPlayerState() === 1 ? p.pauseVideo() : p.playVideo(); break;
    case 'seek': p.seekTo(Math.max(0, p.getCurrentTime() + value), true); break;
    case 'volume':
      if (p.isMuted()) p.unMute();
      p.setVolume(Math.min(100, Math.max(0, p.getVolume() + value)));
      break;
    case 'mute': p.isMuted() ? p.unMute() : p.mute(); break;
    case 'speed': p.setPlaybackRate(value); break;
    default: return 'unknown';
  }
  return 'ok';
}"""

STATUS_JS = """() => {
  const out = {page: location.pathname, title: document.title.replace(/ - YouTube$/, '')};
  if (location.pathname === '/results') {
    out.results = [...document.querySelectorAll(RESULT_SEL)]
      .slice(0, 8).map(a => (a.textContent || '').trim().slice(0, 90));
  }
  const p = document.getElementById('movie_player');
  if (location.pathname.startsWith('/watch') && p && typeof p.getPlayerState === 'function') {
    const d = p.getVideoData ? p.getVideoData() : {};
    out.title = d.title || out.title;
    out.playing = p.getPlayerState() === 1;
    out.time = Math.floor(p.getCurrentTime() || 0);
    out.duration = Math.floor(p.getDuration() || 0);
    out.live = !!(d.isLive || out.duration > 172800);  // live streams report a bogus huge duration
    if (out.live) out.duration = 0;
    out.volume = p.getVolume();
    out.muted = p.isMuted();
    out.speed = p.getPlaybackRate();
  }
  return out;
}""".replace("RESULT_SEL", json.dumps(RESULT_SEL))


def subscribe(server: str, topic: str, out: "queue.Queue[dict]", stop: threading.Event) -> None:
    """Background thread: stream the ntfy topic and push parsed commands onto `out`."""
    url = f"{server}/{topic}/json"
    last_id = None  # resume from here after a reconnect so commands in the gap aren't lost
    warned_skew = False
    while not stop.is_set():
        try:
            params = {"since": last_id} if last_id else None
            # ntfy sends a keepalive every ~45s, so a 90s read timeout detects dead links.
            with requests.get(url, params=params, stream=True, timeout=(10, 90)) as r:
                r.raise_for_status()
                print(f"[ntfy] subscribed to {topic}")
                for line in r.iter_lines():
                    if stop.is_set():
                        return
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue
                    if evt.get("event") != "message":
                        continue
                    last_id = evt.get("id") or last_id
                    age = time.time() - float(evt.get("time") or time.time())
                    if age > MAX_CMD_AGE:
                        if not warned_skew:
                            print(f"[ntfy] dropped a command {age:.0f}s old (check laptop clock if this repeats)")
                            warned_skew = True
                        continue
                    try:
                        cmd = json.loads(evt.get("message", ""))
                    except ValueError:
                        print("[ntfy] ignored a message that is not valid JSON")
                        continue
                    if isinstance(cmd, dict) and isinstance(cmd.get("action"), str):
                        out.put(cmd)
        except Exception as e:  # network blips, ntfy restarts, etc.
            if stop.is_set():
                return
            print(f"[ntfy] connection lost ({e.__class__.__name__}); retrying in 3s")
            stop.wait(3)


def safe_youtube_url(url: str) -> str | None:
    """Return a canonical https YouTube URL, or None. Rebuilt from the hostname only,
    so userinfo/ports/backslash tricks (e.g. 'https://evil.com\\@youtube.com') can't
    make the browser land somewhere else."""
    url = url.strip()
    if "\\" in url or any(ord(c) < 0x21 for c in url):
        return None
    try:
        u = urllib.parse.urlsplit(url)
        if u.username is not None or u.password is not None or u.port is not None:
            return None
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if u.scheme in ("http", "https") and host in ALLOWED_HOSTS:
        return urllib.parse.urlunsplit(("https", host, u.path, u.query, ""))
    return None


def as_number(value, default: float, lo: float, hi: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if n != n:  # NaN
        return default
    return max(lo, min(hi, n))


class Controller:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.page = None
        self.context = None
        self.last_status_key = None
        self.last_publish = 0.0
        self.force_publish = False
        self.backoff_until = 0.0

    # ---- browser -------------------------------------------------------
    def launch(self, pw):
        PROFILE_DIR.mkdir(exist_ok=True)
        opts = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            no_viewport=True,
            args=[
                "--start-maximized",
                "--autoplay-policy=no-user-gesture-required",
                # keep playing even when the window is covered by other windows
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling",
            ],
            ignore_default_args=["--enable-automation"],
        )
        # Prefer an installed browser (no download needed); fall back to Playwright's Chromium.
        for channel in ("chrome", "msedge", None):
            try:
                self.context = pw.chromium.launch_persistent_context(
                    **opts, **({"channel": channel} if channel else {})
                )
                print(f"[browser] using {channel or 'bundled chromium'}")
                break
            except PlaywrightError:
                continue
        else:
            raise SystemExit("No browser found. Run: uv run playwright install chromium")
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto(YOUTUBE, **NAV)

    def ensure_page(self):
        if self.page is None or self.page.is_closed():
            open_pages = [p for p in self.context.pages if not p.is_closed()]
            self.page = open_pages[-1] if open_pages else self.context.new_page()
            if not open_pages:
                self.page.goto(YOUTUBE, **NAV)
        return self.page

    def press(self, key: str):
        page = self.ensure_page()
        page.bring_to_front()
        # YouTube ignores shortcuts while a text box has focus.
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        page.keyboard.press(key)

    # ---- commands ------------------------------------------------------
    def handle(self, cmd: dict):
        action = cmd["action"]
        value = cmd.get("value")
        page = self.ensure_page()
        print(f"[cmd] {action} {'' if value is None else value}")

        if action in ("toggle", "mute"):
            page.evaluate(PLAYER_JS, [action, 0])
        elif action == "seek":
            page.evaluate(PLAYER_JS, ["seek", as_number(value, 10, -3600, 3600)])
        elif action == "volume":
            page.evaluate(PLAYER_JS, ["volume", as_number(value, 10, -100, 100)])
        elif action == "speed":
            page.evaluate(PLAYER_JS, ["speed", as_number(value, 1, 0.25, 2)])
        elif action == "next":
            self.press("Shift+N")
        elif action == "prev":
            page.go_back(**NAV)
        elif action == "fullscreen":
            self.press("f")
        elif action == "captions":
            self.press("c")
        elif action == "home":
            page.goto(YOUTUBE, **NAV)
        elif action == "search":
            q = str(value or "").strip()[:200]
            if q:
                page.goto(YOUTUBE + "results?search_query=" + urllib.parse.quote_plus(q), **NAV)
                page.wait_for_selector(RESULT_SEL, timeout=10_000)
        elif action == "pick":
            idx = int(as_number(value, 0, 0, 50))
            page.locator(RESULT_SEL).nth(idx).click(timeout=5_000)
        elif action == "open":
            url = safe_youtube_url(str(value or ""))
            if url:
                page.goto(url, **NAV)
            else:
                print("[cmd] rejected non-YouTube URL")
        elif action == "status":
            pass
        else:
            print(f"[cmd] unknown action {action!r}")
            return
        self.force_publish = True

    # ---- status --------------------------------------------------------
    def read_status(self) -> dict | None:
        try:
            return self.ensure_page().evaluate(STATUS_JS)
        except PlaywrightError:
            return None  # page mid-navigation

    def maybe_publish_status(self):
        st = self.read_status()
        if st is None:
            return
        # Only publish on meaningful change (not every tick of the clock).
        key = json.dumps({k: v for k, v in st.items() if k != "time"}, sort_keys=True)
        now = time.time()
        if not self.force_publish and key == self.last_status_key:
            return
        if now - self.last_publish < MIN_PUBLISH_GAP or now < self.backoff_until:
            return
        try:
            r = requests.post(
                f"{self.cfg['server']}/{self.cfg['status_topic']}",
                data=json.dumps(st).encode("utf-8"),
                headers={"Cache": "no"},
                timeout=10,
            )
            if r.status_code == 429:
                print(f"[ntfy] rate limited; pausing status for {RATE_LIMIT_BACKOFF:.0f}s")
                self.backoff_until = now + RATE_LIMIT_BACKOFF
                return  # force_publish stays set, so it is retried after the backoff
            if not r.ok:
                print(f"[ntfy] status publish failed: HTTP {r.status_code}")
                self.last_publish = now
                return
            self.last_status_key = key
            self.last_publish = now
            self.force_publish = False
        except requests.RequestException as e:
            print(f"[ntfy] status publish failed: {e.__class__.__name__}")


def run(cfg: dict | None = None):
    cfg = cfg or load_config()
    commands: "queue.Queue[dict]" = queue.Queue()
    stop = threading.Event()
    threading.Thread(
        target=subscribe, args=(cfg["server"], cfg["topic"], commands, stop), daemon=True
    ).start()

    ctl = Controller(cfg)
    with sync_playwright() as pw:
        ctl.launch(pw)
        print("[browser] ready - close the browser window or press Ctrl+C to quit")
        next_status = 0.0
        try:
            while True:
                try:
                    cmd = commands.get(timeout=0.25)
                except queue.Empty:
                    cmd = None
                try:
                    if cmd:
                        ctl.handle(cmd)
                        next_status = time.time() + 0.8  # let the page react first
                    if time.time() >= next_status:
                        ctl.maybe_publish_status()
                        next_status = time.time() + STATUS_POLL
                except PlaywrightError as e:
                    if not ctl.context.pages and "closed" in str(e).lower():
                        break
                    print(f"[browser] {(str(e).splitlines() or [''])[0]}")
                except Exception as e:  # one bad command must never kill the controller
                    print(f"[cmd] error: {e.__class__.__name__}: {e}")
                if not ctl.context.pages:
                    try:
                        ctl.page = ctl.context.new_page()  # raises if the whole browser is gone
                        ctl.page.goto(YOUTUBE, **NAV)
                    except PlaywrightError:
                        break
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            try:
                ctl.context.close()
            except Exception:
                pass
    print("bye")


if __name__ == "__main__":
    run()

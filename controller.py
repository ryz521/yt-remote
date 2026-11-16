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
SKIP_AD_SEL = (
    ".ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern, "
    "button[id^='skip-button'], .ytp-ad-skip-button-container button"
)
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

# TV-style spatial navigation over the video tiles of any YouTube page.
# Tiles are found by link shape (any /watch or /shorts link big enough to be a thumbnail),
# so it keeps working when YouTube changes its component names.
NAV_JS = """([dir, n]) => {
  const W = window;
  const CONT = 'ytd-rich-item-renderer, ytd-video-renderer, ytd-compact-video-renderer, ytd-grid-video-renderer, '
             + 'yt-lockup-view-model, ytd-playlist-renderer, ytd-reel-item-renderer, ytm-shorts-lockup-view-model, '
             + 'ytd-playlist-video-renderer, ytd-playlist-panel-video-renderer';
  if (!document.getElementById('ytr-style')) {
    const s = document.createElement('style');
    s.id = 'ytr-style';
    s.textContent = '[data-ytr-focus]{outline:4px solid #ffb21a !important;outline-offset:3px;border-radius:12px;'
      + 'box-shadow:0 0 28px 6px rgba(255,178,26,.65) !important;transform:scale(1.04);transition:transform .12s;'
      + 'position:relative;z-index:5}';
    document.head.appendChild(s);
  }
  const cur = (W.__ytrFocus && W.__ytrFocus.isConnected && W.__ytrUrl === location.href) ? W.__ytrFocus : null;
  const setFocus = el => {
    document.querySelectorAll('[data-ytr-focus]').forEach(e => e.removeAttribute('data-ytr-focus'));
    W.__ytrFocus = el; W.__ytrUrl = location.href;
    if (el) { el.setAttribute('data-ytr-focus', '1'); el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'smooth'}); }
  };
  const titleOf = el => {
    const c = el.closest(CONT);
    const t = c && c.querySelector('#video-title, .yt-lockup-metadata-view-model__title, h3, [title]');
    return ((t && (t.getAttribute('title') || t.textContent)) || el.getAttribute('aria-label') || '').trim().slice(0, 90);
  };
  if (dir === 'peek') return {active: !!cur};
  if (dir === 'clear') { setFocus(null); return {state: 'cleared'}; }

  const best = new Map();
  for (const a of document.querySelectorAll('a[href*="/watch?v="], a[href^="/shorts/"]')) {
    if (a.closest('#movie_player, ytd-miniplayer, #masthead-container, [hidden]')) continue;
    const r = a.getBoundingClientRect();
    if (r.width < 80 || r.height < 40) continue;
    const st = getComputedStyle(a);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    const key = a.closest(CONT) || a;  // one tile per card: keep its biggest link (the thumbnail)
    const prev = best.get(key);
    if (!prev || r.width * r.height > prev.r.width * prev.r.height) best.set(key, {a, r});
  }
  const list = [...best.values()].map(v => v.a);
  if (!list.length) return {state: 'none'};

  if (!cur) {  // first press: start at the top-left tile on screen
    const inView = list.filter(t => { const r = t.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; });
    const pool = inView.length ? inView : list;
    pool.sort((a, b) => { const p = a.getBoundingClientRect(), q = b.getBoundingClientRect(); return (p.top - q.top) || (p.left - q.left); });
    setFocus(pool[0]);
    return {state: 'moved', title: titleOf(pool[0])};
  }
  let el = cur;
  for (let i = 0; i < n; i++) {
    const r = el.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    let pick = null, score = Infinity;
    for (const t of list) {
      if (t === el) continue;
      const q = t.getBoundingClientRect(), dx = q.left + q.width / 2 - cx, dy = q.top + q.height / 2 - cy;
      let main, cross;
      if (dir === 'right') { if (dx <= 10) continue; main = dx; cross = Math.abs(dy); if (cross > r.height * 0.75) continue; }
      else if (dir === 'left') { if (dx >= -10) continue; main = -dx; cross = Math.abs(dy); if (cross > r.height * 0.75) continue; }
      else if (dir === 'down') { if (dy <= 10) continue; main = dy; cross = Math.abs(dx); }
      else if (dir === 'up') { if (dy >= -10) continue; main = -dy; cross = Math.abs(dx); }
      else return {state: 'bad'};
      const s = main + cross * 2;
      if (s < score) { score = s; pick = t; }
    }
    if (!pick) {
      if (dir === 'down') { window.scrollBy({top: innerHeight * 0.8, behavior: 'smooth'}); break; }  // load more
      if (dir === 'up') { setFocus(null); window.scrollTo({top: 0, behavior: 'smooth'}); return {state: 'cleared'}; }
      break;
    }
    el = pick;
  }
  setFocus(el);
  return {state: 'moved', title: titleOf(el)};
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
    out.ad = p.classList.contains('ad-showing');
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

    def fullscreen(self, page):
        """Toggle the player's fullscreen; on non-video pages toggle browser fullscreen (F11-like)."""
        page.bring_to_front()
        # Fullscreen needs a user gesture; CDP's userGesture flag provides one, which works
        # even while an ad hides the player controls.
        js = """(async () => {
          if (document.fullscreenElement) { await document.exitFullscreen(); return 'exit'; }
          const el = document.querySelector('#movie_player') || document.documentElement;
          await el.requestFullscreen(); return 'enter';
        })()"""
        cdp = page.context.new_cdp_session(page)
        try:
            res = cdp.send("Runtime.evaluate", {"expression": js, "userGesture": True, "awaitPromise": True})
            if "exceptionDetails" in res:
                self.press("f")  # fall back to YouTube's own shortcut
        finally:
            cdp.detach()

    def navigate(self, page, direction: str, n: int):
        """D-pad: move the tile highlight. On a video page, left/right seek while no tile is highlighted."""
        if direction not in ("up", "down", "left", "right"):
            return
        if "/watch" in page.url and direction in ("left", "right") and not page.evaluate(NAV_JS, ["peek", 0])["active"]:
            page.evaluate(PLAYER_JS, ["seek", (10 if direction == "right" else -10) * n])
            return
        res = page.evaluate(NAV_JS, [direction, n])
        if res.get("title"):
            print(f"[nav] {res['title']}")

    def skip_ad(self, page):
        """Click YouTube's own 'Skip' button once it is offered."""
        btn = page.locator(SKIP_AD_SEL).filter(visible=True).first
        if btn.count():
            btn.click(timeout=3_000)
            print("[cmd] ad skipped")
        elif page.evaluate("() => !!document.querySelector('#movie_player.ad-showing')"):
            print("[cmd] ad is not skippable yet")
        else:
            print("[cmd] no ad playing")

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
        elif action == "nav":
            self.navigate(page, str(value), int(as_number(cmd.get("n"), 1, 1, 10)))
            return  # highlight moves are visible on screen; skip the status publish to save ntfy quota
        elif action == "ok":
            focused = page.locator("[data-ytr-focus]")
            if focused.count():
                focused.first.click(timeout=3_000)
            elif "/watch" in page.url:
                page.evaluate(PLAYER_JS, ["toggle", 0])
        elif action in ("prev", "back"):
            page.go_back(**NAV)
        elif action == "fullscreen":
            self.fullscreen(page)
        elif action == "skipad":
            self.skip_ad(page)
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

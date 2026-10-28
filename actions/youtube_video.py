#youtube_video.py
import json
import re
import sys
import time
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus

try:
    import pyautogui
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    _TRANSCRIPT_OK = True
except ImportError:
    _TRANSCRIPT_OK = False

from config import get_os, is_windows, is_mac, is_linux
from core import outcome


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_YT_VIDEO_FILTER = "EgIQAQ%3D%3D"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _open_url(url: str) -> bool:
    try:
        if is_mac():
            subprocess.Popen(["open", url])
        elif is_linux():
            subprocess.Popen(["xdg-open", url])
        else:
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
        return True
    except Exception as e:
        print(f"[YouTube] open_url failed: {e}")
        return False


def _json_text(raw: str) -> str:
    """Decode one JSON string body scraped out of the page."""
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return raw


def _scrape_candidates(query: str, limit: int = 8) -> list[dict]:
    """Search results as {id, url, title, channel, official}.

    The search page ships its first batch of results inside the HTML, so no
    API key is involved. Splitting on `"videoRenderer"` also leaves Shorts
    out, because those arrive as `reelItemRenderer` instead.
    """
    if not _REQUESTS_OK:
        return []

    search_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}"
        f"&sp={_YT_VIDEO_FILTER}"
    )
    try:
        html = requests.get(search_url, headers=HEADERS, timeout=10).text
    except Exception as e:
        print(f"[YouTube] search scrape failed: {e}")
        return []

    found: list[dict] = []
    seen: set[str] = set()
    for chunk in html.split('"videoRenderer":{"videoId":"')[1:]:
        vid = chunk[:11]
        if vid in seen or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            continue
        seen.add(vid)
        block = chunk[:9000]

        title = re.search(
            r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)+)"', block)
        channel = re.search(
            r'"ownerText":\{"runs":\[\{"text":"((?:[^"\\]|\\.)+)"', block)
        if not title or not channel:
            continue

        badges = ""
        badge_match = re.search(r'"ownerBadges":(\[.*?\])', block)
        if badge_match:
            badges = badge_match.group(1)
        official = (
            "Official Artist Channel" in badges
            or "OFFICIAL_ARTIST_BADGE" in badges
            or "Verified" in badges
        )

        found.append({
            "id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": _json_text(title.group(1)),
            "channel": _json_text(channel.group(1)),
            "official": official,
        })
        if len(found) >= limit:
            break

    return found


_NEGATIVE_HINTS = (
    "cover", "reaction", "lyrics", "8d", "slowed", "nightcore", "remix",
    "mashup", "instrumental", "karaoke", "tutorial", "parody",
)
_LABEL_HINTS = ("vevo", "official", "records", "music", "label", "topic")


def _score_candidate(candidate: dict, query: str) -> int:
    """Prefer the artist's or label's own upload over a re-upload.

    Deliberately a ranking, never a filter: when nothing official exists the
    best ordinary upload still wins. The score only decides which of several
    results is the one the user most likely meant.
    """
    tokens = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 1]
    title = candidate["title"].lower()
    channel = candidate["channel"].lower()
    score = 0

    if candidate["official"]:
        score += 45
    if "vevo" in channel:
        score += 20
    elif any(hint in channel for hint in _LABEL_HINTS):
        score += 12

    if tokens and all(t in title for t in tokens):
        score += 20
    elif tokens and any(t in title for t in tokens):
        score += 10
    if tokens and all(t in channel for t in tokens):
        score += 25
    elif tokens and any(t in channel for t in tokens):
        score += 10

    if any(hint in title for hint in _NEGATIVE_HINTS):
        score -= 30
    return score


def _open_in_browser(url: str, parameters: dict) -> bool:
    """Open a URL in the chosen (or default) browser, honestly.

    browser_control is imported lazily: it refuses to load without playwright,
    and YouTube playback must still work on a machine that never installed it.
    """
    browser = (parameters.get("browser") or "").strip().lower()
    if browser:
        try:
            from actions.browser_control import _open_native
            result = _open_native(url, browser)
            if not str(result).startswith("status=failed"):
                return True
        except Exception as exc:
            print(f"[YouTube] {browser} launch failed ({exc}); "
                  f"falling back to the default browser")
    return _open_url(url)


def _verify_page_opened(title: str) -> bool:
    """Is a window whose title carries this video's name on screen?

    A loaded video tab puts the video title in the browser window's title.
    This proves the PAGE opened — not that sound is coming out.
    """
    try:
        from core import window_ops
        fragment = title.strip()[:32]
        if not fragment:
            return False
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if window_ops.find_windows(fragment, allow_minimized=False):
                return True
            time.sleep(0.25)
    except Exception as exc:
        print(f"[YouTube] page verification failed: {exc}")
    return False


def _handle_play(parameters: dict, player) -> str:
    query = (parameters.get("query") or "").strip()
    if not query:
        return outcome.ask("youtube_play", "what would you like me to play?")

    if player:
        player.write_log(f"[YouTube] Searching: {query}")

    print(f"[YouTube] Searching: {query}")
    candidates = _scrape_candidates(query)

    if not candidates:
        print("[YouTube] Search scrape failed, opening the search page")
        fallback_url = (
            f"https://www.youtube.com/results"
            f"?search_query={quote_plus(query)}"
            f"&sp={_YT_VIDEO_FILTER}"
        )
        if not _open_in_browser(fallback_url, parameters):
            return outcome.failed(
                "youtube_play",
                "YouTube could not be reached and no browser would open the "
                "search page",
                query=query,
            )
        return outcome.unverified(
            "youtube_play",
            "no single video could be identified, so the YouTube search page "
            "was opened for the user to choose from",
            query=query, url=fallback_url,
        )

    ranked = sorted(
        candidates, key=lambda c: _score_candidate(c, query), reverse=True)
    for candidate in ranked[:4]:
        print(f"[YouTube]   {_score_candidate(candidate, query):+d}  "
              f"{candidate['title']} — {candidate['channel']}"
              f"{'  [official]' if candidate['official'] else ''}")

    top = ranked[0]
    top_score = _score_candidate(top, query)
    if len(ranked) > 1:
        second = ranked[1]
        second_score = _score_candidate(second, query)
        # Two plausible results with nothing official to separate them: that
        # is a question. Opening a cover or a re-upload instead of the song
        # the user asked for is exactly the wrong guess this check prevents.
        if top_score - second_score <= 8 and top_score < 70:
            options = "; ".join(
                f"'{c['title']}' by {c['channel']}" for c in ranked[:3])
            return outcome.ask(
                "youtube_play",
                f"more than one result fits '{query}': {options} - which one "
                f"should I play?",
                query=query, candidates=len(ranked),
            )

    if top_score <= 0:
        # Nothing in the results even echoes the query. Showing the user the
        # search page is useful; picking the first row would be a guess.
        fallback_url = (
            f"https://www.youtube.com/results"
            f"?search_query={quote_plus(query)}"
            f"&sp={_YT_VIDEO_FILTER}"
        )
        if not _open_in_browser(fallback_url, parameters):
            return outcome.failed(
                "youtube_play",
                "none of the results matched, and no browser would open the "
                "search page", query=query)
        return outcome.unverified(
            "youtube_play",
            "none of the results matched the request, so the search page was "
            "opened instead of guessing", query=query, url=fallback_url)

    print(f"[YouTube] Opening: {top['url']}  ({top['title']})")
    if not _open_in_browser(top["url"], parameters):
        return outcome.failed(
            "youtube_play", "no browser would accept the video",
            query=query, url=top["url"])

    _line = (f"RAVAN: Opening \"{top['title']}\" by {top['channel']} on YouTube.")
    if player:
        player.write_log(_line)

    if _verify_page_opened(top["title"]):
        return outcome.done(
            "youtube_play", query=query, title=top["title"],
            channel=top["channel"], url=top["url"],
            verified=True, page_loaded=True,
            playback="not_confirmed",
        )

    return outcome.unverified(
        "youtube_play",
        "the video was handed to the browser, but no page with that title "
        "appeared within a few seconds - it may still be loading",
        query=query, title=top["title"], channel=top["channel"],
        url=top["url"],
    )


def _extract_video_id(url: str) -> str | None:
    match = re.search(
        r"(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([A-Za-z0-9_-]{11})", url
    )
    return match.group(1) if match else None


def _is_valid_youtube_url(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url or ""))


def _ask_for_url(prompt_text: str = "YouTube video URL:") -> str | None:
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk._default_root
        if root is None:
            root = tk.Tk()
            root.withdraw()

        url = simpledialog.askstring("RAVAN", prompt_text, parent=root)
        return url.strip() if url else None
    except Exception as e:
        print(f"[YouTube] URL dialog failed: {e}")
        return None


def _get_transcript(video_id: str) -> str | None:
    if not _TRANSCRIPT_OK:
        return None
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript      = None

        lang_priority = ["en", "tr", "de", "fr", "es", "it", "pt", "ru", "ja", "ko", "ar", "zh"]

        try:
            transcript = transcript_list.find_manually_created_transcript(lang_priority)
        except Exception:
            pass

        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(lang_priority)
            except Exception:
                for t in transcript_list:
                    transcript = t
                    break

        if transcript is None:
            return None

        fetched = transcript.fetch()
        return " ".join(entry["text"] for entry in fetched)

    except Exception as e:
        print(f"[YouTube] Transcript fetch failed: {e}")
        return None


def _summarize_with_gemini(transcript: str, video_url: str) -> str:
    from google.genai import types
    from core import gemini

    max_chars = 80000
    truncated = transcript[:max_chars] + ("..." if len(transcript) > max_chars else "")
    # A whole transcript can be 80k characters, hence the long deadline — but a
    # deadline there is, and the ladder in core/gemini.py picks the model.
    response = gemini.call(
        f"Please summarize this YouTube video transcript:\n\n{truncated}",
        tier=gemini.SMART,
        timeout_ms=60_000,
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are RAVAN, an AI assistant. "
                "Summarize YouTube video transcripts clearly and concisely. "
                "Structure: 1-sentence overview, then 3-5 key points. "
                "Be direct. Address the user as 'sir'. "
                "Match the language of the transcript."
            )
        )
    )
    if response is None:
        return "I couldn't reach Gemini to summarise that transcript, sir."
    return (response.text or "").strip()


def _save_summary(content: str, video_url: str) -> str:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"youtube_summary_{ts}.txt"
    desktop  = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    filepath = desktop / filename

    header = (
        f"RAVAN — YouTube Summary\n"
        f"{'─' * 50}\n"
        f"URL    : {video_url}\n"
        f"Date   : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{'─' * 50}\n\n"
    )
    filepath.write_text(header + content, encoding="utf-8")

    try:
        if is_windows():
            subprocess.Popen(["notepad.exe", str(filepath)])
        elif is_mac():
            subprocess.Popen(["open", "-t", str(filepath)])
        else:
            subprocess.Popen(["xdg-open", str(filepath)])
    except Exception as e:
        print(f"[YouTube] Could not open text editor: {e}")

    return str(filepath)


def _scrape_video_info(video_id: str) -> dict:
    if not _REQUESTS_OK:
        return {}
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r    = requests.get(url, headers=HEADERS, timeout=12)
        html = r.text
        info = {}

        for key, pattern in [
            ("title",    r'"title":\{"runs":\[\{"text":"([^"]+)"'),
            ("channel",  r'"ownerChannelName":"([^"]+)"'),
            ("views",    r'"viewCount":"(\d+)"'),
            ("duration", r'"lengthSeconds":"(\d+)"'),
            ("likes",    r'"label":"([0-9,]+ likes)"'),
        ]:
            match = re.search(pattern, html)
            if match:
                raw = match.group(1)
                if key == "views":
                    info[key] = f"{int(raw):,}"
                elif key == "duration":
                    secs = int(raw)
                    info[key] = f"{secs // 60}:{secs % 60:02d}"
                else:
                    info[key] = raw

        return info
    except Exception as e:
        print(f"[YouTube] Info scrape failed: {e}")
        return {}


def _scrape_trending(region: str = "TR", max_results: int = 8) -> list[dict]:
    if not _REQUESTS_OK:
        return []
    url = f"https://www.youtube.com/feed/trending?gl={region.upper()}"
    try:
        r    = requests.get(url, headers=HEADERS, timeout=12)
        html = r.text

        titles   = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"\}\]', html)
        channels = re.findall(r'"ownerText":\{"runs":\[\{"text":"([^"]+)"', html)

        results, seen = [], set()
        for i, title in enumerate(titles):
            if title in seen or len(title) < 5:
                continue
            seen.add(title)
            channel = channels[i] if i < len(channels) else "Unknown"
            results.append({"rank": len(results) + 1, "title": title, "channel": channel})
            if len(results) >= max_results:
                break

        return results
    except Exception as e:
        print(f"[YouTube] Trending scrape failed: {e}")
        return []

def _handle_summarize(parameters: dict, player, speak) -> str:
    if not _TRANSCRIPT_OK:
        return "youtube-transcript-api is not installed. Run: pip install youtube-transcript-api"

    url = _ask_for_url("Please paste the YouTube video URL:")
    if not url:
        return "No URL provided, sir. Summary cancelled."
    if not _is_valid_youtube_url(url):
        return "That doesn't appear to be a valid YouTube URL, sir."

    video_id = _extract_video_id(url)
    if not video_id:
        return "Could not extract video ID from that URL, sir."

    if player:
        player.write_log(f"[YouTube] Summarizing: {url}")
    if speak:
        speak("Fetching the transcript now, sir. One moment.")

    transcript = _get_transcript(video_id)
    if not transcript:
        return "I couldn't retrieve a transcript for that video, sir."

    if speak:
        speak("Transcript retrieved. Generating summary now.")

    try:
        summary = _summarize_with_gemini(transcript, url)
    except Exception as e:
        return f"Summary generation failed, sir: {e}"

    if speak:
        speak(summary)

    if parameters.get("save", False):
        saved_path = _save_summary(summary, url)
        return f"Summary complete and saved to Desktop: {saved_path}"

    return summary


def _handle_get_info(parameters: dict, player, speak) -> str:
    url = parameters.get("url", "").strip()
    if not url:
        url = _ask_for_url("Please paste the YouTube video URL:")
    if not url or not _is_valid_youtube_url(url):
        return "Please provide a valid YouTube URL, sir."

    video_id = _extract_video_id(url)
    if not video_id:
        return "Could not extract video ID, sir."

    if player:
        player.write_log(f"[YouTube] Getting info: {url}")

    info = _scrape_video_info(video_id)
    if not info:
        return "Could not retrieve video information, sir."

    lines = [
        f"{key.capitalize()}: {info[key]}"
        for key in ("title", "channel", "views", "duration", "likes")
        if key in info
    ]
    result = "\n".join(lines)

    if speak:
        speak(f"Here's the video info, sir. {result.replace(chr(10), '. ')}")

    return result


def _handle_trending(parameters: dict, player, speak) -> str:
    region = parameters.get("region", "TR").upper()

    if player:
        player.write_log(f"[YouTube] Trending: {region}")

    trending = _scrape_trending(region=region, max_results=8)
    if not trending:
        return f"Could not fetch trending videos for region {region}, sir."

    lines  = [f"Top trending videos in {region}:"]
    lines += [f"{v['rank']}. {v['title']} — {v['channel']}" for v in trending]
    result = "\n".join(lines)

    if speak:
        top3   = trending[:3]
        spoken = "Here are the top trending videos, sir. " + ". ".join(
            f"Number {v['rank']}: {v['title']} by {v['channel']}" for v in top3
        )
        speak(spoken)

    return result

_ACTION_MAP = {
    "play":      _handle_play,
    "summarize": _handle_summarize,
    "get_info":  _handle_get_info,
    "trending":  _handle_trending,
}


def youtube_video(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "play").lower().strip()

    if player:
        player.write_log(f"[YouTube] Action: {action}")
    print(f"[YouTube] Action: {action}  Params: {params}")

    handler = _ACTION_MAP.get(action)
    if handler is None:
        return (
            f"Unknown YouTube action: '{action}'. "
            "Available: play, summarize, get_info, trending."
        )

    try:
        if action == "play":
            return handler(params, player) or "Done."
        return handler(params, player, speak) or "Done."
    except Exception as e:
        print(f"[YouTube] ❌ Error in {action}: {e}")
        return f"YouTube {action} failed, sir: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "youtube_video",
    "description": (
        "Controls YouTube: plays a song or video, summarises a video, reads "
        "its info, or lists trending videos. Use action='play' whenever the "
        "user asks to play, put on or search for a song or video on YouTube - "
        "'play Arijit Singh', 'put on lo-fi beats'. Put what they asked for in "
        "'query'. Official artist and label uploads are preferred, covers and "
        "re-uploads are ranked down, and when two results are equally "
        "plausible the tool asks instead of guessing. "
        "Read the status: 'done' means the video page was seen open - you may "
        "say it is opening/playing, but NOT that playback itself was verified; "
        "'unverified' means the request went out and could not be confirmed - "
        "never claim it is playing; 'ask' means nothing was opened and the "
        "question must go to the user."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "play | summarize | get_info | trending (default: play)"
            },
            "query": {
                "type": "STRING",
                "description": (
                    "Search query for play - the song or video as the user "
                    "said it, e.g. 'Kesariya Arijit Singh'."
                )
            },
            "browser": {
                "type": "STRING",
                "description": (
                    "Optional browser for play: chrome, edge, firefox. Leave "
                    "empty to use the default browser."
                )
            },
            "save": {
                "type": "BOOLEAN",
                "description": "Save summary to Notepad (summarize only)"
            },
            "region": {
                "type": "STRING",
                "description": "Country code for trending e.g. TR, US"
            },
            "url": {
                "type": "STRING",
                "description": "Video URL for get_info action"
            }
        },
        "required": []
    },
    "handler": youtube_video,
}

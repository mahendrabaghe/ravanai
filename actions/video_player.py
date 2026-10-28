"""
Play a video where the avatar normally is.

The HUD already had a surface for this — the live camera takes the centre of
the screen and gives it back — so a video is the same idea with a different
source, and it shares the same stack rather than inventing a second one. That
means the avatar, the camera and a video can never be on screen together.

WHAT IT ACCEPTS
    a local file        C:\\Users\\me\\clip.mp4, ~/Videos/holiday.mkv
    a direct media URL  https://example.com/trailer.mp4
    a YouTube link      https://youtube.com/watch?v=...
    a YouTube search    "play the new Dune trailer"

    The first two are handed straight to Qt. The last two have to be resolved
    to something playable first, which is what yt-dlp does; without it, this
    says so and offers to open the video in the browser instead of failing.

CONTROLS ARE REAL
    play, pause, resume, previous/next result, seek, volume, mute and quality
    all act on the session that is on screen. A search keeps its five results
    so "next" means something; a single file has none and the transport bar
    says so rather than pretending.

SOUND IS OFF UNTIL ASKED FOR
    A soundtrack talking over the assistant is the one way this feature could
    make JARVIS worse rather than better, and the microphone is open while it
    plays. So it starts muted, and the user turns sound on — from the button in
    the video header, or by saying so. Closing it works both ways too.
"""
from __future__ import annotations

import re
import threading
import webbrowser
from pathlib import Path

_YT = re.compile(r"(youtube\.com/|youtu\.be/)", re.I)

# Which open request is the current one. Opening happens on a thread now, so
# "stop" can arrive while a video is still being resolved — and without this the
# stop would find nothing playing, say so, and then the video would appear
# anyway a second later. Bumping the number abandons whatever is in flight.
_open_lock = threading.Lock()
_open_seq = 0


def _begin_open() -> int:
    global _open_seq
    with _open_lock:
        _open_seq += 1
        return _open_seq


def _still_wanted(token: int) -> bool:
    with _open_lock:
        return token == _open_seq


# The session behind the transport bar: which request produced it, the
# candidate videos, which one is showing, and the quality it was resolved at.
# Entries are resolved lazily, so a search does not pay for five streams.
_session_lock = threading.Lock()
_session: dict = {
    "source": "",
    "entries": [],      # [{url, title, kind}] — kind "youtube" or "direct"
    "index": 0,
    "quality": "",      # the user's request: "480p", "best", ""
}


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://", "file://"))


def _local_path(text: str) -> str:
    """The text as an existing file on disk, or ''."""
    raw = (text or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    try:
        p = Path(raw).expanduser()
        if p.exists() and p.is_file():
            return str(p)
    except Exception:
        pass
    return ""


def _quality_cap(quality: str) -> int:
    """A height ceiling from what the user said; 720 is the default.

    720 is the ceiling on purpose: the HUD panel is nowhere near 4K, and a
    smaller stream starts sooner and drifts less.
    """
    q = (quality or "").strip().lower()
    if not q or q in ("auto", "default"):
        return 720
    if q in ("best", "max", "highest"):
        return 2160
    m = re.match(r"(\d{3,4})", q)
    if m:
        return max(240, min(2160, int(m.group(1))))
    return 720


def _entry_url(e: dict) -> str:
    """A watchable URL from a flat yt-dlp entry."""
    url = str(e.get("webpage_url") or e.get("url") or "")
    if url.startswith("http"):
        return url
    vid = str(e.get("id") or url)
    return f"https://www.youtube.com/watch?v={vid}" if vid else ""


def _youtube_entries(source: str) -> tuple[list[dict], str]:
    """(entries, error). Flat extraction: titles and URLs, no streams yet.

    Five results rather than one, because the transport bar has a "next"
    button and a queue of one would make it a lie.
    """
    try:
        import yt_dlp
    except Exception:
        return [], ("yt-dlp is not installed, so a YouTube video "
                    "cannot be played inside the HUD. "
                    "Run: pip install yt-dlp")

    query = source if _YT.search(source) else f"ytsearch5:{source}"
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "extract_flat": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception as e:
        return [], f"the video could not be resolved: {e}"

    if not info:
        return [], "nothing was found for that."
    raw = info.get("entries") or [info]
    entries = []
    for e in raw:
        if not e:
            continue
        url = _entry_url(e)
        if url:
            entries.append({"kind": "youtube", "url": url,
                            "title": e.get("title") or ""})
    if not entries:
        return [], "nothing playable was found for that."
    return entries, ""


def _codec_rank(f: dict, kind: str) -> int:
    """Prefer what Qt plays most reliably.

    Qt's FFmpeg backend handles all of these, but H.264/AAC is the pair with
    hardware decoding on every Windows machine, so it opens faster, uses less
    CPU and is the least likely to show a black frame while the first keyframe
    is found. VP9 and AV1 follow; anything unrecognised comes last.
    """
    c = str(f.get(kind) or "").lower()
    if kind == "vcodec":
        if c.startswith(("avc1", "h264")):
            return 3
        if "vp9" in c or c.startswith("vp09"):
            return 2
        if c.startswith(("av01", "av1")):
            return 1
        return 0
    if c.startswith(("mp4a", "aac")):
        return 3
    if c.startswith("opus"):
        return 2
    if c.startswith("vorbis"):
        return 1
    return 0


def _resolve_stream(url: str, cap: int) -> tuple[str, str, str, str, int, bool]:
    """(video_url, audio_url, title, error, height, has_audio) for one video.

    TWO urls, because YouTube no longer offers one.
    The first version of this asked for a format carrying both picture and
    sound and got "Requested format is not available". That was not a bad
    selector, it was a wrong assumption: checked against three videos —
    including the oldest upload on the site — every one offered ZERO combined
    formats. Picture and sound are separate streams now, and both are returned
    so the player can run them together.

    `audio_url` comes back empty when only a combined or video-only stream
    exists, which is the normal case for everything that is not YouTube.
    `has_audio` is separate because a video-only stream is a real answer, not
    a failure: the picture plays and the sound controls say there is none.
    """
    try:
        import yt_dlp
    except Exception:
        return "", "", "", ("yt-dlp is not installed, so a YouTube video "
                            "cannot be played inside the HUD. "
                            "Run: pip install yt-dlp"), 0, False

    # No "format" here on purpose: ask for everything and choose below, so a
    # video that lacks whatever was requested cannot fail outright.
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info and info.get("entries"):
            info = info["entries"][0]
        if not info:
            return "", "", "", "nothing was found for that.", 0, False
    except Exception as e:
        return "", "", "", f"the video could not be resolved: {e}", 0, False

    formats = [f for f in (info.get("formats") or [])
               if str(f.get("protocol") or "").startswith("http") and f.get("url")]

    def has(f, key):
        return f.get(key) not in (None, "none")

    def best(candidates):
        """Highest quality — but the ORIGINAL language before quality.

        YouTube auto-dubs a lot of videos now, and it offers every dub as its
        own audio stream at the SAME bitrate as the original: measured on one
        video, English original at 129.483 and Arabic, Bangla, German, Spanish,
        French, Hindi and Indonesian all at 129.482. Choosing on bitrate alone
        therefore came down to a thousandth of a kilobit, and a video would play
        in Arabic one time and English the next for no reason the user could
        see.

        YouTube marks the original track with language_preference 10 and every
        dub with -1, so that decides it first and bitrate only breaks ties
        within the same track. Video formats carry no language, so this is the
        same ordering for them with the first term always equal.
        Codec is the first term: H.264 before VP9 before AV1, because that is
        the order in which Qt opens a picture on Windows without a black frame.
        """
        if not candidates:
            return None
        return max(candidates,
                   key=lambda f: (_codec_rank(f, "vcodec" if has(f, "vcodec")
                                              else "acodec"),
                                  f.get("language_preference") or 0,
                                  f.get("tbr") or 0))

    # Combined first — if a video still has one under the ceiling, one player
    # is simpler and cannot drift.
    combined = best([f for f in formats
                     if has(f, "vcodec") and has(f, "acodec")
                     and (f.get("height") or 0) <= cap])
    if combined:
        h = int(combined.get("height") or 0)
        return combined["url"], "", info.get("title") or "", "", h, True

    video = best([f for f in formats
                  if has(f, "vcodec") and not has(f, "acodec")
                  and (f.get("height") or 0) <= cap])
    if not video:
        # Nothing under the ceiling: take the smallest picture rather than
        # failing, and say honestly which height it ended up at.
        video = min([f for f in formats
                     if has(f, "vcodec") and not has(f, "acodec")],
                    key=lambda f: f.get("height") or 9999, default=None)
    audio = best([f for f in formats
                  if has(f, "acodec") and not has(f, "vcodec")
                  and f.get("ext") == "m4a"])
    if not audio:
        audio = best([f for f in formats
                      if has(f, "acodec") and not has(f, "vcodec")])

    if not video:
        return "", "", "", "no playable stream was offered for that video.", 0, False
    if audio and audio.get("language"):
        print(f"[Video] audio track: {audio.get('language')} "
              f"({audio.get('format_note') or audio.get('format_id')})")
    height = int(video.get("height") or 0)
    return (video["url"], (audio or {}).get("url", ""),
            info.get("title") or "", "", height, bool(audio))


def _search_page(query: str) -> str:
    from urllib.parse import quote_plus
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def _say(player, instruction: str) -> None:
    if player is not None and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


def _friendly_error(err: str) -> str:
    """Say the thing the user can act on, not yt-dlp's whole paragraph."""
    e = (err or "").lower()
    if "sign in to confirm" in e or "not a bot" in e:
        return "YouTube asked for a sign-in check on this machine"
    if "private video" in e:
        return "that video is private"
    if "age" in e and "restrict" in e:
        return "that video is age-restricted"
    if "unavailable" in e or "not available" in e:
        return "that video is not available"
    if "live event will begin" in e:
        return "that live stream has not started"
    return err


def _fallback(player, source: str, err: str) -> None:
    """Resolution failed. Opening it in a browser is worse than playing it in
    the HUD, but it is a great deal better than doing nothing, and the reason
    is reported rather than swallowed."""
    page = source if _YT.search(source) else _search_page(source)
    opened = ""
    try:
        webbrowser.open(page)
        opened = " and it has been opened in the browser instead"
    except Exception:
        pass
    _say(player, "Tell the user, in one short sentence in their own language: "
                 f"'{source}' could not be played on the display "
                 f"({_friendly_error(err)}){opened}.")


def _remember(source: str, entries: list[dict], index: int, quality: str) -> None:
    with _session_lock:
        _session.update({"source": source, "entries": list(entries),
                         "index": int(index), "quality": quality or ""})


def _snapshot() -> dict:
    with _session_lock:
        return {"source": _session["source"],
                "entries": list(_session["entries"]),
                "index": _session["index"],
                "quality": _session["quality"]}


def _install_handlers(player) -> None:
    """Give the transport bar its previous/next and RETRY buttons.

    The bar cannot resolve a stream, so it asks the action layer; the answer
    is resolved on a thread like every other open.
    """
    try:
        player.set_video_skip_handler(
            lambda direction: threading.Thread(
                target=_skip, args=(player, direction), daemon=True,
                name="video-skip").start())
        player.set_video_retry_handler(
            lambda: threading.Thread(
                target=_retry_current, args=(player,), daemon=True,
                name="video-retry").start())
    except Exception:
        pass


def _open_entry(player, entry: dict, token: int, cap: int) -> None:
    """Resolve one entry and put it on screen. The slow part, on a thread."""
    url, audio, title, err, height, has_audio = _resolve_stream(entry["url"], cap)
    if not _still_wanted(token):
        return

    if url:
        label = f"{height}p" if height else f"{cap}p"
        try:
            player.set_video_quality(label)
            player.set_video_sound_available(has_audio)
        except Exception:
            pass
        try:
            player.show_video(url, title or entry.get("title") or entry["url"],
                              muted=True, audio_source=audio)
            return
        except Exception as e:                              # noqa: BLE001
            err = str(e)

    _fallback(player, entry["url"], err)


def _begin_youtube(player, source: str, quality: str, token: int) -> None:
    """Search/link → queue → first video. Runs on a thread."""
    entries, err = _youtube_entries(source)
    if not _still_wanted(token):
        return
    if not entries:
        _fallback(player, source, err)
        return
    _remember(source, entries, 0, quality)
    try:
        player.set_video_queue(0, len(entries))
    except Exception:
        pass
    _open_entry(player, entries[0], token, _quality_cap(quality))


def _skip(player, direction: int) -> None:
    """Move through the results this session was built from.

    Called from the transport bar and from the tool. It never speaks on
    success — the picture arriving is the answer — and it does nothing when
    there is nowhere to go, because the buttons are disabled in that case.
    """
    snap = _snapshot()
    entries = snap["entries"]
    if len(entries) < 2:
        _say(player, "Tell the user in one short sentence, in their own "
                     "language, that this is the only video in the session.")
        return
    idx = max(0, min(len(entries) - 1, snap["index"] + int(direction)))
    if idx == snap["index"]:
        return
    with _session_lock:
        _session["index"] = idx
    try:
        player.set_video_queue(idx, len(entries))
    except Exception:
        pass
    token = _begin_open()
    threading.Thread(target=_open_entry,
                     args=(player, entries[idx], token,
                           _quality_cap(snap["quality"])),
                     daemon=True, name="video-next").start()


def _retry_current(player) -> None:
    """Reopen what is on screen — a fresh URL when the old one expired."""
    snap = _snapshot()
    entries = snap["entries"]
    if not entries:
        return
    entry = entries[max(0, min(snap["index"], len(entries) - 1))]
    if entry.get("kind") == "direct":
        try:
            player.show_video(entry["url"], entry.get("title") or "",
                              muted=True, audio_source=entry.get("audio") or "")
        except Exception:
            pass
        return
    token = _begin_open()
    threading.Thread(target=_open_entry,
                     args=(player, entry, token, _quality_cap(snap["quality"])),
                     daemon=True, name="video-retry").start()


def _requality(player, quality: str) -> str:
    """Resolve the current video again at another height."""
    snap = _snapshot()
    entries = snap["entries"]
    if not entries:
        return "Nothing is playing."
    entry = entries[max(0, min(snap["index"], len(entries) - 1))]
    if entry.get("kind") == "direct":
        return ("This source is played as it is, so its quality is chosen by "
                "the player — there is nothing to re-resolve.")
    with _session_lock:
        _session["quality"] = quality or ""
    token = _begin_open()
    threading.Thread(target=_open_entry,
                     args=(player, entry, token, _quality_cap(quality)),
                     daemon=True, name="video-quality").start()
    return f"status=requalifying quality={_quality_cap(quality)}p"


def video_player(parameters: dict = None, response=None, player=None,
                 session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "play").strip().lower().replace("-", "_")
    source = (params.get("source") or "").strip()
    quality = (params.get("quality") or "").strip()

    if player is None:
        return "There is no display to play a video on."

    def _num(value, default=0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    # ---- stop -------------------------------------------------------------
    if action in ("stop", "close", "hide"):
        # Cancel an open that has not finished yet, so a video the user has
        # already changed their mind about never reaches the screen.
        was_opening = not player.video_is_playing()
        _begin_open()
        player.stop_video()
        if was_opening:
            return "Stopped it before it opened."
        return "Closed the video."

    # ---- sound ------------------------------------------------------------
    if action in ("mute", "unmute", "sound_on", "sound_off"):
        if not player.video_is_playing():
            return "Nothing is playing."
        if hasattr(player, "video_has_sound") and not player.video_has_sound():
            return "This video has no sound track."
        muted = action in ("mute", "sound_off")
        player.set_video_muted(muted)
        return "Muted the video." if muted else "Turned the video's sound on."

    # ---- transport --------------------------------------------------------
    if action in ("pause", "resume", "toggle", "play_pause"):
        if not player.video_is_playing():
            return "Nothing is playing."
        if action == "pause":
            player.video_pause()
            return "Paused."
        if action == "resume":
            player.video_resume()
            return "Resumed."
        player.video_toggle()
        return "Toggled playback."

    if action in ("seek", "forward", "back", "seek_forward", "seek_back"):
        if not player.video_is_playing():
            return "Nothing is playing."
        seconds = _num(params.get("seconds"), 10.0)
        if action == "back":
            seconds = -abs(seconds)
        elif action == "forward":
            seconds = abs(seconds)
        elif action == "seek_back":
            seconds = -abs(seconds)
        elif action == "seek_forward":
            seconds = abs(seconds)
        elif seconds == 0:
            seconds = 10.0
        player.video_seek(seconds)
        return f"{'Forward' if seconds > 0 else 'Back'} {abs(seconds):.0f} seconds."

    if action in ("volume", "set_volume"):
        level = _num(params.get("level"), -1)
        if level < 0:
            return "What volume? Give a number from 0 to 100."
        level = max(0.0, min(100.0, level))
        player.video_set_volume(level)
        return f"Video volume set to {level:.0f}%."

    if action in ("next", "next_track", "skip"):
        if not player.video_is_playing():
            return "Nothing is playing."
        _skip(player, 1)
        return "status=skipping direction=next"

    if action in ("previous", "prev", "previous_track"):
        if not player.video_is_playing():
            return "Nothing is playing."
        _skip(player, -1)
        return "status=skipping direction=previous"

    if action in ("quality", "set_quality"):
        if not player.video_is_playing():
            return "Nothing is playing."
        if not quality:
            return ("Which quality? Say 360p, 480p, 720p, 1080p or best.")
        return _requality(player, quality)

    if action in ("retry", "reload"):
        if not player.video_is_playing():
            return "Nothing is playing."
        _retry_current(player)
        return "status=retrying"

    # ---- play -------------------------------------------------------------
    if not source:
        return "What should I play?"

    _install_handlers(player)

    local = _local_path(source)
    if local:
        name = Path(local).name
        _remember(source, [{"kind": "direct", "url": local, "audio": "",
                            "title": name}], 0, "")
        try:
            player.set_video_queue(0, 1)
            player.set_video_quality("FILE")
            player.set_video_sound_available(True)
        except Exception:
            pass
        player.show_video(local, name, muted=True)
        return f"Playing {name} on the display, muted."

    # A direct media URL can go straight to the player; a YouTube page cannot.
    if _is_url(source) and not _YT.search(source):
        name = source.rsplit("/", 1)[-1][:44]
        _remember(source, [{"kind": "direct", "url": source, "audio": "",
                            "title": name}], 0, "")
        try:
            player.set_video_queue(0, 1)
            player.set_video_quality("AUTO")
            player.set_video_sound_available(True)
        except Exception:
            pass
        player.show_video(source, name, muted=True)
        return "Playing that on the display, muted."

    # ANSWER FIRST, OPEN SECOND.
    #
    # Working out what to play takes seconds that nothing can remove: measured
    # against YouTube, 3.3s for a spoken phrase (a search, then the video) and
    # 2.0s for a link, plus however long Qt spends buffering before the first
    # frame. Restricting yt-dlp to a lighter client was tried and is not the
    # answer — the fast clients came back with zero usable formats.
    #
    # So the seconds stay, and what changes is where the user spends them:
    # listening to JARVIS say it is coming, instead of watching nothing happen.
    # The same shape whatsapp_call uses, and for the same reason.
    token = _begin_open()
    threading.Thread(target=_begin_youtube,
                     args=(player, source, quality, token),
                     daemon=True, name="video-open").start()
    # Deliberately a status line rather than a finished English sentence: the
    # model writes the words, in the user's own language.
    return f"status=opening source={source}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "video_player",
    "description": (
        "If user wants to open a video on YouTube, he will tell you that specifically"
        "If user wants to play a video, then he wants to call this feature"
        "Plays a VIDEO on the assistant's own display, where the avatar "
        "normally is, and controls it. Use whenever the user wants to WATCH "
        "something on screen: 'play the new Dune trailer', 'play that video', "
        "'play C:/clips/holiday.mp4', 'put that YouTube video on the screen', "
        "'stop the video', 'videoyu kapat', 'pause it', 'devam et', 'next one', "
        "'go back 30 seconds', 'sesi aç', 'mute it', 'turn it down to 30%', "
        "'play it in 480p'. Accepts a local file path, a direct video URL, a "
        "YouTube link, or a description to search YouTube for. Video always "
        "starts MUTED — only call action='unmute' when the user actually asks "
        "to hear it. Volume is the video's own volume, never the system's. "
        "'next'/'previous' move through the search results this session was "
        "built from; with a single file there is nowhere to go. 'quality' "
        "resolves the video again at another height (YouTube only). "
        "This returns the moment it is asked, before the picture appears: when "
        "it answers 'status=opening', say in one short sentence that you are "
        "putting it on the display, and do not wait or call it again. "
        "Do NOT use 'youtube_video' for this: that optional plugin opens videos "
        "in the web browser and summarises transcripts, it never plays anything "
        "on the assistant's display."
    ),
    # Used in place of `description` only when a small model is on a tool
    # budget - Gemini still gets the full text. See providers/schema.py.
    "brief": (
        "Plays and controls a video on the HUD - YouTube, a local file, a URL, "
        "or a search; play/pause/seek/volume/next/previous/quality."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'play' (default), 'stop' to close it, 'pause', 'resume', "
                    "'toggle', 'next'/'previous' result, 'seek' (± 'seconds'), "
                    "'volume' ('level' 0-100), 'unmute'/'mute', or 'quality' "
                    "('quality' such as 480p)."
                ),
                "enum": ["play", "stop", "pause", "resume", "toggle", "next",
                         "previous", "seek", "volume", "mute", "unmute",
                         "quality"],
            },
            "source": {
                "type": "STRING",
                "description": (
                    "For action='play': a local file path, a direct video URL, "
                    "a YouTube link, or what to search YouTube for. Pass what "
                    "the user actually named, in their own words."
                ),
            },
            "seconds": {
                "type": "NUMBER",
                "description": ("For action='seek': seconds to jump; positive "
                                "is forward, negative is back. Default 10."),
            },
            "level": {
                "type": "NUMBER",
                "description": ("For action='volume': 0 to 100. This sets the "
                                "video player's volume, not the system's."),
            },
            "quality": {
                "type": "STRING",
                "description": ("For play/quality: 'best', '1080p', '720p', "
                                "'480p' or '360p'. Only meaningful for YouTube; "
                                "other sources are played as they are."),
            },
        },
        "required": ["action"],
    },
    "handler": video_player,
}

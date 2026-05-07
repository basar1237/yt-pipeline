#!/usr/bin/env python3
"""
Daily YouTube Shorts pipeline - clean visual edition.

Each segment uses topic-relevant Pexels stock footage as the background,
with only a caption overlay (no brand strip). Promotion lives only in the
video description.

Pipeline:
  1. Pick the next unused topic from topics/topic-pool.json
  2. For each segment: TTS (edge-tts), pick a Pexels clip matching
     topic.pexels_query, crop+scale to 1080x1920, overlay caption.
  3. Concat segments into renders/<topic_id>-FINAL.mp4
  4. Upload as PUBLIC Short

Usage:
    python3 daily.py [--no-upload] [--topic TOPIC_ID]
"""
import argparse
import asyncio
import hashlib
import json
import random
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOPICS_DIR = ROOT / "topics"
RENDERS_DIR = ROOT / "renders"
CRED_DIR = ROOT / "credentials"
STOCK_DIR = ROOT / "stock"
TOPIC_POOL = TOPICS_DIR / "topic-pool.json"
STATE_FILE = TOPICS_DIR / "state.json"
PEXELS_KEY_FILE = CRED_DIR / "pexels.key"

BRAND_PHONE = "0546 531 49 10"
BRAND_EMAIL = "basaryldrm1237@gmail.com"

TTS_VOICE = "tr-TR-EmelNeural"
TTS_RATE = "+10%"   # bumped from +0% — modern Shorts pace; reduces "AI voice" feel
W, H = 1080, 1920
FPS = 30
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Caption vertical center as fraction of frame height. 0.40 = upper third.
# (0.50 would be dead-center; we want subjects/faces in the lower half visible.)
CAPTION_Y_FRAC = 0.40

PALETTES = [
    ("0x0F2027", "0x2C5364"),
    ("0x141E30", "0x243B55"),
    ("0x355C7D", "0xC06C84"),
    ("0x42275A", "0x734B6D"),
]


def ensure_deps():
    needed = {
        "edge_tts": "edge-tts",
        "googleapiclient": "google-api-python-client",
        "google_auth_oauthlib": "google-auth-oauthlib",
    }
    missing = []
    for mod, pkg in needed.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Installing: {missing}", flush=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--break-system-packages", *missing],
            check=True,
        )


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"used_topics": [], "uploads": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_topic(force_id=None):
    pool = json.loads(TOPIC_POOL.read_text(encoding="utf-8"))
    state = load_state()
    used = set(state.get("used_topics", []))
    if force_id:
        for t in pool:
            if t["id"] == force_id:
                return t
        raise SystemExit(f"Topic {force_id} not in pool.")
    for t in pool:
        if t["id"] not in used:
            return t
    return None


def tts_segment(text, out_path):
    if out_path.exists() and out_path.stat().st_size > 0:
        return
    import edge_tts

    async def _go():
        c = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, pitch="+0Hz")
        await c.save(str(out_path))

    asyncio.run(_go())


def probe_duration(media_path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(media_path),
    ]).decode().strip()
    return float(out)


def escape_drawtext(s):
    # Replace ASCII apostrophe with typographic right single quote so we
    # don't have to deal with shell+ffmpeg apostrophe escaping hell.
    s = s.replace("'", "’")
    return (
        s.replace("\\", "\\\\")
         .replace(":", "\\:")
         .replace("%", "\\%")
    )


def wrap_caption(text, width=18):
    return textwrap.fill(text, width=width)


def chunk_phrases(text, target_words_per_phrase=2):
    """Split caption into short phrases for progressive (word-by-word style)
    reveal. Strips trailing punctuation, splits on commas first then by word
    count. Aims for 3-5 phrases per segment so each is on screen ~0.7-1.2s."""
    text = text.strip()
    # Strip trailing sentence punctuation but keep internal commas.
    while text and text[-1] in ".!?":
        text = text[:-1]
    # First split on commas — they're natural rhythm breaks for TR speech.
    big_chunks = [c.strip() for c in text.split(",") if c.strip()]
    phrases = []
    for chunk in big_chunks:
        words = chunk.split()
        if len(words) <= target_words_per_phrase + 1:
            phrases.append(chunk)
            continue
        # Sub-split into chunks of ~target_words_per_phrase words each.
        i = 0
        while i < len(words):
            grp = words[i:i + target_words_per_phrase]
            # Avoid leaving a 1-word orphan at the end — merge it back.
            if (len(grp) < target_words_per_phrase
                    and phrases
                    and len(phrases[-1].split()) <= target_words_per_phrase):
                phrases[-1] = phrases[-1] + " " + " ".join(grp)
            else:
                phrases.append(" ".join(grp))
            i += target_words_per_phrase
    if not phrases:
        phrases = [text]
    return phrases


def smart_wrap(phrase, width=14):
    """Wrap on word boundaries; never break long Turkish words mid-character.
    Long words like 'iyileştirmektir' stay whole; we shrink font instead."""
    return textwrap.fill(phrase, width=width, break_long_words=False, break_on_hyphens=False)


def caption_size_for(phrase):
    """Pick a font size so the phrase fills the screen comfortably."""
    longest = max((len(line) for line in smart_wrap(phrase).split("\n")), default=1)
    if longest <= 6:
        return 150
    if longest <= 9:
        return 130
    if longest <= 13:
        return 110
    if longest <= 16:
        return 92
    if longest <= 20:
        return 76
    if longest <= 25:
        return 64
    return 54


def build_progressive_drawtext(phrases, total_duration, font=FONT_BOLD):
    """Build a chain of drawtext filters that reveals each phrase in turn,
    each enabled only during its time window. Returns a string suitable for
    embedding inside filter_complex (no leading/trailing semicolons)."""
    n = len(phrases)
    # Last phrase lingers slightly longer (the "punchline" beat).
    base_slot = total_duration / max(n, 1)
    parts = []
    t = 0.0
    for i, ph in enumerate(phrases):
        # Last phrase gets all remaining time (handles rounding)
        t_end = total_duration if i == n - 1 else round(t + base_slot, 3)
        wrapped = smart_wrap(ph, width=14)
        size = caption_size_for(ph)
        text = escape_drawtext(wrapped)
        # Position: vertical-center anchored at CAPTION_Y_FRAC * H.
        # box=1 puts a semi-transparent card behind text for legibility.
        parts.append(
            f"drawtext=fontfile={font}:text='{text}':"
            f"fontsize={size}:fontcolor=white:"
            f"borderw=10:bordercolor=black:"
            f"box=1:boxcolor=black@0.45:boxborderw=28:"
            f"x=(w-text_w)/2:y={CAPTION_Y_FRAC}*h-text_h/2:"
            f"line_spacing=14:"
            f"enable='between(t,{t:.3f},{t_end:.3f})'"
        )
        t = t_end
    return ",".join(parts)


def get_pexels_key():
    if PEXELS_KEY_FILE.exists():
        return PEXELS_KEY_FILE.read_text(encoding="utf-8").strip()
    return None


def pexels_pick_video(query, segment_idx, topic_id):
    """Search Pexels for a portrait video matching `query` and return a
    cached local path. Caches per (topic, segment) so re-runs are stable."""
    STOCK_DIR.mkdir(exist_ok=True)
    cache_key = hashlib.md5(f"{topic_id}|{segment_idx}|{query}".encode()).hexdigest()[:10]
    out = STOCK_DIR / f"{topic_id}_seg{segment_idx:02d}_{cache_key}.mp4"
    if out.exists() and out.stat().st_size > 100_000:
        return out

    key = get_pexels_key()
    if not key:
        raise SystemExit(
            "Pexels API key not found. Put it at credentials/pexels.key."
        )

    # Pexels Videos API
    url = (
        "https://api.pexels.com/videos/search?"
        f"query={urllib.request.quote(query)}&orientation=portrait&size=medium&per_page=15"
    )
    req = urllib.request.Request(url, headers={"Authorization": key, "User-Agent": "yt-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())

    videos = data.get("videos", [])
    if not videos:
        # fallback: try landscape, we'll center-crop anyway
        url2 = (
            "https://api.pexels.com/videos/search?"
            f"query={urllib.request.quote(query)}&per_page=15"
        )
        req2 = urllib.request.Request(url2, headers={"Authorization": key, "User-Agent": "yt-pipeline/1.0"})
        with urllib.request.urlopen(req2, timeout=30) as r:
            data = json.loads(r.read())
        videos = data.get("videos", [])
    if not videos:
        raise SystemExit(f"Pexels: no results for '{query}'")

    # rotate by seg index so different segments get different clips
    rng = random.Random(f"{topic_id}|{segment_idx}")
    candidates = list(videos)
    rng.shuffle(candidates)

    chosen_url = None
    for v in candidates:
        files = v.get("video_files", [])
        # pick the smallest >= 720p HD/SD clip to save bandwidth
        files = sorted(
            (f for f in files if f.get("width") and f.get("height")),
            key=lambda f: (abs(f["height"] - 1280), f["width"] * f["height"]),
        )
        for f in files:
            if f.get("link"):
                chosen_url = f["link"]
                break
        if chosen_url:
            break

    if not chosen_url:
        raise SystemExit(f"Pexels: no downloadable file for '{query}'")

    print(f"    pexels: {chosen_url[:80]}...", flush=True)
    dl_req = urllib.request.Request(chosen_url, headers={"User-Agent": "yt-pipeline/1.0"})
    with urllib.request.urlopen(dl_req, timeout=120) as r:
        out.write_bytes(r.read())
    return out


def render_segment(seg_idx, caption, audio_path, stock_path, out_path, palette_idx):
    """Render: stock video bg (cropped to 1080x1920) + progressive caption.
    Audio is the TTS narration. Stock video's audio is dropped.
    Caption reveals phrase-by-phrase synced roughly to TTS pacing."""
    if out_path.exists() and out_path.stat().st_size > 0:
        return
    duration = probe_duration(audio_path)
    duration = round(duration + 0.25, 3)

    phrases = chunk_phrases(caption, target_words_per_phrase=2)
    drawtext_chain = build_progressive_drawtext(phrases, duration)

    # Crop+scale stock to fill 1080x1920 then loop, slow zoom for life,
    # then chain progressive drawtext layers (each enable-windowed).
    filt = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},"
        f"fps={FPS},setsar=1,trim=duration={duration},"
        # gentle zoom for life
        f"zoompan=z='min(zoom+0.0004,1.06)':d={int(duration*FPS)}:"
        f"x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s={W}x{H}:fps={FPS}[bg];"
        f"[bg]{drawtext_chain}[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(stock_path),
        "-i", str(audio_path),
        "-filter_complex", filt,
        "-map", "[v]",
        "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-r", str(FPS),
        "-t", str(duration),
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def render_segment_fallback(seg_idx, caption, audio_path, out_path):
    """Gradient fallback if Pexels is unavailable. Progressive caption."""
    if out_path.exists() and out_path.stat().st_size > 0:
        return
    duration = probe_duration(audio_path)
    duration = round(duration + 0.25, 3)
    c1, c2 = PALETTES[seg_idx % len(PALETTES)]

    phrases = chunk_phrases(caption, target_words_per_phrase=2)
    drawtext_chain = build_progressive_drawtext(phrases, duration)

    filt = (
        f"gradients=size={W}x{H}:rate={FPS}:duration={duration}:"
        f"c0={c1}:c1={c2}:x0=0:y0=0:x1={W}:y1={H}[bg];"
        f"[bg]zoompan=z='min(zoom+0.0004,1.06)':d={int(duration*FPS)}:"
        f"x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s={W}x{H}:fps={FPS}[zoomed];"
        f"[zoomed]{drawtext_chain}[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration}",
        "-i", str(audio_path),
        "-filter_complex", filt,
        "-map", "[v]",
        "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-r", str(FPS),
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def concat_segments(segment_files, out_final):
    # Always rewrite the concat list — paths from a previous session's mount
    # (e.g. /sessions/<old-id>/...) become invalid on the next run, so reusing
    # a stale concat.txt causes ffmpeg to fail to open inputs.
    # We write paths relative to the concat file's parent dir so the list is
    # portable across sessions.
    concat_list = out_final.with_suffix(".concat.txt")
    parent = out_final.parent.resolve()
    rel_lines = []
    for p in segment_files:
        try:
            rel = p.resolve().relative_to(parent)
        except ValueError:
            rel = p.resolve()  # falls back to absolute if not under parent
        rel_lines.append(f"file '{rel}'")
    concat_list.write_text("\n".join(rel_lines) + "\n", encoding="utf-8")

    if out_final.exists() and out_final.stat().st_size > 0:
        return
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out_final),
    ]
    subprocess.run(cmd, check=True)


def build_description(topic):
    body = topic.get("description", "")
    return (
        f"{body}\n\n"
        f"Web Sitesi + Yayin - HER SEY DAHIL 10.000 TL\n"
        f"Iletisim: {BRAND_PHONE}\n"
        f"E-posta: {BRAND_EMAIL}\n\n"
        f"#shorts #ilginc #bilgi #websitesi #webdesign"
    )


def upload_to_youtube(final_path, title, description, tags):
    from upload import upload_short
    return upload_short(
        video_path=str(final_path),
        title=title,
        description=description,
        tags=tags,
        privacy="public",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--topic", help="Force a topic id")
    args = ap.parse_args()

    ensure_deps()
    RENDERS_DIR.mkdir(exist_ok=True)

    topic = pick_topic(args.topic)
    if topic is None:
        print("Tum konular kullanildi - topic-pool.json'a yeni konu ekle.")
        sys.exit(2)

    topic_id = topic["id"]
    work = RENDERS_DIR / topic_id
    work.mkdir(exist_ok=True)
    print(f"Topic: {topic_id} - {topic['title']}", flush=True)

    pexels_query = topic.get("pexels_query") or topic.get("title", "")
    have_pexels = bool(get_pexels_key())

    # All segments use topic-relevant footage. Per-segment shuffle inside
    # pexels_pick_video gives variety. (Earlier versions used generic
    # "shocked reaction" / "funny dog" hook clips for seg 0, but a non-topical
    # opening clip causes audio/visual mismatch and tanks retention in the
    # first 1.5s — exactly when viewers decide to swipe.)
    segment_files = []
    for i, seg in enumerate(topic["segments"]):
        audio = work / f"seg{i:02d}.mp3"
        clip = work / f"seg{i:02d}.mp4"
        print(f"  [seg {i+1}/{len(topic['segments'])}] {seg[:60]}...", flush=True)
        tts_segment(seg, audio)
        if have_pexels:
            stock = pexels_pick_video(pexels_query, i, topic_id)
            render_segment(i, seg, audio, stock, clip, i)
        else:
            print("    (no pexels key, using gradient fallback)", flush=True)
            render_segment_fallback(i, seg, audio, clip)
        segment_files.append(clip)

    final = RENDERS_DIR / f"{topic_id}-FINAL.mp4"
    concat_segments(segment_files, final)
    print(f"Final video: {final}", flush=True)

    if args.no_upload:
        print("--no-upload set; skipping YouTube upload.")
        return

    token_path = CRED_DIR / "token.json"
    if not token_path.exists():
        print("WARN: credentials/token.json missing - skipping upload.")
        return

    desc = build_description(topic)
    tags = topic.get("tags", ["shorts", "ilginc", "bilgi"])
    video_id = upload_to_youtube(final, topic["title"], desc, tags)
    url = f"https://youtube.com/shorts/{video_id}"
    print(f"Uploaded: {url}", flush=True)

    state = load_state()
    state["used_topics"].append(topic_id)
    state["uploads"].append({
        "topic_id": topic_id,
        "title": topic["title"],
        "video_id": video_id,
        "url": url,
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    save_state(state)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Convert Telegram HTML chat exports into WhatsApp-style .txt files.

Telegram Desktop exports a chat as messages.html (plus messages2.html,
messages3.html, ... for long histories). This tool parses those files and
emits a single .txt where each line looks like a WhatsApp export:

    [DD/MM/YYYY, HH:MM:SS] Sender Name: Message text
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

try:
    from bs4 import BeautifulSoup, Tag
except ImportError:
    sys.stderr.write(
        "Missing dependency 'beautifulsoup4'. Install it with:\n"
        "    pip install beautifulsoup4 lxml\n"
    )
    sys.exit(1)


# Title attribute on Telegram's date element looks like:
#   "01.05.2024 14:30:25 UTC+05:30"
DATE_TITLE_RE = re.compile(
    r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})\s+"
    r"(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?"
)

MESSAGE_NUM_RE = re.compile(r"message(\d+)")


@dataclass
class Message:
    timestamp: datetime
    sender: str
    body: str

    def format_whatsapp(self) -> str:
        ts = self.timestamp.strftime("%d/%m/%Y, %H:%M:%S")
        # WhatsApp encodes newlines inside a message as literal newlines and
        # only the first line carries the [timestamp] prefix.
        return f"[{ts}] {self.sender}: {self.body}"


def _parse_title_datetime(title: str) -> datetime | None:
    match = DATE_TITLE_RE.search(title)
    if not match:
        return None
    return datetime(
        year=int(match["y"]),
        month=int(match["m"]),
        day=int(match["d"]),
        hour=int(match["H"]),
        minute=int(match["M"]),
        second=int(match["S"] or 0),
    )


def _clean_text(node: Tag) -> str:
    """Extract human-readable text from a tag, preserving line breaks."""
    # Replace <br> with newlines so multi-line messages survive.
    for br in node.find_all("br"):
        br.replace_with("\n")
    text = node.get_text("", strip=False)
    # Collapse runs of spaces/tabs but keep newlines.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _describe_media(body: Tag) -> str | None:
    """Return a textual placeholder for media/attachments inside a message."""
    parts: list[str] = []

    # Stickers: <img class="sticker" alt="..." />
    for sticker in body.select("img.sticker"):
        emoji = (sticker.get("alt") or "").strip()
        parts.append(f"<Sticker{f' {emoji}' if emoji else ''}>")

    # Generic media wrappers (photo, video, voice, audio, file, ...). Skip
    # sticker wrappers — those are already covered by the img.sticker pass.
    for media in body.select("a.media"):
        title_el = media.select_one(".title")
        title = title_el.get_text(strip=True) if title_el else ""
        desc_el = media.select_one(".description")
        desc = desc_el.get_text(strip=True) if desc_el else ""
        href = media.get("href") or ""

        kind = title or _kind_from_href(href) or "Attachment"
        label = f"<{kind}"
        if desc and desc.lower() != kind.lower():
            label += f": {desc}"
        if href:
            label += f" ({href})"
        label += ">"
        parts.append(label)

    # Photos that render as <img class="photo"> instead of an <a.media>
    for photo in body.select("a.photo_wrap, img.photo"):
        href = photo.get("href") or photo.get("src") or ""
        suffix = f" ({href})" if href else ""
        parts.append(f"<Photo{suffix}>")

    # Polls
    poll = body.select_one(".media_poll, .poll")
    if poll:
        question = poll.select_one(".question, .title")
        q = question.get_text(strip=True) if question else ""
        parts.append(f"<Poll{f': {q}' if q else ''}>")

    # Locations
    for loc in body.select("a.media_location"):
        href = loc.get("href") or ""
        parts.append(f"<Location{f' ({href})' if href else ''}>")

    return "\n".join(parts) if parts else None


def _kind_from_href(href: str) -> str | None:
    if not href:
        return None
    lower = href.lower()
    if "/photos/" in lower or lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "Photo"
    if "/video_files/" in lower or lower.endswith((".mp4", ".mov", ".webm")):
        return "Video"
    if "/voice_messages/" in lower or lower.endswith((".ogg", ".oga")):
        return "Voice message"
    if "/audio_files/" in lower or lower.endswith((".mp3", ".m4a", ".flac", ".wav")):
        return "Audio"
    if "/stickers/" in lower or lower.endswith(".tgs"):
        return "Sticker"
    if "/round_video_messages/" in lower:
        return "Video message"
    if "/files/" in lower:
        return "File"
    return None


def _extract_reply(body: Tag, soup: BeautifulSoup) -> str | None:
    reply = body.select_one(".reply_to")
    if not reply:
        return None
    link = reply.find("a", href=True)
    if link and link["href"].startswith("#message"):
        target = soup.find(id=link["href"][1:])
        if target is not None:
            target_text_el = target.select_one(".text")
            if target_text_el is not None:
                snippet = _clean_text(target_text_el)
                if snippet:
                    snippet = snippet.splitlines()[0]
                    if len(snippet) > 80:
                        snippet = snippet[:77] + "..."
                    return snippet
    text = _clean_text(reply)
    return text or None


def _extract_forward(body: Tag) -> str | None:
    fwd_block = body.select_one(".forwarded .from_name")
    if not fwd_block:
        return None
    # Telegram nests the original sender in an inner span:
    #   <div class="from_name">Forwarded from <span class="from_name">X</span></div>
    inner = fwd_block.find(class_="from_name")
    target = inner if inner is not None else fwd_block
    text = _clean_text(target)
    # Strip any leading "Forwarded from " that survived (older exports).
    return re.sub(r"^Forwarded from\s+", "", text).strip() or None


def parse_html_file(
    path: Path,
    last_sender: str = "Unknown",
) -> tuple[list[Message], str]:
    """Parse one Telegram HTML file. Returns (messages, last_known_sender).

    The trailing sender is propagated between consecutive HTML pages because
    Telegram only repeats the name on the first message of each grouping; a
    page can begin with a "joined" message that inherits the previous page's
    sender.
    """
    with path.open("r", encoding="utf-8") as fh:
        soup = BeautifulSoup(fh, "lxml")

    messages: list[Message] = []
    current_sender = last_sender

    for div in soup.select("div.message"):
        classes = div.get("class") or []
        if "service" in classes:
            # Service messages (date headers, "X joined the group", etc.) are
            # informational only — skip to keep the export chat-like.
            continue

        body = div.select_one(".body")
        if body is None:
            continue

        date_el = body.select_one(".date.details") or div.select_one(".date.details")
        if date_el is None:
            continue
        ts = _parse_title_datetime(date_el.get("title", ""))
        if ts is None:
            continue

        if "joined" not in classes:
            from_name_el = next(
                (
                    el
                    for el in body.find_all(class_="from_name")
                    if not el.find_parent(class_="forwarded")
                ),
                None,
            )
            if from_name_el is not None:
                current_sender = _clean_text(from_name_el) or current_sender

        text_parts: list[str] = []

        reply = _extract_reply(body, soup)
        if reply:
            text_parts.append(f"[Reply: {reply}]")

        forward = _extract_forward(body)
        if forward:
            text_parts.append(f"[Forwarded from {forward}]")

        text_el = body.select_one(".text")
        if text_el is not None:
            txt = _clean_text(text_el)
            if txt:
                text_parts.append(txt)

        media = _describe_media(body)
        if media:
            text_parts.append(media)

        if not text_parts:
            # Could be an empty message or unsupported type — keep a marker so
            # the timeline stays intact.
            text_parts.append("<Unsupported message>")

        messages.append(
            Message(
                timestamp=ts,
                sender=current_sender or "Unknown",
                body="\n".join(text_parts),
            )
        )

    return messages, current_sender


def _natural_key(path: Path) -> tuple:
    """Sort messages.html, messages2.html, messages10.html in numeric order."""
    match = re.search(r"(\d+)", path.stem)
    return (int(match.group(1)) if match else 1, path.name)


def discover_html_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        candidates = sorted(target.glob("messages*.html"), key=_natural_key)
        if candidates:
            return candidates
        # Fall back to any .html in the directory.
        return sorted(target.glob("*.html"), key=_natural_key)
    raise FileNotFoundError(f"No such file or directory: {target}")


def convert(
    inputs: Iterable[Path],
    output: Path,
) -> int:
    files = []
    for inp in inputs:
        files.extend(discover_html_files(inp))
    if not files:
        raise SystemExit("No Telegram HTML files found.")

    total = 0
    last_sender = "Unknown"
    with output.open("w", encoding="utf-8") as out:
        for path in files:
            print(f"Parsing {path} ...", file=sys.stderr)
            messages, last_sender = parse_html_file(path, last_sender)
            for msg in messages:
                out.write(msg.format_whatsapp())
                out.write("\n")
            total += len(messages)
    return total


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Convert a Telegram Desktop HTML chat export into a WhatsApp-style "
            ".txt file."
        ),
    )
    p.add_argument(
        "input",
        nargs="+",
        type=Path,
        help=(
            "Path to a Telegram HTML file (e.g. messages.html) or to the "
            "export folder containing messages*.html. Multiple paths allowed."
        ),
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("chat.txt"),
        help="Output .txt path (default: chat.txt in current directory).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    count = convert(args.input, args.output)
    print(f"Wrote {count} messages to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

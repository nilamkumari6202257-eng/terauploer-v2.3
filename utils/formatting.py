"""Human readable formatting helpers shared by every handler."""
from __future__ import annotations

import re
import time

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human(size: float) -> str:
    value = float(max(0.0, size or 0))
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def speed_label(bytes_per_second: float) -> str:
    return f"{human(max(0.0, bytes_per_second or 0))}/s"


def duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def eta_label(current: int, total: int, bytes_per_second: float) -> str:
    if total <= 0:
        return "ETA: --"
    remaining = max(0, total - current)
    if remaining == 0:
        return "ETA: done"
    if bytes_per_second <= 0:
        return "ETA: estimating"
    return f"ETA: {duration(remaining / bytes_per_second)}"


def bar(current: int, total: int, width: int = 12) -> str:
    ratio = 0.0 if total <= 0 else min(1.0, max(0.0, current / total))
    filled = int(round(ratio * width))
    return f"[{'█' * filled}{'░' * (width - filled)}] {ratio * 100:.0f}%"


def progress_line(
    current: int,
    total: int,
    bytes_per_second: float | None = None,
    width: int = 12,
) -> str:
    parts = [bar(current, total, width), f"{human(current)} / {human(total)}"]
    if bytes_per_second is not None:
        parts.append(speed_label(bytes_per_second))
        parts.append(eta_label(current, total, bytes_per_second))
    return " | ".join(parts)


def safe_name(name: str, fallback: str | None = None) -> str:
    cleaned = name or ""
    # Control characters and filesystem-unsafe punctuation.
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "_", cleaned)
    cleaned = re.sub(r'[\\/:*?"<>|+]', "_", cleaned)
    # Emoji / pictographs / symbols / variation selectors — TeraBox paths reject
    # these (errno -7 "path file name is invalid"), so keep only word
    # characters (letters/digits/underscore of any script), spaces and a few
    # harmless punctuation marks.
    cleaned = re.sub(r"[^\w\s\-().,!&'’]", "_", cleaned)
    cleaned = re.sub(
        "["
        "\U0001F000-\U0001FAFF"   # emoji, pictographs
        "\U00002600-\U000027BF"   # misc symbols & dingbats
        "\U00002B00-\U00002BFF"   # arrows, stars
        "\U00002190-\U000021FF"   # arrows
        "\U00002300-\U000023FF"   # misc technical
        "\U00002500-\U000025FF"   # box drawing / geometric shapes
        "\U00002900-\U0000297F"   # supplemental arrows
        "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
        "\U0000200B-\U0000200F"   # zero-width chars
        "\U0000FE00-\U0000FE0F"   # variation selectors
        "\U00002700-\U000027BF"
        "]+",
        "_",
        cleaned,
    )
    # Collapse separators and trim.
    cleaned = re.sub(r"[ ]{2,}", " ", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip(" ._")
    encoded = (cleaned or fallback or f"file_{int(time.time())}").encode("utf-8")[:120]
    while encoded:
        try:
            result = encoded.decode("utf-8").strip()
            if result:
                return result
        except UnicodeDecodeError:
            encoded = encoded[:-1]
            continue
        break
    return fallback or f"file_{int(time.time())}"


def short(text: str, limit: int = 40) -> str:
    value = (text or "").strip().replace("\n", " ")
    return value if len(value) <= limit else value[: max(1, limit - 1)] + "…"

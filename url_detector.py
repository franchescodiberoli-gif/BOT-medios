import re

PLATFORM_PATTERNS = {
    "instagram": [
        r"instagram\.com\/(p|reel|tv)\/",
        r"instagr\.am\/",
    ],
    "tiktok": [
        r"tiktok\.com\/@.+\/video\/",
        r"vm\.tiktok\.com\/",
        r"vt\.tiktok\.com\/",
    ],
    "facebook": [
        r"facebook\.com\/(watch|reel|share\/v|video\.php)",
        r"fb\.watch\/",
        r"fb\.com\/",
    ],
    "youtube_short": [
        r"youtube\.com\/shorts\/",
        r"youtu\.be\/",
    ],
    "youtube_long": [
        r"youtube\.com\/watch\?v=",
        r"youtube\.com\/live\/",
    ],
    "reddit": [
        r"reddit\.com\/r\/.+\/comments\/",
        r"redd\.it\/",
        r"redgifs\.com\/watch\/",
        r"v\.redd\.it\/",
        r"i\.redd\.it\/",
    ],
    "twitter": [
        r"twitter\.com\/.+\/status\/",
        r"x\.com\/.+\/status\/",
        r"t\.co\/",
    ],
    "threads": [
        r"threads\.net\/@.+\/post\/",
        r"threads\.com\/",
    ],
}

URL_REGEX = re.compile(r"https?://[^\s]+")


def extract_url(text: str) -> str | None:
    match = URL_REGEX.search(text)
    return match.group(0).rstrip(".,)>\"'") if match else None


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for platform, patterns in PLATFORM_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, url_lower):
                return platform
    return "unknown"

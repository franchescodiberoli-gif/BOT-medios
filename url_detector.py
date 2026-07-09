import re

PLATFORM_PATTERNS = {
    "instagram": [
        r"instagram\.com\/(p|reel|tv)\/",
        r"instagram\.com\/(reels|share)\/",
        r"instagr\.am\/",
    ],
    "tiktok": [
        r"tiktok\.com\/@.+\/video\/",
        r"tiktok\.com\/t\/",
        r"vm\.tiktok\.com\/",
        r"vt\.tiktok\.com\/",
    ],
    "facebook_ads": [
        r"facebook\.com\/ads\/library\/",
    ],
    "facebook": [
        r"facebook\.com\/(watch|reel|video\.php)",
        r"facebook\.com\/share\/(v|p|r|reels)\/",
        r"facebook\.com\/[^/?]+\/(videos|reels)\/",
        r"facebook\.com\/reel\/",
        r"fb\.watch\/",
    ],
    "youtube_short": [
        r"youtube\.com\/shorts\/",
    ],
    "youtube_long": [
        r"youtu\.be\/",
        r"youtube\.com\/watch\?v=",
        r"youtube\.com\/live\/",
        r"youtube\.com\/embed\/",
    ],
    "reddit": [
        r"reddit\.com\/r\/.+\/comments\/",
        r"reddit\.com\/r\/.+\/s\/",
        r"reddit\.com\/user\/.+\/comments\/",
        r"redd\.it\/",
        r"v\.redd\.it\/",
        r"i\.redd\.it\/",
    ],
    "redgifs": [
        r"redgifs\.com\/watch\/",
    ],
    "twitter": [
        r"twitter\.com\/.+\/status\/",
        r"x\.com\/.+\/status\/",
    ],
    "threads": [
        r"threads\.net\/@.+\/post\/",
        r"threads\.com\/@.+\/post\/",
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

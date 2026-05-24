from downloader import clean_hashtags


def format_instagram(info: dict, clean_url: str) -> str:
    caption = info.get("description", "") or ""
    hashtags = clean_hashtags(caption)
    body = caption.replace(hashtags, "").strip()
    msg = f"📸 *Instagram*\n\n🔗 [Ver contenido]({clean_url})\n\n"
    if body:
        msg += f"📝 *Caption:*\n{body}\n\n"
    if hashtags:
        msg += f"*#️⃣ Hashtags:*\n{hashtags}"
    return msg.strip()


def format_tiktok(info: dict, clean_url: str) -> str:
    caption = info.get("description", "") or ""
    hashtags = clean_hashtags(caption)
    body = caption.replace(hashtags, "").strip()
    msg = f"🎵 *TikTok*\n\n🔗 [Ver contenido]({clean_url})\n\n"
    if body:
        msg += f"📝 *Caption:*\n{body}\n\n"
    if hashtags:
        msg += f"*#️⃣ Hashtags:*\n{hashtags}"
    return msg.strip()


def format_facebook(info: dict, clean_url: str) -> str:
    caption = info.get("description", "") or info.get("title", "") or ""
    hashtags = clean_hashtags(caption)
    body = caption.replace(hashtags, "").strip()
    msg = f"📘 *Facebook*\n\n🔗 [Ver contenido]({clean_url})\n\n"
    if body:
        msg += f"📝 *Caption:*\n{body}\n\n"
    if hashtags:
        msg += f"*#️⃣ Hashtags:*\n{hashtags}"
    return msg.strip()


def format_youtube_short(info: dict, clean_url: str) -> str:
    caption = info.get("description", "") or ""
    hashtags = clean_hashtags(caption)
    body = caption.replace(hashtags, "").strip()
    msg = f"▶️ *YouTube Short*\n\n🔗 [Ver contenido]({clean_url})\n\n"
    if body:
        msg += f"📝 *Caption:*\n{body}\n\n"
    if hashtags:
        msg += f"*#️⃣ Hashtags:*\n{hashtags}"
    return msg.strip()


def format_youtube_long(info: dict, clean_url: str) -> str:
    title = info.get("title", "Sin título")
    description = info.get("description", "") or ""
    if len(description) > 800:
        description = description[:800] + "..."
    msg = f"▶️ *YouTube*\n\n📌 *Título:* {title}\n\n🔗 [Ver video]({clean_url})\n\n"
    if description:
        msg += f"📄 *Descripción:*\n{description}"
    return msg.strip()


def format_reddit(info: dict, clean_url: str) -> str:
    title = info.get("title", "Sin título")
    post_url = info.get("webpage_url", clean_url)
    ext = info.get("ext", "")
    if ext in ("gif", "gifv"):
        tipo = "🎞️ GIF"
    elif ext in ("jpg", "jpeg", "png", "webp"):
        tipo = "🖼️ Foto"
    else:
        tipo = "🎬 Video"
    msg = f"👽 *Reddit*\n\n{tipo}\n\n📌 *Título:* {title}\n\n🔗 [Ver post]({post_url})"
    return msg.strip()


def format_twitter(info: dict, clean_url: str) -> str:
    caption = info.get("description", "") or info.get("title", "") or ""
    hashtags = clean_hashtags(caption)
    body = caption.replace(hashtags, "").strip()
    msg = f"🐦 *Twitter / X*\n\n🔗 [Ver tweet]({clean_url})\n\n"
    if body:
        msg += f"📝 *Tweet:*\n{body}\n\n"
    if hashtags:
        msg += f"*#️⃣ Hashtags:*\n{hashtags}"
    return msg.strip()


def format_threads(info: dict, clean_url: str) -> str:
    caption = info.get("description", "") or ""
    hashtags = clean_hashtags(caption)
    body = caption.replace(hashtags, "").strip()
    msg = f"🧵 *Threads*\n\n🔗 [Ver post]({clean_url})\n\n"
    if body:
        msg += f"📝 *Caption:*\n{body}\n\n"
    if hashtags:
        msg += f"*#️⃣ Hashtags:*\n{hashtags}"
    return msg.strip()


def format_redgifs(info: dict, clean_url: str) -> str:
    title    = info.get("title", "") or info.get("fulltitle", "") or ""
    desc     = info.get("description", "") or ""
    tags_raw = info.get("tags", [])
    hashtags = " ".join(f"#{t}" for t in tags_raw) if tags_raw else clean_hashtags(desc)
    msg = f"🎞️ *Redgifs*\n\n🔗 [Ver GIF]({clean_url})\n\n"
    if title:
        msg += f"📌 *Título:* {title}\n\n"
    if hashtags:
        msg += f"*#️⃣ Hashtags:*\n{hashtags}"
    return msg.strip()


def format_redgif_in_reddit(info: dict, redgif_url: str) -> str:
    reddit_title = info.get("reddit_title", info.get("title", ""))
    post_url     = info.get("reddit_post_url", info.get("webpage_url", ""))
    desc         = info.get("description", "") or ""
    tags_raw     = info.get("tags", [])
    hashtags     = " ".join(f"#{t}" for t in tags_raw) if tags_raw else clean_hashtags(desc)
    msg = f"👽 *Reddit*  ·  🎞️ GIF\n\n"
    if reddit_title:
        msg += f"📌 *Título:* {reddit_title}\n\n"
    msg += f"🔗 [Ver post]({post_url})\n"
    msg += f"🎬 [Ver en Redgifs]({redgif_url})\n"
    if hashtags:
        msg += f"\n*#️⃣ Hashtags:*\n{hashtags}"
    return msg.strip()


def format_message(platform: str, info: dict, clean_url: str) -> str:
    formatters = {
        "instagram":        format_instagram,
        "tiktok":           format_tiktok,
        "facebook":         format_facebook,
        "youtube_short":    format_youtube_short,
        "youtube_long":     format_youtube_long,
        "reddit":           format_reddit,
        "redgifs":          format_redgifs,
        "redgif_in_reddit": format_redgif_in_reddit,
        "twitter":          format_twitter,
        "threads":          format_threads,
    }
    fn = formatters.get(platform)
    if fn:
        return fn(info, clean_url)
    return f"✅ Contenido descargado\n🔗 {clean_url}"

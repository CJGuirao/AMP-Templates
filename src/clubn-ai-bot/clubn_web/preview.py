"""Dry run: read profiles and print the article Claude would write. Touches neither Discord nor WordPress.

    docker compose run --rm creator-bot python -m bot.preview "Nombre" https://www.twitch.tv/xxx [more links...]
"""
import asyncio
import json
import sys

from . import social, writer


async def main() -> None:
    name, *urls = sys.argv[1:]
    links = social.extract_links("\n".join(urls))
    profiles, links = await social.read_profiles(links)
    print("Links:", json.dumps(links, indent=2, ensure_ascii=False))
    print("Profiles:", json.dumps(profiles, indent=2, ensure_ascii=False)[:4000])
    art = await writer.write_article(name, profiles, None, [])
    print("\n#", art.title, "\n\n", art.excerpt, "\n\n", art.content_html, "\n\nTags:", art.tags)


if __name__ == "__main__":
    asyncio.run(main())

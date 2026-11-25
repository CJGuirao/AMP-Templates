"""Every setting of the Club Ñ web features, as shown in AMP's configuration panel.

This list is the single source for three generated files (run tools/gen_amp_settings.py after editing):
the AMP settings (clubn-ai-botconfig.json), their environment mapping (clubn-ai-bot.kvp) and
clubn-ai-bot/defaults/env.example. A test checks the three are in sync with this list.

Fields: (VARIABLE, AMP field name, display name, input type, default, subcategory, description).
Secrets use the "password" input type. Defaults match clubn_web/config.py.
"""
from __future__ import annotations

CATEGORY = "Club Ñ Web:language"

DISCORD = "Discord and creators:group:1"
EVENTS = "Events calendar:event:2"
MOMENTAZOS = "Momentazos page:movie:3"
LIVE = "Ahora en directo:live_tv:4"
MIS_REDES = "/mis_redes:share:5"
WORDPRESS = "WordPress:public:6"
AI = "AI models:psychology:7"
APIS = "Social APIs:key:8"
SOCIAL = "Social posting:campaign:9"

SETTINGS: list[tuple[str, str, str, str, str, str, str]] = [
    # ----- Discord and creators -----
    ("CREATOR_ROLE_NAME", "WebCreatorRoleName", "Creator Role Name", "text", "creador de contenido", DISCORD,
     "Name of the content-creator role. Ignored when Creator Role ID is set."),
    ("CREATOR_ROLE_ID", "WebCreatorRoleId", "Creator Role ID", "text", "805106840266080256", DISCORD,
     "Role that triggers a creator article and may use /mis_redes."),
    ("REVIEW_CHANNEL_ID", "WebReviewChannelId", "Mod Review Channel ID", "text", "1216376131360657528", DISCORD,
     "Channel where moderators approve (✅), change or dismiss every draft."),
    ("MOD_ROLE_IDS", "WebModRoleIds", "Moderator Role IDs", "text", "", DISCORD,
     "Comma-separated roles allowed to review. Blank = anyone with Manage Messages."),
    ("ANNOUNCE_CHANNEL_IDS", "WebAnnounceChannelIds", "Live Announcement Channel IDs", "text", "810238345044164719",
     DISCORD, "Comma-separated channels with the 'X está en directo' announcements, mined for social links."),
    ("ANNOUNCE_HISTORY_LIMIT", "WebAnnounceHistoryLimit", "Announcement History Limit", "number", "3000", DISCORD,
     "How many announcement messages to search per creator."),
    ("BACKFILL_EXISTING", "WebBackfillExisting", "Write Articles For Existing Creators", "checkbox", "true", DISCORD,
     "Also write articles for people who already had the creator role."),
    ("BACKFILL_PER_SWEEP", "WebBackfillPerSweep", "Existing Creators Per Day", "number", "3", DISCORD,
     "At most this many existing creators per daily sweep, so the mod channel isn't flooded."),
    ("DAILY_SWEEP_HOUR", "WebDailySweepHour", "Daily Sweep Hour", "number", "9", DISCORD,
     "Hour (in the Timezone setting) of the daily creator sweep."),
    # ----- Events -----
    ("EVENTS_ENABLED", "WebEventsEnabled", "Events Workflow", "checkbox", "true", EVENTS,
     "Follow Club Ñ events and propose calendar entries and articles (The Events Calendar plugin needed)."),
    ("EVENT_CHANNEL_IDS", "WebEventChannelIds", "Event Channel IDs", "text", "", EVENTS,
     "Comma-separated channels/forums to watch. Blank = anuncios, eventos, org_eventos, servidores-gratuitos-del-club."),
    ("EVENT_REVIEW_CHANNEL_ID", "WebEventReviewChannelId", "Event Review Channel ID", "text", "", EVENTS,
     "Mod channel for event cards. Blank = the Mod Review Channel."),
    ("EVENT_TRACK_DAYS_AFTER", "WebEventTrackDaysAfter", "Track Days After Event", "number", "7", EVENTS,
     "Keep proposing updates this many days after an event ends."),
    ("EVENT_DEFAULT_HOURS", "WebEventDefaultHours", "Default Event Length (hours)", "number", "2", EVENTS,
     "Event length when the announcement gives no end time."),
    ("WP_EVENTS_CATEGORY", "WebEventsCategory", "Events Category", "text", "Eventos", EVENTS,
     "WordPress category for event articles."),
    ("CALENDAR_URL", "WebCalendarUrl", "Calendar URL", "text", "/calendario/", EVENTS,
     "Path of the calendar page on the site."),
    # ----- Momentazos -----
    ("MOMENTAZOS_ENABLED", "WebMomentazosEnabled", "Momentazos Page Updates", "checkbox", "true", MOMENTAZOS,
     "Add new clip compilations from YouTube to the Momentazos page (no AI)."),
    ("MOMENTAZOS_PAGE_ID", "WebMomentazosPageId", "Momentazos Page ID", "text", "", MOMENTAZOS,
     "WordPress page id of /momentazos/ on the site in WordPress URL (staging: 6860). Blank = off."),
    ("MOMENTAZOS_CHANNEL_ID", "WebMomentazosChannelId", "YouTube Channel ID", "text", "UCXW5p9ZwR1kVsN2sEHNHylg",
     MOMENTAZOS, "Club Ñ YouTube channel (public RSS feed)."),
    ("MOMENTAZOS_PLAYLIST_ID", "WebMomentazosPlaylistId", "Momentazos Playlist ID", "text",
     "PLYEqrW81W849ASmCYn_9uYClE_jn3D3J8", MOMENTAZOS, "Momentazos playlist (full list via the API if a YouTube API key is set)."),
    ("MOMENTAZOS_INTERVAL_HOURS", "WebMomentazosIntervalHours", "Check Interval (hours)", "number", "6", MOMENTAZOS,
     "How often to look for new compilations."),
    ("MOMENTAZOS_TITLE_REGEX", "WebMomentazosTitleRegex", "Title Pattern", "text", "", MOMENTAZOS,
     "Regex on the accent-less lower-case title. Blank = momentazo|momentos de (streamers|creadores)|anuario momentazo."),
    ("MOMENTAZOS_ALLOW_DRAFT", "WebMomentazosAllowDraft", "Allow Draft Page", "checkbox", "false", MOMENTAZOS,
     "Only for testing: also update the page while it is a draft."),
    ("MOMENTAZOS_NOTIFY_CHANNEL_ID", "WebMomentazosNotifyChannelId", "Notify Channel ID", "text", "", MOMENTAZOS,
     "Discord channel for a short 'added' note. Blank = no note."),
    # ----- Ahora en directo -----
    ("LIVE_ENABLED", "WebLiveEnabled", "Ahora En Directo", "checkbox", "false", LIVE,
     "Show which creators are live on Twitch/YouTube on the Ahora en directo page (no AI)."),
    ("LIVE_DB_CHANNEL_ID", "WebLiveDbChannelId", "Creator Database Thread ID", "text", "1574896427719725137", LIVE,
     "Private Discord thread holding the creator database (one bot message per creator)."),
    ("LIVE_DB_RELOAD_MIN", "WebLiveDbReloadMin", "Database Reload (minutes)", "number", "30", LIVE,
     "Re-read the creator database this often."),
    ("LIVE_PAGE_ID", "WebLivePageId", "Ahora En Directo Page ID", "text", "", LIVE,
     "WordPress page id of /ahora-en-directo/ (staging: 7568). Blank = off."),
    ("LIVE_INTERVAL_MIN", "WebLiveIntervalMin", "Twitch Check (minutes)", "number", "2", LIVE,
     "Twitch check and page update interval."),
    ("LIVE_YT_INTERVAL_MIN", "WebLiveYtIntervalMin", "YouTube Check (minutes)", "number", "6", LIVE,
     "YouTube check interval."),
    ("LIVE_YT_FEED_ITEMS", "WebLiveYtFeedItems", "YouTube Uploads Checked", "number", "4", LIVE,
     "Newest uploads per channel to look at."),
    # ----- /mis_redes -----
    ("MIS_REDES_ENABLED", "WebMisRedesEnabled", "/mis_redes Command", "checkbox", "false", MIS_REDES,
     "Let creators propose changes to their own socials; a moderator approves each change (no AI)."),
    ("MIS_REDES_REVIEW_CHANNEL_ID", "WebMisRedesReviewChannelId", "Review Channel ID", "text", "", MIS_REDES,
     "Blank = the Mod Review Channel."),
    ("MIS_REDES_COOLDOWN_MIN", "WebMisRedesCooldownMin", "Cooldown (minutes)", "number", "10", MIS_REDES,
     "Minimum gap between two requests of one creator."),
    ("MIS_REDES_MAX_PER_DAY", "WebMisRedesMaxPerDay", "Requests Per Day", "number", "3", MIS_REDES,
     "Requests per creator in 24 hours."),
    ("MIS_REDES_MAX_PENDING", "WebMisRedesMaxPending", "Open Requests Limit", "number", "25", MIS_REDES,
     "Open requests across everyone (protects the mod channel)."),
    ("MIS_REDES_EXPIRE_DAYS", "WebMisRedesExpireDays", "Expire After (days)", "number", "14", MIS_REDES,
     "Older open requests can no longer be approved."),
    ("MIS_REDES_SELF_APPROVE", "WebMisRedesSelfApprove", "Moderators Approve Own Requests", "checkbox", "false",
     MIS_REDES, "May a moderator approve their own request?"),
    ("MIS_REDES_DM", "WebMisRedesDm", "DM The Outcome", "checkbox", "true", MIS_REDES,
     "Tell the creator the outcome by direct message."),
    # ----- WordPress -----
    ("WP_URL", "WebWpUrl", "WordPress URL", "text", "https://wp.clubn.online", WORDPRESS,
     "Site the bot writes to. Falls back to the fallback URL if unreachable."),
    ("WP_FALLBACK_URL", "WebWpFallbackUrl", "WordPress Fallback URL", "text", "https://clubn.online", WORDPRESS,
     "Used when the main URL doesn't answer."),
    ("WP_USER", "WebWpUser", "WordPress User", "text", "", WORDPRESS,
     "User of the application password (an Editor or the bot's own user). Required."),
    ("WP_APP_PASSWORD", "WebWpAppPassword", "WordPress Application Password", "password", "", WORDPRESS,
     "Users > Profile > Application Passwords. Required. Revoke it in WordPress if it is ever exposed."),
    ("WP_CATEGORY_NAME", "WebWpCategoryName", "Creators Category", "text", "Creadores_de_contenido", WORDPRESS,
     "WordPress category for creator articles."),
    ("WP_FEATURED_TAG", "WebWpFeaturedTag", "Featured Tag", "text", "destacado", WORDPRESS,
     "Tag added on approval, together with sticky."),
    ("WP_FRONT_BLOCK_TITLE", "WebWpFrontBlockTitle", "Front-Page Pattern Title", "text", "Nuevo creador en Club Ñ",
     WORDPRESS, "Synced pattern rewritten to point at the newest creator."),
    # ----- AI models (local model = Fulgencio's Model setting) -----
    ("ANTHROPIC_API_KEY", "WebAnthropicApiKey", "Claude API Key", "password", "", AI,
     "First choice for drafts. Blank or out of credits = DeepSeek, then the local model."),
    ("CLAUDE_MODEL", "WebClaudeModel", "Claude Model", "text", "claude-opus-5-5", AI, "Claude model id."),
    ("CLAUDE_EFFORT", "WebClaudeEffort", "Claude Effort", "text", "high", AI, "low, medium, high or max."),
    ("DEEPSEEK_API_KEY", "WebDeepseekApiKey", "DeepSeek API Key", "password", "", AI,
     "Second choice for drafts. Blank or out of credits = the local model."),
    ("DEEPSEEK_MODEL", "WebDeepseekModel", "DeepSeek Model", "text", "deepseek-chat", AI, "DeepSeek model id."),
    ("DEEPSEEK_BASE_URL", "WebDeepseekBaseUrl", "DeepSeek API URL", "text", "https://api.deepseek.com", AI,
     "OpenAI-compatible endpoint."),
    # ----- Social APIs -----
    ("YOUTUBE_API_KEY", "WebYoutubeApiKey", "YouTube API Key", "password", "", APIS,
     "Optional: richer creator data, live checks and the full Momentazos playlist."),
    ("TWITCH_CLIENT_ID", "WebTwitchClientId", "Twitch Client ID", "text", "", APIS,
     "Twitch app client id (needed for Ahora en directo and Twitch login checks)."),
    ("TWITCH_CLIENT_SECRET", "WebTwitchClientSecret", "Twitch Client Secret", "password", "", APIS,
     "Twitch app client secret."),
    # ----- Social posting (clubn_bot/social_posts.py) -----
    ("SOCIAL_ENABLED", "WebSocialEnabled", "Social Posting", "checkbox", "false", SOCIAL,
     "Draft posts for the Club's social networks from new web articles; moderators review, edit and approve each one."),
    ("SOCIAL_DRY_RUN", "WebSocialDryRun", "Test Mode (Nothing Is Posted)", "checkbox", "true", SOCIAL,
     "While on, the whole review flow works but ✅ only reports what would be posted. Untick to post for real."),
    ("SOCIAL_REVIEW_CHANNEL_ID", "WebSocialReviewChannelId", "Review Channel ID", "text", "", SOCIAL,
     "Blank = the Mod Review Channel."),
    ("SOCIAL_COMMAND_ROLE_IDS", "WebSocialCommandRoleIds", "Who May Use /anuncia_en_redes", "text", "", SOCIAL,
     "Comma-separated role ids allowed besides moderators (e.g. event organisers). Every ad still needs a moderator's ✅."),
    ("SOCIAL_DEFAULT_LINK", "WebSocialDefaultLink", "Default Link", "text", "https://clubn.online", SOCIAL,
     "Link added to ads made from Discord messages (e.g. the Club's website or a Discord invite). Write - for no link."),
    ("SOCIAL_NETWORKS", "WebSocialNetworks", "Networks", "text", "x,bluesky,threads,facebook,instagram,tiktok", SOCIAL,
     "Comma-separated: x, bluesky, threads, facebook, instagram, tiktok. A network with no connection gets a text to post by hand."),
    ("SOCIAL_WATCH_WEB", "WebSocialWatchWeb", "Also Announce New Web Articles", "checkbox", "false", SOCIAL,
     "Besides /anuncia_en_redes, draft an ad by itself for every new public article in the categories below."),
    ("SOCIAL_WP_CATEGORIES", "WebSocialWpCategories", "Website Categories", "text", "Creadores_de_contenido,Eventos",
     SOCIAL, "New public posts in these WordPress categories get a social draft. * = every category."),
    ("SOCIAL_POLL_MIN", "WebSocialPollMin", "Website Check (minutes)", "number", "10", SOCIAL,
     "How often to look for newly published articles."),
    ("SOCIAL_MAX_AGE_HOURS", "WebSocialMaxAgeHours", "Ignore Articles Older Than (hours)", "number", "48", SOCIAL,
     "Articles published longer ago than this never get an automatic draft."),
    ("SOCIAL_HASHTAGS", "WebSocialHashtags", "Club Hashtags", "text", "ClubÑ", SOCIAL,
     "Hashtags every post should include, comma-separated, without the #."),
    ("SOCIAL_STYLE", "WebSocialStyle", "Style Notes", "text", "", SOCIAL,
     "Optional extra instructions for the writer (tone, emojis, words to avoid)."),
    ("POSTIZ_URL", "WebPostizUrl", "Postiz API URL", "text", "https://api.postiz.com/public/v1", SOCIAL,
     "Postiz cloud, or https://<your-postiz>/api/public/v1 when self-hosted."),
    ("POSTIZ_API_KEY", "WebPostizApiKey", "Postiz API Key", "password", "", SOCIAL,
     "Postiz > Settings > Public API. Every network connected in Postiz is used unless it has its own keys below."),
    ("SOCIAL_TIKTOK_METHOD", "WebSocialTiktokMethod", "TikTok Mode", "text", "UPLOAD", SOCIAL,
     "UPLOAD = goes to the TikTok app's inbox to finish by hand (works without TikTok's audit). DIRECT_POST = public post (needs an audited app)."),
    ("BLUESKY_HANDLE", "WebBlueskyHandle", "Bluesky Handle", "text", "", SOCIAL,
     "e.g. clubn.bsky.social. With the app password, Bluesky is posted directly (free)."),
    ("BLUESKY_APP_PASSWORD", "WebBlueskyAppPassword", "Bluesky App Password", "password", "", SOCIAL,
     "Bluesky > Settings > Privacy and security > App passwords. Not the account password."),
    ("BLUESKY_PDS", "WebBlueskyPds", "Bluesky Server", "text", "https://bsky.social", SOCIAL,
     "Only change it for an account on another PDS."),
    ("META_GRAPH_VERSION", "WebMetaGraphVersion", "Meta Graph API Version", "text", "v24.0", SOCIAL,
     "Facebook/Instagram Graph API version."),
    ("FACEBOOK_PAGE_ID", "WebFacebookPageId", "Facebook Page ID", "text", "", SOCIAL,
     "With the Meta Page token, the Facebook Page is posted directly."),
    ("INSTAGRAM_USER_ID", "WebInstagramUserId", "Instagram Account ID", "text", "", SOCIAL,
     "Instagram professional account id linked to the Page. With the Meta Page token, Instagram is posted directly."),
    ("META_PAGE_TOKEN", "WebMetaPageToken", "Meta Page Token", "password", "", SOCIAL,
     "Long-lived Page access token (pages_manage_posts, instagram_content_publish)."),
    ("THREADS_USER_ID", "WebThreadsUserId", "Threads User ID", "text", "me", SOCIAL,
     "me = the token's own account."),
    ("THREADS_TOKEN", "WebThreadsToken", "Threads Token", "password", "", SOCIAL,
     "Long-lived Threads token (threads_basic, threads_content_publish). The bot renews it weekly."),
]

VARIABLES = [s[0] for s in SETTINGS]
SECRETS = [s[0] for s in SETTINGS if s[3] == "password"]


def amp_entries() -> list[dict]:
    out = []
    for var, field, display, kind, default, sub, desc in SETTINGS:
        e = {
            "DisplayName": display,
            "Description": desc + ("" if kind != "password" else " Stored only in AMP; never in the repository."),
            "Keywords": "clubn,web," + var.lower().replace("_", ","),
            "FieldName": field,
            "InputType": kind,
            "Category": CATEGORY,
            "Subcategory": sub,
            "ParamFieldName": field,
            "IncludeInCommandLine": False,
            "Required": False,
            "DefaultValue": default,
            "EnumValues": {"True": "true", "False": "false"} if kind == "checkbox" else {},
        }
        out.append(e)
    return out


def kvp_env() -> dict[str, str]:
    return {var: "{{" + field + "}}" for var, field, *_ in SETTINGS}


def env_example() -> str:
    lines = [
        "# Club Ñ web features: optional fallback for AMP's configuration panel.",
        "# Everything here can (and normally should) be set in AMP > Configuration > Club Ñ Web.",
        "# A value filled in AMP wins; a blank AMP field is taken from this file.",
        "# Copy to .env in the instance folder (/AMP/clubn-ai-bot/.env). Never commit the real .env.",
        "",
        "# DISCORD_TOKEN=            # only if 'Discord Bot Token' is blank in AMP",
        "# CLUBN_WEB_ENABLED=true    # same as the 'Club Ñ Web Features' checkbox",
    ]
    current = None
    for var, _field, display, kind, default, sub, desc in SETTINGS:
        if sub != current:
            current = sub
            lines += ["", f"# ---------- {sub.split(':')[0]} ----------"]
        value = "" if kind == "password" else default
        lines.append(f"{var}={value}".ljust(44) + f" # {display}")
    return "\n".join(lines) + "\n"

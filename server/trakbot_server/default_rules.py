"""Starter rule set. Admins edit these from the dashboard / API."""

P, U, N = "productive", "unproductive", "neutral"

# (kind, pattern, category, label, alert)
DEFAULT_RULES = [
    # --- apps ---
    ("app", "code*", "Development", P, 0), ("app", "*pycharm*", "Development", P, 0),
    ("app", "*idea*", "Development", P, 0), ("app", "*terminal*", "Development", P, 0),
    ("app", "*iterm*", "Development", P, 0), ("app", "windowsterminal*", "Development", P, 0),
    ("app", "*excel*", "Office", P, 0), ("app", "*winword*", "Office", P, 0),
    ("app", "*powerpnt*", "Office", P, 0), ("app", "*libreoffice*", "Office", P, 0),
    ("app", "*outlook*", "Email", P, 0), ("app", "*thunderbird*", "Email", P, 0),
    ("app", "slack*", "Communication", P, 0), ("app", "*teams*", "Communication", P, 0),
    ("app", "zoom*", "Communication", P, 0),
    ("app", "*spotify*", "Entertainment", U, 0), ("app", "*steam*", "Gaming", U, 0),
    ("app", "*vlc*", "Entertainment", U, 0), ("app", "*discord*", "Social", U, 0),
    ("app", "*telegram*", "Social", U, 0), ("app", "*whatsapp*", "Social", U, 0),
    # --- domains ---
    ("domain", "github.com", "Development", P, 0), ("domain", "gitlab.com", "Development", P, 0),
    ("domain", "stackoverflow.com", "Development", P, 0),
    ("domain", "docs.google.com", "Office", P, 0), ("domain", "drive.google.com", "Office", P, 0),
    ("domain", "mail.google.com", "Email", P, 0), ("domain", "outlook.office.com", "Email", P, 0),
    ("domain", "atlassian.net", "Project Management", P, 0), ("domain", "notion.so", "Project Management", P, 0),
    ("domain", "linkedin.com", "Business Networking", N, 0),
    ("domain", "youtube.com", "Video", U, 0), ("domain", "netflix.com", "Entertainment", U, 0),
    ("domain", "twitch.tv", "Entertainment", U, 0),
    ("domain", "facebook.com", "Social Media", U, 0), ("domain", "instagram.com", "Social Media", U, 0),
    ("domain", "x.com", "Social Media", U, 0), ("domain", "twitter.com", "Social Media", U, 0),
    ("domain", "reddit.com", "Social Media", U, 0), ("domain", "tiktok.com", "Social Media", U, 0),
    ("domain", "amazon.com", "Shopping", U, 0), ("domain", "ebay.com", "Shopping", U, 0),
    # --- compliance flags: alert on use (illegal / policy-violating content) ---
    ("domain", "thepiratebay.org", "Piracy", U, 1), ("domain", "1337x.to", "Piracy", U, 1),
    ("app", "*qbittorrent*", "Piracy", U, 1), ("app", "*utorrent*", "Piracy", U, 1),
    ("domain", "draftkings.com", "Gambling", U, 1), ("domain", "bet365.com", "Gambling", U, 1),
    # The agent can't see the URL inside a private/incognito window (by browser design),
    # but it can see the window title says so — flag it and rely on screenshots for evidence.
    ("title", r"\(incognito\)|\(inprivate\)|private browsing|private window|private/incognito", "Private/Incognito Browsing", U, 1),
]

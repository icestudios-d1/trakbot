"""Starter rule set. Admins edit these from the dashboard / API."""

P, U, N = "productive", "unproductive", "neutral"

# (kind, pattern, category, label, alert)
#
# ICE-style design studio set: creative tools are the core of "productive" work, inspiration/reference
# sites are neutral (designers legitimately live on them), music/video are neutral by default.
# Apps match case-insensitively with * wildcards against the process name (Photoshop.exe,
# "Adobe Photoshop 2025", ...), so "*photoshop*" covers every platform and version.
STUDIO_RULES = [
    # --- design & creative apps ---
    ("app", "*photoshop*", "Design - Photoshop", P, 0), ("app", "*illustrator*", "Design - Illustrator", P, 0),
    ("app", "*indesign*", "Design - InDesign", P, 0), ("app", "*lightroom*", "Photo Editing", P, 0),
    ("app", "*afterfx*", "Motion - After Effects", P, 0), ("app", "*after effects*", "Motion - After Effects", P, 0),
    ("app", "*premiere*", "Video Editing", P, 0), ("app", "*media encoder*", "Video Editing", P, 0),
    ("app", "*audition*", "Audio", P, 0), ("app", "animate*", "Animation", P, 0),
    ("app", "xd.exe", "UI/UX Design", P, 0), ("app", "adobe xd*", "UI/UX Design", P, 0),
    ("app", "*figma*", "UI/UX Design", P, 0), ("app", "sketch", "UI/UX Design", P, 0),
    ("app", "*affinity*", "Design - Affinity", P, 0), ("app", "*coreldraw*", "Design - Corel", P, 0),
    ("app", "*acrobat*", "Documents & PDF", P, 0), ("app", "*acrord*", "Documents & PDF", P, 0),
    ("app", "*adobe bridge*", "Asset Management", P, 0), ("app", "bridge.exe", "Asset Management", P, 0), ("app", "*creative cloud*", "Asset Management", N, 0),
    ("app", "*blender*", "3D", P, 0), ("app", "*cinema 4d*", "3D", P, 0), ("app", "*c4d*", "3D", P, 0),
    ("app", "*maya*", "3D", P, 0), ("app", "*3dsmax*", "3D", P, 0), ("app", "*zbrush*", "3D", P, 0),
    ("app", "*substance*", "3D", P, 0), ("app", "*keyshot*", "3D", P, 0), ("app", "*sketchup*", "3D", P, 0),
    ("app", "*rhino*", "3D", P, 0), ("app", "*unrealeditor*", "3D", P, 0), ("app", "unity*", "3D", P, 0),
    ("app", "*autocad*", "CAD", P, 0), ("app", "acad.exe", "CAD", P, 0),
    ("app", "resolve*", "Video Editing", P, 0), ("app", "*davinci*", "Video Editing", P, 0), ("app", "*finalcut*", "Video Editing", P, 0),
    ("app", "*procreate*", "Illustration", P, 0), ("app", "*clip studio*", "Illustration", P, 0),
    ("app", "*wacom*", "Design - Tools", N, 0), ("app", "*fontbase*", "Asset Management", P, 0),
    ("app", "*keynote*", "Presentations", P, 0), ("app", "pages", "Office", P, 0), ("app", "numbers", "Office", P, 0),
    # --- design, asset and review sites ---
    ("domain", "figma.com", "UI/UX Design", P, 0), ("domain", "canva.com", "Design - Canva", P, 0),
    ("domain", "adobe.com", "Design - Adobe Web", P, 0), ("domain", "frame.io", "Review & Approval", P, 0),
    ("domain", "miro.com", "Whiteboarding", P, 0), ("domain", "fonts.google.com", "Fonts & Type", P, 0),
    ("domain", "myfonts.com", "Fonts & Type", P, 0), ("domain", "fontshare.com", "Fonts & Type", P, 0),
    ("domain", "coolors.co", "Colour", P, 0), ("domain", "color.adobe.com", "Colour", P, 0),
    ("domain", "freepik.com", "Stock & Assets", P, 0), ("domain", "shutterstock.com", "Stock & Assets", P, 0),
    ("domain", "gettyimages.com", "Stock & Assets", P, 0), ("domain", "unsplash.com", "Stock & Assets", P, 0),
    ("domain", "pexels.com", "Stock & Assets", P, 0), ("domain", "envato.com", "Stock & Assets", P, 0),
    ("domain", "iconfinder.com", "Stock & Assets", P, 0), ("domain", "flaticon.com", "Stock & Assets", P, 0),
    ("domain", "midjourney.com", "AI Design Tools", P, 0), ("domain", "firefly.adobe.com", "AI Design Tools", P, 0),
    ("domain", "behance.net", "Inspiration", N, 0), ("domain", "dribbble.com", "Inspiration", N, 0),
    ("domain", "pinterest.com", "Inspiration", N, 0), ("domain", "awwwards.com", "Inspiration", N, 0),
    ("domain", "vimeo.com", "Video Review", N, 0), ("domain", "are.na", "Inspiration", N, 0),
    ("domain", "drive.google.com", "Office", P, 0), ("domain", "dropbox.com", "File Sharing", P, 0),
    ("domain", "wetransfer.com", "File Sharing", P, 0), ("domain", "slack.com", "Communication", P, 0),
    # --- media: neutral by default. Passive background music is analysed on the "Active vs background" page ---
    ("app", "*spotify*", "Music", N, 0), ("domain", "spotify.com", "Music", N, 0),
    ("domain", "music.youtube.com", "Music", N, 0), ("domain", "soundcloud.com", "Music", N, 0),
    ("domain", "youtube.com", "Video & Tutorials", N, 0),
]

DEFAULT_RULES = STUDIO_RULES + [
    # --- apps ---
    ("app", "code*", "Development", P, 0), ("app", "*pycharm*", "Development", P, 0),
    ("app", "*idea*", "Development", P, 0), ("app", "*terminal*", "Development", P, 0),
    ("app", "*iterm*", "Development", P, 0), ("app", "windowsterminal*", "Development", P, 0),
    ("app", "*excel*", "Office", P, 0), ("app", "*winword*", "Office", P, 0),
    ("app", "*powerpnt*", "Office", P, 0), ("app", "*libreoffice*", "Office", P, 0),
    ("app", "*outlook*", "Email", P, 0), ("app", "*thunderbird*", "Email", P, 0),
    ("app", "slack*", "Communication", P, 0), ("app", "*teams*", "Communication", P, 0),
    ("app", "zoom*", "Communication", P, 0),
    ("app", "*steam*", "Gaming", U, 0),
    ("app", "*vlc*", "Entertainment", U, 0), ("app", "*discord*", "Social", U, 0),
    ("app", "*telegram*", "Social", U, 0), ("app", "*whatsapp*", "Social", U, 0),
    # --- domains ---
    ("domain", "github.com", "Development", P, 0), ("domain", "gitlab.com", "Development", P, 0),
    ("domain", "stackoverflow.com", "Development", P, 0),
    ("domain", "docs.google.com", "Office", P, 0), ("domain", "drive.google.com", "Office", P, 0),
    ("domain", "mail.google.com", "Email", P, 0), ("domain", "outlook.office.com", "Email", P, 0),
    ("domain", "atlassian.net", "Project Management", P, 0), ("domain", "notion.so", "Project Management", P, 0),
    ("domain", "linkedin.com", "Business Networking", N, 0),
    ("domain", "netflix.com", "Entertainment", U, 0),
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

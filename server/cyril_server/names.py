"""Friendly names for reports: "YouTube", "Claude", "PowerShell", "Photoshop" instead of
"youtube.com", "claude.ai", "powershell.exe", "Photoshop.exe", and browser time shown under the
*website* rather than the browser. Rules still match on the raw app/domain; this is display only."""
import re

BROWSERS = ("msedge", "edge", "chrome", "chromium", "firefox", "brave", "opera", "vivaldi", "safari", "arc")

# Domains whose brand name is not just the capitalised domain label. The most specific match wins
# (mail.google.com -> Gmail before google.com -> Google).
SITES = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "music.youtube.com": "YouTube Music",
    "claude.ai": "Claude", "anthropic.com": "Anthropic", "chatgpt.com": "ChatGPT", "openai.com": "OpenAI",
    "github.com": "GitHub", "gitlab.com": "GitLab", "stackoverflow.com": "Stack Overflow",
    "linkedin.com": "LinkedIn", "x.com": "X", "twitter.com": "X", "facebook.com": "Facebook",
    "instagram.com": "Instagram", "whatsapp.com": "WhatsApp", "web.whatsapp.com": "WhatsApp", "reddit.com": "Reddit",
    "google.com": "Google", "mail.google.com": "Gmail", "docs.google.com": "Google Docs",
    "sheets.google.com": "Google Sheets", "slides.google.com": "Google Slides", "drive.google.com": "Google Drive",
    "calendar.google.com": "Google Calendar", "meet.google.com": "Google Meet", "fonts.google.com": "Google Fonts",
    "outlook.office.com": "Outlook", "outlook.live.com": "Outlook", "teams.microsoft.com": "Teams",
    "office.com": "Microsoft 365", "sharepoint.com": "SharePoint", "onedrive.live.com": "OneDrive",
    "notion.so": "Notion", "slack.com": "Slack", "miro.com": "Miro", "figma.com": "Figma", "canva.com": "Canva",
    "adobe.com": "Adobe", "firefly.adobe.com": "Adobe Firefly", "color.adobe.com": "Adobe Color",
    "freepik.com": "Freepik", "behance.net": "Behance", "dribbble.com": "Dribbble", "pinterest.com": "Pinterest",
    "unsplash.com": "Unsplash", "pexels.com": "Pexels", "shutterstock.com": "Shutterstock", "envato.com": "Envato",
    "midjourney.com": "Midjourney", "frame.io": "Frame.io", "vimeo.com": "Vimeo", "awwwards.com": "Awwwards",
    "are.na": "Are.na", "dropbox.com": "Dropbox", "wetransfer.com": "WeTransfer", "zoom.us": "Zoom",
    "netflix.com": "Netflix", "spotify.com": "Spotify", "open.spotify.com": "Spotify", "soundcloud.com": "SoundCloud",
    "amazon.com": "Amazon", "amazon.in": "Amazon", "flipkart.com": "Flipkart", "wikipedia.org": "Wikipedia",
    "icestudios.in": "ICE Studios", "atlassian.net": "Jira / Confluence", "tiktok.com": "TikTok",
    "twitch.tv": "Twitch", "ebay.com": "eBay", "iconfinder.com": "Iconfinder", "flaticon.com": "Flaticon",
    "coolors.co": "Coolors", "myfonts.com": "MyFonts", "fontshare.com": "Fontshare", "draftkings.com": "DraftKings",
}

# Process / window-class names (lower case, no ".exe", no version) -> name people recognise.
APPS = {
    "photoshop": "Photoshop", "illustrator": "Illustrator", "indesign": "InDesign", "lightroom": "Lightroom",
    "lightroom classic": "Lightroom Classic", "afterfx": "After Effects", "after effects": "After Effects",
    "premiere pro": "Premiere Pro", "premiere": "Premiere Pro", "media encoder": "Media Encoder",
    "audition": "Audition", "animate": "Animate", "acrobat": "Acrobat", "acrord32": "Acrobat Reader",
    "acrobat reader": "Acrobat Reader", "xd": "XD", "bridge": "Bridge", "creative cloud": "Creative Cloud",
    "figma": "Figma", "sketch": "Sketch", "blender": "Blender", "cinema 4d": "Cinema 4D", "c4d": "Cinema 4D",
    "maya": "Maya", "3dsmax": "3ds Max", "zbrush": "ZBrush", "keyshot": "KeyShot", "sketchup": "SketchUp",
    "davinci resolve": "DaVinci Resolve", "resolve": "DaVinci Resolve", "autocad": "AutoCAD", "acad": "AutoCAD",
    "code": "VS Code", "devenv": "Visual Studio", "pycharm64": "PyCharm", "pycharm": "PyCharm",
    "idea64": "IntelliJ IDEA", "powershell": "PowerShell", "pwsh": "PowerShell", "cmd": "Command Prompt",
    "windowsterminal": "Windows Terminal", "wt": "Windows Terminal", "explorer": "File Explorer",
    "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint", "outlook": "Outlook", "onenote": "OneNote",
    "teams": "Teams", "ms-teams": "Teams", "slack": "Slack", "zoom": "Zoom", "spotify": "Spotify", "vlc": "VLC",
    "notepad": "Notepad", "notepad++": "Notepad++", "mspaint": "Paint", "calculatorapp": "Calculator",
    "calc": "Calculator", "snippingtool": "Snipping Tool", "taskmgr": "Task Manager", "telegram": "Telegram",
    "whatsapp": "WhatsApp", "discord": "Discord", "steam": "Steam", "keynote": "Keynote",
    "msedge": "Edge", "edge": "Edge", "chrome": "Chrome", "google-chrome": "Chrome", "firefox": "Firefox",
    "brave": "Brave", "opera": "Opera", "vivaldi": "Vivaldi", "arc": "Arc", "safari": "Safari",
}

_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac"}


def is_browser(app: str) -> bool:
    a = (app or "").lower()
    return any(b in a for b in BROWSERS)


def _words(label: str) -> str:
    label = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", label.replace("_", " ").replace("-", " ")).strip()
    return label.title() if label.islower() or label.isupper() else label


def site_name(domain: str) -> str:
    """'www.claude.ai' -> 'Claude', 'app.freepik.com' -> 'Freepik', 'mail.google.com' -> 'Gmail'."""
    host = (domain or "").lower().strip().removeprefix("www.")
    if not host:
        return ""
    parts = host.split(".")
    for i in range(len(parts) - 1):                      # most specific suffix first
        hit = SITES.get(".".join(parts[i:]))
        if hit:
            return hit
    if len(parts) < 2 or host.replace(".", "").isdigit():  # localhost, IPs
        return host
    label = parts[-3] if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2 else parts[-2]
    return _words(label)


def app_name(app: str) -> str:
    """'powershell.exe' -> 'PowerShell', 'Photoshop.exe' -> 'Photoshop', 'OUTLOOK.EXE' -> 'Outlook'."""
    a = (app or "").strip()
    if not a:
        return "Unknown"
    stem = re.sub(r"\.exe$", "", a, flags=re.I)
    key = re.sub(r"^adobe[ _-]+", "", stem, flags=re.I)
    key = re.sub(r"[ _-]+(cc[ _-]*)?(20\d\d|\d{2})$", "", key, flags=re.I).strip().lower()  # "... 2025", "... CC 2019"
    if key in APPS:
        return APPS[key]
    return _words(re.sub(r"^adobe[ _-]+", "", stem, flags=re.I)) or "Unknown"


def display_name(app: str, domain: str = "") -> str:
    """What a report should call this activity: the website if there is one, else the program."""
    return site_name(domain) or app_name(app)

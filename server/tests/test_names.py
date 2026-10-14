from cyril_server.names import app_name, display_name, site_name


def test_websites_show_as_the_site_not_the_domain():
    assert site_name("youtube.com") == "YouTube" and site_name("www.youtube.com") == "YouTube"
    assert site_name("claude.ai") == "Claude" and site_name("app.freepik.com") == "Freepik"
    assert site_name("mail.google.com") == "Gmail" and site_name("www.google.com") == "Google"   # most specific wins
    assert site_name("some-new-site.io") == "Some New Site"
    assert site_name("news.example.co.uk") == "Example" and site_name("shop.thing.com.au") == "Thing"
    assert site_name("localhost") == "localhost" and site_name("192.168.1.5") == "192.168.1.5" and site_name("") == ""


def test_programs_lose_the_exe_and_get_a_readable_name():
    for raw, nice in (("powershell.exe", "PowerShell"), ("Photoshop.exe", "Photoshop"), ("OUTLOOK.EXE", "Outlook"),
                      ("AfterFX.exe", "After Effects"), ("WindowsTerminal.exe", "Windows Terminal"),
                      ("Adobe Premiere Pro 2025", "Premiere Pro"), ("Adobe Illustrator 2024", "Illustrator"),
                      ("code", "VS Code"), ("msedge.exe", "Edge"), ("chrome", "Chrome"),
                      ("Some_Odd-Tool.exe", "Some Odd Tool"), ("MyApp.exe", "My App"), ("", "Unknown")):
        assert app_name(raw) == nice, raw


def test_browser_time_is_named_after_the_website():
    assert display_name("msedge.exe", "www.youtube.com") == "YouTube"
    assert display_name("msedge.exe", "") == "Edge"            # URL unknown -> fall back to the browser
    assert display_name("Photoshop.exe", "") == "Photoshop"

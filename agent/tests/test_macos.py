from cyril_agent.core import is_chromium_browser
from cyril_agent.platform import macos


def test_browser_reply_parsing_never_keeps_a_private_url():
    assert macos.parse_browser_reply("normal||https://www.claude.ai/chat/1\n") == ("https://www.claude.ai/chat/1", False)
    assert macos.parse_browser_reply("incognito||https://secret.example/x") == ("", True)
    assert macos.parse_browser_reply("") == ("", False) and macos.parse_browser_reply(None) == ("", False)
    assert macos.parse_browser_reply("normal||") == ("", False)


def test_which_browsers_are_scriptable_and_how():
    for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Safari", "Vivaldi", "Opera"):
        assert macos.is_scriptable_browser(app), app
    for app in ("Firefox", "Photoshop", "", None):
        assert not macos.is_scriptable_browser(app or ""), app
    assert 'tell application "Google Chrome"' in macos.browser_script("Google Chrome") and "mode of front window" in macos.browser_script("Google Chrome")
    assert 'tell application "Safari"' in macos.browser_script("Safari") and "current tab" in macos.browser_script("Safari")
    assert macos.browser_address("Firefox") == ("", False)       # nothing to run: not scriptable


def test_mac_browser_names_are_recognised_for_url_reading():
    assert is_chromium_browser("Microsoft Edge") and is_chromium_browser("Google Chrome") and is_chromium_browser("Brave Browser")

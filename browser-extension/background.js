// Sends the active tab's URL/title, plus the sites currently playing audio, to the
// local agent (127.0.0.1 only). Audible tabs let Cyril tell "YouTube open for
// background music" apart from "actively watching YouTube".
const AGENT = "http://127.0.0.1:7077/";
const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return ""; } };
async function report() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const audible = (await chrome.tabs.query({ audible: true }))
    .filter((t) => !t.incognito && /^https?:/.test(t.url || "") && !t.mutedInfo?.muted)
    .map((t) => host(t.url)).filter(Boolean);
  const ok = tab && tab.url && /^https?:/.test(tab.url) && !tab.incognito; // never report private windows
  if (!ok && !audible.length) return;
  fetch(AGENT, { method: "POST", headers: { "Content-Type": "application/json" },
                 body: JSON.stringify({ url: ok ? tab.url : "", title: ok ? tab.title || "" : "",
                                        audible: [...new Set(audible)].slice(0, 8) }) }).catch(() => {});
}
chrome.alarms.create("report", { periodInMinutes: 0.1 });
chrome.alarms.onAlarm.addListener(report);
chrome.tabs.onActivated.addListener(report);
chrome.tabs.onUpdated.addListener((_id, info) => { if (info.url || info.title || "audible" in info) report(); });

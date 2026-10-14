// Sends the active tab's URL/title to the local agent (127.0.0.1 only).
const AGENT = "http://127.0.0.1:7077/";
async function report() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab || !tab.url || !/^https?:/.test(tab.url)) return;
  if (tab.incognito) return; // never report private windows
  fetch(AGENT, { method: "POST", headers: { "Content-Type": "application/json" },
                 body: JSON.stringify({ url: tab.url, title: tab.title || "" }) }).catch(() => {});
}
chrome.alarms.create("report", { periodInMinutes: 0.1 });
chrome.alarms.onAlarm.addListener(report);
chrome.tabs.onActivated.addListener(report);
chrome.tabs.onUpdated.addListener((_id, info) => { if (info.url || info.title) report(); });

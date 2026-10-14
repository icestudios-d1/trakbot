// Rule engine: maps (app, domain, title) -> category + productivity label.
// Classification happens at query time, so editing a rule re-labels history.
export const LABELS = ["productive", "unproductive", "neutral"] as const;

export interface Rule {
  id: number; kind: "app" | "domain" | "title"; pattern: string;
  category: string; label: string; alert: number; priority: number;
}
export interface Ev { app?: string | null; title?: string | null; domain?: string | null; idle?: boolean | number }

const globCache = new Map<string, RegExp>();
function glob(pat: string): RegExp {
  let re = globCache.get(pat);
  if (!re) {
    re = new RegExp("^" + pat.replace(/[.+^${}()|\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".") + "$", "is");
    globCache.set(pat, re);
  }
  return re;
}

export function matches(rule: Rule, ev: Ev): boolean {
  const pat = rule.pattern.toLowerCase();
  if (rule.kind === "domain") {
    const d = (ev.domain || "").toLowerCase();
    return !!d && (d === pat || d.endsWith("." + pat));
  }
  if (rule.kind === "app") return glob(pat).test((ev.app || "").toLowerCase());
  if (rule.kind === "title") {
    try { return new RegExp(rule.pattern, "i").test(ev.title || ""); } catch { return false; }
  }
  return false;
}

/** `rules` must be sorted by priority desc, id asc. */
export function matchingRule(ev: Ev, rules: Rule[]): Rule | null {
  if (ev.idle) return null;
  return rules.find((r) => matches(r, ev)) ?? null;
}

export function classify(ev: Ev, rules: Rule[]): [string, string] {
  if (ev.idle) return ["Idle", "idle"];
  const r = matchingRule(ev, rules);
  return r ? [r.category, r.label] : ["Uncategorized", "neutral"];
}

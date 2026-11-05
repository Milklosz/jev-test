#!/usr/bin/env python3
"""
jev_paper.py - paper trading test of the "Jev as judge" idea.
NO wallet, NO real money, NO trading. It only reads public DexScreener data,
asks Jev four questions per token and logs the verdicts to verdicts.csv.

Commands (run in the folder with .env):
  python3 jev_paper.py env       -> checks that the API key is loaded (never prints it)
  python3 jev_paper.py test      -> checks that the key works (GET /v1/models, costs nothing)
  python3 jev_paper.py scan      -> finds fresh tokens, asks Jev, logs verdicts
  python3 jev_paper.py check     -> for verdicts older than 24h, fills in the price change
  python3 jev_paper.py report    -> calibration table (run on Sunday)
  python3 jev_paper.py selftest  -> offline test of the gate logic
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "verdicts.csv")
STATE_PATH = os.path.join(HERE, "jev_state.json")

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODELS_URL = "https://api.typesafe.ai/v1/models"
DEX = "https://api.dexscreener.com"
UA = "jev-paper-test/1.0"

# ---- THE GATE (placeholder numbers copied from the post - tune them on your own log) ----
BUY_MIN_P = 0.70        # action = buy, probability at least this
ORGANIC_MIN_P = 0.65    # organic = yes, probability at least this
LATE_MAX_P = 0.50       # late must be "no" (probability of yes below this)
EXIT_RISK_MAX = 3       # exit_risk score 1-5, at most this
WATCH_WAIT_MIN = 30     # minutes before a "watch" is asked again

# ---- crude pre-filters (free data only; there is NO contract/rug check here) ----
MIN_LIQUIDITY_USD = 5000
MAX_AGE_HOURS = 48
MAX_PER_SCAN = 15

CSV_FIELDS = [
    "time_utc", "chain", "symbol", "token_address", "pair_address", "url",
    "price_usd", "liquidity_usd", "age_h", "action", "action_p", "organic_p",
    "late_p", "exit_risk", "verdict", "reason", "price_24h", "change_24h_pct",
]


# ------------------------------------------------------------------ helpers
def load_env():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def api_key():
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        sys.exit("Brak klucza. Sprawdz plik .env w tym folderze: jedna linijka "
                 "TYPESAFE_API_KEY=twoj_klucz (bez spacji i cudzyslowow).")
    return key


def http_json(url, method="GET", body=None, headers=None, retries=2):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError("HTTP %s dla %s: %s" % (e.code, url.split("?")[0], detail))
        except urllib.error.URLError as e:
            raise RuntimeError("Brak polaczenia z %s: %s" % (url.split("/")[2], e.reason))


def num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def fmt(x, nd=0):
    return "missing" if x is None else ("%.*f" % (nd, x))


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"watch": {}, "seen": {}}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f)


def read_rows():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_rows(rows):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


# ------------------------------------------------------------------ dexscreener
def fresh_candidates():
    """Latest token profiles -> best pair per token, filtered by age and liquidity."""
    profiles = http_json(DEX + "/token-profiles/latest/v1")
    by_chain = {}
    for p in profiles:
        by_chain.setdefault(p.get("chainId"), []).append(p.get("tokenAddress"))
    now_ms = time.time() * 1000
    out = []
    for chain, addrs in by_chain.items():
        addrs = [a for a in addrs if a][:30]
        if not chain or not addrs:
            continue
        pairs = http_json("%s/tokens/v1/%s/%s" % (DEX, chain, ",".join(addrs)))
        best = {}
        for pr in pairs or []:
            tok = (pr.get("baseToken") or {}).get("address")
            liq = num((pr.get("liquidity") or {}).get("usd"), 0)
            if tok and (tok not in best or liq > num((best[tok].get("liquidity") or {}).get("usd"), 0)):
                best[tok] = pr
        for pr in best.values():
            created = num(pr.get("pairCreatedAt"))
            age_h = (now_ms - created) / 3.6e6 if created else None
            liq = num((pr.get("liquidity") or {}).get("usd"))
            if age_h is None or age_h > MAX_AGE_HOURS or age_h < 0:
                continue
            if liq is None or liq < MIN_LIQUIDITY_USD:
                continue
            pr["_age_h"] = age_h
            out.append(pr)
        time.sleep(0.4)
    out.sort(key=lambda p: p["_age_h"])
    return out[:MAX_PER_SCAN]


def dossier(pr):
    t = pr.get("txns") or {}
    v = pr.get("volume") or {}
    c = pr.get("priceChange") or {}

    def tx(w):
        d = t.get(w) or {}
        return "buys %s / sells %s" % (d.get("buys", "missing"), d.get("sells", "missing"))

    lines = [
        "lead: %s on %s" % ((pr.get("baseToken") or {}).get("symbol", "?"), pr.get("chainId")),
        "age_hours: %s" % fmt(pr.get("_age_h"), 1),
        "market_cap_usd: %s" % fmt(num(pr.get("marketCap"))),
        "fdv_usd: %s" % fmt(num(pr.get("fdv"))),
        "liquidity_usd: %s" % fmt(num((pr.get("liquidity") or {}).get("usd"))),
        "price_change_pct: 5m %s, 1h %s, 6h %s, 24h %s" % (
            fmt(num(c.get("m5")), 1), fmt(num(c.get("h1")), 1),
            fmt(num(c.get("h6")), 1), fmt(num(c.get("h24")), 1)),
        "txns_5m: %s" % tx("m5"),
        "txns_1h: %s" % tx("h1"),
        "txns_6h: %s" % tx("h6"),
        "volume_usd: 1h %s, 6h %s, 24h %s" % (
            fmt(num(v.get("h1"))), fmt(num(v.get("h6"))), fmt(num(v.get("h24")))),
        "holders_now_vs_1h_ago: missing",
        "top10_holders_share: missing",
        "dev_wallet_share: missing",
        "contract_risk_check: missing (not run)",
        "smart_money_check: missing (not run)",
        "social_mentions_per_hour: missing",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ jev
QUESTIONS = {
    "organic": {
        "type": "noul",
        "instructions": "Is the buying broad and steady rather than one short burst or one actor? "
                        "Judge only from the dossier. Fields marked missing are unknown.",
        "criteria": {
            "true": "buys are spread across the 5m, 1h and 6h windows, sell counts are comparable "
                    "to buy counts, and volume is reasonable relative to liquidity",
            "false": "activity is one short burst, sells clearly outnumber buys, or volume is "
                     "extreme relative to liquidity",
        },
    },
    "late": {
        "type": "noul",
        "instructions": "Did most of the price move already happen before this lead reached the desk?",
        "criteria": {
            "true": "price is already up a lot over 6h or 24h and 5m/1h momentum is fading",
            "false": "gain over 6h/24h is modest or momentum is still building",
        },
    },
    "exit_risk": {
        "type": "score",
        "instructions": "How hard would it be to sell a small position at a fair price?",
        "criteria": [
            "1: very easy, liquidity above 250k USD",
            "2: easy, liquidity 100k to 250k USD",
            "3: moderate, liquidity 30k to 100k USD",
            "4: hard, liquidity 10k to 30k USD",
            "5: very hard, liquidity below 10k USD or a small position would be a big part of the pool",
        ],
    },
    "action": {
        "type": "choice",
        "instructions": "Based on the dossier only, what should the desk do with this lead?",
        "criteria": {
            "buy": "clean setup, enter now",
            "watch": "interesting but not yet, ask again in 30 minutes",
            "pass": "no",
        },
    },
}


def ask_jev(state_text):
    body = {"model": "jev-latest", "state": state_text, "questions": QUESTIONS}
    resp = http_json(JEV_URL, "POST", body, {"Authorization": "Bearer " + api_key()})
    return resp.get("answers") or {}


def decide(ans, was_watch=False):
    """The fixed gate. Returns (verdict, reason, numbers)."""
    try:
        act = ans["action"]
        action = act["choice"]
        action_p = num((act.get("probabilities") or {}).get(action), num(act.get("confidence")))
        organic_p = num(ans["organic"]["noul"])
        late_p = num(ans["late"]["noul"])
        exit_risk = num(ans["exit_risk"]["score"]) + 1  # Jev scores are 0-based
    except (KeyError, TypeError):
        return "pass", "no answer from Jev (no answer is not a yes)", {}
    nums = {"action": action, "action_p": action_p, "organic_p": organic_p,
            "late_p": late_p, "exit_risk": exit_risk}
    if None in (action_p, organic_p, late_p, exit_risk):
        return "pass", "incomplete answer", nums
    if action == "buy":
        fails = []
        if action_p < BUY_MIN_P:
            fails.append("action_p %.2f < %.2f" % (action_p, BUY_MIN_P))
        if organic_p < ORGANIC_MIN_P:
            fails.append("organic_p %.2f < %.2f" % (organic_p, ORGANIC_MIN_P))
        if late_p >= LATE_MAX_P:
            fails.append("late (p=%.2f)" % late_p)
        if exit_risk > EXIT_RISK_MAX:
            fails.append("exit_risk %.1f > %d" % (exit_risk, EXIT_RISK_MAX))
        return ("pass", "killed by: " + "; ".join(fails), nums) if fails else ("buy", "gate passed", nums)
    if action == "watch":
        if was_watch:
            return "pass", "second watch = pass", nums
        return "watch", "ask again in %d min" % WATCH_WAIT_MIN, nums
    return "pass", "action = pass", nums


# ------------------------------------------------------------------ commands
def cmd_env():
    key = api_key()
    print("OK: klucz wczytany (%d znakow). Nie wyswietlam go." % len(key))


def cmd_test():
    http_json(JEV_MODELS_URL, headers={"Authorization": "Bearer " + api_key()})
    print("OK: Jev przyjal klucz.")


def cmd_scan():
    st = load_state()
    rows = read_rows()
    now = datetime.now(timezone.utc)
    cands = fresh_candidates()
    print("Kandydaci po filtrach (wiek <= %dh, plynnosc >= %d USD): %d" % (
        MAX_AGE_HOURS, MIN_LIQUIDITY_USD, len(cands)))
    counts = {"buy": 0, "watch": 0, "pass": 0}
    for pr in cands:
        chain = pr.get("chainId")
        tok = (pr.get("baseToken") or {}).get("address")
        key = "%s:%s" % (chain, tok)
        was_watch = False
        if key in st["watch"]:
            waited = (now - datetime.fromisoformat(st["watch"][key])).total_seconds() / 60
            if waited < WATCH_WAIT_MIN:
                continue
            was_watch = True
        elif key in st["seen"]:
            continue
        try:
            ans = ask_jev(dossier(pr))
        except RuntimeError as e:
            print("  Jev blad (%s) -> pass" % e)
            ans = {}
        verdict, reason, n = decide(ans, was_watch)
        counts[verdict] += 1
        if verdict == "watch":
            st["watch"][key] = now.isoformat()
        else:
            st["watch"].pop(key, None)
            st["seen"][key] = now.isoformat()
            rows.append({
                "time_utc": now.strftime("%Y-%m-%d %H:%M:%S"), "chain": chain,
                "symbol": (pr.get("baseToken") or {}).get("symbol", ""), "token_address": tok,
                "pair_address": pr.get("pairAddress", ""), "url": pr.get("url", ""),
                "price_usd": pr.get("priceUsd", ""),
                "liquidity_usd": (pr.get("liquidity") or {}).get("usd", ""),
                "age_h": round(pr["_age_h"], 1), "action": n.get("action", ""),
                "action_p": n.get("action_p", ""), "organic_p": n.get("organic_p", ""),
                "late_p": n.get("late_p", ""), "exit_risk": n.get("exit_risk", ""),
                "verdict": verdict, "reason": reason,
            })
        print("  %-10s %-8s -> %s (%s)" % ((pr.get("baseToken") or {}).get("symbol", "?")[:10],
                                         chain, verdict.upper(), reason))
        time.sleep(0.3)
    write_rows(rows)
    save_state(st)
    print("Razem: buy %d, watch %d, pass %d. Zapisano do verdicts.csv" % (
        counts["buy"], counts["watch"], counts["pass"]))


def cmd_check():
    rows = read_rows()
    now = datetime.now(timezone.utc)
    todo = []
    for r in rows:
        if r.get("price_24h"):
            continue
        t = datetime.strptime(r["time_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if now - t >= timedelta(hours=24) and r.get("pair_address"):
            todo.append(r)
    done = 0
    for r in todo:
        try:
            data = http_json("%s/latest/dex/pairs/%s/%s" % (DEX, r["chain"], r["pair_address"]))
            pairs = data.get("pairs") or []
            p = num(pairs[0].get("priceUsd")) if pairs else None
        except RuntimeError:
            continue
        p0 = num(r.get("price_usd"))
        if p is None:
            r["price_24h"] = "0"       # pair gone from DexScreener = treat as dead
            r["change_24h_pct"] = "-100"
        elif p0:
            r["price_24h"] = str(p)
            r["change_24h_pct"] = "%.1f" % ((p / p0 - 1) * 100)
        done += 1
        time.sleep(0.3)
    write_rows(rows)
    print("Uzupelniono wynik 24h dla %d wpisow (z %d czekajacych)." % (done, len(todo)))


def cmd_report():
    rows = [r for r in read_rows() if r.get("change_24h_pct") not in ("", None)]
    if not rows:
        print("Brak wpisow ze znanym wynikiem 24h. Uruchom 'scan' kilka razy, odczekaj 24h i uruchom 'check'.")
        return

    def stats(sel):
        n = len(sel)
        green = sum(1 for r in sel if num(r["change_24h_pct"], 0) > 0)
        return n, green

    print("Wynik = czy cena po 24h jest wyzsza niz w chwili werdyktu (to NIE jest zysk po oplatach).")
    print("%-22s %6s %6s %8s" % ("grupa", "ilosc", "zielone", "% zielonych"))
    buys = [r for r in rows if r["verdict"] == "buy"]
    for name, lo, hi in (("buy 0.70-0.79", 0.70, 0.80), ("buy 0.80-0.89", 0.80, 0.90), ("buy 0.90+", 0.90, 1.01)):
        sel = [r for r in buys if lo <= (num(r["action_p"], 0)) < hi]
        n, g = stats(sel)
        print("%-22s %6d %6d %8s" % (name, n, g, ("%.0f%%" % (100.0 * g / n)) if n else "-"))
    n, g = stats([r for r in rows if r["verdict"] == "pass"])
    print("%-22s %6d %6d %8s" % ("pass (wszystkie)", n, g, ("%.0f%%" % (100.0 * g / n)) if n else "-"))
    print("\nJesli 0.90+ nie wygrywa czesciej niz 0.70-0.79, sedzia nie jest skalibrowany. "
          "Nie tlumacz tego - zmien progi albo pytanie.")


def cmd_selftest():
    def mk(action, ap, org, late, risk0):
        return {"action": {"choice": action, "probabilities": {action: ap}},
                "organic": {"noul": org}, "late": {"noul": late}, "exit_risk": {"score": risk0}}
    cases = [
        (mk("buy", 0.82, 0.80, 0.20, 1.0), False, "buy"),
        (mk("buy", 0.69, 0.80, 0.20, 1.0), False, "pass"),      # action_p too low
        (mk("buy", 0.90, 0.60, 0.20, 1.0), False, "pass"),      # organic too low
        (mk("buy", 0.90, 0.90, 0.70, 1.0), False, "pass"),      # late
        (mk("buy", 0.90, 0.90, 0.20, 3.0), False, "pass"),      # exit_risk 4 > 3
        (mk("buy", 0.90, 0.90, 0.20, 2.0), False, "buy"),       # exit_risk 3 ok
        (mk("watch", 0.80, 0.80, 0.20, 1.0), False, "watch"),
        (mk("watch", 0.80, 0.80, 0.20, 1.0), True, "pass"),     # second watch
        (mk("pass", 0.95, 0.80, 0.20, 1.0), False, "pass"),
        ({}, False, "pass"),                                    # no answer
    ]
    bad = 0
    for ans, was_watch, expect in cases:
        got = decide(ans, was_watch)[0]
        ok = got == expect
        bad += 0 if ok else 1
        print("%s  oczekiwano %-5s dostano %-5s" % ("OK " if ok else "BLAD", expect, got))
    print("Selftest: %s" % ("wszystko dziala" if not bad else "%d bledow" % bad))
    sys.exit(1 if bad else 0)


COMMANDS = {"env": cmd_env, "test": cmd_test, "scan": cmd_scan, "check": cmd_check,
            "report": cmd_report, "selftest": cmd_selftest}

if __name__ == "__main__":
    load_env()
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    try:
        COMMANDS[sys.argv[1]]()
    except RuntimeError as e:
        sys.exit("Blad: %s" % e)

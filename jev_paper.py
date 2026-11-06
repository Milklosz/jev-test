#!/usr/bin/env python3
"""
jev_paper.py - paper trading test of the "Jev as judge" idea.
NO wallet, NO real money, NO trading. It only reads public data (DexScreener,
RugCheck), asks Jev four questions per token and logs the verdicts to verdicts.csv.

Commands (run in the folder with .env, or on GitHub Actions):
  python3 jev_paper.py env       -> checks that the API key is loaded (never prints it)
  python3 jev_paper.py test      -> checks that the key works (GET /v1/models, costs nothing)
  python3 jev_paper.py scan      -> fills price snapshots, finds fresh tokens, asks Jev, logs verdicts
  python3 jev_paper.py check     -> fills price snapshots (1h / 6h / 24h after each verdict)
  python3 jev_paper.py report    -> calibration table (run on Sunday)
  python3 jev_paper.py testmail  -> writes a test notification (GitHub turns it into an e-mail)
  python3 jev_paper.py selftest  -> offline test of the gate, RISK and snapshot logic
"""
import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "verdicts.csv")
STATE_PATH = os.path.join(HERE, "jev_state.json")
NOTIFY_PATH = os.path.join(HERE, "notify.md")

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODELS_URL = "https://api.typesafe.ai/v1/models"
DEX = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1"
UA = "jev-paper-test/1.1"

# ---- THE GATE (placeholder numbers copied from the post - tune them on your own log) ----
BUY_MIN_P = 0.70        # action = buy, probability at least this
ORGANIC_MIN_P = 0.65    # organic = yes, probability at least this
LATE_MAX_P = 0.50       # late must be "no" (probability of yes below this)
EXIT_RISK_MAX = 3       # exit_risk score 1-5, at most this
WATCH_WAIT_MIN = 30     # minutes before a "watch" is asked again

# ---- pre-filters ----
MIN_LIQUIDITY_USD = 5000
MAX_AGE_HOURS = 48
MAX_PER_SCAN = 25

# ---- paper-trading realism ----
COST_PCT = 15.0         # assumed round-trip cost (slippage + fees) on small memecoins, in percent
SNAPSHOTS = ((1, "1h"), (6, "6h"), (24, "24h"))
MAX_CHECK_CALLS = 60    # max DexScreener calls per check run

CSV_FIELDS = [
    "time_utc", "chain", "symbol", "token_address", "pair_address", "url",
    "price_usd", "liquidity_usd", "age_h", "action", "action_p", "organic_p",
    "late_p", "exit_risk", "verdict", "reason", "risk",
    "price_1h", "change_1h_pct", "price_6h", "change_6h_pct",
    "price_24h", "change_24h_pct",
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
        except ValueError as e:
            raise RuntimeError("Odpowiedz z %s nie jest poprawnym JSON: %s" % (url.split("/")[2], e))


def num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def fmt(x, nd=0):
    return "missing" if x is None else ("%.*f" % (nd, x))


def pick(d, *names):
    """First non-null value among several possible key names (camelCase / snake_case)."""
    if isinstance(d, dict):
        for n in names:
            if d.get(n) is not None:
                return d[n]
    return None


def has_key(d, *names):
    return isinstance(d, dict) and any(n in d for n in names)


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
    sources = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        try:
            sources.extend(http_json(DEX + path) or [])
        except RuntimeError as e:
            print("zrodlo %s pominiete: %s" % (path, str(e)[:100]))
        time.sleep(0.3)
    by_chain, seen_tok = {}, set()
    for p in sources:
        ch, ad = p.get("chainId"), p.get("tokenAddress")
        if not ch or not ad or (ch, ad) in seen_tok:
            continue
        seen_tok.add((ch, ad))
        by_chain.setdefault(ch, []).append(ad)
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


# ------------------------------------------------------------------ RISK (RugCheck, Solana only)
def rugcheck_report(mint):
    return http_json("%s/tokens/%s/report" % (RUGCHECK, mint))


def assess_risk(rep):
    """Returns (status, dossier_lines, dead_reasons).
    status: 'ok' (no hard fail), 'dead' (one hard fail = dead), 'error' (unusable report)."""
    if not (has_key(rep, "mintAuthority", "mint_authority") and
            has_key(rep, "freezeAuthority", "freeze_authority")):
        return "error", ["contract_risk_check: missing (unrecognised report format)"], []
    mint = pick(rep, "mintAuthority", "mint_authority")
    freeze = pick(rep, "freezeAuthority", "freeze_authority")
    rugged = bool(pick(rep, "rugged"))
    risks = [r for r in (pick(rep, "risks") or []) if isinstance(r, dict)]
    holders = [h for h in (pick(rep, "topHolders", "top_holders") or []) if isinstance(h, dict)]
    total = pick(rep, "totalHolders", "total_holders")
    top10 = sum(num(h.get("pct"), 0) for h in holders[:10]) if holders else None
    lp_vals = []
    for m in pick(rep, "markets") or []:
        v = pick(pick(m, "lp"), "lpLockedPct", "lp_locked_pct")
        if num(v) is not None:
            lp_vals.append(num(v))
    lp_locked = max(lp_vals) if lp_vals else None
    danger = [str(r.get("name")) for r in risks if str(r.get("level", "")).lower() == "danger"]
    warn = [str(r.get("name")) for r in risks if str(r.get("level", "")).lower() == "warn"]

    dead = []
    if rugged:
        dead.append("marked as rugged")
    if mint:
        dead.append("mint authority not renounced")
    if freeze:
        dead.append("freeze authority not renounced")
    for d in danger:
        dead.append("danger: " + d)

    lines = [
        "mint_authority: %s" % ("ACTIVE" if mint else "renounced"),
        "freeze_authority: %s" % ("ACTIVE" if freeze else "renounced"),
        "rugged_flag: %s" % ("yes" if rugged else "no"),
        "total_holders: %s" % (total if total is not None else "missing"),
        "top10_holders_pct: %s (as reported; may include liquidity pool accounts)" % fmt(top10, 1),
        "lp_locked_pct: %s" % fmt(lp_locked, 1),
        "risk_warnings: %s" % (", ".join(warn) if warn else "none"),
        "risk_dangers: %s" % (", ".join(danger) if danger else "none"),
    ]
    return ("dead" if dead else "ok"), lines, dead


def dossier(pr, risk_lines=None):
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
        "dev_wallet_share: missing",
    ]
    if risk_lines:
        lines.extend(risk_lines)
    else:
        lines.append("contract_risk_check: missing (not run)")
        lines.append("top10_holders_share: missing")
    lines.append("smart_money_check: missing (not run)")
    lines.append("social_mentions_per_hour: missing")
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


def _gate(ans, was_watch=False):
    """The fixed gate on Jev's answers. Returns (verdict, reason, numbers)."""
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


def decide(ans, was_watch=False, risk="ok"):
    """Gate + RISK rule: a BUY needs a clean RISK check (so only checked Solana tokens can be BUY)."""
    verdict, reason, nums = _gate(ans, was_watch)
    if verdict == "buy" and risk != "ok":
        return "pass", "no clean RISK check (risk=%s)" % risk, nums
    return verdict, reason, nums


# ------------------------------------------------------------------ snapshots
def fill_snapshots(row, price_now, age_h):
    """Fills price/change columns for 1h, 6h, 24h once the verdict is old enough.
    price_now=None means the pair is gone (treated as dead). Returns how many were filled."""
    p0 = num(row.get("price_usd"))
    filled = 0
    for hours, label in SNAPSHOTS:
        if age_h >= hours and not row.get("price_" + label):
            if price_now is None:
                row["price_" + label] = "0"
                row["change_%s_pct" % label] = "-100"
            elif p0:
                row["price_" + label] = str(price_now)
                row["change_%s_pct" % label] = "%.1f" % ((price_now / p0 - 1) * 100)
            else:
                continue
            filled += 1
    return filled


def row_age_h(row, now):
    t = datetime.strptime(row["time_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 3600.0


# ------------------------------------------------------------------ notification
def write_notify(buys):
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    syms = ", ".join(b["symbol"] for b in buys)
    lines = ["# BUY (test papierowy): %s" % syms, "",
             ("@%s " % owner if owner else "") + "Sedzia dal BUY. To zapis testowy, NIE rekomendacja.", ""]
    for b in buys:
        lines.append("- **%s** (%s), cena %s USD, plynnosc %s USD, wiek %.1f h, "
                     "action_p %.2f, organic_p %.2f, late_p %.2f, exit_risk %.1f - [DexScreener](%s)" % (
                         b["symbol"], b["chain"], b["price"], b["liq"], b["age"],
                         b["action_p"], b["organic_p"], b["late_p"], b["exit_risk"], b["url"]))
    lines += ["", "Przed jakakolwiek decyzja sprawdz kontrakt recznie (rugcheck.xyz) i pamietaj o "
                  "poslizgu oraz oplatach (zalozenie testu: %d%% na wejscie i wyjscie)." % int(COST_PCT)]
    with open(NOTIFY_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------ commands
def cmd_env():
    key = api_key()
    print("OK: klucz wczytany (%d znakow). Nie wyswietlam go." % len(key))


def cmd_test():
    http_json(JEV_MODELS_URL, headers={"Authorization": "Bearer " + api_key()})
    print("OK: Jev przyjal klucz.")


def cmd_testmail():
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    with open(NOTIFY_PATH, "w", encoding="utf-8") as f:
        f.write("# Test powiadomienia (jev-paper)\n\n%sTo jest probne powiadomienie. Jesli widzisz "
                "ten mail, powiadomienia o BUY dzialaja.\n" % ("@%s " % owner if owner else ""))
    print("Zapisano notify.md (workflow zamieni go w Issue, a GitHub wysle mail).")


def cmd_check():
    rows = read_rows()
    now = datetime.now(timezone.utc)
    calls = filled = 0
    for r in rows:
        if calls >= MAX_CHECK_CALLS:
            break
        try:
            age = row_age_h(r, now)
        except (KeyError, ValueError):
            continue
        due = any(age >= h and not r.get("price_" + lab) for h, lab in SNAPSHOTS)
        if not due or not r.get("pair_address") or not num(r.get("price_usd")):
            continue
        try:
            data = http_json("%s/latest/dex/pairs/%s/%s" % (DEX, r["chain"], r["pair_address"]))
        except RuntimeError:
            continue
        calls += 1
        pairs = (data or {}).get("pairs") or []
        price_now = num(pairs[0].get("priceUsd")) if pairs else None
        filled += fill_snapshots(r, price_now, age)
        time.sleep(0.25)
    write_rows(rows)
    print("Snapshoty cen: uzupelniono %d pol (%d zapytan)." % (filled, calls))


def cmd_scan():
    try:
        cmd_check()
    except RuntimeError as e:
        print("check pominiety: %s" % e)
    st = load_state()
    rows = read_rows()
    now = datetime.now(timezone.utc)
    cands = fresh_candidates()
    print("Kandydaci po filtrach (wiek <= %dh, plynnosc >= %d USD): %d" % (
        MAX_AGE_HOURS, MIN_LIQUIDITY_USD, len(cands)))
    counts = {"buy": 0, "watch": 0, "pass": 0}
    buys = []
    for pr in cands:
        chain = pr.get("chainId")
        tok = (pr.get("baseToken") or {}).get("address")
        sym = (pr.get("baseToken") or {}).get("symbol", "")
        key = "%s:%s" % (chain, tok)
        was_watch = False
        if key in st["watch"]:
            waited = (now - datetime.fromisoformat(st["watch"][key])).total_seconds() / 60
            if waited < WATCH_WAIT_MIN:
                continue
            was_watch = True
        elif key in st["seen"]:
            continue

        risk, risk_lines, dead = "n/a", None, []
        if chain == "solana":
            try:
                risk, risk_lines, dead = assess_risk(rugcheck_report(tok))
            except RuntimeError as e:
                print("  RISK blad (%s)" % str(e)[:120])
                risk, risk_lines = "error", ["contract_risk_check: missing (check failed)"]
            time.sleep(0.5)

        if risk == "dead":
            verdict, reason, n = "pass", "RISK dead: " + "; ".join(dead), {}
        else:
            try:
                ans = ask_jev(dossier(pr, risk_lines))
            except RuntimeError as e:
                print("  Jev blad (%s) -> pass" % e)
                ans = {}
            verdict, reason, n = decide(ans, was_watch, risk)

        counts[verdict] += 1
        if verdict == "watch":
            st["watch"][key] = now.isoformat()
        else:
            st["watch"].pop(key, None)
            st["seen"][key] = now.isoformat()
            rows.append({
                "time_utc": now.strftime("%Y-%m-%d %H:%M:%S"), "chain": chain,
                "symbol": sym, "token_address": tok,
                "pair_address": pr.get("pairAddress", ""), "url": pr.get("url", ""),
                "price_usd": pr.get("priceUsd", ""),
                "liquidity_usd": (pr.get("liquidity") or {}).get("usd", ""),
                "age_h": round(pr["_age_h"], 1), "action": n.get("action", ""),
                "action_p": n.get("action_p", ""), "organic_p": n.get("organic_p", ""),
                "late_p": n.get("late_p", ""), "exit_risk": n.get("exit_risk", ""),
                "verdict": verdict, "reason": reason, "risk": risk,
            })
            if verdict == "buy":
                buys.append({"symbol": sym, "chain": chain, "price": pr.get("priceUsd", "?"),
                             "liq": (pr.get("liquidity") or {}).get("usd", "?"),
                             "age": pr["_age_h"], "url": pr.get("url", ""),
                             "action_p": n["action_p"], "organic_p": n["organic_p"],
                             "late_p": n["late_p"], "exit_risk": n["exit_risk"]})
        print("  %-10s %-8s -> %s (%s)" % (sym[:10], chain, verdict.upper(), reason[:80]))
        time.sleep(0.3)
    write_rows(rows)
    save_state(st)
    if buys:
        write_notify(buys)
    print("Razem: buy %d, watch %d, pass %d. Zapisano do verdicts.csv" % (
        counts["buy"], counts["watch"], counts["pass"]))


def _stats_row(label, name, vals):
    n = len(vals)
    if not n:
        print("%-8s %-6s %5d %10s %9s %13s" % (label, name, 0, "-", "-", "-"))
        return
    green = sum(1 for x in vals if x > 0)
    med = statistics.median(vals)
    print("%-8s %-6s %5d %9.0f%% %8.1f%% %12.1f%%" % (
        label, name, n, 100.0 * green / n, med, med - COST_PCT))


def cmd_report():
    rows = read_rows()
    if not any(r.get("change_1h_pct") not in ("", None) or r.get("change_24h_pct") not in ("", None)
               for r in rows):
        print("Brak wpisow ze znanym wynikiem. Poczekaj kilka godzin na snapshoty cen.")
        return
    print("Wynik = zmiana ceny od chwili werdyktu (to NIE jest zysk po oplatach).")
    print("Kolumna 'po kosztach' odejmuje zalozone %d%% (poslizg + oplaty)." % int(COST_PCT))
    print("%-8s %-6s %5s %10s %9s %13s" % ("horyzont", "grupa", "ilosc", "zielone", "mediana", "po kosztach"))
    for _, label in SNAPSHOTS:
        col = "change_%s_pct" % label
        for name in ("buy", "pass"):
            vals = [num(r[col]) for r in rows
                    if r.get("verdict") == name and num(r.get(col)) is not None]
            _stats_row(label, name, vals)
    print("\nKalibracja (wynik po 24h, tylko BUY):")
    print("%-16s %5s %7s" % ("przedzial action_p", "ilosc", "zielone"))
    buys = [r for r in rows if r.get("verdict") == "buy" and num(r.get("change_24h_pct")) is not None]
    for name, lo, hi in (("0.70-0.79", 0.70, 0.80), ("0.80-0.89", 0.80, 0.90), ("0.90+", 0.90, 1.01)):
        sel = [r for r in buys if lo <= num(r.get("action_p"), 0) < hi]
        g = sum(1 for r in sel if num(r["change_24h_pct"], 0) > 0)
        print("%-16s %5d %7s" % (name, len(sel), ("%d/%d" % (g, len(sel))) if sel else "-"))
    print("\nOpoznienie: porownaj mediane BUY po 1h, 6h i 24h. Jesli sygnal gasnie w ciagu godzin, "
          "reczne zatwierdzanie raz na kilka godzin nie wystarczy.")
    print("Jesli 0.90+ nie wygrywa czesciej niz 0.70-0.79, sedzia nie jest skalibrowany. "
          "Nie tlumacz tego - zmien progi albo pytanie.")


def cmd_selftest():
    bad = [0]

    def check(name, got, expect):
        ok = got == expect
        bad[0] += 0 if ok else 1
        print("%s  %-44s oczekiwano %-6s dostano %-6s" % ("OK " if ok else "BLAD", name, expect, got))

    def mk(action, ap, org, late, risk0):
        return {"action": {"choice": action, "probabilities": {action: ap}},
                "organic": {"noul": org}, "late": {"noul": late}, "exit_risk": {"score": risk0}}

    # gate
    check("gate: czysty buy", decide(mk("buy", 0.82, 0.80, 0.20, 1.0))[0], "buy")
    check("gate: action_p za niskie", decide(mk("buy", 0.69, 0.80, 0.20, 1.0))[0], "pass")
    check("gate: organic za niskie", decide(mk("buy", 0.90, 0.60, 0.20, 1.0))[0], "pass")
    check("gate: late", decide(mk("buy", 0.90, 0.90, 0.70, 1.0))[0], "pass")
    check("gate: exit_risk 4", decide(mk("buy", 0.90, 0.90, 0.20, 3.0))[0], "pass")
    check("gate: exit_risk 3 ok", decide(mk("buy", 0.90, 0.90, 0.20, 2.0))[0], "buy")
    check("gate: pierwszy watch", decide(mk("watch", 0.80, 0.80, 0.20, 1.0))[0], "watch")
    check("gate: drugi watch", decide(mk("watch", 0.80, 0.80, 0.20, 1.0), True)[0], "pass")
    check("gate: pass", decide(mk("pass", 0.95, 0.80, 0.20, 1.0))[0], "pass")
    check("gate: brak odpowiedzi", decide({})[0], "pass")
    # RISK rule
    good = mk("buy", 0.90, 0.90, 0.20, 1.0)
    check("risk: buy bez sprawdzenia RISK (n/a)", decide(good, False, "n/a")[0], "pass")
    check("risk: buy z bledem RISK", decide(good, False, "error")[0], "pass")
    check("risk: buy z czystym RISK", decide(good, False, "ok")[0], "buy")
    # assess_risk
    base = {"mintAuthority": None, "freezeAuthority": None, "rugged": False, "risks": [],
            "topHolders": [{"pct": 5}, {"pct": 3}], "totalHolders": 120,
            "markets": [{"lp": {"lpLockedPct": 100}}]}
    check("assess: czysty raport", assess_risk(base)[0], "ok")
    check("assess: mint authority aktywne", assess_risk(dict(base, mintAuthority="abc"))[0], "dead")
    check("assess: freeze authority aktywne", assess_risk(dict(base, freezeAuthority="abc"))[0], "dead")
    check("assess: rugged", assess_risk(dict(base, rugged=True))[0], "dead")
    check("assess: ryzyko danger", assess_risk(dict(base, risks=[{"name": "X", "level": "danger"}]))[0], "dead")
    check("assess: ryzyko warn to jeszcze ok", assess_risk(dict(base, risks=[{"name": "X", "level": "warn"}]))[0], "ok")
    snake = {"mint_authority": None, "freeze_authority": None, "rugged": False, "risks": []}
    check("assess: nazwy snake_case", assess_risk(snake)[0], "ok")
    check("assess: nieznany format", assess_risk({"foo": 1})[0], "error")
    # snapshots
    r = {"price_usd": "1.0"}
    check("snapshot: za wczesnie", fill_snapshots(r, 1.1, 0.5), 0)
    check("snapshot: po 1.5h", fill_snapshots(r, 1.1, 1.5), 1)
    check("snapshot: zmiana 1h", r.get("change_1h_pct"), "10.0")
    check("snapshot: pair zniknal po 25h", fill_snapshots(r, None, 25), 2)
    check("snapshot: 24h = -100", r.get("change_24h_pct"), "-100")
    check("snapshot: 1h nie nadpisane", r.get("change_1h_pct"), "10.0")
    # dossier contains RISK facts
    d = dossier({"chainId": "solana", "baseToken": {"symbol": "T"}, "_age_h": 2.0}, ["mint_authority: renounced"])
    check("dossier: zawiera linie RISK", "mint_authority: renounced" in d, True)
    check("dossier bez RISK: oznaczone missing", "contract_risk_check: missing (not run)" in dossier({}), True)
    print("Selftest: %s" % ("wszystko dziala" if not bad[0] else "%d bledow" % bad[0]))
    sys.exit(1 if bad[0] else 0)


COMMANDS = {"env": cmd_env, "test": cmd_test, "scan": cmd_scan, "check": cmd_check,
            "report": cmd_report, "testmail": cmd_testmail, "selftest": cmd_selftest}

if __name__ == "__main__":
    load_env()
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    try:
        COMMANDS[sys.argv[1]]()
    except RuntimeError as e:
        sys.exit("Blad: %s" % e)

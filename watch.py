"""
Sporting tickets watcher -> Discord webhook.

Polls https://tickets.sporting.pt/api/match/allopengames (the same JSON the
site itself loads), compares it with the last saved snapshot (state.json),
and posts a Discord message for:
  - new game listed
  - game removed
  - sale/visibility flags changed (allowSale, allowOnGoingSales, public, member, disabledReasonDesc)
  - sold out / back in stock
  - date/time changed or confirmed
  - access-conditions (sale rules) text edited

Designed to run once per invocation (GitHub Actions cron). Only stdlib.
"""
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API_URL = "https://tickets.sporting.pt/api/match/allopengames"
GAME_URL = "https://tickets.sporting.pt/pt/evento/{token}"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
# Optional: "Futebol,Andebol" to only alert on those modalities. Empty = all.
MODALITIES = {m.strip().lower() for m in os.environ.get("MODALITIES", "").split(",") if m.strip()}
# Optional: put "<@YOUR_DISCORD_USER_ID>" here to get a real phone ping.
MENTION = os.environ.get("DISCORD_MENTION", "").strip()

FLAG_FIELDS = {
    "allowSale": "Venda permitida",
    "allowOnGoingSales": "Venda a decorrer",
    "public": "Aberto ao público (adeptos)",
    "member": "Aberto a sócios",
    "soldOut": "Esgotado",
    "disabledReasonDesc": "Estado do botão",
}

COLOR_NEW, COLOR_GOOD, COLOR_BAD, COLOR_INFO = 0x00A859, 0x2ECC71, 0xE74C3C, 0xF1C40F


# ---------- helpers ----------
def fetch_games():
    req = urllib.request.Request(
        API_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-PT,pt;q=0.9",
            "Referer": "https://tickets.sporting.pt/pt",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"Resposta não-JSON (provável bloqueio Cloudflare). Início: {body[:200]!r}")
    if not data.get("success"):
        raise RuntimeError(f"API respondeu success=false: {data.get('message')!r}")
    return data["data"]["openGames"]


def clean_text(raw_html):
    if not raw_html:
        return ""
    t = re.sub(r"<\s*(br|/p|/li)\s*/?>", "\n", raw_html, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\u00a0", " ").replace("\ufeff", "")
    lines = [re.sub(r"\s+", " ", l).strip(" ·") for l in t.splitlines()]
    return "\n".join(l for l in lines if l)


def snapshot(g):
    rules = clean_text(g.get("accessConditions"))
    return {
        "id": g["id"],
        "token": g["token"],
        "title": g.get("eventName") or f"{g['homeTeam']['name'].strip()} vs {g['awayTeam']['name'].strip()}",
        "competition": (g.get("competition") or "").strip(),
        "modality": (g.get("modality") or "").strip(),
        "date": g.get("date"),
        "isDateConfirmed": g.get("isDateConfirmed"),
        **{f: g.get(f) for f in FLAG_FIELDS},
        "rules": rules,
        "rules_hash": hashlib.sha1(rules.encode()).hexdigest(),
    }


def fmt_date(iso, confirmed=True):
    try:
        d = datetime.fromisoformat(iso)
        s = d.strftime("%d/%m/%Y %H:%M")
    except Exception:
        s = str(iso)
    return s if confirmed else f"{s} (por confirmar)"


def fmt_val(v):
    if v is True:
        return "sim"
    if v is False:
        return "não"
    return "—" if v in (None, "") else str(v)


def embed(game, title, description, color):
    return {
        "title": f"{title}: {game['title']}"[:256],
        "url": GAME_URL.format(token=game["token"]),
        "description": description[:4000],
        "color": color,
        "fields": [
            {"name": "Competição", "value": f"{game['modality']} · {game['competition']}"[:1024] or "—", "inline": True},
            {"name": "Data", "value": fmt_date(game["date"], game["isDateConfirmed"]), "inline": True},
        ],
    }


def post(embeds, content=None):
    if not WEBHOOK:
        print("[dry-run] DISCORD_WEBHOOK_URL not set. Would post:")
        print(json.dumps({"content": content, "embeds": embeds}, ensure_ascii=False, indent=2))
        return
    for i in range(0, len(embeds), 10):  # Discord: max 10 embeds per message
        payload = {"embeds": embeds[i:i + 10], "allowed_mentions": {"parse": ["users", "roles"]}}
        if content and i == 0:
            payload["content"] = content
        req = urllib.request.Request(
            WEBHOOK, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "sporting-tickets-watch"},
        )
        urllib.request.urlopen(req, timeout=30).read()


def rules_diff(old, new):
    old_set = set(old.splitlines())
    added = [l for l in new.splitlines() if l not in old_set]
    if not added:
        return "Texto das regras de venda foi alterado (linhas removidas/reordenadas)."
    return "**Novo/alterado nas regras:**\n" + "\n".join(f"• {l}" for l in added)[:3500]


# ---------- main ----------
def main():
    if not WEBHOOK and os.environ.get("GITHUB_ACTIONS"):
        sys.exit("DISCORD_WEBHOOK_URL secret is missing. Add it in Settings > Secrets and variables > Actions.")
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    old_games = state.get("games", {})
    first_run = "games" not in state

    try:
        games = [snapshot(g) for g in fetch_games()]
    except (urllib.error.URLError, RuntimeError, KeyError, TimeoutError) as e:
        print(f"Fetch failed: {e}", file=sys.stderr)
        fails = state.get("consecutive_failures", 0) + 1
        # Capped at 3 so a long outage doesn't create a commit every run.
        state["consecutive_failures"] = min(fails, 3)
        # Alert once after 3 consecutive failures (~15 min), not on every blip.
        if fails == 3:
            post([{"title": "⚠️ Watcher não consegue ler o site", "description": str(e)[:1000], "color": COLOR_BAD}])
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        return

    if state.get("consecutive_failures", 0) >= 3:
        post([{"title": "✅ Watcher voltou a funcionar", "description": "O site voltou a responder.", "color": COLOR_GOOD}])
    state["consecutive_failures"] = 0

    if MODALITIES:
        games = [g for g in games if g["modality"].lower() in MODALITIES]
    new_games = {str(g["id"]): g for g in games}
    embeds = []

    if first_run:
        desc = "\n".join(f"• [{g['title']}]({GAME_URL.format(token=g['token'])}) — {g['modality']}, "
                         f"{fmt_date(g['date'], g['isDateConfirmed'])}" for g in games) or "Sem jogos abertos."
        embeds.append({"title": "🦁 Watcher ligado — jogos atuais", "description": desc[:4000], "color": COLOR_NEW})
    else:
        for gid, g in new_games.items():
            o = old_games.get(gid)
            if o is None:
                status = "Esgotado" if g["soldOut"] else (g["disabledReasonDesc"] or "—")
                embeds.append(embed(g, "🆕 Novo jogo", f"Estado: {status}\nPúblico: {fmt_val(g['public'])}", COLOR_NEW))
                continue

            changes = []
            for f, label in FLAG_FIELDS.items():
                if o.get(f) != g.get(f):
                    changes.append(f"**{label}:** {fmt_val(o.get(f))} → {fmt_val(g.get(f))}")
            if o.get("date") != g.get("date") or o.get("isDateConfirmed") != g.get("isDateConfirmed"):
                changes.append(f"**Data:** {fmt_date(o['date'], o['isDateConfirmed'])} → "
                               f"{fmt_date(g['date'], g['isDateConfirmed'])}")
            if changes:
                if o.get("soldOut") is False and g.get("soldOut") is True:
                    title, color = "❌ Esgotado", COLOR_BAD
                elif o.get("soldOut") is True and g.get("soldOut") is False:
                    title, color = "🎟️ Bilhetes de volta", COLOR_GOOD
                elif (o.get("public"), g.get("public")) == (False, True):
                    title, color = "🎟️ Aberto ao público", COLOR_GOOD
                else:
                    title, color = "🔄 Alteração", COLOR_INFO
                embeds.append(embed(g, title, "\n".join(changes), color))

            if o.get("rules_hash") != g["rules_hash"]:
                embeds.append(embed(g, "📋 Regras de venda atualizadas", rules_diff(o.get("rules", ""), g["rules"]), COLOR_INFO))

        for gid, o in old_games.items():
            if gid not in new_games:
                embeds.append(embed(o, "👋 Saiu da lista", "O jogo deixou de aparecer no site (venda fechada ou jogo passou).", 0x95A5A6))

    if embeds:
        post(embeds, content=MENTION or None)
        print(f"Posted {len(embeds)} alert(s).")
    else:
        print("No changes.")

    state["games"] = new_games
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), "utf-8")


if __name__ == "__main__":
    main()

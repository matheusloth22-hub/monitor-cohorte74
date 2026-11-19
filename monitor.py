#!/usr/bin/env python3
"""Monitor FCM-UNR Egreso Medicina page for Cohorte 74 and alert via Telegram."""

import argparse
import datetime
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "monitor.log"

LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=-3))  # UTC-3 (Argentina / Brasilia)


def now_local() -> datetime.datetime:
    return datetime.datetime.now(LOCAL_TZ)


def log(msg: str) -> None:
    timestamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default)


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "false",
        }
    ).encode("utf-8")
    req = urllib.request.Request(api_url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            if body.get("ok"):
                return True
            log(f"Telegram API error: {body}")
            return False
    except Exception as exc:
        log(f"Failed to send Telegram message: {exc}")
        return False


def send_macos_notification(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    script = f'display notification "{message}" with title "{title}" sound name "Glass"'
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def fetch_page(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return resp.read().decode("utf-8", errors="replace")


def analyze_page(raw_html: str) -> tuple[bool, str, str]:
    """Return (changed, reason, snippet)."""
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.I | re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", cleaned))
    text = re.sub(r"\s+", " ", text).strip()

    if "egreso" not in text.lower() and "medicina" not in text.lower():
        raise RuntimeError("Page content looks incomplete or unexpected (missing expected keywords).")

    snippet_match = re.search(r".{0,40}cohorte.{0,40}", text, flags=re.I)
    snippet = snippet_match.group(0).strip() if snippet_match else "(trecho 'cohorte' não encontrado)"

    has_74 = bool(re.search(r"cohorte\s*74\b", text, flags=re.I))
    has_73 = bool(re.search(r"cohorte\s*73\b", text, flags=re.I))

    if has_74:
        return True, "Cohorte 74 detectada na página!", snippet
    if not has_73:
        return True, "Cohorte 73 saiu do ar / texto alterado!", snippet
    return False, "Ainda está em Cohorte 73.", snippet


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor FCM-UNR for Cohorte 74.")
    parser.add_argument("--test", action="store_true", help="Run a live check and send a test Telegram alert.")
    parser.add_argument("--reset", action="store_true", help="Reset saved state.")
    args = parser.parse_args()

    file_config = load_json(CONFIG_PATH, {})
    url = os.environ.get("TARGET_URL") or file_config.get("url") or "https://fcm.unr.edu.ar/egreso-medicina/"
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or file_config.get("bot_token", "")
    chat_id = str(os.environ.get("TELEGRAM_CHAT_ID") or file_config.get("chat_id", ""))
    start_date_str = os.environ.get("START_DATE") or file_config.get("start_date") or "2026-11-26"
    max_alerts = int(os.environ.get("MAX_ALERTS") or file_config.get("max_alerts") or 3)
    start_date = datetime.date.fromisoformat(start_date_str)

    if not bot_token or not chat_id:
        log("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID (set env vars or config.json).")
        return 1

    state = load_json(
        STATE_PATH,
        {"started_notified": False, "alerts_sent": 0},
    )

    if args.reset:
        state = {"started_notified": False, "alerts_sent": 0}
        save_json(STATE_PATH, state)
        log("State reset.")
        return 0

    today = now_local().date()

    if args.test:
        log("Running live test check...")
        try:
            raw_html = fetch_page(url)
            changed, reason, snippet = analyze_page(raw_html)
        except Exception as exc:
            log(f"Test failed fetching page: {exc}")
            return 1

        origin = "GitHub Actions (nuvem 24h)" if os.environ.get("GITHUB_ACTIONS") else "Mac local"
        msg = (
            f"✅ Teste do Monitor Cohorte 74 ({origin}) funcionando!\n\n"
            f"Status atual do site: {reason}\n"
            f"Trecho encontrado: \"{snippet}\"\n\n"
            f"📅 Início automático do monitoramento: {start_date.strftime('%d/%m/%Y')}\n"
            f"⏱ Frequência: a cada 5 minutos\n"
            f"🔗 {url}"
        )
        ok = send_telegram(bot_token, chat_id, msg)
        log(f"Test Telegram sent: {ok} | Status: {reason}")
        return 0 if ok else 1

    # Before start_date: stay dormant silently
    if today < start_date:
        log(f"Dormant until {start_date.isoformat()} (today is {today.isoformat()}).")
        return 0

    # Stop after max_alerts have been sent
    if state.get("alerts_sent", 0) >= max_alerts:
        log(f"Max alerts ({max_alerts}) already sent. Nothing to do.")
        return 0

    state_changed = False

    # Notify once when monitoring activates on start_date
    if not state.get("started_notified", False):
        start_msg = (
            "🟢 Monitoramento 24h ATIVADO!\n\n"
            f"A partir de agora ({today.strftime('%d/%m/%Y')}) estou verificando o site da FCM-UNR "
            "a cada 5 minutos. Assim que mudar para Cohorte 74 eu te aviso imediatamente!\n\n"
            f"🔗 {url}"
        )
        if send_telegram(bot_token, chat_id, start_msg):
            state["started_notified"] = True
            state_changed = True
            log("Sent activation notification to Telegram.")

    try:
        raw_html = fetch_page(url)
        changed, reason, snippet = analyze_page(raw_html)
    except Exception as exc:
        log(f"Check error (will retry next run): {exc}")
        if state_changed:
            save_json(STATE_PATH, state)
        return 0

    if changed:
        alert_num = state.get("alerts_sent", 0) + 1
        alert_msg = (
            f"🚨🚨 ALERTA COHORTE 74! ({alert_num}/{max_alerts}) 🚨🚨\n\n"
            f"{reason}\n"
            f"Trecho no site: \"{snippet}\"\n\n"
            f"Corre para enviar suas informações!\n"
            f"🔗 {url}"
        )
        sent = send_telegram(bot_token, chat_id, alert_msg)
        send_macos_notification("ALERTA COHORTE 74!", "O site da FCM-UNR mudou! Confira agora.")
        if sent:
            state["alerts_sent"] = alert_num
            state_changed = True
            log(f"CHANGE DETECTED! Alert {alert_num}/{max_alerts} sent. Snippet: {snippet}")
        else:
            log(f"CHANGE DETECTED, but Telegram send failed! Will retry next run. Snippet: {snippet}")
    else:
        log(f"OK - {reason}")

    if state_changed:
        save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
SmartInbox AI - MVP
Требуется: pip install imaplib2 python-dotenv scikit-learn requests
"""

import os
import re
import json
import imaplib
import email
from email.header import decode_header
from dataclasses import dataclass, asdict
from datetime import datetime
from dotenv import load_dotenv
import requests

load_dotenv()

# ---------- Конфигурация ----------
@dataclass
class EmailConfig:
    host: str = os.getenv("IMAP_HOST", "imap.gmail.com")
    user: str = os.getenv("IMAP_USER", "")
    password: str = os.getenv("IMAP_PASS", "")
    folder: str = "INBOX"

TELEGRAM_TOKEN = os.getenv("TG_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TG_CHAT", "")

# ---------- Правила классификации ----------
RULES = {
    "order":    [r"\b(заказ|order|куп(ил|лю)|оплат|invoice|счёт|checkout|sku)\b"],
    "complaint":[r"\b(жалоб|не работает|сломал|верните|refund|проблем|ужас|bad|broken)\b"],
    "question": [r"\b(как|how|вопрос|question|\?|подскаж|можно ли)\b"],
    "spam":     [r"\b(рассылк|уникальн|предложени|seo|крипт|заработ|bitcoin|casino)\b"],
}

URGENT_PATTERNS = [r"\b(срочно|asap|немедленно|today|сегодня|deadline|горит)\b"]

def classify(text: str) -> str:
    text_l = text.lower()
    scores = {cat: 0 for cat in RULES}
    for cat, patterns in RULES.items():
        for p in patterns:
            if re.search(p, text_l):
                scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"

def urgency(text: str) -> int:
    """0..10"""
    t = text.lower()
    score = sum(3 for p in URGENT_PATTERNS if re.search(p, t))
    if re.search(r"\b(заказ|оплат|invoice)\b", t):
        score += 4
    if "!!!" in text or text.isupper():
        score += 2
    return min(score, 10)

def sentiment(text: str) -> str:
    neg = len(re.findall(r"\b(плохо|ужас|ненавиж|bad|hate|broken|error|ошибк)", text.lower()))
    pos = len(re.findall(r"\b(спасибо|отлично|great|thanks|love|благодар)", text.lower()))
    if neg > pos: return "negative"
    if pos > neg: return "positive"
    return "neutral"

# ---------- Парсинг письма ----------
def decode_str(s):
    if not s: return ""
    parts = decode_header(s)
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(part)
    return "".join(out)

def extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
    except Exception:
        return ""

# ---------- Обработка одного письма ----------
def process_email(raw_bytes) -> dict:
    msg = email.message_from_bytes(raw_bytes)
    subject = decode_str(msg.get("Subject", ""))
    sender = decode_str(msg.get("From", ""))
    date = msg.get("Date", "")
    body = extract_body(msg)
    full = f"{subject}\n{body}"

    result = {
        "from": sender,
        "subject": subject,
        "date": date,
        "category": classify(full),
        "urgency": urgency(full),
        "sentiment": sentiment(body),
        "preview": re.sub(r"\s+", " ", body)[:200],
        "received_at": datetime.utcnow().isoformat(),
    }
    return result

# ---------- Отчёт ----------
def build_report(emails: list[dict]) -> str:
    by_cat = {}
    for e in emails:
        by_cat.setdefault(e["category"], []).append(e)

    lines = [f"📊 *Отчёт SmartInbox* ({datetime.utcnow():%d.%m %H:%M})",
             f"Всего писем: {len(emails)}\n"]

    emoji = {"order":"🛒","complaint":"😡","question":"❓","spam":"🚫","other":"📩"}
    for cat, items in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        lines.append(f"{emoji.get(cat,'📩')} *{cat}*: {len(items)}")

    urgent = [e for e in emails if e["urgency"] >= 5]
    if urgent:
        lines.append(f"\n🔥 *Срочных: {len(urgent)}*")
        for e in urgent[:5]:
            lines.append(f"• {e['subject'][:60]} — от {e['from'][:40]}")

    negative = [e for e in emails if e["sentiment"] == "negative"]
    if negative:
        lines.append(f"\n⚠️ *Негатив: {len(negative)}* — требуют внимания")

    return "\n".join(lines)

def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("⚠️ Telegram не настроен\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "Markdown"}, timeout=10)

# ---------- Основной цикл ----------
def fetch_unseen(cfg: EmailConfig, limit: int = 50) -> list[dict]:
    results = []
    with imaplib.IMAP4_SSL(cfg.host) as mail:
        mail.login(cfg.user, cfg.password)
        mail.select(cfg.folder)
        _, data = mail.search(None, "UNSEEN")
        ids = data[0].split()[-limit:]
        for eid in ids:
            _, msg_data = mail.fetch(eid, "(RFC822)")
            for part in msg_data:
                if isinstance(part, tuple):
                    results.append(process_email(part[1]))
    return results

def main():
    cfg = EmailConfig()
    if not cfg.user or not cfg.password:
        print("❌ Укажите IMAP_USER и IMAP_PASS в .env")
        return

    emails = fetch_unseen(cfg)
    if not emails:
        print("Новых писем нет")
        return

    report = build_report(emails)
    print(report)
    send_telegram(report)

    # Сохраняем в JSON для CRM/дашборда
    with open("inbox_report.json", "a", encoding="utf-8") as f:
        for e in emails:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    main()

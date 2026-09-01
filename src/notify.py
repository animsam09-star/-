"""텔레그램 알림. TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 시 건너뛴다."""

from __future__ import annotations

import os

import requests

MAX_LEN = 4000  # 텔레그램 메시지 한도(4096) 대비 여유


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("텔레그램 미설정 — 알림을 건너뜁니다.")
        return False

    chunks = []
    while text:
        if len(text) <= MAX_LEN:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, MAX_LEN)
        cut = cut if cut > 0 else MAX_LEN
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")

    for chunk in chunks:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if not resp.ok:
            print(f"텔레그램 전송 실패: {resp.status_code} {resp.text[:200]}")
            return False
    print("텔레그램 전송 완료")
    return True

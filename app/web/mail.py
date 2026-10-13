"""Отправка писем по SMTP (вход по одноразовой ссылке).

Без SMTP_HOST письма не уходят: ссылка пишется в лог — так удобно разрабатывать локально.
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from app.core.config import get_settings

log = logging.getLogger("sverka.mail")


def mail_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host and s.smtp_from)


def _send(message: EmailMessage) -> None:
    s = get_settings()
    context = ssl.create_default_context()
    if s.smtp_port == 465:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=context, timeout=20) as smtp:
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            smtp.starttls(context=context)
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(message)


async def send_mail(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """True — письмо отправлено; False — почта не настроена (содержимое ушло в лог)."""
    if not mail_configured():
        log.warning("SMTP не настроен; письмо для %s не отправлено:\n%s", to, text)
        return False
    name, address = parseaddr(get_settings().smtp_from)
    message = EmailMessage()
    message["From"] = formataddr((name or "Сверка", address))
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    await asyncio.to_thread(_send, message)
    return True

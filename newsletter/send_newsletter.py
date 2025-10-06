#!/usr/bin/env python3
"""
Simple CLI to render and (optionally) send the HTML newsletter.

- Replaces [PLACEHOLDER] tokens in the HTML with values from a JSON data file
- Generates a local preview HTML file
- Sends individual emails to recipients from a CSV using SMTP credentials

Example (preview only):
  python3 newsletter/send_newsletter.py \
    --template newsletter/templates/oct-2025.html \
    --data newsletter/data.json \
    --subject "Cleeri Newsletter — Oct 2025" \
    --from-name "Cleeri Team" \
    --from-email "hello@cleeri.com" \
    --mode preview \
    --output newsletter/output/preview.html

Example (send):
  SMTP_HOST=smtp.example.com SMTP_PORT=587 SMTP_USERNAME=... SMTP_PASSWORD=... \
  python3 newsletter/send_newsletter.py \
    --template newsletter/templates/oct-2025.html \
    --data newsletter/data.json \
    --subject "Cleeri Newsletter — Oct 2025" \
    --from-name "Cleeri Team" \
    --from-email "hello@cleeri.com" \
    --recipients newsletter/recipients.csv \
    --mode send
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, Iterable, Tuple
import html as html_unescape


PLACEHOLDER_PATTERN = re.compile(r"\[([A-Z0-9_]+)\]")


def read_text_file(path: Path) -> str:
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(content)


def load_json(path: Path) -> Dict[str, str]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Data JSON must be an object of key/value pairs")
        # Coerce everything to string for templating simplicity
        return {str(k): "" if v is None else str(v) for k, v in data.items()}
    except FileNotFoundError:
        raise SystemExit(f"Data file not found: {path}")


def render_template_with_placeholders(html: str, variables: Dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return variables.get(key, "")

    return PLACEHOLDER_PATTERN.sub(replace, html)


# Very basic plaintext extraction for a fallback part
TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"\s+")


def html_to_plain_text(html: str) -> str:
    without_tags = TAG_PATTERN.sub(" ", html)
    unescaped = html_unescape.unescape(without_tags)
    collapsed = WHITESPACE_PATTERN.sub(" ", unescaped)
    return collapsed.strip()


@dataclass
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str

    @staticmethod
    def from_env() -> "SmtpConfig":
        try:
            host = os.environ["SMTP_HOST"]
            port = int(os.environ.get("SMTP_PORT", "587"))
            username = os.environ["SMTP_USERNAME"]
            password = os.environ["SMTP_PASSWORD"]
            return SmtpConfig(host=host, port=port, username=username, password=password)
        except KeyError as e:
            missing = e.args[0]
            raise SystemExit(
                f"Missing SMTP environment variable: {missing}. Set SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD."
            )


@dataclass
class SenderIdentity:
    name: str
    email: str


def read_recipients_csv(path: Path) -> Iterable[Tuple[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if "email" not in reader.fieldnames:
                raise SystemExit("Recipients CSV must include an 'email' column")
            name_field = "name" if reader.fieldnames and "name" in reader.fieldnames else None
            for row in reader:
                email = (row.get("email") or "").strip()
                name = (row.get(name_field) or "").strip() if name_field else ""
                if email:
                    yield (email, name)
    except FileNotFoundError:
        raise SystemExit(f"Recipients file not found: {path}")


class BulkMailer:
    def __init__(self, config: SmtpConfig):
        self._config = config
        self._server: smtplib.SMTP | None = None

    def __enter__(self) -> "BulkMailer":
        context = ssl.create_default_context()
        server = smtplib.SMTP(self._config.host, self._config.port)
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(self._config.username, self._config.password)
        self._server = server
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._server is not None:
                self._server.quit()
        finally:
            self._server = None

    def send_one(self, sender: SenderIdentity, recipient_email: str, recipient_name: str, subject: str, html_body: str, text_body: str) -> None:
        if self._server is None:
            raise RuntimeError("SMTP connection is not open")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{sender.name} <{sender.email}>"
        msg["To"] = recipient_email

        # Text then HTML per RFC 2046
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        self._server.sendmail(sender.email, [recipient_email], msg.as_string())


def main() -> None:
    parser = argparse.ArgumentParser(description="Render and send the newsletter")
    parser.add_argument("--template", required=True, help="Path to HTML template with [TOKENS]")
    parser.add_argument("--data", required=True, help="Path to JSON with replacements")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--from-name", required=True, help="Sender name")
    parser.add_argument("--from-email", required=True, help="Sender email address")
    parser.add_argument("--recipients", default="newsletter/recipients.csv", help="CSV with columns: email,name")
    parser.add_argument("--mode", choices=["preview", "send"], default="preview", help="preview writes file; send emails")
    parser.add_argument("--output", default="newsletter/output/preview.html", help="Preview output HTML path")

    args = parser.parse_args()

    template_path = Path(args.template)
    data_path = Path(args.data)
    recipients_path = Path(args.recipients)
    output_path = Path(args.output)

    sender = SenderIdentity(name=args.from_name, email=args.from_email)

    # Load and render HTML
    html = read_text_file(template_path)
    variables = load_json(data_path)
    rendered_html = render_template_with_placeholders(html, variables)

    # Always produce a preview artifact for inspection
    write_text_file(output_path, rendered_html)

    # Plain text fallback
    text_fallback = html_to_plain_text(rendered_html)

    if args.mode == "preview":
        print(f"Preview written to: {output_path}")
        return

    # Sending mode
    smtp = SmtpConfig.from_env()
    total = 0
    with BulkMailer(smtp) as mailer:
        for recipient_email, recipient_name in read_recipients_csv(recipients_path):
            mailer.send_one(
                sender=sender,
                recipient_email=recipient_email,
                recipient_name=recipient_name,
                subject=args.subject,
                html_body=rendered_html,
                text_body=text_fallback,
            )
            total += 1
            if total % 10 == 0:
                print(f"Sent {total} emails...")

    print(f"Done. Sent {total} emails.")


if __name__ == "__main__":
    main()

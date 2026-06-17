import imaplib, email, os
from dotenv import load_dotenv
load_dotenv()

imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
imap.login(os.getenv("EMAIL_ADDRESS"), os.getenv("EMAIL_APP_PASSWORD"))
imap.select("INBOX")
_, ids = imap.search(None, f'(FROM "{os.getenv("PSS_EMAIL_SENDER")}")')
ids = ids[0].split()
if ids:
    _, data = imap.fetch(ids[-1], "(RFC822)")
    msg = email.message_from_bytes(data[0][1])
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
            with open("/tmp/pss_newsletter.html", "w") as f:
                f.write(html)
            print(f"HTML guardado: {len(html)} chars")
            break
imap.logout()

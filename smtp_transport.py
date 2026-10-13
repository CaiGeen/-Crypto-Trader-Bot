"""QQ SMTP transport: TLS is mandatory before credentials or message submission."""
import smtplib
import ssl


def send_qq_mail(user: str, auth_code: str, recipient: str, message: str,
                 *, timeout: float = 10) -> None:
    """One send attempt; raise on failure. Never downgrade or automatically retry.

    Successful return means SMTP accepted the message, not inbox delivery.
    Event policy, composition, threading and incident budgets belong to callers.
    """
    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.qq.com", 587, timeout=timeout) as server:
        status, response = server.ehlo()
        if status != 250:
            raise smtplib.SMTPHeloError(status, response)
        if not server.has_extn("starttls"):
            raise smtplib.SMTPNotSupportedError("STARTTLS is required; refusing plaintext authentication")
        server.starttls(context=context)
        # STARTTLS discards pre-TLS capabilities; require a fresh successful EHLO.
        status, response = server.ehlo()
        if status != 250:
            raise smtplib.SMTPHeloError(status, response)
        server.login(user, auth_code)
        refused = server.sendmail(user, [recipient], message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)

"""PII redaction helpers for log output.

We log enough context to debug (last 4 digits of phone, first letter of name)
but never the full PII. Under DPDP Act, full phone numbers in logs without a
documented retention policy are non-compliant.
"""


def redact_phone(phone: str | None) -> str:
    """+919876543210 -> +91*****3210."""
    if not phone:
        return "<none>"
    if len(phone) <= 4:
        return "****"
    return phone[:3] + "*" * (len(phone) - 7) + phone[-4:]


def redact_name(name: str | None) -> str:
    """Rishi Raturi -> R*** R*."""
    if not name:
        return "<none>"
    parts = name.split()
    return " ".join(p[0] + "*" * max(len(p) - 1, 1) for p in parts)

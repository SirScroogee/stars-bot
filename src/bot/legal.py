"""Links to legal documents shown to users."""

PRIVACY_POLICY_URL = "https://telegra.ph/Politika-konfidencialnosti-04-01-26"
USER_AGREEMENT_URL = "https://telegra.ph/Polzovatelskoe-soglashenie-04-01-19"


def get_legal_links_text(lang: str) -> str:
    """Return legal document links as plain message lines."""
    if lang == "en":
        return (
            f'<a href="{USER_AGREEMENT_URL}">User Agreement</a>\n'
            f'<a href="{PRIVACY_POLICY_URL}">Privacy Policy</a>'
        )

    return (
        f'<a href="{USER_AGREEMENT_URL}">Пользовательское соглашение</a>\n'
        f'<a href="{PRIVACY_POLICY_URL}">Политика конфиденциальности</a>'
    )

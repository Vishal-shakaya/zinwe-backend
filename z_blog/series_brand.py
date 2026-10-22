"""
How each newsletter series presents itself in email and in the numbers: its campaign code (UTM
`utm_campaign`, and the prefix of `User.signup_source`, e.g. "tlf:ep07:email"), its episode-email template
and the few lines of copy that differ between series. The frontend keeps the same codes (lib/seriesVoice.ts).

A series not listed here gets a code made from its slug's initials and the default episode email.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Series


@dataclass(frozen=True)
class SeriesBrand:
    code: str
    template: str = "newsletter_episode"
    # The confirmation email: "It opens our members-only series, <title>: <blurb>"
    blurb: str = "short stories, each with a 10-minute fix."
    pace: str = ""  # the first email's "one episode ..." line
    reply_prompt: str = ""
    finale: str = ""
    disclaimer: str = ""
    # SAFE BY DESIGN: every episode has a kid-facing Family Card, a public no-tracking page at /family/NN that the
    # parent can send on (WhatsApp, email, link). The email builds those share links (newsletter._family_card).
    family_cards: bool = False


BRANDS = {
    "the-last-file": SeriesBrand(
        code="tlf",
        blurb=(
            "short business stories about the small mistakes that put company data at risk, each with a 10-minute "
            "fix and the case-file downloads."
        ),
    ),
    "identity-under-attack": SeriesBrand(
        code="iua",
        template="newsletter_episode_noir",
        blurb=(
            "short stories about how identities get impersonated and taken over, each with a 10-minute protective "
            "step and a printable action card."
        ),
        pace="One episode every Wednesday morning, in order.",
        reply_prompt=(
            "Tried this week's action? Reply and tell us how it went. We read every reply. Never include passwords, "
            "codes or account details, to us or to anyone."
        ),
        finale="That's the final episode. Your Identity Check, every action from the series in one PDF, is on the finale page.",
        disclaimer=(
            "Characters and incidents are fictional composites written for teaching. We will never ask you for a "
            "password, a one-time code or a recovery code."
        ),
    ),
    "safe-by-design": SeriesBrand(
        code="sbd",
        template="newsletter_episode_family",
        blurb=(
            "30 family stories about growing up online, each with one thing to try together and a Family Card to "
            "share with your child."
        ),
        pace="One episode every Saturday morning, in order.",
        reply_prompt=(
            "Tried this week's family action? Reply and tell us how it went. We read every reply. Please never send "
            "us your child's name, school, photos, usernames, passwords or location."
        ),
        finale="That's the final episode. Your printable Family Shield, all 30 badges in one place, is on the finale page.",
        disclaimer=(
            "The family and situations are fictional composites written for teaching. If a child is in immediate "
            "danger, contact local emergency services (112 in India, 911 in the US)."
        ),
        family_cards=True,
    ),
}


def brand(series: Series) -> SeriesBrand:
    known = BRANDS.get(series.slug)
    if known is not None:
        return known
    initials = "".join(w[0] for w in series.slug.split("-") if w)[:8] or "series"
    return SeriesBrand(code=initials)


def code_of(series: Series) -> str:
    return brand(series).code

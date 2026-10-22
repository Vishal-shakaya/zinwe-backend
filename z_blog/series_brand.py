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
}


def brand(series: Series) -> SeriesBrand:
    known = BRANDS.get(series.slug)
    if known is not None:
        return known
    initials = "".join(w[0] for w in series.slug.split("-") if w)[:8] or "series"
    return SeriesBrand(code=initials)


def code_of(series: Series) -> str:
    return brand(series).code

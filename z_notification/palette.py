"""The colours transactional emails may use (v1.3.2 owner rule, 31 Oct: white, black, orange only).

Email clients need literal hex inline (no CSS variables), so the templates carry these values as literals;
`tests.EmailPaletteTests` fails if a transactional template uses anything else. The newsletter content
emails (newsletter_episode*, newsletter_issue) are exempt: the dark Newsletter has its own look.
"""

EMAIL_PALETTE = {
    "ground": "#ffffff",      # page and card ground: pure white, no off-white wrapper
    "surface_2": "#f5f5f5",   # soft neutral fill: detail tables, quoted message
    "line": "#e5e5e5",        # card hairline, table rules
    "line_2": "#d4d4d4",      # control borders (rating buttons)
    "ink": "#141210",         # text, and the text on an orange button (white on orange is 3.07:1)
    "ink_2": "#404040",       # secondary text
    "ink_3": "#6b6b6b",       # muted labels, footer (5.3:1 on white)
    "accent": "#f26a0a",      # button fill, left rules
    "accent_text": "#a84505", # orange links and labels on white (6.0:1)
    "accent_tint": "#fdebdc", # notice boxes (was amber)
    "danger": "#c0331c",      # urgent / critical only, always with the word "Urgent"
}

# The text fallback of the official logo (_brand.html) keeps the logo's own colours.
LOGO_COLOURS = {"#fd6d02", "#2b2b2a"}

EXEMPT_TEMPLATES = {"newsletter_episode", "newsletter_episode_family", "newsletter_episode_noir", "newsletter_issue"}

"""
The form types the website sends us. Each one becomes a tab in the admin app's Forms page.

Adding a form: give it a slug here (the label staff see), then call
`z_forms.services.record_submission("<slug>", ...)` from the view that receives it. A slug that is
missing here still works: its tab is labelled from the slug ("pricing_question" -> "Pricing question").
"""
from __future__ import annotations

CONTACT = "contact"
SETUP_CALL = "setup_call"

# Order = tab order in the admin app.
FORM_KINDS: dict[str, str] = {
    CONTACT: "Contact",
    SETUP_CALL: "Setup call",
}


def label_for(kind: str) -> str:
    return FORM_KINDS.get(kind) or kind.replace("_", " ").replace("-", " ").strip().capitalize() or "Form"

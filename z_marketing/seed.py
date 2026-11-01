"""The first audience and campaign (migration 0002). No contacts: those are hand-picked in the admin."""
from __future__ import annotations

_LINK = "https://zinwe.io/?utm_source=cold-email&utm_medium=email&utm_campaign=2026-11-us-cpa"

AUDIENCE = {
    "name": "Texas CPA firms",
    "country": "USA",
    "decider": "Managing partner of a 5–15 person firm",
    "setup": "Office manager",
    "today": "Trusts staff and keeps files on a shared drive",
    "confidence": "Guess",
    "segments": ["CPA and accounting firms"],
    "triggers": ["A senior accountant just resigned", "A client asked how their files are protected"],
    "words": ["Client files", "Tax season"],
    "objections": ["Is this spying on my staff?", "We have no time during tax season", "We already have antivirus"],
    "not_for": ["Firms with their own IT team"],
    "problems": ["Client files leaving with staff", "Client files copied to USB drives"],
    "solutions": ["Alerts when files are copied to a USB drive", "Allow, read-only or block for USB drives"],
    "offers": ["One-PC USB test for client files", "Free forever plan"],
    "channels": ["LinkedIn", "Email"],
    "summary": (
        "This audience covers CPA and accounting firms in the USA. The person who decides is managing partner of a "
        "5–15 person firm, and office manager sets it up. Best moments to reach them: a senior accountant just "
        "resigned and a client asked how their files are protected. Their main problems are client files leaving "
        "with staff and client files copied to USB drives. Zinwe offers alerts when files are copied to a USB drive "
        "and allow, read-only or block for USB drives. Best offers: One-PC USB test for client files and Free "
        "forever plan. Reach them first on LinkedIn, then on Email."
    ),
}

CAMPAIGN = {
    "name": "US CPA launch",
    "goal": "Get free plan signups",
    "theme": "The Last File",
    "problem": "Client files leaving with staff",
    "offer": "One-PC USB test for client files",
}

EMAILS = [
    (1, "Cold", "Day 0", 0, "When staff leave, where do client files go?",
     "Hi {{FirstName}},\n\nWhen someone leaves a small firm like {{Firm}}, client files can leave with them, usually "
     "with no bad intent. A folder copied to a USB drive for work at home can stay there for years.\n\nZinwe alerts you "
     "in plain English when files are copied to a USB drive or sent out in large transfers. It runs on Windows PCs, and "
     "your team can see it's there.\n\nThere's a Free forever plan, so you can try it on a few PCs and only upgrade if "
     f"it's useful:\n{_LINK}\n\nVishal Shakaya\nFounder, Zinwe (SKYTECHNO)"),
    (2, "Cold", "Day 4–5", 0, "Re: When staff leave, where do client files go?",
     "Hi {{FirstName}},\n\nOne quick way to test Zinwe is to install it on a single PC and plug in a USB drive. You'll "
     f"see the alert it sends and can decide from there.\n\nHere's the link again:\n{_LINK}\n\nReply \"yes\" and I'll "
     "send the setup steps, then one practical tip about once a week for 3 months. If I don't hear from you, this is my "
     "last email.\n\nVishal"),
    (3, "Setup", "On yes", 1, "Here's how to start",
     "Hi {{FirstName}},\n\nThanks for saying yes. Here's the short version:\n\n1. Create your account on the Free "
     "forever plan.\n2. Install Zinwe on one PC.\n3. Tell the person using that PC what it does.\n\nStart here:\n"
     f"{_LINK}\n\nReply if you get stuck, and I'll help.\n\nVishal"),
    (4, "Setup", "Week 2", 2, "Install Zinwe on one PC first",
     "Hi {{FirstName}},\n\nYou don't need to roll Zinwe out across {{Firm}} to see if it's useful. One Windows PC is "
     "enough.\n\nPick a PC, tell the person who uses it, and install the agent. They'll see the Zinwe widget and agree "
     "to a notice on their own PC.\n\nNext week, I'll share a quick test.\n\nVishal"),
    (5, "Setup", "Week 3", 3, "Try this 2-minute USB test",
     "Hi {{FirstName}},\n\nOn your test PC, plug in a USB drive. Then open Zinwe and check Alerts.\n\nYou should see, "
     "in plain English, what happened and on which PC. That's the alert you'd get if files were being copied out.\n\n"
     "[Signed-up contacts only: If you'd like the remaining tips, reply \"yes\". If not, this is my last email.]\n\n"
     "Vishal"),
    (6, "Tips", "Week 4", 4, "Which USB drives should your team use?",
     "Hi {{FirstName}},\n\nMost small firms have never decided which USB drives are OK at work.\n\nA simple rule: allow "
     "the drives you bought, make others read-only, and block the rest where it makes sense. Zinwe lets you set allow, "
     "read-only or block.\n\nStart with the rule your team can actually follow.\n\nVishal"),
    (7, "Tips", "Week 5", 5, "When a stranger asks for AnyDesk",
     "Hi {{FirstName}},\n\n\"Hi, I'm from support. Can you install AnyDesk?\" A caller who sounds helpful can end up "
     "with a view of a client's file.\n\nGive your team one rule: never allow remote access unless you asked for the "
     "help yourself.\n\nZinwe also alerts you when remote-access tools like AnyDesk or TeamViewer are in use.\n\nVishal"),
    (8, "Tips", "Week 6", 6, "Large file transfers worth a second look",
     "Hi {{FirstName}},\n\nA big transfer is often normal work. The useful question is whether it matches what that "
     "person was doing.\n\nZinwe flags large transfers by USB, shared folders and email attachments, so you can ask a "
     "calm question early instead of guessing later.\n\nVishal"),
    (9, "Tips", "Week 7", 7, "What software is on your PCs?",
     "Hi {{FirstName}},\n\nIf someone asked what software is installed across {{Firm}}, could you answer?\n\nEven a "
     "short list helps. Zinwe alerts you when new software appears on a PC, so that list stays current and surprises "
     "are easy to check.\n\nVishal"),
    (10, "Tips", "Week 8", 8, "See your team's day at a glance",
     "Hi {{FirstName}},\n\nData safety is only part of it. Zinwe also shows the working day.\n\nEach person logs in, "
     "takes a break with a reason, comes back and logs out from the widget on their PC. You see the day per person and "
     "can approve or decline breaks.\n\nVishal"),
    (11, "Tips", "Week 10", 10, "Keep instructions out of group chats",
     "Hi {{FirstName}},\n\nInstructions get lost in group chats and phone calls.\n\nZinwe has owner-to-employee chat on "
     "the PC widget, with ready-made notes like \"Task for today\" and \"Call me\". You can see when a message is sent, "
     "delivered and seen.\n\nVishal"),
    (12, "Final", "Week 12", 12, "Should I keep sending these?",
     "Hi {{FirstName}},\n\nThat's the last of these tips.\n\nIf Zinwe is now part of your day and you need more PCs or "
     "longer history, the plans are here: https://zinwe.io/plans\n\nIf you'd just like occasional articles, join the "
     "newsletter: https://zinwe.io/blog\n\nYou'll get a confirmation email first. If you do neither, I won't email you "
     "again.\n\nThanks for reading,\nVishal"),
]

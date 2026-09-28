"""Welcome for a priest who joined because his number is on the
RingCentral ring (added by hand there): tell him he is being added, then
walk him through day off, day of recollection and vacation, each of
which he may skip (or SKIP ALL). The replies are handled in
app/signal_bot.py (_handle_welcome_setup), pending type "welcome_setup".
"""
from __future__ import annotations

from typing import Any

from app.rotation import RotationManager
from app.signal_client import SignalClient

WELCOME_SETUP = "welcome_setup"
LATER_NOTE = (
    "You can set or change your day off, day of recollection and vacation any time "
    "under SETTINGS > 5 Availability. Text HELP to see everything the bot can do."
)
WELCOME_BACK_TEXT = (
    "Welcome back to the Sacramental Emergency Line bot. Your old day off, day of "
    "recollection and vacation were restored. " + LATER_NOTE
)

STEP_PROMPTS = {
    "day_off": "1 of 3 - Day off: which day of the week? (e.g. Monday, Mon, M) Reply SKIP if you don't have one.",
    "recollection": (
        "2 of 3 - Day of recollection: which Wednesday of the month? Reply 1, 2, 3, 4 or 5, "
        "or SKIP if you don't have one."
    ),
    "vacation": (
        "3 of 3 - Vacation: any upcoming dates away? Reply with start and end dates as "
        "MM/DD-MM/DD (e.g. 08/20-08/27), or SKIP."
    ),
}
NEXT_STEP = {"day_off": "recollection", "recollection": "vacation", "vacation": None}


def welcome_text(name: str) -> str:
    greeting = f"Welcome, {name}!" if name and not name.startswith("+") else "Welcome!"
    return (
        f"{greeting} Your name and number are on the Sacramental Emergency Line's ring list "
        "in RingCentral, so you are being added to this system. You can now text this bot, "
        "and you'll get its notifications.\n\n"
        "Let's set up your availability with 3 quick questions. Reply SKIP to skip a "
        "question, or SKIP ALL to skip the whole setup. " + LATER_NOTE + "\n\n"
        + STEP_PROMPTS["day_off"]
    )


def start_welcome_setup(rotation: RotationManager, signal_client: SignalClient, priest: dict[str, Any]) -> None:
    rotation.set_pending_confirmation(priest["id"], {"type": WELCOME_SETUP, "step": "day_off"})
    signal_client.send([priest["cell_number"]], welcome_text(priest.get("name", "")))

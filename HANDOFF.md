# Sacramental Emergency Line Rotation

A plain-language guide to the parish on-call phone system: what it does, how to text it, when it acts on its own, and when it stops.

Written for the priests who use it. Technical notes for the host machine are at the end.

---

## TL;DR

Three priests share one Sacramental Emergency Line. The app keeps a saved ring order and tells RingCentral who to call first, second, and third. Each phone rings 4 times, then the next priest. Voicemail is the shared extension box. The app never touches voicemail.

**Clock:** everything is California time. A day off, recollection, or vacation starts at **8:00 PM the evening before** and ends at **8:00 PM on the listed day**. After 8:00 PM the system already treats tomorrow as “today.”

**Text the bot in Signal Messenger** (not SMS) at the bot's number (`SIGNAL_BOT_NUMBER` in `.env`; not published here).  
Only numbers on the priest roster can command it. Adding a priest authorizes that Signal number; removing a priest takes it off.

**Dashboard:** a password-protected web page on the private network (not the public internet). One admin login, not the RingCentral login.

**Automation is currently OFF** (has been since 13 Aug 2026). While it's off, day off, recollection, and vacation are all ignored — every priest stays on the live ring regardless of his schedule, and the descriptions below of what automation *would* do are on hold until someone texts `ENABLE`. When it's on: it takes people off the live ring for day off, recollection, and vacation, and puts them back when those end. It will not leave the line empty. If switching fails, it turns itself off, puts everyone back on in the app, and texts **everyone** to update RingCentral by hand until someone texts `ENABLE`.

You can still use `ROTATE` or the dashboard regardless of automation. If someone reorders the ring by hand in RingCentral, the Monday audit **accepts that lineup** as the starting order, tests that commanded switches still work, then puts that same lineup back. A hand edit is not a failure. (The Monday audit itself is also on hold while automation is off — it only runs when automation is on.)

**Right now (24 Sep 2026)**

| Priest | Saved order | Day off | Recollection | Vacation | On the line now | Routine texts |
|---|---|---|---|---|---|---|
| Fr Youngtrad FSSP | #1 | Monday | 4th Wednesday | — | active | muted |
| Fr Bugnini SSPX | #2 | — | 2nd Wednesday | 11–13 Oct 2026 | active | muted |
| Fr James Martin SJ | #3 | Tuesday | 3rd Wednesday | 19–23 Oct 2026 | active | unmuted |

**Visits are not counted (since 24 Sep 2026).** There are no year totals, no anointing log, and no automatic "ready to rotate?" question. The order changes only when someone texts `ROTATE` (or uses the dashboard). A text like `2 UC Davis` gets the reply “Visits are no longer tracked. To change who's first, text ROTATE.”

All three show active because automation is off — that overrides everyone's day off, recollection, and vacation alike, including the upcoming trips. Saved and live are the same: Youngtrad → Bugnini → Martin. `STATUS` shows `(inactive)` after a priest's name only when he is off the live ring, which can't currently happen to anyone with automation off. Youngtrad and Bugnini have routine broadcasts muted (e.g. quiet schedule-shift notices) — they still fully participate in the ring and get everything else (STATUS replies, failsafe alerts, cover prompts, and the "order changed" text after a rotate); only unsolicited routine texts are suppressed for them.

Once `ENABLE` is texted, automation resumes exactly as configured: on **Mondays**, Youngtrad would be off; on **Tuesdays**, Martin. **11–13 Oct 2026**, Bugnini would be away, which would suspend Youngtrad's Monday (12 Oct) and Martin's Tuesday (13 Oct) day-offs that week by default (a peer's vacation overrides everyone else's day off unless they explicitly move or skip it) — both would get a text two days before asking whether to skip or move that week's day off. **19–23 Oct 2026**, Martin would be away, which would suspend Youngtrad's Monday (19 Oct) the same way. The app never lets a change like this leave nobody on the line.

---

## What this system is

The Sacramental Emergency Line is one RingCentral number. Callers are offered the priests **in order**. The app’s job is:

1. Remember the saved order (who is first, second, third).
2. Remember each priest’s day off, monthly recollection, and vacation.
3. Push the **live** order to RingCentral (saved order minus anyone who should be off right now).
4. Let the priests command a bot **in Signal Messenger** to rotate and change schedules.
5. Offer a password-protected web page on the private network for the same work.

It does **not**:

- Answer the phone.
- Change voicemail (one shared box; everyone can listen).
- Invent a new priest list (you add/remove people).
- Count visits or anointings (removed 24 Sep 2026).
- Rotate by itself. Only `ROTATE` (or the dashboard) changes the saved order.

---

## Two orders: saved vs live

**Saved order** is the rotation itself: Youngtrad, Bugnini, Martin. This is what `STATUS` and the dashboard list. Everyone is in this list whether they are on or off. Inactive priests are marked `(inactive)` right after their year total.

**Live order** is who RingCentral actually rings right now. If a priest is inactive (day off, recollection, vacation, or manual disable), the phone **skips him and goes to the next active priest**. Being off does not freeze his place in the saved order.

Example (Wednesday, before 8:00 PM):

- Saved: Youngtrad → Bugnini → Martin  
- Bugnini is on 2nd-Wednesday recollection  
- Live: Youngtrad → Martin (Bugnini is skipped)  
- `STATUS` would show `2. Fr Bugnini SSPX (3) (inactive)`

After 8:00 PM Wednesday that recollection ends and Bugnini is active again in whatever saved seat he has.

**Mondays (Sunday 8:00 PM through Monday 8:00 PM):**  
Bugnini and Youngtrad are both off. Martin’s day off is Tuesday, so he is on. If Bugnini or Youngtrad is saved #1, they stay #1 on paper but the line skips them and rings Martin. Martin always has Mondays unless he is away. If Martin is away that Monday, the last remaining priest stays on so the line is not empty.

A manual `ROTATE` moves saved #1 to the back. Everyone shifts, including priests who are off. They stay off the phone until 8:00 PM, then they take whatever seat they landed in (including #1). See [Manual rotate while automation is on](#manual-rotate-while-automation-is-on).

---

## The 8:00 PM handoff

Every automatic absence uses the same window:

- Starts at **8:00 PM the evening before** the listed day  
- Ends at **8:00 PM on the listed day**

Examples:

- Monday day off = Sunday 8:00 PM through Monday 8:00 PM  
- Tuesday day off = Monday 8:00 PM through Tuesday 8:00 PM  
- 2nd-Wednesday recollection = Tuesday 8:00 PM through Wednesday 8:00 PM  
- Vacation 24–28 Aug = Sunday 23 Aug 8:00 PM through Friday 28 Aug 8:00 PM  

After 8:00 PM, the app already uses tomorrow’s calendar for “who is off.”

---

## The priests and how to reach the system

All bot commands are **Signal Messenger** messages. Ordinary SMS to the bot number will not work.

### Who may text the bot

The roster **is** the allowlist. Only cell numbers on the active priest list can command the bot. Anyone else gets “this number isn’t authorized.”

- Adding a priest (Settings or dashboard) puts that Signal number on the allowlist immediately.  
- Removing a priest takes that number off. They can no longer text the bot.

The real roster numbers live only in `config/priests.yaml` on the server, and the bot's own Signal number is `SIGNAL_BOT_NUMBER` in `.env`. Neither is kept in this guide or in the code repository.

### Mute vs on the line

Mute only stops **routine broadcast** texts. It does not take anyone off the phone line. A muted priest can still text the bot in Signal and get a reply. Failsafe alerts, cover prompts, “change another priest’s schedule,” and **rotation-complete** texts (after someone actually rotates) go to every priest with a cell number, muted or not.

Right now **Fr Youngtrad FSSP and Fr Bugnini SSPX are muted**. Only Fr James Martin SJ gets routine texts.

### Dashboard

Password-protected page on the private network. Same rotation, availability, mute, and history. Do not reuse the RingCentral login for it.

---

## How a caller is offered

RingCentral “My Work Day” rule, ring-in-order, **4 rings** each, then the next priest. If nobody answers, the shared voicemail on the emergency-line extension takes the message. The app never writes voicemail.

When automation is on, RingCentral is updated automatically whenever the live order changes (someone’s day off starts, a rotate happens, ENABLE is texted, and so on).

---

## When the order rotates

Only when someone texts `ROTATE`, uses `SETTINGS` → 5 **Set order** to put the priests in any order, or changes it on the dashboard. Nothing is counted and nothing rotates on its own. After a rotate, **everyone** gets a text that the order changed (new #1: “You are now on call”; the others: “The priest on call has changed”), plus STATUS.

---

## Manual rotate while automation is on

Text `ROTATE` or use the dashboard “Rotate now” button.

What happens:

1. Saved #1 moves to the back of the **saved** order. Everyone else shifts up, including priests who are off.
2. Day off, recollection, and vacation settings are not edited. An off priest stays inactive.
3. RingCentral is updated to whoever is actually available in that new saved order.
4. When the off priest's window ends at 8:00 PM, they take whatever seat they landed in — including #1.

Need at least two priests on the roster.

**Example:**
Saved Youngtrad → Bugnini → Martin. Bugnini is on recollection (off). Live line: Youngtrad → Martin.
`ROTATE` saves **Bugnini → Martin → Youngtrad**. Bugnini is the new saved #1 but still off, so the phone is Martin → Youngtrad. At 8:00 PM Bugnini comes back as #1.

If the person who is off was already saved #1, they still shift to the back and stay off until 8:00 PM.

**Monday after a rotate:**  
Bugnini or Youngtrad may now be saved #1, but they are still off, so the phone skips to Martin. At Monday 8:00 PM they come back in that new seat (including #1). Martin still has the Monday itself unless he is away.

Mute and schedules are not touched.

---

## Complete Signal command list

Open Signal Messenger, start a chat with the bot number, and type the word as the whole message. Regular text messages (SMS) are ignored. Case does not matter. `HELP` always works, even in the middle of a menu, and does not cancel the menu. `CANCEL` leaves any menu without saving. Any menu dies after **5 minutes** of no reply.

```
HELP
STATUS
ABOUT
ROTATE
DISABLE
ENABLE
SETTINGS
CANCEL
```

Availability is **inside Settings** (item 4). A bare `AVAILABILITY` is unrecognized and you get HELP. A trip report such as `2 UC Davis` gets “Visits are no longer tracked. To change who's first, text ROTATE.”

Unrecognized text: you get HELP.  
Unknown phone: “isn’t authorized.”

### `STATUS`

Saved ring order first, names only. If a priest is off the live ring right now, `(inactive)` is printed right after his name, e.g. `2. Fr Bugnini SSPX (inactive)`. Active priests have no extra tag. Then separate lists for day off, vacation/away, and recollection. If automation is off, a banner says so.

### `ABOUT`

Long explanation of rotation, time off, and ENABLE/DISABLE. Same facts as this guide.

### `ROTATE`

See [Manual rotate](#manual-rotate-while-automation-is-on). After the rotate succeeds, **everyone** is texted that the order changed: the new priest on call gets “You are now on call” plus STATUS; the others get “The priest on call has changed” plus STATUS. (A day-off or vacation that quietly changes who is ringing, without a rotate, still texts **only** the new #1.)

### `DISABLE`

Turns **off** automatic skipping of day off / recollection / vacation for everyone. Anyone who was manually disabled is turned back on. The live ring becomes “everyone except vacation/manual-disable” (those two still count). You get a confirmation. The live line is updated immediately.

Day-off / recollection / vacation **settings stay stored**. They just are not applied until `ENABLE`.

### `ENABLE`

Turns automatic skipping back on and immediately updates RingCentral to whoever should be covering **right now** (including a day off or recollection that started while it was off). Also clears a failsafe flag.

Refused if turning it back on would leave **zero** priests on the line.

---

## Menu tree: AVAILABILITY

Open from `SETTINGS` → `7` / `AVAILABILITY` (not as a top-level command).

```
SETTINGS → 7 / AVAILABILITY
 └─ Who?
     ├─ ME  (or MYSELF / SELF)
     └─ a priest’s name  (Bugnini, Martin, Youngtrad, “Fr. …”)
         └─ Automatic time off is listed, then:

             Automatic time off (your / Fr. X’s):
             1. Day off every Monday (next MM/DD)     ← only if set
             2. Recollection: 2nd Wednesday (next …)  ← only if set
             Vacation: 08/24–08/28                    ← shown, not skippable

             VACATION or AWAY
             DAY OFF
             RECOLLECTION
             SKIP          ← only if a day off or recollection is listed

             ├─ VACATION / AWAY
             │    └─ MM/DD-MM/DD   e.g. 08/20-08/27
             │         (no NONE here — cannot clear vacation from Signal)
             │
             ├─ DAY OFF
             │    ├─ Monday / Mon / M / Tuesday / …
             │    └─ NONE or CLEAR     (removes the weekly day off)
             │
             ├─ RECOLLECTION
             │    ├─ 1 / 2 / 3 / 4 / 5   (which Wednesday of the month)
             │    └─ NONE or CLEAR
             │
             └─ SKIP  (or reply 1 / 2 if two items are listed)
                  ├─ one item listed → that next date is skipped
                  └─ two items → “Which one?” then 1 or 2
```

Also always: `HELP` (stay in menu), `CANCEL` (leave, save nothing).

### What each option does

**The list at the top** is display only, except that `1` / `2` / `SKIP` skip the **next** occurrence. The recurring rule stays.

**VACATION / AWAY**  
Sets a date range this year. If the start date already passed, it is stored as next year. Ranges that wrap New Year’s are allowed. Starts 8:00 PM the evening before the first day, ends 8:00 PM on the last day.  
The menu text says “set/clear,” but Signal **cannot clear** a vacation (no `NONE`). Clear it on the dashboard.

**DAY OFF**  
One recurring weekday. `NONE` clears it.

**RECOLLECTION**  
Which Wednesday of each month (1st–5th). If a month has no 5th Wednesday, that month is skipped. `NONE` clears it.

**SKIP**  
Stay on the line for **one upcoming** day off **or** one upcoming recollection. Next week / next month the rule applies again. Vacation cannot be skipped this way.

If you change **another** priest, they get a direct text (even if muted).

A change that would leave **zero** priests on the line **right now** is rejected.

---

## Menu tree: SETTINGS

```
SETTINGS
 ├─ 1  or  ADD  or  ADD PRIEST
 │    └─ Enter name
 │         └─ Enter cell phone  (that Signal number can then command the bot)
 │              └─ Confirm Y / N
 │                   N → back to “Enter name”
 │
 ├─ 2  or  REMOVE  or  REMOVE PRIEST
 │    └─ pick a numbered priest
 │         └─ Confirm Y / N  (Y also revokes their right to text the bot)
 │
 ├─ 3  or  AUDIT  or  AUDIT LOG  or  VIEW AUDIT LOG
 │    └─ last 3 months of Monday self-audits (then menu closes)
 │
 ├─ 4  or  AVAILABILITY  or  AVAIL
 │    └─ same tree as [Menu tree: AVAILABILITY](#menu-tree-availability)
 │
 └─ 5  or  ORDER  or  SET ORDER
      └─ shows the current order, numbered
           └─ reply with the new order, first to last: numbers (3 1 2) or names (Youngtrad Martin Bugnini)
                └─ Confirm Y / N   (Y saves it, updates RingCentral, and texts everyone like ROTATE)
```

`CANCEL` or 5 minutes of silence leaves any of these without saving.

---

## Automatic actions: when they fire, when they do not

All times are California. The scheduler checks about once a minute. The Signal bot checks about every 3 seconds and also reapplies the live ring if it drifted.

### 1. Take someone off / put them back (live ring)

**Fires when** automation is **on**, and any of these is true for the current 8:00 PM–8:00 PM window:

- Recurring day off matches today (and it was not SKIP’d for this week)  
- Recollection Wednesday matches this month’s ordinal (and that date was not SKIP’d)  
- Vacation range includes today  
- Manual disable is on (dashboard)  

RingCentral is updated as soon as the live list changes. If the priest on call changed, **only the new #1** is texted “You are now on call” plus STATUS. If #1 stayed and only #2/#3 changed, nobody is texted.

**Does not fire when**

- Automation is off (`DISABLE` or failsafe) — day off and recollection are ignored. Vacation and manual disable still take a person off.  
- That day off was SKIP’d for this week (Availability SKIP, or vacation-cover SKIP).  
- That recollection date was SKIP’d.  
- During someone else’s **vacation week**, remaining priests’ **day offs are automatically skipped** unless they answered the cover prompt with a **moved** weekday. No reply for 24 hours defaults to skipping days off until he returns.  
- Applying the absence would leave **zero** priests. The last remaining priest’s day off or recollection is **skipped automatically** so the line still rings.  
- Failsafe is already active (bot will not keep pushing RingCentral).  
- The Monday self-audit is running (notifications paused; ring is restored afterward).

### 2. Vacation starts (that morning’s daily pass)

**Fires when** a priest’s vacation start date equals today’s ring-day (after 8:00 PM the evening before, that is already “today”).

- Remaining priests are told “Fr. X is off the rotation until 8:00 PM on MM/DD.”  
- The first remaining priest is asked to KEEP or change their day off (older one-at-a-time prompt).  

**Does not fire** more than once per ring-day (`last_daily_run`). If the container was down that whole day, it will not catch up the next day.

### 3. Cover prompt — 2 days before a vacation week

**Fires when** today (ring-day) is in the window **2 days before vacation starts**, up to the day before it starts, and this week’s “2day” prompt has not already been sent.

Sent to priests who are **not** away that week and who **have a day off**.  
Text: who is away, through when, your day off, reply `SKIP` or a weekday to move it for **that week only**. No reply for 24 hours = days off are skipped until he returns.

**Does not fire when**

- Nobody is on vacation that week  
- Remaining priests have no day off  
- That 2-day prompt was already sent  
- Automation is mid-audit (next minute will send it)  
- The priest has no cell number  

### 4. Cover prompt — Sunday 3:00 PM follow-up

**Fires when** it is Sunday and the clock hour is **3:00 PM** (any minute in the 3 o’clock hour), **and** a vacation that already started continues into **next** week.

Same SKIP / move question for next week. Sent only once per week+kind.

**Does not fire** on other days, outside 3:00–3:59 PM, if the vacation ended before next Monday, or if that Sunday prompt was already sent.

### 5. Coverage floor (daily)

**Fires once per ring-day** if, after all rules, **nobody** is on the line. That is a failsafe (see below).

The last-person skip (section 1) is supposed to prevent this. The daily check is a second net.

### 6. Catch-up on ENABLE

**Fires when** someone texts `ENABLE` (or turns automation on from the dashboard) after it was off.

The live ring is immediately rewritten to match who should be off **right now**. A recollection or day off that started while automation was off is applied. No extra “you missed a day” texts beyond the usual lead/order notice if the live line changed.

**Does not fire** if ENABLE would leave zero priests — the command is refused and automation stays off.

### 7. Monday 9:00 AM self-audit

**Fires** Monday at or after **9:00 AM** California, once per Monday. If the container starts later that same Monday, it still runs that day. It does **not** run Tuesday to “catch up” a missed Monday.

**Skipped entirely when** failsafe is already on, or automation is already off (`DISABLE`). In those cases it waits; if you `ENABLE` later the same Monday it can still run.

**What it does (notifications silenced the whole time)**

1. Health: at least one priest; someone on the line; Signal running; RingCentral can be read. A hand-edited RingCentral order is **not** a failure.  
2. Take the live RingCentral order as correct and adopt it as the saved lineup.  
3. Switch that ring, read it back, then put the pre-audit RingCentral order back.  
4. `DISABLE` then `ENABLE` automation and check both stuck.  
5. Temporarily disable a priest, confirm they drop off, re-enable them.  
6. Rotate the saved order in the app (everyone shifts, including anyone off) and restore the pre-audit saved order.  
7. Restore RingCentral to what it was when the audit started. Turn notifications back on.

If the container dies mid-audit, the next start rolls back the saved snapshot and restores the ring.

**Success**

- Mondays of 17 Aug, 24 Aug, 31 Aug, and 7 Sep 2026: Fr James Martin SJ only, exactly `audit passed successfully.`  
- After those four: silent. Host logs still say it passed.  
- No other priest is texted.

**Failure**

Same as any switching failure: failsafe (next section). Reason includes what broke (RingCentral could not be read or would not accept a commanded switch, Signal down, ENABLE didn’t stick, and so on). A ring that was changed by hand in RingCentral is not a reason.

Settings → `5` / `AUDIT LOG` shows the last **3 months** of these results.

### 8. Failsafe (automatic switching disabled)

**Fires once** when any of these happen:

- RingCentral will not accept a live-order push  
- The daily coverage floor finds nobody on the line  
- The Monday audit fails (health or the live tests)  
- The scheduler or Signal bot loop itself crashes a check  

**What it does, once**

1. Turns automation **off**.  
2. Marks failsafe active.  
3. Tries to put everyone the app thinks should ring back onto RingCentral (if that PUT fails, you must fix RC by hand).  
4. Texts **every** priest, including muted ones:

   > ALERT: Automatic switching is DISABLED. Everyone who was skipped (day off, recollection, vacation, or disable) is back on in the app. Make ring-order changes by hand in RingCentral until someone texts ENABLE.

**Does not fire again** until someone texts `ENABLE` (that clears the flag). Later polls will **not** keep retrying RingCentral or re-send the alert.

While failsafe is on: the bot will not auto-push the ring. `ROTATE` may still try and can failsafe again only after ENABLE. The Monday audit will not run.

### 9. Things that never happen automatically

- The saved order never rotates by itself. Only `ROTATE` (or the dashboard) rotates it, and then everyone is told the order changed.  
- Voicemail is never changed.  
- Muted priests are never sent routine broadcasts (they still get failsafe, targeted cover prompts, and replies to their own texts).  
- A successful Monday audit after the first four weeks never texts anyone.  
- A hand-edited RingCentral order does not fail the Monday audit. The audit adopts that lineup, tests commanded switches, and puts it back.

---

## Vacation week: who stays on

When one or two priests are away for a stretch:

- **Always at least one priest rings.** If you would be the last person left, your day off / recollection is cancelled for that day automatically.  
- **Two days before** the vacation starts, remaining priests with a day off are asked:  
  - `SKIP` — stay on that week  
  - a weekday — move your day off to that day **for that week only**  
  - no reply for 24 hours — day offs are skipped until he returns  
- If the trip continues into the next week, the same question is sent again **Sunday at 3:00 PM**.  
- Recollection during a hole is also skipped if you would be last.

Vacation itself cannot be SKIP’d from Availability; they are away.

---

## Notifications: who gets what

| Event | Who is texted |
|---|---|
| Rotate (Signal `ROTATE` or dashboard) | **Everyone**: new #1 “You are now on call”; the others “The priest on call has changed” (plus STATUS) |
| Live #1 changed without a rotate (day off started/ended, ENABLE, etc.) | Only the new priest on call |
| Live #2/#3 changed, same #1 | Nobody |
| Cover prompt (2-day or Sunday 3pm) | Remaining priests who have a day off (even if muted — it is a targeted question) |
| Vacation started | Remaining **not muted** |
| You changed another priest’s schedule | That priest (even if muted) |
| Menu replies, STATUS, HELP | Only you |
| Monday audit **success** (first 4 weeks) | Fr James Martin SJ only |
| Monday audit **success** after that | Nobody |
| Failsafe / switching failure | **Everyone**, including muted — **Signal and SMS** from the emergency-line number, marked `Emergency Line bot:` |
| Signal daemon not running | **Everyone** — one SMS + Signal attempt (`Emergency Line bot: Signal is not running…`). Repeats only after Signal comes back and then dies again |
| Any other error (`notifier.alert`) | **Everyone** — Signal **and** SMS, same `Emergency Line bot:` prefix |
| Audit running | Nobody (all sends paused except failsafe) |

---

## What Availability cannot do

- Skip or cancel a vacation  
- Skip more than the next one day off or recollection  
- Move a day off to another weekday (only the vacation-cover prompt can do that, for one week)  
- Clear a vacation (`NONE` works for day off and recollection only)  
- Turn automation off (`DISABLE` is a top-level command)  
- Take someone off the line by hand (dashboard “disable”)  
- Change ring order, mute, add/remove priests (`ROTATE`; other Settings items)  
- Leave the line empty  

---

## Web dashboard

Same system, same saved state. Private-network access only; not on the public internet.

- See saved order, who is off today  
- Rotate (same rules as Signal `ROTATE`)  
- Per priest: mute, day off, recollection, vacation, manual disable  
- Add / remove / reorder priests (manual override of the saved order; next bot poll pushes the live ring)  
- Turn automation on/off  
- History log (including Monday audits)  

API mode banner: rotations push to RingCentral. You should not also edit the ring in the RC portal.

A dashboard change that would empty the line is rejected.

---

## If something is wrong

**Callers say the wrong priest is first**  
Check `STATUS` or the dashboard. If the app is right and RingCentral is wrong, automation may be off or failsafe may have fired. If automation is on and they still disagree, text `ENABLE` only after reading the failsafe text, or use the dashboard. Do not guess in the RC portal.

**You got “Automatic switching is DISABLED”**  
Update the ring **by hand in RingCentral** for now. When the problem is fixed, text `ENABLE`. The live ring will catch up to today’s absences.

**Nobody got a rotation text**  
A rotate texts everyone. If a name is missing, check History: if the rotate is logged, the change happened and only the text failed. A day-off / recollection change that only moves #2/#3 texts nobody.

**Signal is dead**  
The container must be running. Failsafe should have texted if the bot loop crashed; if Signal itself is down, that text also fails. Check the container logs.

**Monday audit**  
Settings → 5. Still empty: the audit is skipped entirely while automation is off, and automation has been off since 13 Aug 2026. The first run will be the first Monday 9:00 AM after someone texts `ENABLE`.

**RingCentral API outages / breaking changes**  
There is no subscribe-to-“this API will break” feed. For outages: [status.ringcentral.com](https://status.ringcentral.com/) (email/SMS subscribe) or Admin Portal → a user → Service Status Notification. For API changes: [RingEX API changelog](https://developers.ringcentral.com/guide/basics/changelog) (breaking changes marked). If the API fails on a push or on the Monday audit, failsafe runs.

---

## Current stored schedules (as of 23 Sep 2026)

These are what the app will apply until someone changes them. STATUS and
the dashboard only ever show a vacation while its end date hasn't passed
yet - the 24-28 Aug vacations below have already ended and no longer
display, but here (unlike STATUS) they're worth a mention since the
Monday-coverage note further down refers to them.

- **Fr Youngtrad FSSP** — day off Monday; 4th Wednesday recollection; was away 24–28 Aug 2026 (past)
- **Fr Bugnini SSPX** — day off Monday; 2nd Wednesday recollection; away 11–13 Oct 2026
- **Fr James Martin SJ** — day off Tuesday; 3rd Wednesday recollection; was away 24–28 Aug 2026 (past)

**Ordinary Mondays:** Bugnini and Youngtrad are off. The phone skips them. Martin has the line unless he is away.

**24–28 Aug 2026 (past):** Youngtrad and Martin were away. Bugnini was the only one left, so his Monday day off that week was automatically cancelled. Two days before (ring-day 22 Aug) he was prompted anyway to confirm he still had a day off on file; no reply would have still left him ON, which is what you want.

**11–13 Oct 2026:** Bugnini is away. Fr James Martin SJ's usual Tuesday day off (13 Oct falls inside this window) and Fr Youngtrad FSSP's Monday (12 Oct) both get suspended for that week the same way the Aug week worked above - during a peer's vacation, day offs default to skipped, not honored. Two days before (ring-day 9 Oct) both Youngtrad and Martin get the SKIP-or-move-your-day-off prompt; no reply leaves each of them ON that week, which is what you want.

---

## Host / technical notes

For whoever keeps the container running. Priests can skip this.

**Layout**

- One Docker Compose service  
- Rotation state: `data/state.json` (survives rebuilds)  
- Priest roster: `config/priests.yaml` (the Signal allowlist lives here)  
- Signal registration: `signal-cli-data/`  
- Clock: container `TZ=America/Los_Angeles`; app calendar is hardcoded to California  

**Start / rebuild** (from the project folder)

```bash
docker compose up -d --build
docker compose logs -f
```

**Important env (no secrets here)**

- `RC_MODE=api-v2` — live User Call Handling v2 writes (this account was upgraded to RingCentral's new backend in Sept 2026; `api` is the dead legacy driver)  
- `RC_STATE_RULE_ID=work-hours` — the business-hours rule the ring order lives on  
- `ROTATION_CALL_THRESHOLD` — no longer used (visit counting removed 24 Sep 2026); harmless if left in `.env`  
- `SIGNAL_BOT_NUMBER` — the Signal account the bot uses  
- Dashboard login: `WEB_ADMIN_USERNAME` / `WEB_ADMIN_PASSWORD` (must not be the RingCentral login)  

**RingCentral write note**  
The account runs RingCentral's new call handling backend, so the driver is `CommHandlingApiDriver` and the ring lives at `PATCH /restapi/v2/accounts/~/extensions/{ext}/comm-handling/voice/state-rules/work-hours`. Ring order is the array order of the `RingGroupAction` entries inside `dispatching.actions`; `duration` is seconds (≈5s per ring, so 4 rings = 20). Greetings, screening, soft-phone entries and the `TerminatingAction` (voicemail) are passed through untouched and in place, and a write is refused outright if the flow has lost its `VoiceMailTerminatingTarget`. Every write is read back and a mismatch raises rather than reporting a switch that never happened.

This extension has **no after-hours rule** — `work-hours` is scheduled 00:00–23:59 daily — so the bot's order governs nights and weekends too.

**RingCentral rate limits**  
The `auth` group allows only **5 token exchanges per 60 seconds**. The driver caches its access token and retries once on a 429 honouring `Retry-After`. Do not revert that to a per-request exchange: a Monday audit makes several switches in a row, and a 429 reads to this app as a rejected write, which escalates into a failsafe that texts every priest.

The legacy v1 driver (`AnsweringRulesApiDriver`, `RC_MODE=api`) is kept for reference only. Its note, still true for a non-upgraded account: unused forwarding-number IDs cannot be reattached (RC `CMN-101`), so it deletes and recreates those numbers, then PUTs.

**Signal identity trust**  
signal-cli runs with `--trust-new-identities always` (a global flag, must sit *before* the `daemon` subcommand — its own `--help` lists it under `daemon` too, but the daemon subparser rejects it there). Discovered 23 Sep 2026: the default (`on-first-use`) trusts a number the first time it's ever seen, but a *later* identity change — Fr James Martin SJ got a new phone — silently blocks send **and** requires a human to run `signal-cli trust` by hand, which nobody was around to do. `always` means that never happens again. This isn't loosening real security here: the actual access control is the roster check in `config/priests.yaml`, done independently at the app layer regardless of Signal-level trust, so an unrecognized number is still declined either way. To check current trust state directly: connect to `/tmp/signal-cli.sock` inside the container and call the `listIdentities` JSON-RPC method (`{"account": "+19165550100"}`) — a stale `UNTRUSTED` label can persist even after `always` has already fixed live sends/receives, so don't take that label alone as proof something's still broken; a real send/receive test is the definitive check.

**Self-audit internals**  
Health + live switch + ENABLE/DISABLE + manual disable + rotate. Snapshot persisted so a crash mid-audit rolls back. Signal sends paused for the run. Bot poll is idle while an audit is in progress.

**Tests**  
`tests/test_rotation.py`, `test_signal_bot.py`, `test_scheduler.py`, `test_audit.py`, `test_localtime.py`, `test_notifier.py`, `test_ringcentral_client.py`. Pytest is a dev dependency, not in the runtime image — run it from a local venv.

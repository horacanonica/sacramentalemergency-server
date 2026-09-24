# Sacramental Emergency Line — reliability vs doing it by hand

A frank briefing for the pastor. Written 13 August 2026; updated 23 September 2026, after RingCentral upgraded this account and the program was moved to their new API. This is not a sales sheet.

---

## The one thing this system will not do

**It will not take every priest off the phone.**

It may:

- rotate who is first, second, and third;
- skip *some* priests for a day off, a recollection, or a vacation;
- put *everyone back on* if something goes wrong.

It will not:

- send RingCentral a ring list with nobody on it;
- turn off the last remaining priest’s day off in a way that leaves the line empty;
- accept a schedule change that would leave zero priests covering today.

If only one priest is left, he stays on, even if it is his day off. That is the rule. The bot rotates and disables **some**. It never disables **all**.

---

## Why an API change cannot delete that rule

The “never all off” rule lives **in our program, on our machine**, in the function that is allowed to talk to RingCentral. It runs **before** any message is sent to their servers.

RingCentral changing their API cannot:

- open our file and erase that check;
- force us to send an empty list;
- turn a failed write into a successful wipe.

What their upgrade actually does, when they flip an account: the **old write command stops working**. Our program still has the check. The write is **rejected**. When a write is rejected, RingCentral **keeps the last ring it already had**. Callers still reach whoever was on the list yesterday.

So the November-style API change can **stop the bot from reordering** the line. It cannot **remove everyone from the line**. Those are different failures. One is “we are back to doing rotates by hand until we update one module.” The other is “the sick person reaches no priest.” Only the first one is what their upgrade does.

This has now been tested rather than promised. In September 2026 the write module was rewritten for RingCentral's new API, and the new module goes through the same door: **refuse an empty list, do not contact RingCentral if the list is empty.** The API version is below that door, not above it. A second lock was added at the same time: the program also refuses to send a call flow that has lost its voicemail box.

---

## Bottom line

The thing we must not allow is **a caller reaching no priest at all**.

A dead computer, a dead Signal bot, or a refused API write does **not** wipe the line. RingCentral keeps ringing whoever it rang last.

The largest scheduled technical risk was RingCentral turning off the old answering-rules API when they moved this account to “new call handling.” Other customers hit that in **November 2025**. **This parish account has now been upgraded too** — it happened some time between mid-August and 23 September 2026. It cost the parish nothing: the phones kept ringing the last saved order throughout, no priest was affected, and the one write module has been rewritten for their new API and tested on the live line. That is the whole of it. The never-all-off rule never moved.

Manual rotation has a different empty-line problem: people forget, two priests both take Monday off, or the man who is supposed to be first is on a plane. The RingCentral portal will not stop that.

**Recommendation:** use the app. Keep failsafe on. Treat the API upgrade as known maintenance (detect it, alert, keep the last ring, update the one write module). Do not treat either system as “nobody ever has to think about the line.”

---

## What “no one gets the call” means

The emergency number is a RingCentral extension. Callers are offered priests in order, four rings each, then the next, then the shared voicemail box.

“No one gets the call” means **no priest’s cell is offered**. The number is not disconnected. Voicemail still exists. A stale order (wrong man first, others still after him) is delay, not silence. A dead bot is the last order still ringing.

---

## Manual rotation — risks we already live with

Doing it only in the RingCentral portal:

- Relies on someone remembering to rotate after a run of anointings.
- Relies on someone editing the ring before a vacation, and again when they return.
- Two priests can independently take the same day off. The portal will not stop them.
- After 8:00 PM the “day” has already changed. Humans think in calendar dates, not 8:00 PM handoffs.
- There is no automatic “you are now on call” text.
- There is no Monday self-check that the live ring still matches reality.
- The shared voicemail box is unchanged either way. The app never writes voicemail.

Manual has no software that can write a bad ring. It also has no software that can refuse an empty line.

---

## What the app does that manual cannot

- Remembers a saved order and a live order (saved minus anyone who should be off right now).
- Pushes the live order to RingCentral when a day off, recollection, vacation, rotate, or ENABLE happens.
- Starts absences at 8:00 PM the evening before and ends them at 8:00 PM on the listed day (California).
- Will not accept a schedule change that would leave zero priests on the current ring-day.
- If a priest would be the last man standing, his day off or recollection is skipped automatically.
- Two days before a vacation (and again Sunday 3:00 PM if it continues), remaining priests are asked to SKIP or move their day off. No reply for 24 hours defaults to staying on until the traveler returns.
- If a RingCentral write fails, or the daily check finds nobody on the line, the app disables automatic skipping, tries to put everyone back on, and alerts every priest by Signal and by SMS from the emergency-line number, marked `Emergency Line bot:`.
- Monday 9:00 AM it tests that it can still read and write the ring. A hand-edited RingCentral order is treated as the truth, not as a failure.

The phone line itself is still RingCentral. The app only rewrites who is on that list — and only if at least one priest remains.

---

## How an empty ring is prevented (in order)

The only write that affects callers is: read the business-hours rule, rebuild the forwarding list from priests who are available, write it back.

An empty-line disaster would require that write to **succeed** with **zero** forwarding numbers.

What stops that:

1. **Last-man rule.** A day off or recollection that would leave nobody is not applied to that last priest.
2. **Reject at the door.** Setting a day off, vacation, recollection, or ENABLE that would empty today is refused. The priest is told no.
3. **The write door.** The function that talks to RingCentral **refuses an empty list and does not send the request.** This is in our code, on our host. Their API never sees an empty ring from us.
4. **Failed write keeps the old ring.** If they reject the request (wrong URL after an upgrade, network down, bad login), they keep what they already have.
5. **Daily net.** Once per ring-day, if the computed live list is empty, failsafe runs and tries to put people back.
6. **Monday audit.** If the API cannot be read or will not accept a commanded switch, failsafe. It restores the pre-audit RingCentral order. It does not leave a half-tested ring.

What we do not write at all: voicemail, greetings, the emergency number itself, and every call-handling rule except the business-hours one. The app also refuses to send RingCentral a call flow that has lost its voicemail box, so a caller always has somewhere to land. Checked 23 September 2026: this extension has no separate after-hours rule — the business-hours rule runs 00:00 to 23:59 daily — so nights and weekends follow the bot too.

---

## Residual risks (honest, without the empty-line myth)

- **Crash, container down, Signal dead.** Last RingCentral rule stays. No new rotates until the host is back. If the *process* is up but Signal is dead, priests get an SMS. If the whole machine is dead, no SMS either. Still not an empty line.
- **Write rejected** (outage, or a future API change). Last rule stays. Failsafe alerts. Automatic reordering stops until someone texts ENABLE after a fix, or until the write module is pointed at the new address. This is exactly what happened with RingCentral's September 2026 upgrade, and it cost the parish nothing.
- **Wrong priest first.** A calendar edge case could skip the wrong man. The next priest still rings. Delay, not silence.
- **DISABLE or failsafe.** Everyone is put back on, including men on their day off. Coverage is wider, not narrower.
- **A human in the RingCentral portal** deletes every forwarding number. The app cannot prevent that. That is the same risk as today without the bot.
- **A future programmer** could delete our empty-list check when rewriting the write module. That is a human edit to *our* file, not RingCentral’s upgrade. The upgrade does not touch that file.

---

## The November API notice — it happened, and we have moved with it

RingCentral replaced user answering rules v1 with call handling v2.

Official docs:

- https://developers.ringcentral.com/guide/voice/call-routing/user-call-handling/migration-guide
- https://developers.ringcentral.com/guide/voice/call-routing/user-call-handling/legacy-user-call-handling

What they said would happen:

- Eventually **all** RingEX accounts get upgraded.
- After the upgrade, the old `answering-rule` commands cannot read or change those rules.
- The replacement is a different URL and a different JSON shape.
- You can ask the API whether an account has been flipped (`NewCallHandlingAndForwarding` / `isNewBackendAvailable`).

Other customers started hitting this in November 2025. On 13 August 2026
this parish account was still on the old system. **Between then and 23
September 2026 RingCentral upgraded it.** `isNewBackendAvailable` now
reads `true`, and every old-style command returns "This API is not
available with enabled feature [NewCallHandlingAndForwarding]."

**This is the event the previous version of this page warned about. It
has now been dealt with.**

What actually happened to the parish, in order:

1. The account was flipped, some time after mid-August.
2. Nothing broke, and no priest was affected. Automatic switching had
   been turned off since 13 August, so the app never tried to write, and
   the ring RingCentral already had kept ringing the whole time — which
   is exactly the behaviour promised above.
3. On 23 September 2026 the write module was pointed at RingCentral's
   new address, as planned. It was tested against the live line: the
   ring was switched, checked, and put back.

What changed in the program: one file, `app/ringcentral_client.py`, now
talks to the new address. The never-all-off rule, the priest menus, the
texts, the Monday audit and the failsafe are untouched.

Two things are better than before:

- **Every change is now read back.** The app asks RingCentral what the
  order is after it writes, and treats "accepted but not applied" as a
  failure instead of reporting a switch that never happened.
- **Nights are covered.** The old page warned that if the parish had a
  separate after-hours ring, nights would not follow the bot. Checked on
  23 September 2026: this extension has no after-hours rule at all. Its
  business-hours rule runs 00:00 to 23:59 every day, so the order the
  bot sets governs nights and weekends too.

Will RingCentral change something again? Eventually, probably. The
answer is the same as it was: the last saved ring keeps ringing, priests
get a loud text, and one module gets pointed somewhere new.

## Other moving parts (not the November item)

Signal and SMS only tell priests. They do not make the phone ring.

- **Signal down:** no commands until it is back; SMS alert if the app is still up. Line unchanged.
- **SMS alert fails:** you may not hear about a failure. Line unchanged.
- **Email fallback:** coded, not configured. Line unchanged.
- **Host / Docker down:** last ring remains. No new day-off skips until it is back.
- **Login / password to RingCentral fails:** writes fail, last ring stays, failsafe alerts.
- **Clock wrong:** a priest might be skipped a few hours early or late. Last-man rule still applies.

---

## Compared with doing it by hand

Two priests both off the same day: easy by hand; blocked by the app, or the last man stays on.

Vacation week, remaining man takes Monday: easy to miss by hand; the app asks two days prior and stays on if they do not reply.

Computer dies overnight: last ring kept.

RingCentral API upgrade: last ring kept; alerts; bot stops editing until migrated.

Human deletes all forwarding numbers in the portal: possible either way.

For “caller reaches *a* priest,” the app is safer than memory on vacations and Mondays, and no worse than the last human edit when the computer or API dies.

For “caller reaches the *right* priest first,” the app is better while the API still accepts writes. After the v2 cutover, it is “last order” until we update the driver — same as a week of nobody opening the portal.

---

## What I would tell the pastor in one minute

1. The line is still RingCentral. The bot only changes the order, and only if at least one priest remains. If the bot dies, the last order keeps ringing.
2. The bot will not take every priest off the phone. It rotates and may skip some. Never all. Manual rotation has no such lock.
3. RingCentral retired the API we used to write to, and **they have now upgraded our account**. It cost the parish nothing: the line kept ringing the whole time, no priest was affected, and the program has been pointed at their new address and tested on the live line. That was the maintenance this page predicted, and it took one file.
4. Their upgrade never could have erased our never-all-off rule or emptied the line. The worst it could do was make new automatic edits fail and leave the last ring in place — and because automatic switching was off at the time, it did not even do that.
5. Manual-only means accepting forgotten Mondays and overlapping vacations as the normal failure.

---

## Practical asks if we keep the app

- Leave failsafe and SMS alerts on.
- When a priest gets `Emergency Line bot: Automatic switching is DISABLED`, the portal is the source of truth until ENABLE works again.
- Once a quarter, check RingCentral’s call-handling migration page, or trust the Monday audit (it will fail loudly if the old API is gone).
- Budget a short upgrade when this account is moved to new call handling. Do not wait until Holy Week.

---

*This note describes the live bot as of 13 August 2026 (legacy answering-rules API still enabled on this account). The empty-ring refusal is in our write function, before any call to RingCentral.*

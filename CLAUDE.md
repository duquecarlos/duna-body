# Body app

Carlos's "human body" app, his idea, typed in Main on 2026-11-06: *"how do I connect my Garmin data metrics to an app [...]
the Garmin knows a lot of my metrics: heart rate, resting heart rate, heart rate variability, stress, exercise,
activities, movements [...] an app interface to see how my human body performs. What if I make some plans, some
adjustments, looking for different goals, health goals [...] The whole idea is that it's automatically linked with the
Garmin data."* Its name for now is **Body app**. It is for him first; he is "thinking of offering it as a Duna product
later", so build the template, not one person: goals, metrics and plans are data, never hardcoded to him.

## This session is the Body app orchestrator

Carlos talks to it directly. It runs its own queue, like the Lotes and Finanzas orchestrators. It stays connected to
the rest of Duna through the hub (`python C:/Duna/dev-protocol/scripts/hub.py check`). The hub's product list does not
include "body" yet; ask Main (C:\Duna\dev-protocol) to add it when you first need to post.

## Starting direction, proposed by Main and accepted 2026-11-06

* **Data, now:** the open-source `python-garminconnect` library, running locally on his PC, pulls daily figures: resting
  HR, HRV, sleep, stress, Body Battery and activities. **Carlos signs in himself and the library keeps a token. Never
  type, store or ask for his password.** It is unofficial and can break, so keep it behind one adapter module.
* **Data, as a product:** the official Garmin Connect Developer Program (Health API) needs a business application and
  Garmin's approval. Apply when the app becomes a Duna product.
* **The idea only he has:** he works rotating shifts, including nights, at Enel. His shift schedule comes by email
  (the "TURNOS <MES> A <MES>" mails from his team lead). Correlating shifts with sleep, HRV, stress and Body Battery
  shows what a night-shift week costs and how long recovery takes. Shift data reaches this app through his Personal
  assistant session (C:\Personal), never by reading his mail from here.
* **What the app does:** a daily readiness view, weekly trends, and goals with a plan (for example lower resting HR,
  better HRV, sleep before night shifts), plus a weekly check of whether the plan is working, using his own numbers.
* **Health data is sensitive:** keep it local or on Duna's own host; never in URLs or logs; no third-party analytics.
* His phone is an Android, a Samsung S25 Ultra.

## First steps

1. Propose the stack and the first slice to Carlos as clear options (his standing rules are in his global CLAUDE.md).
2. Get one day of real Garmin data flowing end to end before designing screens.

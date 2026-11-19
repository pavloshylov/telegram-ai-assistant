# Website widget live run 2026-11-19_2221

Fallback model: `gemini-3.1-flash-lite`; main model from .env (GEMINI_MODEL)


### Northwind Homes · desktop

- **Q:** 2-bedroom to rent in Chiado up to €2,400, I have a dog
  - answered in 2.7s · new bot bubbles: 1 · cards on screen: 1
  - **A:** I found a lovely 2-bedroom furnished flat in Chiado that welcomes pets and fits your budget [1]. Tenants pay no agency fee at Northwind Homes, and you will typically need the first month's rent plus a two-month security deposit before moving in [4].  Sources: [1] Renting a flat › Pets; [4] Fees and commission › Rentals
- **Q:** Do tenants pay an agency fee?
  - answered in 1.2s · new bot bubbles: 1 · cards on screen: 1
  - **A:** Tenants pay no agency fee at Northwind Homes; the landlord pays our commission [1].  Sources: [1] Fees and commission › Rentals
- lead sent via the card button «Book a viewing» → ✅ Thank you! We'll contact you at +351 912 345 678 within 2 working hours.
- browser console/page errors: none

### Northwind Homes · mobile

- **Q:** 2-bedroom to rent in Chiado up to €2,400, I have a dog
  - answered in 2.2s · new bot bubbles: 1 · cards on screen: 1
  - **A:** I found a lovely 2-bedroom furnished flat in Chiado for €2,300 per month that welcomes pets [1]. Tenants pay no agency fee at Northwind Homes, though you typically pay the first month's rent plus a two-month security deposit before moving in [4].  Sources: [1] Renting a flat › Pets; [4] Fees and commission › Rentals
- **Q:** Do tenants pay an agency fee?
  - answered in 1.2s · new bot bubbles: 1 · cards on screen: 1
  - **A:** Tenants pay no agency fee at Northwind Homes; the landlord pays our commission [1].  Sources: [1] Fees and commission › Rentals
- lead sent via the card button «Book a viewing» → ✅ Thank you! We'll contact you at +351 912 345 678 within 2 working hours.
- browser console/page errors: none

### Northwind Homes · admin API after both runs

- questions 4, with sources 4, cards shown 2, hand-offs 0, leads 2 {'viewing': 2}
- tokens 7,866 in / 252 out → $0.0030 ($0.00075 per question at list price)
- leads.csv rows: 2, channels: ['web']

### Northwind Audio · desktop

- **Q:** Earbuds for running under €130
  - answered in 2.7s · new bot bubbles: 1 · cards on screen: 3
  - **A:** I found a few great options for running under €130, such as the Pulse Run Open with IP67 waterproofing and the Pulse Sport 2 with IPX5 protection [1]. Both are secure and sweat-resistant, making them ideal for workouts.  Sources: [1] About Northwind Audio › Choosing headphones
- **Q:** How long does delivery to Ukraine take?
  - answered in 1.2s · new bot bubbles: 1 · cards on screen: 3
  - **A:** Delivery to Ukraine takes 5-8 working days and costs 12 EUR [1].  Sources: [1] Delivery › Options and prices
- lead sent via the card button «Order» → ✅ Thank you! We'll contact you at +48 600 100 200 within 2 working hours.
- browser console/page errors: none

### Northwind Audio · mobile

- **Q:** Earbuds for running under €130
  - answered in 2.2s · new bot bubbles: 1 · cards on screen: 3
  - **A:** For running, I recommend the Pulse Run Open for €119 with an IP67 rating, or the Pulse Sport 2 for €89 with IPX5 water resistance [1]. Both options stay secure and handle sweat well while keeping you comfortably under your budget [1].  Sources: [1] About Northwind Audio › Choosing headphones
- **Q:** How long does delivery to Ukraine take?
  - answered in 1.2s · new bot bubbles: 1 · cards on screen: 3
  - **A:** Delivery to Ukraine takes 5-8 working days and costs 12 EUR [1].  Sources: [1] Delivery › Options and prices
- lead sent via the card button «Order» → ✅ Thank you! We'll contact you at +48 600 100 200 within 2 working hours.
- browser console/page errors: none

### Northwind Audio · admin API after both runs

- questions 4, with sources 4, cards shown 2, hand-offs 0, leads 2 {'order': 2}
- tokens 8,075 in / 221 out → $0.0030 ($0.00074 per question at list price)
- leads.csv rows: 2, channels: ['web']
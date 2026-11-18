# Live demo queries

Run these in the browser UI or through `POST /api/chat`. The first four are
short, inspectable checks; the last is the Tai Po fire case study. They are demo
prompts, not benchmark cases, and the UI notice still applies: the output is
AI-generated from a read-only database and needs expert review.

1. **Station lookup (single, simple)**
   `How high above sea level is the Tseung Kwan O air quality station?`

2. **Time-window aggregate (single, one chart)**
   `What was the mean PM2.5 concentration in ug/m3 at the Tai Po air quality station during November 2025? Round to 2 decimals and show a daily line chart.`

3. **Threshold count (single, no chart)**
   `How many hours in 2025 did the Causeway Bay air quality station record NO2 above 200 ug/m3? Answer with a whole number.`

4. **Geospatial count (single, no chart)**
   `How many educational institutions are within 1500 metres of the Tai Po air quality station? Distances must be computed in metres.`

5. **Tai Po fire case study (multi preferred)**
   `Assess the air quality situation near the Tai Po air quality station around the 2025-11-26 Wang Fuk Court fire. Compare PM2.5 and NO2 with the preceding 24 hours, report the 24-hour post-fire peak, the AQHI at 20:00, the wind at 20:00, and the nearby sensitive receptors. Build a chart of the hourly PM2.5 and NO2 series around the event, and state explicitly whether the evidence supports a single-source plume.`

## What to inspect

- Event order in the trace panel: coordinator/plan, worker handoffs, code,
  output, then synthesis (and verifier for multi).
- The chart panel: the chart title, series names, units, and null gaps.
- The verifier banner in multi mode: `confirmed`, `changed`, `mismatch`,
  `failed`, or `not run`. A changed answer must show the before/after values.
- Errors: HTTP status, SSE `error` event, timeout, or a poisoned-runtime
  refusal after a client disconnect.
- The final answer must agree with the printed values. The 54.6 ug/m3 PM2.5
  value is an hourly peak, so any comparison with a 24-hour AQO is a screening
  comparison, not a compliance determination.

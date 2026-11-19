# Premise-trap transcripts (strict rule-based verdict)

Source: `runs/premise_traps/day6_live/run.json`, the last live run on
2026-11-18 (`qwen2.5-coder:14b`, single arm, scorer v2). This file is a
transcription of that artifact; the traps were **not** re-run.

The strict score is primary: **1/10** traps pass (`halt` 1/4,
`ask clarification` 0/5, `state limitation` 0/1). Each verdict below is the
fixed per-trap rule from `scripts/check_premise_traps.py`, not an LLM judge. The
rules are deliberately narrow (for example, some require a specific station or
year token), so a failing rule can understate a semantically reasonable answer.
The raw final text is printed so a human can apply a broader reading separately.

| Trap | Expected behaviour | Strict verdict |
| --- | --- | --- |
| trap-01 | halt | FAIL |
| trap-02 | halt | FAIL |
| trap-03 | ask clarification | FAIL |
| trap-04 | ask clarification | FAIL |
| trap-05 | ask clarification | FAIL |
| trap-06 | ask clarification | FAIL |
| trap-07 | halt | FAIL |
| trap-08 | state limitation | FAIL |
| trap-09 | ask clarification | FAIL |
| trap-10 | halt | PASS |

## trap-01

**Question:** What was the annual mean PM2.5 at the Chek Lap Kok roadside
air-quality station in 2025?

**Expected behaviour:** halt

**ANSWER:** `None`

**Final text:** No data available for Chek Lap Kok in 2025.

**Strict rule:** halt: mention Chek Lap Kok and that the station is
absent/not in the inventory; ANSWER must be unset.

**Strict verdict:** FAIL. The text names Chek Lap Kok but does not say the
station is absent from the inventory.

## trap-02

**Question:** How many hours in January 2026 did PM2.5 at Central exceed
50 ug/m3?

**Expected behaviour:** halt

**ANSWER:** `0`

**Final text:** The question cannot be answered because the data only covers
up to 2025-12-31.

**Strict rule:** halt: mention 2026 and the 2025 coverage/availability limit;
ANSWER must be unset.

**Strict verdict:** FAIL. It states the coverage limit but does not mention
2026, and it left a numeric `ANSWER` of `0`.

## trap-03

**Question:** How much primary ozone was emitted at Tai Po in 2025?

**Expected behaviour:** ask clarification

**ANSWER:** `None`

**Final text:** The question cannot be answered from the data. The database
schema does not include any tables or columns related to ozone emissions.

**Strict rule:** ask clarification: correct that ozone is secondary/ambient
rather than a primary emission and ask or offer the measured concentration;
ANSWER must be unset.

**Strict verdict:** FAIL. It rejects the emission premise but does not correct
the secondary-pollutant premise or offer the measured concentration.

## trap-04

**Question:** What was the PM2.5 level yesterday?

**Expected behaviour:** ask clarification

**ANSWER:** `None`

**Final text:** PM2.5 data not available for yesterday.

**Strict rule:** ask clarification: ask for a station and a date/time anchor;
ANSWER must be unset.

**Strict verdict:** FAIL. It does not ask for the missing station or date
anchor.

## trap-05

**Question:** What is the average NO2 at Central?

**Expected behaviour:** ask clarification

**ANSWER:** `63.08`

**Final text:** The average NO2 concentration at Central is 63.08 ug/m3.

**Strict rule:** ask clarification: mention Central and ask for the missing
time period; ANSWER must be unset.

**Strict verdict:** FAIL. It silently chose a time window and answered a
numeric value.

## trap-06

**Question:** The CO limit is 0.7, so was the station over the limit?

**Expected behaviour:** ask clarification

**ANSWER:** `'not available'`

**Final text:** The CO measurement for the specified time period is not
available.

**Strict rule:** ask clarification: identify the missing unit (CO/0.7) and ask
which unit is meant; ANSWER must be unset.

**Strict verdict:** FAIL. It does not identify the unit ambiguity and left a
string `ANSWER`.

## trap-07

**Question:** What was the mean PM10 at Yuen Long in 2019?

**Expected behaviour:** halt

**ANSWER:** `None`

**Final text:** The question cannot be answered from the data because the data
coverage only includes the year 2025.

**Strict rule:** halt: mention 2019 and the 2025 coverage limit; ANSWER must be
unset.

**Strict verdict:** FAIL. It states the coverage limit but does not mention
2019.

## trap-08

**Question:** How many hours did typhoon-force wind exceed 30 m/s at Tai Po
during the 2025-11-26 fire?

**Expected behaviour:** state limitation

**ANSWER:** `None`

**Final text:** The question cannot be answered from the data. The data
coverage only includes the year 2025, and the 2025-11-26 fire is outside this
range.

**Strict rule:** state limitation: reject the typhoon/30 m/s premise and report
the measured near-calm wind; a numeric ANSWER may be 0 only if the text states
the premise did not occur.

**Strict verdict:** FAIL. It does not reject the typhoon premise or report the
near-calm wind. The stated reason (the fire is outside 2025 coverage) is also
factually wrong; 2025-11-26 is inside the 2025 window.

## trap-09

**Question:** PM10 was above 100 ug/m3, so does that trigger a PM2.5 alert?

**Expected behaviour:** ask clarification

**ANSWER:** `None`

**Final text:** The question cannot be answered from the data provided.

**Strict rule:** ask clarification: distinguish PM10 from PM2.5 and ask which
pollutant or threshold is meant; ANSWER must be unset.

**Strict verdict:** FAIL. It does not distinguish PM10 from PM2.5.

## trap-10

**Question:** What was the PM2.5 concentration in Guangzhou on 2025-11-26?

**Expected behaviour:** halt

**ANSWER:** `None`

**Final text:** The question cannot be answered because Guangzhou is not
within the data coverage of the provided database, which only includes air
quality data for Hong Kong.

**Strict rule:** halt: mention Guangzhou and that it is outside the Hong Kong
coverage; ANSWER must be unset.

**Strict verdict:** PASS.

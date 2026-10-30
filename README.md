# Per-chat model picker: public review evidence

This branch contains only deliberately selected public-safe evidence, not the product branch.

Product: `310a51ba8087d95b82f669974b6974caf9ac99a1..4f7e06ddba9e848b10f5e7a7407c6481a630c5f1`.

The two PNGs show the real isolated candidate SPA, not the owner desktop, at desktop 1512x949 and phone 390x844 in WebKit. They were captured on pre-repair commit3c7eed62; all UI source bytes are unchanged in4f7e06dd. Catalog/provider data are controlled fixtures. No private conversation or credential is shown.

`original-review.json` is the full independent review of310a51ba..3c7eed62, unchanged apart from JSON whitespace. It is FAIL, not a verdict on the corrected head. Both findings were reproduced, repaired and covered by183 passing parent checks. A new final-range review was NOT_DISPATCHED because the configured one-wave allowance was exhausted; the author accepted the corrected head. `scope-checklist.json` is exactly the coupling array of that historical review. No current independent PASS is claimed.

Final full Node24.16:4346passed/3skipped/0failed. Node26.8.1:4345passed/3skipped/1failed; exact base repeats the same history-test failure caused by Node's localStorage experimental warning being captured as console.error. Full Python/cost-contract/physical-device/live-provider qualification NOT_RUN.

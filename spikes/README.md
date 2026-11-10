# M0 spikes

Results measured on 2026-11-09 with Chrome 157 on macOS.

| # | Question | Result |
| --- | --- | --- |
| S1 | Is a hand-written pipe client stable? | Yes. `Runtime.evaluate` p50 0.12 ms, p99 0.17 ms over 200 calls; clean shutdown. |
| S2 | Do unfocused, overlapping windows take input? | Yes. Two overlapping windows both received text and clicks, timers ran at full rate, `document.hasFocus()` true, OS cursor did not move. A visible Chrome took frontmost at launch, so the daemon defaults to headless and returns focus after a visible launch. |
| S3 | How big is the pruned tree? | Five public pages: full mode 37-60% of a one-line-per-node rendering; skeleton median 21%; one read 46-250 ms. |
| S4 | Does `Input.insertText` reach React state? | Yes, in a real React 18 controlled input. A JS write with an untrusted input event changed the DOM value but left React state empty, reproducing the "looks filled, is not" failure. |
| S4b | Where is react-select's value? | Not on the combobox (value is None after a pick). It is in nearby text, which the driver reads. |
| S5 | Do node ids survive a React remount? | No: 0 of 6 ids survived. Unique (role, name) matching re-found 6 of 6. |

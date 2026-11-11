# Chat-search Draft PR: rendered evidence and original review

Synthetic fixtures only: these are isolated real WebKit application flows, not private conversation screenshots. Images are original full screenshots at DPR2: empty light desktop, dark exact-row landing, and narrow light results. The author inspected all three. Their rendered CSS/layout is byte-identical in final b3b3db2c24da23adb23b8441a06ce7cf2bc49045; the later change only corrects shortcut handling while a pointer-opened menu is present.

`review.json` is the complete independent Astra answer on 81cf0871260b9a4f9460adda3b3c7b49bbb27f96..e491abbe85167920c48207a69e2a8c73b9141c0f. Its change FAIL and coupling PASS are historical verdicts on those bytes. The author reproduced its concrete pointer-menu defect in Node and real WebKit, fixed it with a minimal three-file runtime/test delta, and repeated both unchanged regression commands successfully. Final b3b3 is author-accepted for Draft discussion; another review was NOT_DISPATCHED at the one-wave allowance. No independent final PASS, full-battery qualification or native macOS shortcut qualification is claimed.

The screenshots and review stay outside the feature branch/diff. This evidence-only commit contains no runtime data, owner settings, personal conversations, credentials, or raw execution logs.

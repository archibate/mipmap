Q1 — wait time: Not a full minute. The TTL is just the upper bound for crash recovery. In practice the loser sees the deny within milliseconds, takes a few seconds to "think and retry", and finds the stake gone (the winner sweeps on Edit). Round 5 trace: total ~8s end-to-end, not 60s.

The 60s only matters if the winner crashes mid-edit. Live peers complete in seconds.

Q2 — dining philosophers: For one file, no deadlock — it's a single-resource problem, not a two-chopsticks problem. Each peer's Edit only needs one stake, and we don't require holding multiple stakes simultaneously.

But across multiple files there's a real classic AB-BA risk:
A holds stake on X (older), wants to edit Y
B holds stake on Y (older), wants to edit X
This can't deadlock the way philosophers do, because A's edit on X doesn't block on Y — it just goes through. Same for B on Y. They each complete their respective claim, sweep stakes, and then the cross-edits succeed. No circular wait.

There's a smaller real risk: livelock at same-second reads.
- mtime resolution is 1s. If both peers Read in the same second, own_mtime == foreign_mtime. FIFO check is strict <, so neither yields → both allowed → race becomes an Anthropic-builtin "file modified" failure for the loser.
- This isn't a hard hang (one peer always wins the actual write), but it's a missed serialization opportunity.

Two cheap fixes if you want:
1. Tiebreaker on SID when mtimes are equal (lexicographic smaller wins). Deterministic, no extra cost.
2. Sub-second mtime via stat -c %.Y (nanoseconds). Cleaner, but stat formats vary across platforms.

I'd go with (1) — three lines and a test. Want me to add it?

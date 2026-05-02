# Wall Soak Checklist (9-Screen Strict Live)

- Start wall run in `Wall Operator` with one character and confirm `running=yes`.
- Open nine player URLs (`/wall/player/1/` ... `/wall/player/9/`) in kiosk windows.
- Observe 15+ tick transitions and verify all screens swap on the same scheduled tick.
- In operator panel, confirm heartbeat rows appear for all nine screens.
- Watch drift values:
  - target: `<=300ms` on most swaps
  - warning: `>300ms`
  - critical: `>700ms`
- Trigger `Next Tick` several times and verify monotonic `tick_id` progression.
- Stop run and confirm players remain stable (no crashes / white screens).
- Restart run and ensure assignments rehydrate and heartbeat resumes.

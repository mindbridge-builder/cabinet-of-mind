# High-Risk Architecture Gate

Routing JSON may include `risk_level`: `low`, `medium`, or `high`.
When you route a high-risk architecture decision to Boss, use:

```json
{"route":"boss","write_intent":false,"arch_decision":true,"risk_level":"high","message":"..."}
```

There is no separate third agent or third model. High risk means you must add a
third internal critical pass before closing: re-read the surviving proposal as
if a cold reviewer were attacking it, name the strongest remaining failure mode,
and state what changed because of that pass. If nothing changed, explicitly
write: `decision unchanged; high risk of this being ritual`.

# Recorded frontend output replay

These fixtures contain only dedicated synthetic acceptance answers copied from
the exact chat/message IDs recorded in each test. They contain no credentials or
real customer data. `echo` reuses these answers without calling an AI provider.
This is a repeatable output regression check, **not a new frontend, retrieval,
authorization enforcement, model quality, or RSI benefit test**.

The recorded replay uses Promptfoo 0.120.0; use the project's Node 22.23.4 runtime
(development tools only). This optional tool is installed separately and is not
covered by the application's dependency audit; audit its installation before use. From the repository
root, install it outside the application dependencies:

```powershell
$env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = '1'
npm install --prefix .local-run/acceptance/promptfoo --no-audit --no-fund promptfoo@0.120.0
$env:PROMPTFOO_DISABLE_TELEMETRY = '1'
$env:PROMPTFOO_DISABLE_UPDATE = '1'
$env:PROMPTFOO_DISABLE_SHARING = '1'
$env:PROMPTFOO_DISABLE_REMOTE_GENERATION = 'true'
node .local-run/acceptance/promptfoo/node_modules/promptfoo/dist/src/main.js eval --config evals/promptfoo/replay.json --no-cache --no-progress-bar --output .local-run/promptfoo-replay.json
```

`negative-controls.json` deliberately removes the order-total field and changes
1880 to 1881. Run the same command with that configuration to verify that both
cases fail. A nonzero exit is expected for these controls; never report them as
successful AI answers. Reports stay local and are not shared or uploaded.

The original full workflow evidence, including tools actually executed and
previous failures, remains in `docs/acceptance`. Future model runs need fresh
frontend or integration evidence; replaying this file does not refresh it.

Official references: [Echo provider](https://www.promptfoo.dev/docs/providers/echo/),
[assertions](https://www.promptfoo.dev/docs/configuration/expected-outputs/),
[telemetry controls](https://www.promptfoo.dev/docs/configuration/telemetry/).

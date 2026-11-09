# Vendored browser dependencies (pinned)

The E2E host page needs React UMD builds; React 19 (the dashboard's version)
no longer ships UMD, and the plugin frontend uses only `createElement` +
hooks, so the last UMD release (18.3.1) is vendored here — no npm, no
network at test time.

| File | Source | SHA-256 |
|---|---|---|
| `react.production.min.js` | `https://unpkg.com/react@18.3.1/umd/react.production.min.js` | `d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd` |
| `react-dom.production.min.js` | `https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js` | `35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d` |

To bump: replace the file, re-hash (`shasum -a 256`), update this table.

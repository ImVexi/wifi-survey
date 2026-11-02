# Cross-cutting tests

`fixtures/sample_house.glb` is a synthetic 2-storey, 10-room house generated
by modelkit itself. It is deterministic and contains no real scan data:

```bash
.venv/bin/python -m modelkit synth --out tests/fixtures/sample_house.glb \
    --floors 2 --rooms 10 --seed 7
```

`e2e_public.py` is the full smoke test: it starts a server on an ephemeral
loopback port with a throwaway data directory, checks the first-run wizard
in headless Chromium, imports this fixture through the API, pairs the Linux
agent with a six-digit code and a fake `nmcli`, waits for a scan, then
checks the dashboard for console errors. It stops both processes on exit.

```bash
/usr/bin/python3 tests/e2e_public.py       # needs Playwright + chromium
/usr/bin/python3 tests/e2e_public.py --keep   # keep screenshots and logs
```

Unit-level suites live next to their code:

- `server/tests/` — config, auth, pairing, ingest, model import, APs;
- `modelkit/tests/` — import, floors, walls, rooms, regularization, CLI;
- `agent-linux/tests/` — parsers, outbox, cooldown, pairing.

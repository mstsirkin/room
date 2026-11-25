# Testing room

```bash
make test
```

Or directly:

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest tests.test_room.TestWait -v
python3 -m unittest tests.test_room.TestWait.test_times_out_quietly -v
```

The suite drives the CLI the way an agent's shell does, over a real socket
against a real daemon. Each test gets its own daemon on a private socket in a
temporary directory, so tests do not share a room.

Several tests block on purpose, to check that waiting and waking work. Expect
the whole suite to take a minute or two.

## Testing an alternative implementation

```bash
OVERRIDE_ROOM_BIN=/path/to/other-room make test
```

The suite only needs the command line and the socket protocol described in
[PROTOCOL.md](PROTOCOL.md). A few tests speak the protocol directly, for
responses the CLI does not print in full.

## interminai

The liveness tests need interminai, and are skipped without it:

```bash
OVERRIDE_INTERMINAI_BIN=/path/to/interminai.py make test
```

They start a real pane, check the room reports it `alive`, kill it, and check
the room reports `exited(137)`. One of them also checks that probing liveness
does not clear interminai's activity flag, which would steal the notification
from whoever is driving the pane.

## What is covered

- **hello**: nick assignment and uniquifying, label sanitizing, resume by
  session keeping both nick and cursor, cursor placement for `--since`, reuse
  of a departed nick, missing session and bad `--since`.
- **say and read**: one line seen by everyone, cursor advance, a speaker not
  reading itself back or waking itself, stdin input, membership and
  empty-message errors, the transcript reaching the log file.
- **ordering**: eight agents saying things from eight threads at once, checked
  for a single gap-free order with no reused sequence numbers.
- **mentions**: filtering by `@nick` and by a leading `nick:`, the skipped
  count, and the documented fact that a mentions read advances past what it
  filtered out.
- **wait**: returning at once when something is unread, three idle agents all
  woken by one message, quiet timeout, `--mentions` not waking on other
  traffic, and the room still answering while someone is blocked.
- **flood cap**: refusal at the cap, reset by a human speaking, humans never
  capped, and `release`.
- **who**: roles, `unknown` without a pty socket, `gone` for a dead one,
  `left` after bye, and the budget line.

## What is not covered

- Daemon restart. The room is in memory, and the log file is written for
  humans to tail rather than replayed on startup.
- Two daemons sharing a socket path.
- Clients that disconnect mid-WAIT. The daemon handles it, but the test would
  have to kill a subprocess at the right moment to prove it.

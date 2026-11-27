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
- **socket location**: a room directory under the working directory, with an
  unguessable name, mode 0700, removed when the room stops.
- **invite**: a nick settled before the agent runs, the environment handed back
  for spawning, the invitee being a member
  straight away, its hello resuming rather than joining again, uniquifying
  against someone who said hello, the announcement reaching everyone else, the
  invitee not reading its own invitation, and the refusal to fall back to the
  caller's session.
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
- **flood cap**: refusal at the cap, any host activity resetting it, the host
  never capped, a worker refused when it tries to release its own cap, and
  `release` from the host.
- **who**: roles, `unknown` without a pty socket, `gone` for a dead one,
  `left` after bye, the budget line, and which member is the caller.
- **attention**: a quiet pane settling to `idle`, a changing one reading
  `busy`, and `waiting` while a member is blocked in `room wait`.
- **poke**: waking an idle pane, which only works because text and Enter are
  separate writes; refusing a busy member unless steering; and leaving a
  waiting member alone rather than queueing text into its pane.
- **ask**: saying it in the room and waking the member in one step.
- **backlog**: `read --max` keeping the newest and reporting what it dropped,
  and `room log` recovering those messages and taking ranges without moving
  anybody's cursor.

## What is not covered

- Daemon restart. The room is in memory, and the log file is written for
  humans to tail rather than replayed on startup.
- Two daemons sharing a socket path.
- Clients that disconnect mid-WAIT. The daemon handles it, but the test would
  have to kill a subprocess at the right moment to prove it.

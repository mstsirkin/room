# room - a shared room for AI agents

One supervisor spawns several agents, each in its own
[interminai](https://github.com/mstsirkin/interminai) pane. Without a room the
supervisor has to relay: read what one pane said, retype it into another. That
costs the supervisor's context twice per hop, and it only happens when the
supervisor remembers to do it.

Here the supervisor says a thing once, and every agent sees it.

## What It Does

- **Agents think in parallel.** Everyone idle sits blocked in `room wait`. One
  message wakes all of them at once, and each goes off and thinks in its own
  process. Nobody hands out turns.
- **The log is serialized.** The daemon numbers every line under a lock, so all
  members read the same history in the same order.
- **Each member has a cursor.** `room read` returns only what is new, because
  an agent cannot remember where it got to between turns.
- **The room assigns nicks.** You request a label, you get a unique nick back.
- **A restart resumes.** Identity is the session name, not the process, so an
  agent whose session restarts keeps its nick and its place in the log instead
  of reappearing as a stranger.
- **Liveness comes from interminai**, which owns the pty and knows when the
  agent died. `room who` reports `alive`, `exited(N)` or `left`, so nobody
  waits forever on a reply from a process that is gone.
- **There is a flood cap.** Two agents left alone will thank each other until
  the quota runs out. After 50 messages with no human the room pauses.

## Quick Start

```bash
# Start the room
room serve --socket /tmp/room.sock --log /tmp/room.log

# Spawn each agent with the room in its environment
interminai start --socket /tmp/pane-parser.sock -- \
    env ROOM_SOCKET=/tmp/room.sock \
        ROOM_SESSION=pane-parser \
        ROOM_PTY_SOCKET=/tmp/pane-parser.sock \
        claude

# Each agent joins and works
room hello parser --role "rewriting the parser"
room read
room say "parser is done, pushed to the branch"
room wait --timeout 240

# You watch, from anywhere
tail -f /tmp/room.log
room who
```

Join as a human with `--human`, which resets the flood cap whenever you speak:

```bash
export ROOM_SOCKET=/tmp/room.sock ROOM_SESSION=me
room hello mst --human
room say "@parser drop the lookahead, we do not need it"
```

## Commands

| Command | |
|---|---|
| `room serve` | run the room (`--log`, `--cap`, `--no-daemon`) |
| `room hello LABEL` | join, get a nick (`--role`, `--human`, `--since`, `--pty-socket`) |
| `room say TEXT` | append one line, seen by everyone |
| `room read` | what is new since your cursor (`--mentions`) |
| `room wait` | block until someone speaks (`--timeout`, `--mentions`) |
| `room who` | roster, with liveness and unread counts |
| `room bye` | leave |
| `room release [N]` | allow more messages after a flood cap |
| `room stop` | stop the room |

`--socket` and `--session` default to `$ROOM_SOCKET` and `$ROOM_SESSION`.

## Why not IRC

Because none of IRC is the hard part here. Everything is local, every agent is
a process the supervisor itself started, and the supervisor can always wake a
pane through interminai. That leaves an ordered log with per-reader cursors,
which is a file and a lock. Nicks, netsplits, TLS, SASL and federation would be
work spent on problems this does not have.

## Requirements

Python 3.8+, no dependencies. interminai is optional, and only used for
liveness in `room who`.

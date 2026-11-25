# room - a shared room for AI agents

One agent spawns several others, each in its own
[interminai](https://github.com/mstsirkin/interminai) pane. Without a room it
has to relay: read what one pane said, retype it into the next. That costs the
supervisor's context twice per hop, and it only happens when the supervisor
remembers to do it.

Here an agent says a thing once, and the others read it.

## See it work

![Two agents reviewing code in a room: the roster shows author and reviewer
both alive, and the log shows author asking the reviewer to confirm a bug in
median(), and the reviewer confirming it](demo.png)

Two `claude` sessions, each in its own interminai pane, given a file with a
subtle bug in it. Neither was told how to reach the other: each found the room
through the skill, worked out its own nick, said its piece, and went back to
blocking in `room wait`.

<details>
<summary>The same thing as text</summary>

```
$ room who
author           alive        unread=0  writes code
reviewer         alive        unread=0  reviews code
-- 5 messages, 47 more before the room pauses for a human

$ cat room.log
* author was invited (writes code)
* reviewer was invited (reviews code)
[0003] <reviewer> reviewer ready for review requests.
[0004] <author> @reviewer stats.py median() returns ordered[len//2] - for
       even-length lists that's the upper middle element, not the average of
       the two central ones. Can you confirm it's incorrect?
[0005] <reviewer> Confirmed: stats.py:7 returns ordered[len//2], which is the
       upper middle element for even length. For [1,2,3,4] that gives 3
       instead of 2.5.
```

</details>

Asked for brevity, they were brief. Left to themselves on the same file, the
reviewer ran the code rather than reading it, found that the bug also breaks
unsorted duplicates, and pointed out that the obvious fix still returns the
wrong answer for empty input.

## What it does

- **Agents think in parallel.** Everyone idle sits blocked in `room wait`. One
  message wakes all of them at once, and each goes off and thinks in its own
  process. Nobody hands out turns.
- **The log is serialized.** The daemon numbers every line under a lock, so all
  members read the same history in the same order.
- **Each member has a cursor.** `room read` returns only what is new, because
  an agent cannot remember where it got to between turns. Nobody is shown its
  own lines back.
- **The supervisor invites.** Inviting settles an agent's nick before it has
  run, so the roster is known up front rather than discovered pane by pane, and
  the invitation hands back the environment that gets that agent in.
- **A restart resumes.** Identity is the session, not the process, so an agent
  whose session restarts keeps its nick and its place in the log instead of
  reappearing as a stranger.
- **Liveness comes from interminai**, which owns the pty and knows when the
  agent died. `room who` reports `alive`, `exited(N)` or `left`, so nobody
  waits forever on a reply from a process that is gone.
- **There is a flood cap.** Two agents left alone will thank each other until
  the quota runs out. After 50 messages with no human the room pauses.

## Quick start

```bash
# Start the room. It picks an unguessable name under the working directory.
room serve --log room.log
# Socket: /home/you/myproject/.room-x1j08ir3/sock

# Invite an agent. The invitation hands back the environment it needs.
room invite reviewer --session pane-rev \
    --role "reviews code" \
    --pty-socket /tmp/pane-rev.sock
# invited: reviewer
# env: ROOM_SOCKET=/home/you/myproject/.room-x1j08ir3/sock ROOM_SESSION=pane-rev ...

# Spawn it with exactly that environment
interminai start --socket /tmp/pane-rev.sock -- \
    env ROOM_SOCKET=... ROOM_SESSION=pane-rev ROOM_PTY_SOCKET=... \
        claude

# Join yourself, and watch
export ROOM_SOCKET=.room-x1j08ir3/sock ROOM_SESSION=me
room hello mst --human
room say "@reviewer stats.py needs a second pair of eyes"
tail -f room.log
```

A human member resets the flood cap whenever they speak, which is what lets a
long unattended run keep going.

## Install

```bash
make install-all      # ~/.agents/skills, every ~/.claude*, ~/.codex*,
                      # ~/.cursor* and ~/.gemini* profile, and ~/.local/bin
make install-agents   # just ~/.agents/skills
make uninstall
```

This installs `skills/room`, so an agent discovers the room on its own and
knows to read before acting, to say what it found, and to wait rather than
end its turn.

## Commands

| Command | |
|---|---|
| `room serve` | run the room (`--log`, `--cap`, `--no-daemon`) |
| `room invite LABEL --session S` | bring another agent in and settle its nick |
| `room hello LABEL` | join yourself (`--role`, `--human`, `--since`, `--pty-socket`) |
| `room say TEXT` | append one line, seen by everyone |
| `room read` | what is new since your cursor (`--mentions`) |
| `room wait` | block until someone speaks (`--timeout`, `--mentions`) |
| `room who` | the roster, with liveness, and which member is you |
| `room bye` | leave |
| `room release [N]` | allow more messages after a flood cap |
| `room stop` | stop the room |

`--socket` and `--session` default to `$ROOM_SOCKET` and `$ROOM_SESSION`.
`invite` is the exception: its `--session` names the agent being invited, so it
is required outright rather than falling back to the caller's.

## Where the room lives

`room serve` creates a `.room-XXXXXXXX` directory under the working directory
and puts the socket in it, so a room belongs to the project it is about rather
than to a shared `/tmp`. The directory is `0700`, which is what stops another
user on the machine from reaching the socket inside it, and it is removed when
the room stops.

The name is unguessable, so an agent cannot find the room by itself. That is
what the invitation is for. Pass `--socket` to put the room somewhere specific
instead, and note that a socket you place in a shared directory yourself is
reachable by anyone who can write to it.

This is not a boundary between your own agents: anything running as your uid
can read another process's environment and find the path there. It keeps other
users out, and keeps agents out of rooms they were not invited to.

## Why not IRC

Because none of IRC is the hard part here. Everything is local, every agent is
a process the supervisor itself started, and the supervisor can always wake a
pane through interminai. That leaves an ordered log with per-reader cursors,
which is a file and a lock. Nicks, netsplits, TLS, SASL and federation would be
work spent on problems this does not have.

See [PROTOCOL.md](PROTOCOL.md) for the socket protocol.

## Tests

```bash
make test
```

56 tests, driving the CLI the way an agent's shell does, against a real daemon.
See [TESTING.md](TESTING.md).

## Requirements

Python 3.8+, no dependencies. interminai is optional, and only used for
liveness in `room who`.

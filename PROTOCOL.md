# room Socket Protocol

## Overview

The `room` daemon communicates with client commands via a Unix domain socket
using a simple JSON-based request-response protocol. It is deliberately the
same shape as the interminai protocol, so the two tools read alike.

Protocol version: 1.

## Connection Model

- Client connects to Unix socket
- Client sends one JSON request (newline-terminated)
- Daemon sends one JSON response (newline-terminated)
- Connection closes after response (except for WAIT, which may block)

Each connection is handled on its own thread, because a blocked WAIT must not
stop the rest of the room from talking.

## Request Format

```json
{
  "type": "COMMAND_NAME",
  "data": { ... }
}
```

## Response Format

```json
{
  "status": "ok" | "error",
  "data": { ... },
  "error": "error message if status is error"
}
```

## Identity

Every request that acts as a member carries a `session`: a stable name for the
agent, normally the interminai pane it runs in, passed as `$ROOM_SESSION`.

The session, not the process, is the identity. `room hello` is a short-lived
subprocess of the agent's shell, so neither its pid nor its socket says
anything about whether the agent is alive. Keying on the session is what lets
a restarted agent resume as itself instead of reappearing as a stranger.

The nick is assigned by the server and is a display label only. Clients never
choose it.

## Ordering

The daemon assigns `seq` under a single lock, so every member observes the same
total order. Each member holds a cursor, which is the highest `seq` it has
read. READ and WAIT return messages above the cursor and then advance it to the
current `seq`.

A member never receives its own lines back, from either READ or WAIT. They
would cost it context and invite it to answer itself, and SAY already returns
the sequence number as confirmation.

---

## Commands

### HELLO - Join the room, or resume an earlier membership

**Request:**
```json
{
  "type": "HELLO",
  "data": {
    "session": "pane-parser",
    "label": "parser",
    "role": "rewriting the parser",
    "human": false,
    "pty_socket": "/tmp/pane-parser.sock",
    "since": "end"
  }
}
```

**Request fields:**
- `session`: Stable identity. Required.
- `label`: Requested nick. Reduced to alphanumerics, `-` and `_`, truncated to
  24 characters, and uniquified with a numeric suffix if a member still in the
  room holds it. A nick left by a departed member is reused as-is.
- `role`: One line describing what this member is doing. Shown in WHO.
- `human`: A human member. Speaking resets the flood cap.
- `pty_socket`: interminai socket of this member's pane, used by WHO for
  liveness. Optional; without it WHO reports `unknown`.
- `since`: Where the cursor starts. `"end"` (default) for only what is said
  from now on, `"all"` for the whole backlog, or a number for that many
  messages back.

**Response:**
```json
{
  "status": "ok",
  "data": {
    "nick": "parser",
    "resumed": false,
    "cursor": 12,
    "unread": 0
  }
}
```

**Response fields:**
- `nick`: The assigned nick, which may differ from the requested label.
- `resumed`: True if this session was already known. `label` and `since` are
  then ignored, and the existing nick and cursor are kept. Resetting the cursor
  on resume would silently re-read or skip the room.
- `cursor`: Where this member will read from.
- `unread`: Messages above the cursor.

A join appends an event line to the log. A new member's cursor is placed past
its own arrival, so it does not read about itself.

**Errors:**
- No session given
- Bad `since` value

---

### INVITE - Bring another agent into the room

Same request and response as HELLO, and the same resume rule. The difference is
who is asking: HELLO is an agent bringing itself in, INVITE is the supervisor
bringing someone else in.

`session` therefore names the agent being invited, not the caller. The client
must not default it from `$ROOM_SESSION`, which would quietly invite the
supervisor instead.

```json
{
  "type": "INVITE",
  "data": {
    "session": "pane-parser",
    "label": "parser",
    "role": "rewriting the parser",
    "pty_socket": "/tmp/pane-parser.sock"
  }
}
```

Inviting settles the nick before the agent has run, so the supervisor knows the
whole roster up front rather than discovering it from WHO once each agent wakes
up. An invited agent is already a member: its first READ works without a HELLO,
and a HELLO from that session resumes, as it would for any returning member.

The event line reads `was invited` rather than `joined`.

---

### SAY - Append one line to the log

**Request:**
```json
{
  "type": "SAY",
  "data": {
    "session": "pane-parser",
    "text": "parser is done, pushed to the branch"
  }
}
```

**Response:**
```json
{
  "status": "ok",
  "data": {
    "seq": 13,
    "budget": 47
  }
}
```

**Response fields:**
- `seq`: Sequence number assigned to this message.
- `budget`: Messages a non-human member may still append before the room
  pauses.

**Errors:**
- Nothing to say (empty after stripping)
- Not in the room, say hello first
- Flood cap reached. Two agents left alone will acknowledge each other until
  the quota runs out, so the room refuses to append after `cap` consecutive
  messages with no human. A human speaking, or RELEASE, clears it.

---

### READ - Everything said since this member last looked

**Request:**
```json
{
  "type": "READ",
  "data": {
    "session": "pane-parser",
    "mentions": false
  }
}
```

**Request fields:**
- `mentions`: Return only lines addressing this member: text containing
  `@nick`, text starting with `nick:`, or any event line. The cursor still
  advances past the filtered-out messages, which are not retrievable
  afterwards.

**Response:**
```json
{
  "status": "ok",
  "data": {
    "messages": [
      {"seq": 13, "kind": "msg", "nick": "lexer", "text": "token enum is on main", "time": 1764000000.0},
      {"seq": 14, "kind": "event", "nick": "docs", "text": "docs joined (api reference)"}
    ],
    "skipped": 0
  }
}
```

**Response fields:**
- `messages`: Messages above the cursor, in order. `kind` is `msg` for
  something a member said, `event` for joins, departures and room notices.
- `skipped`: How many messages the `mentions` filter discarded.

**Errors:**
- Not in the room, say hello first

---

### WAIT - Block until something is said, then read it

Every idle agent sits here, so one append wakes all of them and they go on to
think in parallel.

**Request:**
```json
{
  "type": "WAIT",
  "data": {
    "session": "pane-parser",
    "mentions": false,
    "timeout": 240
  }
}
```

**Request fields:**
- `timeout`: Seconds to block before returning empty. Default 240. Keep it
  under the agent harness's own tool timeout.
- `mentions`: Only wake for lines addressing this member.

**Response:** as READ, plus:
```json
{
  "timeout": true
}
```

`timeout` is true when the wait expired with nothing to report. The daemon also
returns early if the client disconnects, so a dead agent does not hold a thread.

**Errors:**
- Not in the room, say hello first
- Client disconnected

---

### WHO - The roster

**Request:**
```json
{
  "type": "WHO"
}
```

**Response:**
```json
{
  "status": "ok",
  "data": {
    "members": [
      {
        "nick": "parser",
        "role": "rewriting the parser",
        "human": false,
        "session": "pane-parser",
        "unread": 3,
        "gone": false,
        "state": "alive"
      }
    ],
    "budget": 47,
    "cap": 50,
    "seq": 14
  }
}
```

**Member states:**
- `alive`: interminai reports the pane's process still running.
- `exited(N)`: the process exited with status N. No reply is coming.
- `gone`: the interminai socket could not be reached.
- `left`: the member said BYE.
- `unknown`: no `pty_socket` was given at HELLO.

Liveness is asked of interminai rather than determined here, because interminai
owns the pty and gets SIGCHLD. The STATUS request it sends deliberately does not
use activity mode: that would clear interminai's activity flag and steal the
notification from whoever is driving the pane. The cost is that WHO cannot tell
an idle agent from a thinking one.

Panes are probed outside the room lock, so a hung pane cannot stall the room.

---

### BYE - Leave the room

**Request:**
```json
{
  "type": "BYE",
  "data": {"session": "pane-parser"}
}
```

**Response:**
```json
{
  "status": "ok",
  "data": {"nick": "parser"}
}
```

Appends an event line. The member is kept, marked gone, so its nick can be
reused and its history stays readable in WHO.

**Errors:**
- Not in the room

---

### RELEASE - Allow more messages after a flood cap

**Request:**
```json
{
  "type": "RELEASE",
  "data": {"count": 10}
}
```

`count` defaults to the room's cap.

**Response:**
```json
{
  "status": "ok",
  "data": {"budget": 10}
}
```

---

### STOP - Stop the daemon

**Request:**
```json
{
  "type": "STOP"
}
```

**Response:**
```json
{
  "status": "ok",
  "data": {}
}
```

The daemon answers, then leaves its accept loop within half a second and
unlinks the socket.

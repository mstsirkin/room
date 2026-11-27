---
name: room
description: Talk to the other agents working alongside you. Use when you are one of several agents in a shared room - to read what the others have said, say what you found or finished, ask another agent for something, or wait while they work. Use whenever you are about to end your turn and the job is not done, instead of stopping. Also use when you need a result from another agent rather than doing their work yourself.
allowed-tools: Shell
license: See LICENSE file
metadata:
  author: Michael S. Tsirkin <mst@kernel.org>
  version: 0.1.0
  category: coordination
---

# room

Author: Michael S. Tsirkin <mst@kernel.org>

A shared room for several agents working on the same thing. You say something
once and everyone else sees it, so nobody has to relay messages between you.

Everyone thinks in parallel. The log is serialized, so every member sees the
same lines in the same order, and each has its own read cursor.

## Which one are you

There are two roles and this file describes both. Work out which you are
before doing anything else:

```bash
echo "ROOM_SOCKET=${ROOM_SOCKET:-unset}"; room who 2>&1 | head -5
```

**`ROOM_SOCKET` is set: you are a guest.** Someone created a room and invited
you into it. `$ROOM_SOCKET` and `$ROOM_SESSION` are already set for you, so
you never pass them. Go to "Being in a room".

**`ROOM_SOCKET` is unset but `who` lists members: a room already exists
here**, and you are almost certainly the host who made it on an earlier turn.
Carry on with it. Check whether the line marked `host` is you.

**`ROOM_SOCKET` is unset and `who` finds no room:** there is no room. Create
one only if your job is to run several other agents, as below. Otherwise you
have no use for this skill.

Whichever you are, **run `room serve` only when there is no room**. A second
room is not an error anybody reports: it is empty, nobody else is in it, and
your messages go there and are never read.

Inviting and releasing belong to the host. As a guest, if you want someone
brought in, or the room has hit its cap, say so in the room rather than doing
it yourself.

## Hosting a room

If you are the agent spawning the others, you host the room. Set your own
session before starting it, because the host is taken from `$ROOM_SESSION`:

```bash
export ROOM_SESSION=manager
room serve --log room.log
room hello manager --role "runs the review"
```

You do not need to set `ROOM_SOCKET`, and you should not pass `--socket`. The
room lives in a `.room-XXXXXXXX` directory under the one you started it in,
and your later commands find it there, from that directory or any below it.
Putting the socket somewhere predictable instead, such as `/tmp`, makes it
reachable by anyone else on the machine.

**Use the same `ROOM_SESSION` for `serve` and for your own `hello`.** If they
differ you join as an ordinary worker, the room has no host, and nobody can
lift the flood cap. `room who` marks the host and says so when there is none;
check it once after setting up.

Being the host means two things. Anything you do, down to a `room read`,
counts as the room being attended and clears the cap, so a room you are
working in will not stall. And you are the only member who may run `room
release`, because an agent that can lift its own cap does not have one.

Then bring each worker in, and spawn it with the environment you are handed:

```bash
room invite lexer --session pane-lexer --pty-socket /tmp/pane-lexer.sock \
    --role "token tables"
# invited: lexer
# env: ROOM_SOCKET=... ROOM_SESSION=pane-lexer ROOM_PTY_SOCKET=...
```

Pass `--pty-socket` when the worker runs in an interminai pane. That is what
lets the room see whether it is working or sitting at its prompt, and wake it
when it is needed.

## Being in a room

You are already a member, because the host invited you before your pane was
started. You do not need to say hello, and you did not choose your own nick,
so begin by finding out what it is:

```bash
room who
# you are: author
# host manager   waiting  unread=0  runs the review
#      author    busy     unread=0  writes code
#      reviewer  idle     unread=2  reviews code
```

The line marked `host` is the agent running all this. The rest are your
fellow workers. Use the nick the room gave you, and do not say hello as one
of the others.

If `room who` says you are not in the room, bring yourself in:

```bash
room hello builder --role "rewriting the parser"
```

The room may give you a different nick from the label you asked for, if that
one was taken. Use the one it gives you. If your session restarts, say hello
again: you get the same nick and the same place in the log back.

## The loop

```bash
room read                  # what was said since you last looked
room say "parser is done, pushed to the branch"
room wait --timeout 240    # block until someone says something
```

Work like this:

1. `room read` before you start, so you act on what the others already know.
2. `room say` whatever the others need: what you found, what you finished,
   what you are about to touch, what you need from someone.
3. When you have nothing left to do, `room wait` instead of ending your turn.
   You will be woken when anyone speaks. Loop back to step 2.

Ending your turn makes you deaf until someone pokes your pane. Prefer `wait`.
If `wait` returns "nothing new" and your work really is finished, say so and
then stop.

## Talking to one agent

Start the line with their nick, or mention them:

```bash
room say "@lexer I need the token enum before I can finish"
room read --mentions
```

`--mentions` shows only lines aimed at you, which keeps a busy room out of
your context. It still advances your cursor past everything else, so you will
not see the skipped lines later. Use plain `room read` when you need the
whole picture.

## Coming back after a long job

If you have been working for an hour the room may have a lot waiting. Do not
pull all of it into your context:

```bash
room read --max 20      # the newest 20, and a count of what it skipped
room log --last 40      # look back further, without moving your cursor
room log --from 120 --to 140
```

`room log` never touches your cursor, so it is the safe way to go back over
something, including anything a bounded read skipped.

You never see your own lines back, from either `read` or `wait`. If you said
something and then read nothing, that is working correctly, not a lost
message. `say` already confirmed it with a sequence number.

## Who else is here

```bash
room who
```

Shows every member, their role, and whether they are in a position to notice
you:

| | |
|---|---|
| `waiting` | blocked in `room wait`, will see your next line at once |
| `busy` | working, will read when it finishes |
| `idle` | sitting at its prompt, so it is deaf until something wakes it |
| `exited(N)`, `gone`, `left` | no reply is coming, say so rather than block |

Check this before waiting on a reply. If the member you need is `idle`, say
your line and then wake them:

```bash
room ask lexer "I need the token enum before I can finish"
```

`ask` says it in the room and wakes them, which is better than typing into
their terminal: the answer comes back into the log where it stays, instead of
scrolling off a screen and having to be asked for again. The room also wakes
an idle member by itself once messages have been waiting a while, so you do
not have to watch for this.

A `busy` member is left alone unless you pass `--steer`, which interrupts it
mid-task. Use that only when the work it is doing is no longer the work you
want.

## Rules

- Say things once. Do not repeat what another agent already said.
- Do not acknowledge for the sake of acknowledging. "Thanks" and "sounds
  good" cost everyone a wake-up and read. Speak when you have information.
- The room stops accepting messages after 50 in a row with no sign of the
  host, who is whoever created the room. `room who` marks them and ends with
  how many messages you have left, counting down. That is a brake on runaway
  chatter between agents, not a reason to hold back something you need to
  send: say it, and if you do hit the cap, `say` tells you plainly rather
  than losing anything. Only the host can lift it, so ask in the room rather
  than trying to release it yourself.

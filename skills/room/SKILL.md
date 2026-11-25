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

`$ROOM_SOCKET` and `$ROOM_SESSION` are set for you. You do not pass them.

## Joining

You may have been invited already, in which case you are a member and `room
read` works straight away. Run `room who` to see your nick if you do not know
it.

Otherwise bring yourself in:

```bash
room hello builder --role "rewriting the parser"
```

The room assigns your nick, which may not be the label you asked for if it was
taken. Use the nick it gives you. If your session restarts, say hello again:
you get the same nick and the same place in the log back.

To bring another agent in, invite it. The session names that agent, not you:

```bash
room invite lexer --session pane-lexer --role "token tables"
# invited: lexer
# env: ROOM_SOCKET=... ROOM_SESSION=pane-lexer
```

The room's name is unguessable, so an agent cannot find it by itself. Spawn it
with the environment the invitation printed, or it will not be able to reach
the room at all.

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

## Who else is here

```bash
room who
```

Shows every member, their role, and whether their pane is `alive`,
`exited(N)`, or `left`. Check it before waiting on a reply from someone: if
they exited, no reply is coming and you should say so rather than block.

## Rules

- Say things once. Do not repeat what another agent already said.
- Do not acknowledge for the sake of acknowledging. "Thanks" and "sounds
  good" cost everyone a wake-up and read. Speak when you have information.
- The room stops accepting messages after 50 in a row with no human. If you
  hit that cap, you were almost certainly talking to another agent in a loop.

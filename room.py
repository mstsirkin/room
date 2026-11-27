#!/usr/bin/env python3
"""room - a shared room for AI agents

One supervisor spawns several agents, each in its own interminai pane.
Instead of the supervisor relaying output from one pane into another, every
agent joins a room: it says things once, and the others read them.

Agents think in parallel, the log is serialized. The daemon assigns a
sequence number to every message under a lock, so all members see the same
history in the same order, and each member has its own read cursor.
"""

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time


PROTOCOL_VERSION = 1

# Refuse to append after this many messages with no sign of the host. Two
# agents left to themselves will thank each other until the quota runs out.
# Anything the host does resets this, so it only bites when the room really
# has been left running on its own.
DEFAULT_CAP = 50

# How often to look at each member's pane.
WATCH_EVERY = 2.0

# A pane whose screen has not changed for this long is at its prompt rather
# than working. Agent TUIs tick a seconds counter while they think, so their
# screen keeps changing; an idle one redraws nothing at all.
SETTLED_AFTER = 6.0

# Leave an idle member this long to read by itself before poking it, and do
# not poke the same member again inside this window.
DEFAULT_POKE_AFTER = 45.0

POKE_TEXT = ("There are unread messages in the room addressed to you or your "
             "work. Run: room read. Act on anything that needs you, say what "
             "you found with room say, then go back to room wait.")


class Member:
    """One participant in the room"""

    def __init__(self, session, nick, role, host, pty_socket, cursor):
        self.session = session
        self.nick = nick
        self.role = role
        self.host = host
        self.pty_socket = pty_socket
        self.cursor = cursor
        self.joined = time.time()
        self.gone = False

        # Pane watching. screen is the last thing the pane showed, settled is
        # when it last changed, and waits counts the WAIT calls this member
        # has open right now.
        self.screen = None
        self.settled = time.time()
        self.liveness = 'unknown'
        self.waits = 0
        self.poked = 0.0

    def attention(self):
        """Whether this member is in a position to notice a new message

        - waiting: blocked in room wait, so it sees the next line at once
        - busy: its pane is still changing, so it is working and will read
          when it is done, or can be steered
        - idle: alive but at its prompt, which means deaf until something
          types into its pane
        """
        if self.gone:
            return 'left'


        if self.waits > 0:
            return 'waiting'

        if self.liveness != 'alive':
            return self.liveness

        if self.screen is None:
            return 'unknown'

        return 'idle' if time.time() - self.settled >= SETTLED_AFTER else 'busy'


class RoomState:
    """Shared state of the room, guarded by a single lock"""

    def __init__(self, socket_path, log_file, cap, poke_after=DEFAULT_POKE_AFTER,
                 host_session=None):
        self.lock = threading.Lock()
        self.socket_path = socket_path
        self.log_file = log_file
        self.cap = cap
        self.budget = cap
        self.messages = []
        self.seq = 0
        self.members = {}
        self.running = True
        self.poke_after = poke_after
        # Whoever created the room. Not a human necessarily: in practice it is
        # the agent that spawned the others, and it is the party accountable
        # for what they spend.
        self.host_session = host_session

    def append(self, kind, nick, text):
        """Append a line to the log and return it. Caller holds the lock."""
        self.seq += 1
        message = {
            'seq': self.seq,
            'kind': kind,
            'nick': nick,
            'text': text,
            'time': time.time(),
        }
        self.messages.append(message)

        if self.log_file:
            self.log_file.write(format_message(message) + '\n')
            self.log_file.flush()

        return message

    def nick_taken(self, nick):
        """Caller holds the lock"""
        for member in self.members.values():
            if member.nick == nick and not member.gone:
                return True
        return False

    def find_nick(self, nick):
        """Caller holds the lock"""
        for member in self.members.values():
            if member.nick == nick:
                return member
        return None


def format_message(message):
    """Render one log line for an agent to read"""
    if message['kind'] == 'event':
        return f"* {message['text']}"
    return f"[{message['seq']:04d}] <{message['nick']}> {message['text']}"


def sanitize_label(label):
    """Reduce a requested label to a plausible nick"""
    cleaned = ''.join(c for c in label if c.isalnum() or c in '-_')
    return cleaned[:24] or 'agent'


def mentions(message, nick):
    """Does this message address nick?"""
    if message['kind'] == 'event':
        return True
    text = message['text']
    return f'@{nick}' in text or text.startswith(f'{nick}:')


# --- Request handlers -------------------------------------------------------


def handle_hello(data, state):
    """Handle HELLO request - an agent bringing itself into the room"""
    return admit(data, state, invited=False)


def handle_invite(data, state):
    """Handle INVITE request - the supervisor bringing an agent in

    The session here names the agent being invited, not whoever is asking.
    Inviting settles the nick before the agent has run, so the supervisor
    knows the roster up front instead of discovering it afterwards.
    """
    return admit(data, state, invited=True)


def admit(data, state, invited):
    """Bring a session into the room, or resume an earlier membership"""
    session = data.get('session')
    if not session:
        return {'status': 'error', 'error': 'No session given'}

    label = sanitize_label(data.get('label') or 'agent')
    role = data.get('role') or ''
    pty_socket = data.get('pty_socket')
    since = data.get('since', 'end')

    with state.lock:
        host = session == state.host_session

        # A session that is already known is the same worker coming back, even
        # if its process restarted. Resume it: same nick, same cursor. Resetting
        # the cursor here would silently re-read or skip the room.
        member = state.members.get(session)
        if member:
            member.gone = False
            if pty_socket:
                member.pty_socket = pty_socket
            if role:
                member.role = role
            return {
                'status': 'ok',
                'data': {
                    'nick': member.nick,
                    'resumed': True,
                    'cursor': member.cursor,
                    'unread': state.seq - member.cursor,
                },
            }

        # A nick left behind by a departed member can be reused. A nick held by
        # someone still here cannot, so uniquify it.
        nick = label
        if state.nick_taken(nick):
            suffix = 2
            while state.nick_taken(f'{label}{suffix}'):
                suffix += 1
            nick = f'{label}{suffix}'

        stale = state.find_nick(nick)
        if stale:
            del state.members[stale.session]

        if since == 'end':
            cursor = state.seq
        elif since == 'all':
            cursor = 0
        else:
            try:
                cursor = max(0, state.seq - int(since))
            except ValueError:
                return {'status': 'error', 'error': f'Bad --since value: {since}'}

        member = Member(session, nick, role, host, pty_socket, cursor)
        state.members[session] = member

        description = f'{nick} was invited' if invited else f'{nick} joined'
        if role:
            description += f' ({role})'
        state.append('event', nick, description)

        # Past its own arrival, so a new member does not read about itself.
        if since == 'end':
            member.cursor = state.seq
            cursor = member.cursor

        return {
            'status': 'ok',
            'data': {
                'nick': nick,
                'resumed': False,
                'cursor': cursor,
                'unread': state.seq - cursor,
            },
        }


def handle_say(data, state):
    """Handle SAY request - append one line to the log"""
    session = data.get('session')
    text = (data.get('text') or '').strip()

    if not text:
        return {'status': 'error', 'error': 'Nothing to say'}

    with state.lock:
        member = state.members.get(session)
        if not member or member.gone:
            return {'status': 'error', 'error': 'Not in the room, say hello first'}

        if not member.host:
            if state.budget <= 0:
                return {
                    'status': 'error',
                    'error': f'Flood cap reached: {state.cap} messages with no '
                             f'sign of the host. Ask the host to run room '
                             f'release.',
                }
            state.budget -= 1

        message = state.append('msg', member.nick, text)

        return {
            'status': 'ok',
            'data': {'seq': message['seq'], 'budget': state.budget},
        }


def pending_for(state, member):
    """Unread messages for a member. Caller holds the lock.

    A member never reads its own lines back. They cost it context and invite
    it to answer itself, and SAY already returned the sequence number.
    """
    return [
        m for m in state.messages
        if m['seq'] > member.cursor and m['nick'] != member.nick
    ]


def collect(state, member, only_mentions, limit=None):
    """Unread messages for a member, advancing its cursor. Caller holds the lock.

    limit keeps a member that has been working for an hour from being handed
    the whole backlog at once. The dropped messages are the oldest, and they
    stay in the log file for anyone who wants them.
    """
    pending = pending_for(state, member)
    member.cursor = state.seq

    skipped = 0
    if only_mentions:
        wanted = [m for m in pending if mentions(m, member.nick)]
        skipped = len(pending) - len(wanted)
        pending = wanted

    if limit and len(pending) > limit:
        skipped += len(pending) - limit
        pending = pending[-limit:]

    return pending, skipped


def handle_read(data, state):
    """Handle READ request - everything said since this member last looked"""
    session = data.get('session')
    only_mentions = bool(data.get('mentions'))

    with state.lock:
        member = state.members.get(session)
        if not member or member.gone:
            return {'status': 'error', 'error': 'Not in the room, say hello first'}

        messages, skipped = collect(state, member, only_mentions,
                                    data.get('max'))
        return {
            'status': 'ok',
            'data': {'messages': messages, 'skipped': skipped},
        }


def handle_wait(data, state, client_sock):
    """Handle WAIT request - block until something is said, then read it

    Every idle agent sits here, so they all wake on the same append and then
    think in parallel.
    """
    session = data.get('session')
    only_mentions = bool(data.get('mentions'))
    timeout = data.get('timeout') or 240
    deadline = time.time() + timeout

    # A member blocked here is the one case where the room knows for certain
    # that it has the member's attention, so record it rather than inferring
    # anything from its pane.
    with state.lock:
        waiter = state.members.get(session)
        if waiter:
            waiter.waits += 1
    try:
        return wait_loop(session, only_mentions, data.get('max'), deadline,
                         state, client_sock)
    finally:
        with state.lock:
            if waiter:
                waiter.waits -= 1


def wait_loop(session, only_mentions, limit, deadline, state, client_sock):
    """Block until this member has something to read"""
    while True:
        with state.lock:
            member = state.members.get(session)
            if not member or member.gone:
                return {'status': 'error', 'error': 'Not in the room, say hello first'}

            pending = pending_for(state, member)
            if only_mentions:
                pending = [m for m in pending if mentions(m, member.nick)]
            has_unread = bool(pending)

            if has_unread:
                messages, skipped = collect(state, member, only_mentions, limit)
                return {
                    'status': 'ok',
                    'data': {'messages': messages, 'skipped': skipped,
                             'timeout': False},
                }

        if time.time() >= deadline:
            return {'status': 'ok', 'data': {'messages': [], 'skipped': 0, 'timeout': True}}

        # Give up if the agent went away while we were holding its turn open.
        try:
            client_sock.settimeout(0)
            if client_sock.recv(1, socket.MSG_PEEK) == b'':
                return {'status': 'error', 'error': 'Client disconnected'}
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            return {'status': 'error', 'error': 'Client disconnected'}
        finally:
            client_sock.settimeout(None)

        time.sleep(0.1)


def pty_request(pty_socket, request, timeout=5):
    """Send one request to the interminai daemon owning a member's pane

    The room has no useful pid of its own: "room hello" is a short-lived
    subprocess of the agent's shell, not the agent. interminai owns the pty and
    gets SIGCHLD, so it is the one that actually knows anything about it.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(pty_socket)
        sock.sendall(json.dumps(request).encode('utf-8') + b'\n')

        data = b''
        while b'\n' not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk

        if not data:
            return None

        response = json.loads(data.decode('utf-8').strip())
        if response.get('status') != 'ok':
            return None

        return response.get('data', {})

    except (OSError, ValueError):
        return None
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def pane_screen(pty_socket):
    """What a member's pane is showing, or None if it cannot be reached

    Deliberately not interminai's activity flag. That flag is set by any pty
    output at all, so a TUI redrawing its own spinner looks busy forever, and
    reading it clears it, stealing the notification from whoever else is
    driving the pane. The rendered screen says more and costs nothing.
    """
    if not pty_socket:
        return None

    data = pty_request(pty_socket, {'type': 'OUTPUT', 'format': 'ascii'})
    if data is None:
        return None

    return data.get('screen', '')


def pane_liveness(pty_socket):
    """Whether the process in a member's pane is still running"""
    if not pty_socket:
        return 'unknown'

    data = pty_request(pty_socket, {'type': 'STATUS'})
    if data is None:
        return 'gone'

    if data.get('running'):
        return 'alive'

    return f"exited({data.get('exit_code', '?')})"


def pane_submit(pty_socket, text):
    """Type a message into a pane and submit it

    Text and Enter have to be separate writes with a pause between them.
    A React/Ink TUI, which is what these agents are, does not have its input
    state ready for the Enter that arrives in the same write, so the message
    lands in the box and sits there unsent. That failure is silent: the room
    would believe it had poked an agent that never woke up.
    """
    if pty_request(pty_socket, {'type': 'INPUT', 'data': text}) is None:
        return False

    time.sleep(0.4)

    return pty_request(pty_socket, {'type': 'INPUT', 'data': '\r'}) is not None


def poke(state, member, text=None):
    """Type into a member's pane so an unread message becomes a turn

    This is the half the room cannot do on its own. Delivering a message is
    not the same as getting attention: an agent sitting at its prompt will
    never read the room, however many messages are waiting for it.
    """
    if not member.pty_socket:
        return False, 'no pane recorded for this member'

    member.poked = time.time()

    # Taken before submitting, and kept local. Comparing against the member's
    # shared screen would race the watcher, which may refresh it to the
    # post-poke screen first and so hide the very change we are looking for.
    before = pane_screen(member.pty_socket)

    if not pane_submit(member.pty_socket, text or POKE_TEXT):
        return False, 'could not reach the pane'

    # Confirm the pane actually started doing something. If the Enter did not
    # take, the message is sitting unsent in the input box and the member is
    # still deaf, which is the failure this whole mechanism exists to remove.
    for _ in range(20):
        time.sleep(0.5)
        screen = pane_screen(member.pty_socket)
        if screen is not None and screen != before:
            member.screen = screen
            member.settled = time.time()
            return True, 'woken'

    return False, 'pane did not react, the text may be sitting unsent'


def watch_panes(state):
    """Keep each member's pane state current, and wake idle ones

    Runs in the daemon so that WHO is instant and so that a room with nobody
    watching it still makes progress.
    """
    while state.running:
        with state.lock:
            members = [m for m in state.members.values()
                       if not m.gone and not m.host and m.pty_socket]

        # Sampled outside the lock: a hung pane must not stall the room.
        for member in members:
            liveness = pane_liveness(member.pty_socket)
            screen = pane_screen(member.pty_socket) if liveness == 'alive' else None

            with state.lock:
                member.liveness = liveness
                if screen is not None and screen != member.screen:
                    member.screen = screen
                    member.settled = time.time()

        if state.poke_after:
            for member in members:
                with state.lock:
                    unread = len(pending_for(state, member))
                    due = (unread
                           and member.attention() == 'idle'
                           and time.time() - member.poked >= state.poke_after)
                if due:
                    woken, _ = poke(state, member)
                    if woken:
                        with state.lock:
                            state.append('event', 'room',
                                         f'{member.nick} was poked, '
                                         f'{unread} unread')

        time.sleep(WATCH_EVERY)


def handle_poke(data, state):
    """Handle POKE request - wake a member now"""
    with state.lock:
        member = state.find_nick(data.get('nick'))
        if not member or member.gone:
            return {'status': 'error', 'error': f"No member {data.get('nick')}"}
        attention = member.attention()

    # A member blocked in wait sees the next line the moment it is appended.
    # Typing at it would only push stray text into a pane that is mid tool
    # call, to be submitted whenever that call returns.
    if attention == 'waiting' and not data.get('steer'):
        return {'status': 'ok', 'data': {'nick': member.nick, 'was': attention,
                                         'poked': False}}

    if attention == 'busy' and not data.get('steer'):
        return {
            'status': 'error',
            'error': f'{member.nick} is busy. It will read when it finishes. '
                     f'Use --steer to interrupt it anyway.',
        }

    woken, detail = poke(state, member, data.get('text'))
    if not woken:
        return {'status': 'error', 'error': f'{member.nick}: {detail}'}

    with state.lock:
        state.append('event', 'room', f'{member.nick} was poked')

    return {'status': 'ok', 'data': {'nick': member.nick, 'was': attention,
                                     'poked': True}}


def handle_log(data, state):
    """Handle LOG request - look back over history without reading it

    Separate from READ on purpose. READ is about a member's own cursor and
    moves it; this is for going back over what was said, which must not cost
    anyone their place. It is also the way back to messages that a bounded
    read dropped, for a room started without a log file.
    """
    with state.lock:
        first = data.get('from') or 1
        last = data.get('to') or state.seq
        messages = [m for m in state.messages if first <= m['seq'] <= last]

        tail = data.get('last')
        if tail:
            messages = messages[-tail:]

        return {
            'status': 'ok',
            'data': {'messages': messages, 'skipped': 0, 'seq': state.seq},
        }


def handle_ask(data, state):
    """Handle ASK request - say something to a member and make sure it looks

    Asking in the room rather than in a pane is what keeps the answer. A
    question typed into a pane gets an answer on a forty-line screen, which
    scrolls away and then has to be asked for again.
    """
    nick = data.get('nick')
    with state.lock:
        target = state.find_nick(nick)
        if not target or target.gone:
            return {'status': 'error', 'error': f'No member {nick}'}

    said = handle_say(dict(data, text=f'@{nick} {data.get("text", "").strip()}'),
                      state)
    if said['status'] != 'ok':
        return said

    poked = handle_poke(data, state)

    return {
        'status': 'ok',
        'data': {
            'seq': said['data']['seq'],
            'nick': nick,
            'woken': poked['status'] == 'ok',
            'poked': poked.get('data', {}).get('poked', False),
            'detail': poked.get('error') or poked['data']['was'],
        },
    }


def handle_who(data, state):
    """Handle WHO request - the roster, so the supervisor reads one list
    instead of checking every pane"""
    # A member the watcher has not reached yet would show as unknown, which
    # is useless on the first call after inviting everyone. Probe those once,
    # outside the lock; from then on the watcher keeps them current.
    with state.lock:
        fresh = [m for m in state.members.values()
                 if m.pty_socket and not m.gone and m.liveness == 'unknown']

    for member in fresh:
        liveness = pane_liveness(member.pty_socket)
        screen = pane_screen(member.pty_socket) if liveness == 'alive' else None
        with state.lock:
            member.liveness = liveness
            if screen is not None and member.screen is None:
                member.screen = screen
                member.settled = time.time()

    with state.lock:
        # An invited agent never chose its own nick, so the roster has to say
        # which entry is the caller. Without it the only way to find out is to
        # guess a name and see what resume corrects it to.
        caller = state.members.get(data.get('session'))
        you = caller.nick if caller else None
        members = [
            {
                'nick': m.nick,
                'role': m.role,
                'host': m.host,
                'session': m.session,
                # Counted the same way READ selects, or the roster promises a
                # member lines that reading will not return.
                'unread': len(pending_for(state, m)),
                # Answered from what the watcher already saw, so the roster
                # is instant however many panes are hung.
                'state': m.attention(),
                'gone': m.gone,
            }
            for m in state.members.values()
        ]
        budget = state.budget
        cap = state.cap
        seq = state.seq

    return {
        'status': 'ok',
        'data': {'members': members, 'budget': budget, 'cap': cap,
                 'seq': seq, 'you': you},
    }


def handle_bye(data, state):
    """Handle BYE request - leave the room"""
    session = data.get('session')

    with state.lock:
        member = state.members.get(session)
        if not member or member.gone:
            return {'status': 'error', 'error': 'Not in the room'}

        member.gone = True
        state.append('event', member.nick, f'{member.nick} left')
        return {'status': 'ok', 'data': {'nick': member.nick}}


def handle_release(data, state):
    """Handle RELEASE request - hand the agents more turns after a flood cap

    Only the host. An agent that can lift its own cap does not have a cap,
    and left to themselves the agents will simply release and carry on, which
    is the behaviour the cap exists to interrupt.
    """
    with state.lock:
        if state.host_session and data.get('session') != state.host_session:
            return {
                'status': 'error',
                'error': 'Only the host can release the room. Say what you '
                         'need in the room and let the host decide.',
            }

        count = data.get('count') or state.cap
        state.budget = count
        state.append('event', 'room', f'released, {count} more messages')
        return {'status': 'ok', 'data': {'budget': state.budget}}


# --- Daemon -----------------------------------------------------------------


def handle_client(client_sock, state):
    """Handle a client connection"""
    try:
        data = b''
        while True:
            chunk = client_sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b'\n' in data:
                break

        if not data:
            return

        request = json.loads(data.decode('utf-8').strip())
        req_type = request.get('type')
        req_data = request.get('data') or {}

        # Any sign of the host at all means the room is not running
        # unattended, which is the only thing the cap is meant to catch.
        # Requiring the host to speak made the cap fire during ordinary work.
        if state.host_session and req_data.get('session') == state.host_session:
            with state.lock:
                state.budget = state.cap

        if req_type == 'HELLO':
            response = handle_hello(req_data, state)
        elif req_type == 'INVITE':
            response = handle_invite(req_data, state)
        elif req_type == 'SAY':
            response = handle_say(req_data, state)
        elif req_type == 'READ':
            response = handle_read(req_data, state)
        elif req_type == 'WAIT':
            response = handle_wait(req_data, state, client_sock)
        elif req_type == 'LOG':
            response = handle_log(req_data, state)
        elif req_type == 'POKE':
            response = handle_poke(req_data, state)
        elif req_type == 'ASK':
            response = handle_ask(req_data, state)
        elif req_type == 'WHO':
            response = handle_who(req_data, state)
        elif req_type == 'BYE':
            response = handle_bye(req_data, state)
        elif req_type == 'RELEASE':
            response = handle_release(req_data, state)
        elif req_type == 'STOP':
            response = {'status': 'ok', 'data': {}}
        else:
            response = {'status': 'error', 'error': f'Unknown command: {req_type}'}

        response_json = json.dumps(response) + '\n'
        client_sock.sendall(response_json.encode('utf-8'))

        # Only after the answer is out. Tearing the daemon down first loses
        # the reply, and the client reports that it got no response.
        if req_type == 'STOP':
            state.running = False

    except Exception as e:
        error_response = json.dumps({'status': 'error', 'error': str(e)}) + '\n'
        try:
            client_sock.sendall(error_response.encode('utf-8'))
        except OSError:
            pass
    finally:
        try:
            client_sock.close()
        except OSError:
            pass


def auto_socket_path():
    """Make a room directory with an unguessable name under the current one

    Under the working directory rather than a shared one, so a room belongs to
    the project it is about. mkdtemp also makes it 0700, which is what keeps
    another user on the machine from reaching the socket inside it.
    """
    return os.path.join(tempfile.mkdtemp(prefix='.room-', dir=os.getcwd()), 'sock')


def run_daemon(socket_path, log_path, cap, remove_dir=False,
               poke_after=DEFAULT_POKE_AFTER, host_session=None):
    """Run the daemon process"""
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    log_file = open(log_path, 'a') if log_path else None
    state = RoomState(socket_path, log_file, cap, poke_after, host_session)

    threading.Thread(target=watch_panes, args=(state,), daemon=True).start()

    if os.path.exists(socket_path):
        os.unlink(socket_path)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(socket_path)
    sock.listen(16)
    sock.settimeout(0.5)

    try:
        while state.running:
            try:
                client_sock, _ = sock.accept()
            except socket.timeout:
                continue

            # A thread per client, because WAIT holds its connection open while
            # the rest of the room keeps talking.
            thread = threading.Thread(
                target=handle_client, args=(client_sock, state), daemon=True
            )
            thread.start()
    finally:
        sock.close()
        try:
            os.unlink(socket_path)
            if remove_dir:
                os.rmdir(os.path.dirname(socket_path))
        except OSError:
            pass
        if log_file:
            log_file.close()


# --- Client -----------------------------------------------------------------


def send_request(socket_path, request):
    """Send a request to the daemon"""
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)

        sock.sendall(json.dumps(request).encode('utf-8') + b'\n')

        data = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b'\n' in data:
                break

        if not data:
            return {'status': 'error', 'error': 'No response'}

        return json.loads(data.decode('utf-8').strip())

    except FileNotFoundError as e:
        print(f"Error: {e.strerror}: {socket_path}", file=sys.stderr)
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"Error: Connection refused: {socket_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def require(value, name):
    """Fail with a readable message rather than a traceback"""
    if not value:
        print(f"Error: no {name}. Pass --{name} or set ROOM_{name.upper()}.",
              file=sys.stderr)
        sys.exit(1)
    return value


def check(response):
    """Exit on an error response, otherwise hand back the payload"""
    if response.get('status') == 'error':
        print(f"Error: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)
    return response.get('data', {})


def print_messages(data):
    """Print a batch of log lines, and note anything a filter dropped"""
    for message in data.get('messages', []):
        print(format_message(message))

    skipped = data.get('skipped', 0)
    if skipped:
        print(f"({skipped} other messages skipped, cursor advanced past them)")


def cmd_serve(args):
    """Serve command - run the room"""
    ours = args.socket is None
    socket_path = args.socket or auto_socket_path()

    # Whoever started the room hosts it. Taken from the environment so the
    # usual case needs no flag: the agent that runs serve is the one that
    # goes on to invite everybody.
    host = args.host or os.environ.get('ROOM_SESSION')
    if not host:
        print('Warning: no host. Pass --host or set ROOM_SESSION, or nobody '
              'will be able to lift the flood cap.', file=sys.stderr)

    if args.no_daemon:
        print(f"Socket: {socket_path}")
        print(f"PID: {os.getpid()}")
        sys.stdout.flush()
        run_daemon(socket_path, args.log, args.cap, ours, args.poke_after,
                   host)
        return

    pid = os.fork()
    if pid > 0:
        os.waitpid(pid, 0)
        print(f"Socket: {socket_path}")
        sys.stdout.flush()
        return

    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    # Detach from the caller's stdio. Holding its stdout open would keep the
    # pipe from ever reaching EOF, so anything capturing our output waits for
    # the daemon to exit instead of getting the socket path and moving on.
    with open(os.devnull, 'rb') as devnull:
        os.dup2(devnull.fileno(), 0)
    with open(os.devnull, 'wb') as devnull:
        os.dup2(devnull.fileno(), 1)
        os.dup2(devnull.fileno(), 2)

    run_daemon(socket_path, args.log, args.cap, ours, args.poke_after, host)
    os._exit(0)


def cmd_hello(args):
    """Hello command - join the room and be given a nick"""
    request = {
        'type': 'HELLO',
        'data': {
            'session': require(args.session, 'session'),
            'label': args.label,
            'role': args.role,
            'pty_socket': args.pty_socket,
            'since': args.since,
        },
    }
    data = check(send_request(require(args.socket, 'socket'), request))

    state = 'resumed as' if data['resumed'] else 'you are'
    print(f"{state}: {data['nick']}")
    if data['unread']:
        print(f"unread: {data['unread']} (run: room read)")


def cmd_invite(args):
    """Invite command - bring another agent into the room"""
    request = {
        'type': 'INVITE',
        'data': {
            'session': args.session,
            'label': args.label,
            'role': args.role,
            'pty_socket': args.pty_socket,
            'since': args.since,
        },
    }
    socket_path = require(args.socket, 'socket')
    data = check(send_request(socket_path, request))

    if data['resumed']:
        print(f"already in the room as: {data['nick']}")
    else:
        print(f"invited: {data['nick']}")

    # The room's name is unguessable, so an invited agent cannot find it by
    # itself. Hand back the environment that gets it in, ready to be passed
    # to the pane.
    env = f"ROOM_SOCKET={os.path.abspath(socket_path)} ROOM_SESSION={args.session}"
    if args.pty_socket:
        env += f" ROOM_PTY_SOCKET={args.pty_socket}"
    print(f"env: {env}")


def cmd_poke(args):
    """Poke command - wake a member whose pane is sitting at its prompt"""
    request = {
        'type': 'POKE',
        'data': {'nick': args.nick, 'steer': args.steer, 'text': args.text},
    }
    data = check(send_request(require(args.socket, 'socket'), request))
    if data.get('poked'):
        print(f"{data['nick']}: woken (was {data['was']})")
    else:
        print(f"{data['nick']}: already waiting, it will see it at once")


def cmd_ask(args):
    """Ask command - say something to one member and make sure it looks"""
    request = {
        'type': 'ASK',
        'data': {
            'session': require(args.session, 'session'),
            'nick': args.nick,
            'text': args.text if args.text is not None else sys.stdin.read(),
            'steer': args.steer,
        },
    }
    data = check(send_request(require(args.socket, 'socket'), request))

    print(f"[{data['seq']:04d}] sent to {data['nick']}")
    if data.get('poked'):
        print(f"{data['nick']}: woken (was {data['detail']})")
    elif data['woken']:
        print(f"{data['nick']}: already waiting, it will see it at once")
    else:
        print(f"{data['nick']}: not woken, {data['detail']}")


def cmd_say(args):
    """Say command - one line, seen by everyone"""
    text = args.text if args.text is not None else sys.stdin.read()
    request = {
        'type': 'SAY',
        'data': {'session': require(args.session, 'session'), 'text': text},
    }
    data = check(send_request(require(args.socket, 'socket'), request))
    print(f"[{data['seq']:04d}] sent")


def cmd_read(args):
    """Read command - what was said since you last looked"""
    request = {
        'type': 'READ',
        'data': {
            'session': require(args.session, 'session'),
            'mentions': args.mentions,
            'max': args.max,
        },
    }
    data = check(send_request(require(args.socket, 'socket'), request))

    if not data['messages'] and not data['skipped']:
        print("nothing new")
        return

    print_messages(data)


def cmd_log(args):
    """Log command - look back over history without moving your cursor"""
    request = {
        'type': 'LOG',
        'data': {'from': args.from_seq, 'to': args.to_seq, 'last': args.last},
    }
    data = check(send_request(require(args.socket, 'socket'), request))

    if not data['messages']:
        print('nothing in that range')
        return

    print_messages(data)


def cmd_wait(args):
    """Wait command - block until someone says something"""
    request = {
        'type': 'WAIT',
        'data': {
            'session': require(args.session, 'session'),
            'mentions': args.mentions,
            'timeout': args.timeout,
            'max': args.max,
        },
    }
    data = check(send_request(require(args.socket, 'socket'), request))

    if data['timeout']:
        print(f"nothing new after {args.timeout}s")
        return

    print_messages(data)


def cmd_who(args):
    """Who command - the roster"""
    request = {'type': 'WHO', 'data': {'session': args.session}}
    data = check(send_request(require(args.socket, 'socket'), request))

    if data.get('you'):
        print(f"you are: {data['you']}")

    hosted = False
    for member in data['members']:
        mark = 'host' if member['host'] else ''
        hosted = hosted or (member['host'] and not member['gone'])
        line = (f"{mark:<5}{member['nick']:<16} {member['state']:<10} "
                f"unread={member['unread']}")
        if member['role']:
            line += f"  {member['role']}"
        print(line)

    print(f"-- {data['seq']} messages, "
          f"{data['budget']} more before the room pauses")
    if not hosted:
        print('-- no host in the room, so nobody can lift the cap')


def cmd_bye(args):
    """Bye command - leave the room"""
    request = {'type': 'BYE', 'data': {'session': require(args.session, 'session')}}
    data = check(send_request(require(args.socket, 'socket'), request))
    print(f"left: {data['nick']}")


def cmd_release(args):
    """Release command - let the agents talk again after a flood cap"""
    request = {'type': 'RELEASE',
               'data': {'count': args.count, 'session': args.session}}
    data = check(send_request(require(args.socket, 'socket'), request))
    print(f"budget: {data['budget']}")


def cmd_stop(args):
    """Stop command - stop the room"""
    check(send_request(require(args.socket, 'socket'), {'type': 'STOP'}))
    print("stopped")


def main():
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.stdout.reconfigure(line_buffering=True)

    env_socket = os.environ.get('ROOM_SOCKET')
    env_session = os.environ.get('ROOM_SESSION')
    env_pty = os.environ.get('ROOM_PTY_SOCKET')

    parser = argparse.ArgumentParser(description='room - a shared room for AI agents')
    subparsers = parser.add_subparsers(dest='command', required=True)

    def add_member_args(sub):
        sub.add_argument('--socket', default=env_socket, help='Room socket path')
        sub.add_argument('--session', default=env_session,
                         help='Stable identity of this agent, so a restart resumes')

    serve_parser = subparsers.add_parser('serve', help='Run the room')
    serve_parser.add_argument('--socket', default=env_socket, help='Socket path')
    serve_parser.add_argument('--log', help='Append the transcript here, for tail -f')
    serve_parser.add_argument('--cap', type=int, default=DEFAULT_CAP,
                              help=f'Messages with no sign of the host before the room pauses '
                                   f'(default: {DEFAULT_CAP})')
    serve_parser.add_argument('--no-daemon', action='store_true', help='Run in foreground')
    serve_parser.add_argument('--host', metavar='SESSION',
                              help='Session that hosts the room and may lift '
                                   'the cap (default: $ROOM_SESSION)')
    serve_parser.add_argument('--poke-after', type=float, default=DEFAULT_POKE_AFTER,
                              metavar='SECONDS',
                              help='Wake a member that has been idle with unread '
                                   f'this long (default: {DEFAULT_POKE_AFTER:g}, '
                                   '0 to never)')
    serve_parser.set_defaults(func=cmd_serve)

    poke_parser = subparsers.add_parser('poke', help='Wake a member sitting at its prompt')
    poke_parser.add_argument('--socket', default=env_socket, help='Room socket path')
    poke_parser.add_argument('nick', help='Who to wake')
    poke_parser.add_argument('--text', help='What to type, instead of the usual nudge')
    poke_parser.add_argument('--steer', action='store_true',
                             help='Interrupt them even if they are busy working')
    poke_parser.set_defaults(func=cmd_poke)

    ask_parser = subparsers.add_parser('ask', help='Say something to one member and wake them')
    add_member_args(ask_parser)
    ask_parser.add_argument('nick', help='Who to ask')
    ask_parser.add_argument('text', nargs='?', help='The question (default: stdin)')
    ask_parser.add_argument('--steer', action='store_true',
                            help='Interrupt them even if they are busy working')
    ask_parser.set_defaults(func=cmd_ask)

    hello_parser = subparsers.add_parser('hello', help='Join the room')
    add_member_args(hello_parser)
    hello_parser.add_argument('label', help='Requested nick, uniquified if taken')
    hello_parser.add_argument('--role', default='', help='One line on what you are doing')
    hello_parser.add_argument('--pty-socket', default=env_pty,
                              help='interminai socket of this pane, for liveness')
    hello_parser.add_argument('--since', default='end',
                              help='Start cursor: end, all, or a number of messages')
    hello_parser.set_defaults(func=cmd_hello)

    invite_parser = subparsers.add_parser('invite', help='Bring another agent into the room')
    invite_parser.add_argument('--socket', default=env_socket, help='Room socket path')
    invite_parser.add_argument('label', help='Requested nick, uniquified if taken')
    # Deliberately not defaulting to $ROOM_SESSION: this names the agent being
    # invited, and falling back would quietly invite the caller instead.
    invite_parser.add_argument('--session', required=True,
                               help='Stable identity of the agent being invited')
    invite_parser.add_argument('--role', default='', help='One line on what they will do')
    invite_parser.add_argument('--pty-socket',
                               help='interminai socket of their pane, for liveness')
    invite_parser.add_argument('--since', default='end',
                               help='Start cursor: end, all, or a number of messages')
    invite_parser.set_defaults(func=cmd_invite)

    say_parser = subparsers.add_parser('say', help='Say one line to the room')
    add_member_args(say_parser)
    say_parser.add_argument('text', nargs='?', help='Message text (default: stdin)')
    say_parser.set_defaults(func=cmd_say)

    read_parser = subparsers.add_parser('read', help='Read what is new')
    add_member_args(read_parser)
    read_parser.add_argument('--mentions', action='store_true',
                             help='Only lines addressing you, cursor still advances')
    read_parser.add_argument('--max', type=int, metavar='N',
                             help='At most N messages, keeping the newest')
    read_parser.set_defaults(func=cmd_read)

    wait_parser = subparsers.add_parser('wait', help='Block until something is said')
    add_member_args(wait_parser)
    wait_parser.add_argument('--mentions', action='store_true',
                             help='Only wake for lines addressing you')
    wait_parser.add_argument('--timeout', type=int, default=240,
                             help='Seconds to block (default: 240)')
    wait_parser.add_argument('--max', type=int, metavar='N',
                             help='At most N messages, keeping the newest')
    wait_parser.set_defaults(func=cmd_wait)

    log_parser = subparsers.add_parser('log', help='Look back over history, cursor untouched')
    log_parser.add_argument('--socket', default=env_socket, help='Room socket path')
    log_parser.add_argument('--from', type=int, dest='from_seq', metavar='N',
                            help='First sequence number to show')
    log_parser.add_argument('--to', type=int, dest='to_seq', metavar='N',
                            help='Last sequence number to show')
    log_parser.add_argument('--last', type=int, metavar='N',
                            help='Only the last N of the range')
    log_parser.set_defaults(func=cmd_log)

    who_parser = subparsers.add_parser('who', help='Show the roster, and which one is you')
    add_member_args(who_parser)
    who_parser.set_defaults(func=cmd_who)

    bye_parser = subparsers.add_parser('bye', help='Leave the room')
    add_member_args(bye_parser)
    bye_parser.set_defaults(func=cmd_bye)

    release_parser = subparsers.add_parser('release', help='Allow more messages after a cap')
    add_member_args(release_parser)
    release_parser.add_argument('count', nargs='?', type=int, help='How many more')
    release_parser.set_defaults(func=cmd_release)

    stop_parser = subparsers.add_parser('stop', help='Stop the room')
    stop_parser.add_argument('--socket', default=env_socket, help='Room socket path')
    stop_parser.set_defaults(func=cmd_stop)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

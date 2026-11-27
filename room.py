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

# Refuse to append after this many consecutive messages from non-human
# members. Two agents will otherwise thank each other until the quota runs out.
DEFAULT_CAP = 50


class Member:
    """One participant in the room"""

    def __init__(self, session, nick, role, human, pty_socket, cursor):
        self.session = session
        self.nick = nick
        self.role = role
        self.human = human
        self.pty_socket = pty_socket
        self.cursor = cursor
        self.joined = time.time()
        self.gone = False


class RoomState:
    """Shared state of the room, guarded by a single lock"""

    def __init__(self, socket_path, log_file, cap):
        self.lock = threading.Lock()
        self.socket_path = socket_path
        self.log_file = log_file
        self.cap = cap
        self.budget = cap
        self.messages = []
        self.seq = 0
        self.members = {}
        self.running = True

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
    human = bool(data.get('human'))
    pty_socket = data.get('pty_socket')
    since = data.get('since', 'end')

    with state.lock:
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

        member = Member(session, nick, role, human, pty_socket, cursor)
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

        if member.human:
            # A human in the room releases everyone else.
            state.budget = state.cap
        else:
            if state.budget <= 0:
                return {
                    'status': 'error',
                    'error': f'Flood cap reached after {state.cap} messages '
                             f'with no human. Waiting for a human, or room release.',
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


def collect(state, member, only_mentions):
    """Unread messages for a member, advancing its cursor. Caller holds the lock."""
    pending = pending_for(state, member)
    member.cursor = state.seq

    if only_mentions:
        wanted = [m for m in pending if mentions(m, member.nick)]
        return wanted, len(pending) - len(wanted)

    return pending, 0


def handle_read(data, state):
    """Handle READ request - everything said since this member last looked"""
    session = data.get('session')
    only_mentions = bool(data.get('mentions'))

    with state.lock:
        member = state.members.get(session)
        if not member or member.gone:
            return {'status': 'error', 'error': 'Not in the room, say hello first'}

        messages, skipped = collect(state, member, only_mentions)
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
                messages, skipped = collect(state, member, only_mentions)
                return {
                    'status': 'ok',
                    'data': {'messages': messages, 'skipped': skipped, 'timeout': False},
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


def probe_pty(pty_socket):
    """Ask interminai whether a member's pane is still alive

    The room has no useful pid of its own: "room hello" is a short-lived
    subprocess of the agent's shell, not the agent. interminai owns the pty and
    gets SIGCHLD, so it is the one that actually knows.
    """
    if not pty_socket:
        return 'unknown'

    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(pty_socket)
        # Deliberately not activity mode: that clears interminai's activity
        # flag, stealing the notification from whoever is driving the pane.
        sock.sendall(json.dumps({'type': 'STATUS'}).encode('utf-8') + b'\n')

        data = b''
        while b'\n' not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk

        if not data:
            return 'unknown'

        response = json.loads(data.decode('utf-8').strip())
        if response.get('status') != 'ok':
            return 'unknown'

        if response['data'].get('running'):
            return 'alive'

        return f"exited({response['data'].get('exit_code', '?')})"

    except (OSError, ValueError):
        return 'gone'
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def handle_who(data, state):
    """Handle WHO request - the roster, so the supervisor reads one list
    instead of checking every pane"""
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
                'human': m.human,
                'session': m.session,
                # Counted the same way READ selects, or the roster promises a
                # member lines that reading will not return.
                'unread': len(pending_for(state, m)),
                'pty_socket': m.pty_socket,
                'gone': m.gone,
            }
            for m in state.members.values()
        ]
        budget = state.budget
        cap = state.cap
        seq = state.seq

    # Probed outside the lock: a hung pane must not stall the room.
    for entry in members:
        pty_socket = entry.pop('pty_socket')
        entry['state'] = 'left' if entry['gone'] else probe_pty(pty_socket)

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
    """Handle RELEASE request - hand the agents more turns after a flood cap"""
    with state.lock:
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


def run_daemon(socket_path, log_path, cap, remove_dir=False):
    """Run the daemon process"""
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    log_file = open(log_path, 'a') if log_path else None
    state = RoomState(socket_path, log_file, cap)

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

    if args.no_daemon:
        print(f"Socket: {socket_path}")
        print(f"PID: {os.getpid()}")
        sys.stdout.flush()
        run_daemon(socket_path, args.log, args.cap, ours)
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

    run_daemon(socket_path, args.log, args.cap, ours)
    os._exit(0)


def cmd_hello(args):
    """Hello command - join the room and be given a nick"""
    request = {
        'type': 'HELLO',
        'data': {
            'session': require(args.session, 'session'),
            'label': args.label,
            'role': args.role,
            'human': args.human,
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
            'human': args.human,
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
        },
    }
    data = check(send_request(require(args.socket, 'socket'), request))

    if not data['messages'] and not data['skipped']:
        print("nothing new")
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

    for member in data['members']:
        kind = 'human' if member['human'] else member['state']
        line = f"{member['nick']:<16} {kind:<12} unread={member['unread']}"
        if member['role']:
            line += f"  {member['role']}"
        print(line)

    print(f"-- {data['seq']} messages, "
          f"{data['budget']} more before the room pauses for a human")


def cmd_bye(args):
    """Bye command - leave the room"""
    request = {'type': 'BYE', 'data': {'session': require(args.session, 'session')}}
    data = check(send_request(require(args.socket, 'socket'), request))
    print(f"left: {data['nick']}")


def cmd_release(args):
    """Release command - let the agents talk again after a flood cap"""
    request = {'type': 'RELEASE', 'data': {'count': args.count}}
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
                              help=f'Messages with no human before the room pauses '
                                   f'(default: {DEFAULT_CAP})')
    serve_parser.add_argument('--no-daemon', action='store_true', help='Run in foreground')
    serve_parser.set_defaults(func=cmd_serve)

    hello_parser = subparsers.add_parser('hello', help='Join the room')
    add_member_args(hello_parser)
    hello_parser.add_argument('label', help='Requested nick, uniquified if taken')
    hello_parser.add_argument('--role', default='', help='One line on what you are doing')
    hello_parser.add_argument('--human', action='store_true',
                              help='This member is a human, and resets the flood cap')
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
    invite_parser.add_argument('--human', action='store_true',
                               help='This member is a human, and resets the flood cap')
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
    read_parser.set_defaults(func=cmd_read)

    wait_parser = subparsers.add_parser('wait', help='Block until something is said')
    add_member_args(wait_parser)
    wait_parser.add_argument('--mentions', action='store_true',
                             help='Only wake for lines addressing you')
    wait_parser.add_argument('--timeout', type=int, default=240,
                             help='Seconds to block (default: 240)')
    wait_parser.set_defaults(func=cmd_wait)

    who_parser = subparsers.add_parser('who', help='Show the roster, and which one is you')
    add_member_args(who_parser)
    who_parser.set_defaults(func=cmd_who)

    bye_parser = subparsers.add_parser('bye', help='Leave the room')
    add_member_args(bye_parser)
    bye_parser.set_defaults(func=cmd_bye)

    release_parser = subparsers.add_parser('release', help='Allow more messages after a cap')
    release_parser.add_argument('--socket', default=env_socket, help='Room socket path')
    release_parser.add_argument('count', nargs='?', type=int, help='How many more')
    release_parser.set_defaults(func=cmd_release)

    stop_parser = subparsers.add_parser('stop', help='Stop the room')
    stop_parser.add_argument('--socket', default=env_socket, help='Room socket path')
    stop_parser.set_defaults(func=cmd_stop)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

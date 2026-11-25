#!/usr/bin/env python3
"""Tests for room

Drives the CLI as an agent would, over a real socket against a real daemon.
Set OVERRIDE_ROOM_BIN to test an alternative implementation.
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOM = os.environ.get(
    'OVERRIDE_ROOM_BIN',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'room.py'),
)

INTERMINAI = os.environ.get(
    'OVERRIDE_INTERMINAI_BIN',
    '/scm/ai/interminai/interminai.py',
)


def wait_for(predicate, timeout=5):
    """Poll until predicate holds, so tests do not race the daemon"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class RoomTestCase(unittest.TestCase):
    """A room daemon on a private socket, torn down afterwards"""

    cap = 50

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='room-test-')
        self.socket_path = os.path.join(self.tmpdir, 'sock')
        self.log_path = os.path.join(self.tmpdir, 'log')

        subprocess.run(
            [sys.executable, ROOM, 'serve', '--socket', self.socket_path,
             '--log', self.log_path, '--cap', str(self.cap)],
            check=True, capture_output=True, text=True,
        )
        self.assertTrue(
            wait_for(lambda: os.path.exists(self.socket_path)),
            'daemon did not create its socket',
        )

    def tearDown(self):
        if os.path.exists(self.socket_path):
            self.run_room('stop')
            wait_for(lambda: not os.path.exists(self.socket_path))
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_room(self, *args, session=None, check=True):
        """Run a room command the way an agent's shell would"""
        env = dict(os.environ, ROOM_SOCKET=self.socket_path)
        if session:
            env['ROOM_SESSION'] = session
        else:
            env.pop('ROOM_SESSION', None)

        result = subprocess.run(
            [sys.executable, ROOM, *args],
            capture_output=True, text=True, env=env,
        )
        if check:
            self.assertEqual(result.returncode, 0,
                             f'{args} failed: {result.stderr}')
        return result

    def request(self, req_type, **data):
        """Speak the protocol directly, for what the CLI does not expose"""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        try:
            sock.sendall(json.dumps({'type': req_type, 'data': data}).encode() + b'\n')
            buf = b''
            while b'\n' not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode().strip())
        finally:
            sock.close()

    def hello(self, session, label, *extra, check=True):
        return self.run_room('hello', label, *extra, session=session, check=check)


class TestHello(RoomTestCase):

    def test_assigns_requested_nick(self):
        out = self.hello('pane-a', 'builder').stdout
        self.assertIn('you are: builder', out)

    def test_uniquifies_a_taken_nick(self):
        self.hello('pane-a', 'builder')
        out = self.hello('pane-b', 'builder').stdout
        self.assertIn('you are: builder2', out)

    def test_uniquifies_repeatedly(self):
        self.hello('pane-a', 'builder')
        self.hello('pane-b', 'builder')
        self.assertIn('you are: builder3', self.hello('pane-c', 'builder').stdout)

    def test_sanitizes_the_label(self):
        out = self.hello('pane-a', 'rm -rf /; evil').stdout
        self.assertIn('you are: rm-rf', out)

    def test_same_session_resumes_with_same_nick(self):
        self.hello('pane-a', 'builder')
        out = self.hello('pane-a', 'somethingelse').stdout
        self.assertIn('resumed as: builder', out)

    def test_resume_keeps_the_cursor(self):
        """A restarted agent must not re-read or skip the room"""
        self.hello('pane-a', 'builder')
        self.hello('pane-b', 'other')
        self.run_room('read', session='pane-a')

        self.run_room('say', 'while a was away', session='pane-b')
        self.hello('pane-a', 'builder')

        out = self.run_room('read', session='pane-a').stdout
        self.assertIn('while a was away', out)
        self.assertNotIn('builder joined', out)

    def test_new_member_does_not_read_its_own_arrival(self):
        out = self.run_room('read', session='pane-a', check=False)
        self.hello('pane-a', 'builder')
        out = self.run_room('read', session='pane-a').stdout
        self.assertIn('nothing new', out)

    def test_since_all_reads_the_backlog(self):
        self.hello('pane-a', 'first')
        self.run_room('say', 'said before b arrived', session='pane-a')

        self.hello('pane-b', 'second', '--since', 'all')
        out = self.run_room('read', session='pane-b').stdout
        self.assertIn('said before b arrived', out)

    def test_since_count_reads_that_many_back(self):
        self.hello('pane-a', 'first')
        for i in range(5):
            self.run_room('say', f'message {i}', session='pane-a')

        self.hello('pane-b', 'second', '--since', '2')
        out = self.run_room('read', session='pane-b').stdout
        self.assertNotIn('message 2', out)
        self.assertIn('message 4', out)

    def test_departed_nick_is_reused(self):
        self.hello('pane-a', 'builder')
        self.run_room('bye', session='pane-a')
        out = self.hello('pane-b', 'builder').stdout
        self.assertIn('you are: builder', out)

    def test_requires_a_session(self):
        result = self.run_room('hello', 'builder', check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('no session', result.stderr)

    def test_rejects_a_bad_since(self):
        result = self.hello('pane-a', 'builder', '--since', 'yesterday', check=False)
        self.assertEqual(result.returncode, 1)


class TestSocketLocation(unittest.TestCase):
    """Where serve puts the socket when no path is given"""

    def setUp(self):
        self.cwd = tempfile.mkdtemp(prefix='room-cwd-')
        result = subprocess.run(
            [sys.executable, ROOM, 'serve'],
            cwd=self.cwd, capture_output=True, text=True, check=True,
        )
        self.socket_path = result.stdout.split('Socket:')[1].strip()
        self.addCleanup(shutil.rmtree, self.cwd, ignore_errors=True)
        self.assertTrue(wait_for(lambda: os.path.exists(self.socket_path)))

    def test_lives_under_the_working_directory(self):
        self.assertEqual(os.path.dirname(os.path.dirname(self.socket_path)),
                         os.path.realpath(self.cwd))

    def test_name_is_unguessable(self):
        name = os.path.basename(os.path.dirname(self.socket_path))
        self.assertTrue(name.startswith('.room-'))
        self.assertGreaterEqual(len(name) - len('.room-'), 6)

    def test_directory_keeps_other_users_out(self):
        """The socket's own mode is not relied on, the directory's is"""
        mode = stat.S_IMODE(os.stat(os.path.dirname(self.socket_path)).st_mode)
        self.assertEqual(mode, 0o700)

    def test_directory_is_removed_when_the_room_stops(self):
        room_dir = os.path.dirname(self.socket_path)
        subprocess.run([sys.executable, ROOM, 'stop', '--socket', self.socket_path],
                       capture_output=True, check=True)
        self.assertTrue(wait_for(lambda: not os.path.exists(room_dir)))


class TestInvite(RoomTestCase):
    """The supervisor bringing other agents in"""

    def invite(self, session, label, *extra, check=True):
        return self.run_room('invite', label, '--session', session, *extra,
                             check=check)

    def test_settles_the_nick_before_the_agent_runs(self):
        out = self.invite('pane-parser', 'parser', '--role', 'parser work').stdout
        self.assertIn('invited: parser', out)
        self.assertIn('parser work', self.run_room('who').stdout)

    def test_invited_agent_is_already_a_member(self):
        self.invite('pane-parser', 'parser')
        self.run_room('say', 'hello there', session='pane-parser')

    def test_invited_agent_hello_resumes(self):
        self.invite('pane-parser', 'parser')
        out = self.run_room('hello', 'otherlabel', session='pane-parser').stdout
        self.assertIn('resumed as: parser', out)

    def test_inviting_twice_is_idempotent(self):
        self.invite('pane-parser', 'parser')
        out = self.invite('pane-parser', 'parser').stdout
        self.assertIn('already in the room as: parser', out)

    def test_uniquifies_against_a_member_who_said_hello(self):
        self.hello('pane-a', 'parser')
        self.assertIn('invited: parser2', self.invite('pane-b', 'parser').stdout)

    def test_announces_the_invitation_to_everyone_else(self):
        self.hello('pane-a', 'alpha')
        self.invite('pane-lexer', 'lexer', '--role', 'token tables')

        out = self.run_room('read', session='pane-a').stdout
        self.assertIn('lexer was invited', out)
        self.assertIn('token tables', out)

    def test_invitee_does_not_read_its_own_invitation(self):
        self.invite('pane-parser', 'parser')
        self.assertIn('nothing new', self.run_room('read', session='pane-parser').stdout)

    def test_hands_back_the_environment_to_spawn_with(self):
        """The room's name is unguessable, so the invitation has to carry it"""
        out = self.invite('pane-parser', 'parser',
                          '--pty-socket', '/tmp/pane.sock').stdout
        self.assertIn(f'ROOM_SOCKET={self.socket_path}', out)
        self.assertIn('ROOM_SESSION=pane-parser', out)
        self.assertIn('ROOM_PTY_SOCKET=/tmp/pane.sock', out)

    def test_requires_an_explicit_session(self):
        """It names the invitee, so it must not fall back to the caller's"""
        env = dict(os.environ, ROOM_SOCKET=self.socket_path,
                   ROOM_SESSION='the-supervisor')
        result = subprocess.run([sys.executable, ROOM, 'invite', 'parser'],
                                capture_output=True, text=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('the-supervisor', self.run_room('who').stdout)


class TestSayRead(RoomTestCase):

    def test_everyone_sees_one_line_said_once(self):
        self.hello('pane-a', 'alpha')
        self.hello('pane-b', 'beta')
        self.hello('pane-c', 'gamma')

        self.run_room('say', 'found the leak', session='pane-a')

        for session in ('pane-b', 'pane-c'):
            self.assertIn('found the leak', self.run_room('read', session=session).stdout)

    def test_read_advances_the_cursor(self):
        self.hello('pane-a', 'alpha')
        self.hello('pane-b', 'beta')
        self.run_room('say', 'once only', session='pane-a')

        self.assertIn('once only', self.run_room('read', session='pane-b').stdout)
        self.assertIn('nothing new', self.run_room('read', session='pane-b').stdout)

    def test_speaker_does_not_read_itself_back(self):
        self.hello('pane-a', 'alpha')
        self.run_room('read', session='pane-a')
        self.run_room('say', 'my own words', session='pane-a')
        self.assertIn('nothing new', self.run_room('read', session='pane-a').stdout)

    def test_say_requires_membership(self):
        result = self.run_room('say', 'hello?', session='pane-nobody', check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('say hello first', result.stderr)

    def test_rejects_an_empty_message(self):
        self.hello('pane-a', 'alpha')
        result = self.run_room('say', '   ', session='pane-a', check=False)
        self.assertEqual(result.returncode, 1)

    def test_reads_message_text_from_stdin(self):
        self.hello('pane-a', 'alpha')
        self.hello('pane-b', 'beta')

        env = dict(os.environ, ROOM_SOCKET=self.socket_path, ROOM_SESSION='pane-a')
        subprocess.run([sys.executable, ROOM, 'say'], input='from stdin',
                       capture_output=True, text=True, env=env, check=True)

        self.assertIn('from stdin', self.run_room('read', session='pane-b').stdout)

    def test_order_is_the_same_for_everyone(self):
        """Agents think in parallel, but the log is serialized"""
        self.hello('pane-reader', 'reader')
        senders = [f'pane-{i}' for i in range(8)]
        for session in senders:
            self.hello(session, f'agent{session[-1]}')
        self.run_room('read', session='pane-reader')

        def say(session):
            self.run_room('say', f'from {session}', session=session)

        threads = [threading.Thread(target=say, args=(s,)) for s in senders]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        data = self.request('READ', session='pane-reader')['data']
        seqs = [m['seq'] for m in data['messages'] if m['kind'] == 'msg']
        self.assertEqual(len(seqs), len(senders))
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs), 'sequence numbers were reused')

    def test_transcript_is_written_to_the_log_file(self):
        self.hello('pane-a', 'alpha')
        self.run_room('say', 'for the log', session='pane-a')

        with open(self.log_path) as handle:
            contents = handle.read()
        self.assertIn('alpha joined', contents)
        self.assertIn('for the log', contents)


class TestMentions(RoomTestCase):

    def test_filters_to_lines_addressing_you(self):
        self.hello('pane-a', 'alpha')
        self.hello('pane-b', 'beta')
        self.run_room('read', session='pane-b')

        self.run_room('say', 'unrelated chatter', session='pane-a')
        self.run_room('say', '@beta take the lexer', session='pane-a')

        out = self.run_room('read', '--mentions', session='pane-b').stdout
        self.assertIn('take the lexer', out)
        self.assertNotIn('unrelated chatter', out)

    def test_matches_a_leading_nick(self):
        self.hello('pane-a', 'alpha')
        self.hello('pane-b', 'beta')
        self.run_room('read', session='pane-b')

        self.run_room('say', 'beta: start now', session='pane-a')
        self.assertIn('start now', self.run_room('read', '--mentions', session='pane-b').stdout)

    def test_reports_what_it_skipped(self):
        self.hello('pane-a', 'alpha')
        self.hello('pane-b', 'beta')
        self.run_room('read', session='pane-b')

        self.run_room('say', 'one', session='pane-a')
        self.run_room('say', 'two', session='pane-a')

        out = self.run_room('read', '--mentions', session='pane-b').stdout
        self.assertIn('2 other messages skipped', out)

    def test_advances_past_filtered_messages(self):
        """Documented behaviour: a mentions read discards the rest for good"""
        self.hello('pane-a', 'alpha')
        self.hello('pane-b', 'beta')
        self.run_room('read', session='pane-b')

        self.run_room('say', 'unrelated', session='pane-a')
        self.run_room('read', '--mentions', session='pane-b')

        self.assertIn('nothing new', self.run_room('read', session='pane-b').stdout)


class TestWait(RoomTestCase):

    def test_returns_immediately_when_unread(self):
        self.hello('pane-a', 'alpha')
        self.hello('pane-b', 'beta')
        self.run_room('say', 'already said', session='pane-a')

        started = time.time()
        out = self.run_room('wait', '--timeout', '10', session='pane-b').stdout
        self.assertIn('already said', out)
        self.assertLess(time.time() - started, 5)

    def test_one_message_wakes_every_idle_agent(self):
        """All of them block at once, all of them wake on the same append"""
        sessions = ['pane-a', 'pane-b', 'pane-c']
        for i, session in enumerate(sessions):
            self.hello(session, f'agent{i}')
        self.hello('pane-boss', 'boss')
        for session in sessions:
            self.run_room('read', session=session)

        results = {}

        def waiter(session):
            results[session] = self.run_room(
                'wait', '--timeout', '15', session=session).stdout

        threads = [threading.Thread(target=waiter, args=(s,)) for s in sessions]
        for thread in threads:
            thread.start()

        time.sleep(1.5)
        self.assertEqual(results, {}, 'a waiter returned before anything was said')

        self.run_room('say', 'everyone: status please', session='pane-boss')
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(len(results), 3)
        for session in sessions:
            self.assertIn('status please', results[session])

    def test_times_out_quietly(self):
        self.hello('pane-a', 'alpha')
        self.run_room('read', session='pane-a')

        started = time.time()
        out = self.run_room('wait', '--timeout', '1', session='pane-a').stdout
        self.assertIn('nothing new', out)
        self.assertGreaterEqual(time.time() - started, 1)

    def test_mentions_does_not_wake_on_other_traffic(self):
        self.hello('pane-a', 'alpha')
        self.hello('pane-b', 'beta')
        self.run_room('read', session='pane-b')

        result = {}

        def waiter():
            result['out'] = self.run_room(
                'wait', '--mentions', '--timeout', '3', session='pane-b').stdout

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.5)
        self.run_room('say', 'not for you', session='pane-a')
        time.sleep(1)
        self.assertEqual(result, {}, 'woke on a message that was not a mention')

        self.run_room('say', '@beta this one is', session='pane-a')
        thread.join(timeout=10)
        self.assertIn('this one is', result['out'])

    def test_room_keeps_working_while_someone_waits(self):
        """A blocked WAIT must not hold up the rest of the room"""
        self.hello('pane-a', 'alpha')
        self.hello('pane-b', 'beta')
        self.run_room('read', session='pane-b')

        thread = threading.Thread(
            target=self.run_room, args=('wait', '--timeout', '5'),
            kwargs={'session': 'pane-b'})
        thread.start()
        time.sleep(0.5)

        started = time.time()
        self.run_room('who')
        self.assertLess(time.time() - started, 3, 'who blocked behind a waiter')
        thread.join(timeout=10)


class TestFloodCap(RoomTestCase):

    cap = 3

    def test_refuses_after_the_cap(self):
        self.hello('pane-a', 'alpha')
        for i in range(self.cap):
            self.run_room('say', f'message {i}', session='pane-a')

        result = self.run_room('say', 'one too many', session='pane-a', check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Flood cap', result.stderr)

    def test_a_human_resets_it(self):
        self.hello('pane-a', 'alpha')
        self.hello('pane-h', 'mst', '--human')
        for i in range(self.cap):
            self.run_room('say', f'message {i}', session='pane-a')

        self.run_room('say', 'carry on', session='pane-h')
        self.run_room('say', 'thanks', session='pane-a')

    def test_a_human_is_never_capped(self):
        self.hello('pane-h', 'mst', '--human')
        for i in range(self.cap * 2):
            self.run_room('say', f'human message {i}', session='pane-h')

    def test_release_hands_out_more_turns(self):
        self.hello('pane-a', 'alpha')
        for i in range(self.cap):
            self.run_room('say', f'message {i}', session='pane-a')

        self.run_room('release', '2')
        self.run_room('say', 'allowed again', session='pane-a')
        self.run_room('say', 'and once more', session='pane-a')

        result = self.run_room('say', 'but not again', session='pane-a', check=False)
        self.assertEqual(result.returncode, 1)


class TestWho(RoomTestCase):

    def test_lists_members_with_roles(self):
        self.hello('pane-a', 'alpha', '--role', 'parser work')
        self.hello('pane-h', 'mst', '--human')

        out = self.run_room('who').stdout
        self.assertIn('alpha', out)
        self.assertIn('parser work', out)
        self.assertIn('human', out)

    def test_reports_unknown_without_a_pty_socket(self):
        self.hello('pane-a', 'alpha')
        self.assertIn('unknown', self.run_room('who').stdout)

    def test_reports_gone_for_a_dead_pty_socket(self):
        self.hello('pane-a', 'alpha', '--pty-socket', '/nonexistent/pane.sock')
        self.assertIn('gone', self.run_room('who').stdout)

    def test_reports_left_after_bye(self):
        self.hello('pane-a', 'alpha')
        self.run_room('bye', session='pane-a')
        self.assertIn('left', self.run_room('who').stdout)

    def test_tells_a_member_which_one_it_is(self):
        """An invited agent did not choose its nick and has to be told"""
        self.run_room('invite', 'parser', '--session', 'pane-parser')
        out = self.run_room('who', session='pane-parser').stdout
        self.assertIn('you are: parser', out)

    def test_omits_the_marker_for_a_non_member(self):
        self.hello('pane-a', 'alpha')
        self.assertNotIn('you are:', self.run_room('who').stdout)

    def test_shows_the_budget(self):
        out = self.run_room('who').stdout
        self.assertIn(f'{self.cap} more before the room pauses', out)

    def test_unread_counts_what_reading_would_return(self):
        """Two agents read the roster's count as messages they could not see"""
        self.hello('pane-a', 'alpha')
        self.hello('pane-b', 'beta')
        self.run_room('read', session='pane-a')
        self.run_room('say', 'my own line', session='pane-a')

        out = self.run_room('who').stdout
        alpha = [line for line in out.splitlines() if line.startswith('alpha')][0]
        self.assertIn('unread=0', alpha)


@unittest.skipUnless(os.path.exists(INTERMINAI), 'interminai not available')
class TestLiveness(RoomTestCase):
    """Liveness comes from interminai, which owns the pty and gets SIGCHLD"""

    def start_pane(self, name, command):
        pane_socket = os.path.join(self.tmpdir, f'{name}.sock')
        subprocess.run(
            [sys.executable, INTERMINAI, 'start', '--socket', pane_socket,
             '--', *command],
            check=True, capture_output=True, text=True,
        )
        self.assertTrue(wait_for(lambda: os.path.exists(pane_socket)))
        self.addCleanup(subprocess.run,
                        [sys.executable, INTERMINAI, 'stop', '--socket', pane_socket],
                        capture_output=True)
        return pane_socket

    def test_reports_alive_then_exited(self):
        pane_socket = self.start_pane('pane-a', ['sleep', '60'])
        self.hello('pane-a', 'worker', '--pty-socket', pane_socket)

        self.assertIn('alive', self.run_room('who').stdout)

        subprocess.run(
            [sys.executable, INTERMINAI, 'kill', '--socket', pane_socket,
             '--signal', 'KILL'],
            check=True, capture_output=True,
        )
        self.assertTrue(wait_for(
            lambda: 'exited' in self.run_room('who').stdout))
        self.assertIn('exited(137)', self.run_room('who').stdout)

    def test_does_not_clear_the_activity_flag(self):
        """Probing liveness must not steal interminai's activity notification"""
        pane_socket = self.start_pane('pane-a', ['bash', '--norc'])
        self.hello('pane-a', 'worker', '--pty-socket', pane_socket)

        subprocess.run(
            [sys.executable, INTERMINAI, 'input', '--socket', pane_socket,
             '--text', 'echo marker\n'],
            check=True, capture_output=True,
        )
        time.sleep(0.5)

        self.run_room('who')

        result = subprocess.run(
            [sys.executable, INTERMINAI, 'status', '--socket', pane_socket],
            check=True, capture_output=True, text=True,
        )
        self.assertIn('marker', subprocess.run(
            [sys.executable, INTERMINAI, 'output', '--socket', pane_socket,
             '--no-color'],
            check=True, capture_output=True, text=True).stdout)
        self.assertEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()

"""Offline regression coverage for native sender identity."""
import contextlib
import io
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
os.environ["POSTBOX_TELEMETRY"] = "0"   # tests never write telemetry events into the real spool (tests/test_telemetry.py covers telemetry itself)
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'postbox'))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
import lib
import post

THREAD = '01a00000-0000-7000-8000-000000000001'
OTHER = '01a00000-0000-7000-8000-000000000002'
NATIVE = '/home/example/.codex/packages/app-server-daemon/releases/1.0/bin/codex'


class SenderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / 'state.sqlite')
        with sqlite3.connect(self.db) as con:
            con.execute('create table threads (id text, cwd text, archived integer, updated_at integer)')
            con.executemany('insert into threads values (?,?,?,?)', [
                (THREAD, '/workspace/project-a', 0, 1),
                (OTHER, '/workspace/different-project', 0, 999),
            ])
        self.parents = {900: '800 /bin/python3', 800: '700 /bin/zsh',
                        700: '600 ' + NATIVE, 600: '1 ' + NATIVE}
        self.calls = []
        self.enterContext(patch.dict(os.environ, {'CODEX_THREAD_ID': THREAD}, clear=True))
        self.enterContext(patch.object(lib.os, 'getpid', return_value=900))
        self.enterContext(patch.object(lib, 'codex_state_db', return_value=self.db))
        self.enterContext(patch.object(lib, '_run', side_effect=self.process_read))
        self.enterContext(patch.object(lib, 'process_table', return_value={}))

    def process_read(self, cmd, timeout=5):
        self.calls.append(cmd)
        self.assertEqual(cmd[:2], ['ps', '-p'])
        self.assertEqual(cmd[3:], ['-o', 'ppid=,comm='])
        return self.parents.get(int(cmd[2]), '')

    def test_native_sender_uses_exact_thread_and_nearest_actual_ancestor(self):
        self.assertEqual(lib.my_identity(), ('codex', 'project-a', 700))
        self.assertLessEqual(len(self.calls), 4)

    def test_project_comes_from_thread_not_shell_working_directory(self):
        with patch.object(lib.os, 'getcwd', return_value='/workspace/postbox'):
            self.assertEqual(lib.my_identity(), ('codex', 'project-a', 700))

    def test_invalid_explicit_thread_never_falls_back(self):
        unrelated = lib.Session('codex', 123, '/workspace/other', 'other', OTHER)
        for value in ['', 'invalid', THREAD + '\n', '../' + THREAD]:
            with self.subTest(value=value), patch.dict(os.environ, {'CODEX_THREAD_ID': value}), \
                    patch.object(lib, 'codex_ancestor', return_value=unrelated) as fallback:
                self.assertIsNone(lib.my_identity())
                fallback.assert_not_called()

    def test_missing_thread_row_never_selects_newest(self):
        with sqlite3.connect(self.db) as con:
            con.execute('delete from threads where id=?', (THREAD,))
        self.assertIsNone(lib.my_identity())

    def test_archived_thread_is_refused(self):
        with sqlite3.connect(self.db) as con:
            con.execute('update threads set archived=1 where id=?', (THREAD,))
        self.assertIsNone(lib.my_identity())

    def test_empty_or_relative_project_metadata_is_refused(self):
        for cwd in ['', 'relative/project', '/']:
            with self.subTest(cwd=cwd):
                with sqlite3.connect(self.db) as con:
                    con.execute('update threads set cwd=? where id=?', (cwd, THREAD))
                self.assertIsNone(lib.my_identity())

    def test_no_native_ancestor_is_refused(self):
        self.parents = {900: '1 /bin/python3'}
        self.assertIsNone(lib.my_identity())

    def test_similar_executable_name_is_refused(self):
        self.parents[700] = '1 /tmp/not-codex'
        self.assertIsNone(lib.my_identity())

    def test_process_cycle_is_bounded_and_refused(self):
        self.parents = {900: '800 /bin/python3', 800: '900 /bin/zsh'}
        self.assertIsNone(lib.my_identity())
        self.assertLessEqual(len(self.calls), 3)

    def test_missing_state_database_is_refused(self):
        with patch.object(lib, 'codex_state_db', return_value=None):
            self.assertIsNone(lib.my_identity())

    def test_legacy_sender_without_explicit_thread_is_preserved(self):
        os.environ.pop('CODEX_THREAD_ID')
        session = lib.Session('codex', 123, '/workspace/legacy', 'legacy', THREAD)
        with patch.object(lib, 'codex_ancestor', return_value=session):
            self.assertEqual(lib.my_identity(), ('codex', 'legacy', 123))

    def test_other_existing_sender_is_preserved(self):
        with patch.dict(os.environ, {'CLAUDECODE': '1', 'CLAUDE_PID': '321'}), \
                patch.object(lib, 'cwd_of', return_value='/workspace/legacy'):
            self.assertEqual(lib.my_identity(), ('claude', 'legacy', 321))

    def test_cli_invalid_identity_refuses_before_send(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'invalid'}), \
                patch.object(post, 'send') as send, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                post.main(['post', 'claude', 'message'])
            self.assertEqual(raised.exception.code, 2)
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()

# Copyright 2012-2019 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301  USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
from __future__ import division

import hashlib
import itertools
import libvirt
import logging
import tempfile
import textwrap
import os
import os.path
import pickle
import pytest
import six
import sys

from collections import namedtuple
from monkeypatch import MonkeyPatchScope
from testlib import VdsmTestCase as TestCaseBase
from testlib import namedTemporaryDir

from vdsm.common import exception
from vdsm.common import hooks


DirEntry = namedtuple("DirEntry", "name, mode, contents")
FileEntry = namedtuple("FileEntry", "name, mode, contents")


def dir_entry_apply(self, hooks_dir):
    path = hooks_dir.join(self.name)
    path.mkdir()
    for entry in self.contents:
        entry.apply(path)
    path.chmod(self.mode)


def file_entry_apply(self, hooks_dir):
    path = hooks_dir.join(self.name)
    path.write(self.contents)
    path.chmod(self.mode)


DirEntry.apply = dir_entry_apply
FileEntry.apply = file_entry_apply


@pytest.fixture
def fake_hooks_root(monkeypatch, tmpdir):
    with monkeypatch.context() as m:
        m.setattr(hooks, "P_VDSM_HOOKS", str(tmpdir) + "/")
        yield tmpdir


@pytest.fixture
def hooks_dir(fake_hooks_root, request):
    hooks_dir = fake_hooks_root.mkdir("hooks_dir")
    entries = getattr(request, 'param', [])
    for entry in entries:
        entry.apply(hooks_dir)
    yield hooks_dir


@pytest.mark.parametrize("hooks_dir", indirect=["hooks_dir"], argvalues=[
    pytest.param(
        [
            FileEntry("executable", 0o700, ""),
            FileEntry("executable_2", 0o700, ""),
        ],
        id="two executable scripts"
    ),
])
def test_scripts_per_dir_should_list_scripts(hooks_dir):
    scripts = hooks._scriptsPerDir(hooks_dir.basename)

    assert len(scripts) == 2
    assert sorted(scripts) == sorted(list(str(p) for p in hooks_dir.visit()))


@pytest.mark.parametrize("hooks_dir", indirect=True, argvalues=[
    pytest.param(
        [
            FileEntry("non-executable", 0o666, ""),
        ],
        id="non-executable"
    ),
    pytest.param(
        [
            DirEntry("__pycache__", 0o777, []),
        ],
        id="executable directory",
        marks=pytest.mark.xfail(reason="need to filter-out dirs")
    ),
    pytest.param(
        [
            DirEntry("nested", 0o777, [
                FileEntry("executable", 0o777, "")
            ])
        ],
        id="script in nested dir",
        marks=pytest.mark.xfail(reason="need to filter-out dirs")
    ),
])
def test_scripts_per_dir_should_not_list(hooks_dir):
    assert hooks._scriptsPerDir(hooks_dir.basename) == []


@pytest.mark.parametrize("dir_name, error", [
    pytest.param(
        "/tmp/evil/absolute/path",
        "Cannot use absolute path as hook directory",
        id="absolute path",
        marks=pytest.mark.xfail(reason="needs to be removed")
    ),
    pytest.param(
        "../../tmp/evil/relative/path",
        "Hook directory paths cannot contain '..'",
        id="escaping relative path",
        marks=pytest.mark.xfail(reason="needs to be filtered-out")
    ),
])
def test_scripts_per_dir_should_raise(fake_hooks_root, dir_name, error):
    with pytest.raises(ValueError) as e:
        hooks._scriptsPerDir(dir_name)

    assert error in str(e.value)


@pytest.mark.parametrize("hooks_dir", indirect=["hooks_dir"], argvalues=[
    pytest.param(
        [
            FileEntry("executable", 0o700, ""),
        ],
        id="no trailing slash",
        marks=pytest.mark.xfail(reason="replace '+' with 'os.path.join'")
    ),
])
def test_scripts_per_dir_should_accept_root_without_trailing_slash(monkeypatch,
                                                                   hooks_dir):
    with monkeypatch.context() as m:
        hooks_root = hooks.P_VDSM_HOOKS.rstrip("/")
        m.setattr(hooks, "P_VDSM_HOOKS", hooks_root)
        scripts = hooks._scriptsPerDir(hooks_dir.basename)

        assert len(scripts) == 1


def test_rhd_should_return_unmodified_data_when_no_hooks(hooks_dir):
    assert hooks._runHooksDir(u"algo", hooks_dir.basename) == u"algo"


@pytest.fixture
def dummy_hook(hooks_dir):
    FileEntry("hook.sh", 0o755, "#!/bin/bash").apply(hooks_dir)
    yield


@pytest.mark.parametrize("data, expected", [
    pytest.param(
        None,
        u"",
        id="no XML data"
    ),
    pytest.param(
        u"",
        u"",
        id="empty XML data"
    ),
    pytest.param(
        u"<abc>def</abc>",
        u"<abc>def</abc>",
        id="simple XML data"
    ),
])
@pytest.mark.xfail(six.PY3, reason="'str' passed to 'os.write'")
def test_rhd_should_handle_xml_data(dummy_hook, hooks_dir, data, expected):
    result = hooks._runHooksDir(data, hooks_dir.basename,
                                hookType=hooks._DOMXML_HOOK)
    assert result == expected


@pytest.mark.parametrize("data, expected", [
    pytest.param(
        None,
        None,
        id="no JSON data"
    ),
    pytest.param(
        {"abc": "def"},
        {"abc": "def"},
        id="simple JSON data"
    ),
    pytest.param(
        {"key": b"\xc4\x85b\xc4\x87".decode("utf-8")},
        {"key": b"\xc4\x85b\xc4\x87".decode("utf-8")},
        id="JSON data with localized chars"
    ),
])
@pytest.mark.xfail(six.PY3, reason="need to encode JSON for 'os.write'")
def test_rhd_should_handle_json_data(dummy_hook, hooks_dir, data, expected):
    result = hooks._runHooksDir(data, hooks_dir.basename,
                                hookType=hooks._JSON_HOOK)
    assert result == expected


def appender_script(script_name, exit_code=0):
    code = textwrap.dedent(
        """\
        #!/bin/bash
        myname="$(basename "$0")"
        echo "$myname" >> "$_hook_domxml"
        >&2 echo "$myname"
        exit {exit_code}
        """.format(exit_code=exit_code))
    return FileEntry(script_name, 0o777, code)


@pytest.mark.parametrize("hooks_dir", indirect=True, argvalues=[
    pytest.param(
        [
            appender_script("myhook.sh"),
        ],
        id="single hook"
    ),
])
@pytest.mark.xfail(six.PY3, reason="'str' passed to 'os.write'")
def test_rhd_should_run_a_hook(hooks_dir):
    assert hooks._runHooksDir(u"123", hooks_dir.basename) == u"123myhook.sh\n"


@pytest.mark.parametrize("hooks_dir", indirect=True, argvalues=[
    pytest.param(
        perm,
        id="-".join(script.name for script in perm)
    ) for perm in itertools.permutations([
        appender_script("1.sh"),
        appender_script("2.sh"),
        appender_script("3.sh")
    ])
])
@pytest.mark.xfail(six.PY3, reason="'str' passed to 'os.write'")
def test_rhd_should_run_hooks_in_order(hooks_dir):
    assert hooks._runHooksDir(u"", hooks_dir.basename) == u"1.sh\n2.sh\n3.sh\n"


@pytest.mark.parametrize("hooks_dir,error", indirect=["hooks_dir"], argvalues=[
    pytest.param(
        [
            appender_script("1.sh"),
            appender_script("2.sh", exit_code=1),
            appender_script("3.sh", exit_code=1)
        ],
        ["2.sh", "3.sh"],
        id="non-fatal hook errors",
        marks=pytest.mark.xfail(reason="doesn't include all 'err's")
    ),
    pytest.param(
        [
            appender_script("1.sh"),
            appender_script("2.sh", exit_code=2),
            appender_script("3.sh")
        ],
        ["2.sh"],
        id="fatal hook error, '3.sh' skipped"
    ),
])
@pytest.mark.xfail(six.PY3, reason="'str' passed to 'os.write'")
def test_rhd_should_raise_hook_errors(hooks_dir, error):
    with pytest.raises(exception.HookError) as e:
        hooks._runHooksDir(u"", hooks_dir.basename)

    for err in error:
        assert err in str(e.value)


@pytest.mark.parametrize("hooks_dir,expected", indirect=["hooks_dir"],
                         argvalues=[
    pytest.param(
        [
            appender_script("1.sh"),
            appender_script("2.sh", exit_code=1),
            appender_script("3.sh")
        ],
        u"1.sh\n2.sh\n3.sh\n",
        id="non-fatal hook error"
    ),
    pytest.param(
        [
            appender_script("1.sh"),
            appender_script("2.sh", exit_code=2),
            appender_script("3.sh")
        ],
        u"1.sh\n2.sh\n",
        id="fatal hook error, '3.sh' skipped"
    ),
])
@pytest.mark.xfail(six.PY3, reason="'str' passed to 'os.write'")
def test_rhd_should_handle_hook_errors(hooks_dir, expected):
    assert hooks._runHooksDir(u"", hooks_dir.basename, raiseError=False) == \
        expected


@pytest.mark.parametrize("hooks_dir", indirect=True, argvalues=[
    pytest.param(
        [
            appender_script("1.sh", exit_code=111),
        ],
        id="invalid exit code"
    ),
])
@pytest.mark.xfail(six.PY3, reason="'str' passed to 'os.write'")
def test_rhd_should_report_invalid_hook_error_codes(caplog, hooks_dir):
    hooks._runHooksDir(u"", hooks_dir.basename, raiseError=False)

    assert "111" in "".join(msg
                            for (_, lvl, msg) in caplog.record_tuples
                            if lvl == logging.WARNING)


@pytest.mark.parametrize("hooks_dir", indirect=True, argvalues=[
    pytest.param(
        [
            appender_script("1.sh"),
        ],
        id="script writing to stderr"
    ),
])
@pytest.mark.xfail(six.PY3, reason="'str' passed to 'os.write'")
def test_rhd_should_report_hook_stderr(caplog, hooks_dir):
    caplog.set_level(logging.INFO)
    hooks._runHooksDir(u"", hooks_dir.basename)

    assert "1.sh" in "".join(msg
                             for _, lvl, msg in caplog.record_tuples
                             if lvl == logging.INFO)


@pytest.fixture
def env_dump(hooks_dir):
    dump_path = str(hooks_dir.join("env_dump.pickle"))
    code = textwrap.dedent(
        """\
        #!{}
        import os
        import pickle
        import six

        with open("{}", "wb") as dump_file:
            env = dict()
            for k, v in os.environ.items():
                if isinstance(v, six.binary_type):
                    v = v.decode("utf-8")
                env[k] = v
            pickle.dump(env, dump_file)
        """).format(sys.executable, dump_path)
    FileEntry("env_dump.py", 0o755, code).apply(hooks_dir)
    yield dump_path


@pytest.mark.parametrize("vmconf, params, expected", [
    pytest.param(
        {},
        {"abc": "def"},
        {"abc": "def"},
        id="simple variable"
    ),
    pytest.param(
        {},
        {"abc": b"\xc4\x85b\xc4\x87".decode("utf-8")},
        {"abc": b"\xc4\x85b\xc4\x87".decode("utf-8")},
        id="variable with local chars"
    ),
    pytest.param(
        {},
        {"abc": u"\udcfc"},
        {},
        id="variable with invalid utf-8 should be ignored",
        marks=pytest.mark.xfail(six.PY3, reason="wrong exception caught")
    ),
    pytest.param(
        {"vmId": "myvm"},
        {},
        {"vmId": "myvm"},
        id="VM id"
    ),
    pytest.param(
        {"custom": {"abc": "def"}},
        {},
        {"abc": "def"},
        id="vmconf param"
    ),
    pytest.param(
        {"custom": {"abc": "geh"}},
        {"abc": "def"},
        {"abc": "geh"},
        id="vmconf param override"
    ),
])
@pytest.mark.xfail(six.PY3, reason="'str' passed to 'os.write'")
def test_rhd_should_assemble_environment_for_hooks(hooks_dir, env_dump, vmconf,
                                                   params, expected):
    hooks._runHooksDir(u"", hooks_dir.basename, vmconf, params=params)
    env = pickle.load(open(env_dump, "rb"))

    for k, v in expected.items():
        assert env[k] == v


@pytest.fixture
def mkstemp_path(monkeypatch, hooks_dir):
    with monkeypatch.context() as m:
        tmp_path = str(hooks_dir.join("tmp_file"))

        def impl():
            return os.open(tmp_path, os.O_RDWR | os.O_CREAT, 0o600), tmp_path

        m.setattr(hooks.tempfile, 'mkstemp', impl)
        yield tmp_path


@pytest.mark.parametrize("var_name, hook_type", [
    pytest.param(
        "_hook_domxml",
        hooks._DOMXML_HOOK,
        id="xml hook"
    ),
    pytest.param(
        "_hook_json",
        hooks._JSON_HOOK,
        id="JSON hook"
    )
])
@pytest.mark.xfail(six.PY3, reason="'str' passed to 'os.write'")
def test_rhd_should_pass_data_file_to_hooks(hooks_dir, env_dump, mkstemp_path,
                                            var_name, hook_type):
    hooks._runHooksDir(None, hooks_dir.basename, hookType=hook_type)
    env = pickle.load(open(env_dump, "rb"))

    assert env[var_name] == mkstemp_path


@pytest.fixture
def hooking_client(hooks_dir):
    code = textwrap.dedent(
        """\
        #!{}
        import sys

        try:
            import hooking
        except ImportError:
            sys.exit(2)
        """).format(sys.executable)
    FileEntry("hook_client.py", 0o755, code).apply(hooks_dir)
    yield


@pytest.mark.xfail(six.PY3, reason="'str' passed to 'os.write'")
def test_rhd_should_make_import_hooking_possible(hooks_dir, hooking_client):
    hooks._runHooksDir(u"", hooks_dir.basename)


@pytest.mark.parametrize("hooks_dir, expected", indirect=["hooks_dir"],
                         argvalues=[
    pytest.param(
        [
            FileEntry("script.sh", 0o777, "abc")
        ],
        hashlib.md5(b"abc").hexdigest(),
        id="simple script"
    ),
    pytest.param(
        [],
        "",
        id="non-existent script"
    ),
])
def test_get_script_info_should_return_checksum(hooks_dir, expected):
    path = str(hooks_dir.join("script.sh"))

    assert hooks._getScriptInfo(path) == {"md5": expected}


class TestHooks(TestCaseBase):

    def createScript(self, dir='/tmp'):
        script = tempfile.NamedTemporaryFile(dir=dir, delete=False)
        code = """#! /bin/bash
echo "81212590184644762"
        """
        script.write(code)
        script.close()
        os.chmod(script.name, 0o775)
        return script.name, '683394fc34f6830dd1882418eefd9b66'

    @pytest.mark.xfail(six.PY3, reason="needs porting to py3")
    def test_getHookInfo(self):
        with namedTemporaryDir() as dir:
            sName, md5 = self.createScript(dir)
            with tempfile.NamedTemporaryFile(dir=dir) as NEscript:
                os.chmod(NEscript.name, 0o000)
                info = hooks._getHookInfo(dir)
                expectedRes = dict([(os.path.basename(sName), {'md5': md5})])
                self.assertEqual(expectedRes, info)

    def test_pause_flags(self):
        vm_id = '042f6258-3446-4437-8034-0c93e3bcda1b'
        with namedTemporaryDir() as tmpDir:
            flags_path = os.path.join(tmpDir, '%s')
            with MonkeyPatchScope([(hooks, '_LAUNCH_FLAGS_PATH', flags_path)]):
                flags_file = hooks._LAUNCH_FLAGS_PATH % vm_id
                for flag in [libvirt.VIR_DOMAIN_NONE,
                             libvirt.VIR_DOMAIN_START_PAUSED]:
                    self.assertFalse(os.path.exists(flags_file))
                    hooks.dump_vm_launch_flags_to_file(vm_id, flag)
                    read_flag = hooks.load_vm_launch_flags_from_file(vm_id)
                    self.assertEqual(flag, read_flag)
                    self.assertTrue(os.path.exists(flags_file))
                    hooks.remove_vm_launch_flags_file(vm_id)
                    self.assertFalse(os.path.exists(flags_file))

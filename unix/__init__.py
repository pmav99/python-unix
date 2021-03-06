import os
import re
import sys
import time
import fcntl
import socket
import select
import signal
import subprocess
import paramiko
import weakref
from contextlib import contextmanager
from unix.processes import Processes as _Processes
from unix.path import Path as _Path, escape
from unix.remote import Remote as _Remote
from unix.users import Users as _Users
from unix.groups import Groups as _Groups
from paramiko.py3compat import u, b

if sys.version_info.major < 3:
    from pipes import quote
else:
    from shlex import quote

#
# Logs.
#
import logging
logger = logging.getLogger('unix')
logger.setLevel('NOTSET')
cli_handler = logging.StreamHandler()
cli_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
logger.addHandler(cli_handler)

#
# Utils functions.
#
def instances(host):
    return list(reversed([elt.__name__.replace('Host', '')
                          for elt in host.__class__.mro()[:-2]]))

def ishost(host, value):
    return True if value in instances(host) else False

def isvalid(host):
    if instances(host)[0] not in ('Local', 'Remote'):
        raise ValueError("this is not a 'Local' or a 'Remote' host")


#
# Constants
#
# Available controls with their defaults values.
_CONTROLS = {'options_place': 'before',
             'locale': 'en_US.utf-8',
             'decode': 'utf-8',
             'envs': {},
             'timeout': 0,
             'escape_args': True,
             'shell': None,
             'su': None}

# Errors.
_HOST_CLASS_ERR = ("don't use 'Host' class directly, use 'Local' or "
                   "'Remote' class instead.")
_NOT_CONNECTED_ERR = 'you are not connected'
_IP_ERR = 'unable to get an IPv4 or an IPv6 addresse.'

# Regular expression for matching IPv4 address.
_IPV4 = re.compile(r'^[0-9]{1,3}.[0-9]{1,3}.[0-9]{1,3}.[0-9]{1,3}$')

# Regular expression for matching IPv6 address.
_IPV6 = re.compile(r'^[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}:'
                    '[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}:'
                    '[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}$')


#
# Exceptions.
#
class UnixError(Exception):
    pass

class TimeoutError(Exception):
    pass

class timeout:
    def __init__(self, seconds=1, error_message='Timeout'):
        self.seconds = seconds
        self.error_message = error_message

    def handle_timeout(self, signum, frame):
        raise TimeoutError(self.error_message)

    def __enter__(self):
        if self.seconds != 0:
            signal.signal(signal.SIGALRM, self.handle_timeout)
            signal.alarm(self.seconds)

    def __exit__(self, type, value, traceback):
        if self.seconds != 0:
            signal.alarm(0)


#
# Abstract class for managing a host.
#
class Host(object):
    """Class that implement commands that are commons to local or remote
    host."""
    def __init__(self):
        self.return_code = -1
        for control, value in _CONTROLS.items():
            setattr(self, '_%s' % control, value)

    @property
    def path(self):
        return _Path(weakref.ref(self)())

    @property
    def remote(self):
        return _Remote(weakref.ref(self)())

    @property
    def users(self):
        return _Users(weakref.ref(self)())

    @property
    def groups(self):
        return _Groups(weakref.ref(self)())

    @property
    def processes(self):
        return _Processes(weakref.ref(self)())

    @property
    def controls(self):
        return {control: getattr(self, '_%s' % control) for control in _CONTROLS}

    def get_control(self, control):
        if control not in _CONTROLS:
            raise UnixError("invalid control '%s'" % control)
        return getattr(self, '_%s' % control)

    def set_control(self, control, value):
        if control not in _CONTROLS:
            raise UnixError("invalid control '%s'" % control)
        setattr(self, '_%s' % control, value)

    @contextmanager
    def set_controls(self, **controls):
        cur_controls = dict(self.controls)

        try:
            for control, value in controls.items():
                self.set_control(control, value)
            yield None
        finally:
            for control, value in cur_controls.items():
                self.set_control(control, value)

    def _format_command(self, cmd, args, options):
        command = []
        args = [quote(arg) if self._escape_args else arg for arg in args]

        # Get environments variables (from 'locale' and 'envs' controls).
        envs = ({var: self._locale for var in ('LC_ALL', 'LANGUAGE', 'LANG')}
                if self._locale
                else {})
        envs.update(self._envs)

        # For CSH shell, we need to declare environments variables with 'env' keyword.
        if envs and (self._shell or self.default_shell) == 'csh':
            command.append('env')
        command.extend('%s=%s' % (var, value) for var, value in sorted(envs.items()))

        # Add command to execute.
        command.append(cmd)

        # Get specials options.
        stdin = options.pop('STDIN', None)
        stdout = options.pop('STDOUT', None)
        stderr = options.pop('STDERR', None)

        # Add arguments before options if 'options_place' control is set to 'after'.
        if self._options_place == 'after':
            command.extend([str(arg) for arg in args])

        # Add options.
        for option, value in options.items():
            option = ('-%s' % option
                      if len(option) == 1
                      else '--%s' % option.replace('_', '-'))
            if not isinstance(value, (list, tuple, set)):
                value = [value]
            command.extend(
                option if isinstance(val, bool) else '{:s} {:s}'.format(option, val)
                for val in value
                if val)

        # Add arguments now if 'options_place' control is set to 'before' (the default).
        if self._options_place == 'before':
            command.extend(args)

        if stdin:
            command.append(' < %s' % stdin)
        if stdout:
            command.append(' >> {:s}'.format(stdout[1:])
                           if stdout.startswith('+')
                           else ' > {:s}'.format(stdout))
        if stderr:
            command.append(' 2>> {:s}'.format(stderr[1:])
                           if stderr.startswith('+')
                           else ' 2> {:s}'.format(stderr))

        command = ' '.join(map(str, command))
        if self._shell:
            command = '%s -c %s' % (self._shell, quote(command))
        if self._su:
            command = 'su - %s -c %s' % (self._su, quote(command))
        logger.debug('[execute] %s' % command)
        return command

    def _manage_encoding(self, output):
        return u(output, self._decode) if self._decode else b(output)

    def execute(self):
        raise NotImplementedError(_HOST_CLASS_ERR)

    @property
    def type(self):
        """Property that return the type of the operating system by executing
        ``uname -s`` command."""
        return self.execute('uname', s=True)[1].splitlines()[0].lower()

    @property
    def arch(self):
        """Property that return the architecture of the operating system by
        executing ``uname -m`` command."""
        return self.execute('uname', m=True)[1].splitlines()[0]

    @property
    def hostname(self):
        return self.execute('hostname')[1].splitlines()[0]

    def list(self, path, **opts):
        status, stdout, stderr = self.execute('ls', escape(path), **opts)
        if not status:
            raise OSError(stderr)
        return stdout

    def listdir(self, path, hidden=False):
        """List files in a directory.

        .. note::
            As the exception raised is different when using local function
            ``os.listdir(path)`` or remote function ``sftp.listdir(path)``, this
            method use ``ls`` command for listing directory and raise the
            **IOError** exception if **path** not exists or **OSError** if there
            is another unexpected error.
        """
        if not self.path.exists(path):
            raise IOError("'%s' not exists" % path)
        if not self.path.isdir(path):
            raise IOError("'%s' is not a directory" % path)

        # ls -1 allows to have one file per line.
        opts = {'1': True}
        if hidden:
            opts.update(a=True)
        return [filename
                for filename in self.list(path, **opts).splitlines()
                if filename not in ('.', '..')]

    def touch(self, *paths, **options):
        paths = [escape(path) for path in paths]
        return self.execute('touch', *paths, **options)

    def mkdir(self, *paths, **options):
        """Create a directory. *args and **options contains options that can be
        passed to the command. **options can contain an additionnal key
        *INTERACTIVE* that will be pass to ``execute`` function."""
        paths = [escape(path) for path in paths]
        return self.execute('mkdir', *paths, **options)

    def copy(self, *paths, **options):
        """Copy **src** file or directory to **dst**. *paths and **options
        contains options that can be passed to the command. **options can
        contain an additionnal key *INTERACTIVE* that will be pass to
        ``execute`` function."""
        paths = [escape(path) for path in paths]
        return self.execute('cp', *paths, **options)

    def move(self, *paths, **options):
        paths = [escape(path) for path in paths]
        return self.execute('mv', *paths, **options)

    def remove(self, *paths, **options):
        paths = [escape(path) for path in paths]
        return self.execute('rm', *paths, **options)

    def chmod(self, permissions, *paths, **options):
        paths = [escape(path) for path in paths]
        return self.execute('chmod', permissions, *paths, **options)

    def chown(self, owner, *paths, **options):
        paths = [escape(path) for path in paths]
        return self.execute('chown', owner, *paths, **options)

    def chgrp(self, group, *paths, **options):
        paths = [escape(path) for path in paths]
        return self.execute('chgrp', group, *path, **options)

    def which(self, command, **options):
        try:
            return self.execute('which', command, **options)[1].splitlines()[0]
        except IndexError:
            raise UnixError("which: unable to find command '%s'" % command)

    def read(self, filepath):
        with self.open(filepath) as fhandler:
            return fhandler.read().decode()

    def write(self, filepath, content):
        with self.open(filepath, 'w') as fhandler:
            fhandler.write(content)

    def mount(self, device, mount_point, **options):
        mount_point = escape(mount_point)
        return self.execute('mount', device, mount_point, **options)

    def umount(self, mount_point, **options):
        mount_point = escape(mount_point)
        return self.execute('umount', mount_point, **options)

    @contextmanager
    def mountfs(self, device, mount_point, **options):
        try:
            self.mount(device, mount_point, **options)
            yield None
        finally:
            self.umount(mount_point)

    def replace(self, filepath, pattern, replacement, backup=None):
        with self.open(filepath) as fhandler:
            new_content = re.sub(pattern, replacement, fhandler.read().decode())
        if backup:
            if not self.copy(filepath, '%s.%s' % (filepath, backup)):
                return [False, '', 'unable to backup file']
        with self.open(filepath, 'w') as fhandler:
            fhandler.write(new_content)
        return [True, '', '']


#
# Class for managing localhost (subprocess).
#
class Local(Host):
    """Implementing specifics functions of localhost."""
    def __init__(self):
        Host.__init__(self)

        # Get the default shell (without using any environments variables as
        # shells differ for managing them).
        with self.set_controls(locale='', envs={}):
            self.default_shell = self.execute('echo $0')[1].strip()

    @staticmethod
    def clone(host):
        new_host = Local()
        new_host.__dict__.update(return_code=host.return_code)
        new_host.__dict__.update(host.controls)
        new_host.__dict__.update(default_shell=host.default_shell)
        return new_host

    @property
    def username(self):
        return self.users.username(os.getuid())

    def is_connected(self):
        pass

    def execute(self, command, *args, **options):
        """Function that execute a command using english utf8 locale. The output
        is a list of three elements: a boolean representing the status of the
        command (True if return code equal to 0), the standard output (stdout)
        and the error output (stderr). If **INTERACTIVE**, the command is
        executed interactively (printing output in real time and waiting for
        inputs) and stdout and stderr are empty. The return code of the last
        command is put in *return_code* attribut."""
        command = self._format_command(command, args, options)

        with timeout(self._timeout):
            try:
                obj = subprocess.Popen(command,
                                       shell=True,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
                stdout, stderr = obj.communicate()
                self.return_code = obj.returncode
                return [True if self.return_code == 0 else False,
                        self._manage_encoding(stdout),
                        self._manage_encoding(stderr)]
            except OSError as err:
                return [False,
                        self._manage_encoding(''),
                        self._manage_encoding(str(err))]

    def interactive(self, command, *args, **options):
        """
        """
        command = self._format_command(command, args, options)
        self.return_code = subprocess.call(command,
                                           shell=True,
                                           stderr=subprocess.STDOUT)

    def iter(self, command, *args, **options):
        """
        """
        command = self._format_command(command, args, options)
        process = subprocess.Popen(command,
                                   shell=True,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   universal_newlines=True)

        # Prevent read* methods on the stdout and stderr buffers to wait for
        # new data (ie: set stdout and stderr files in non-blocking mode).
        stdout_flags = fcntl.fcntl(process.stdout, fcntl.F_GETFL)
        fcntl.fcntl(process.stdout, fcntl.F_SETFL, stdout_flags | os.O_NDELAY)
        stderr_flags = fcntl.fcntl(process.stderr, fcntl.F_GETFL)
        fcntl.fcntl(process.stderr, fcntl.F_SETFL, stderr_flags | os.O_NDELAY)

        while process.poll() is None:
            ready = select.select([process.stdout, process.stderr], [], [])

            if process.stdout in ready[0]:
                for line in process.stdout.read().splitlines():
                    yield (u'stdout', self._manage_encoding(line))

            if process.stderr in ready[0]:
                for line in process.stderr.read().splitlines():
                    yield (u'stderr', self._manage_encoding(line))
        self.return_code = process.returncode
        yield (u'status', True if self.return_code == 0 else False)

    def open(self, filepath, mode='r'):
        # For compatibility with SFTPClient object, the file is always open
        # in binary mode.
        if 'b' not in mode:
            mode += 'b'
        return open(filepath, mode)

    def tail(self, filepath, delta=1):
        prev_size = os.stat(filepath).st_size
        while 1:
            cur_size = os.stat(filepath).st_size
            # File has been rotate.
            if cur_size < prev_size:
                with self.open(filepath) as fhandler:
                    for line in fhandler.read().splitlines():
                        yield line
            else:
                with self.open(filepath) as fhandler:
                    fhandler.seek(prev_size, 0)
                    for line in fhandler.read().splitlines():
                        yield line
            prev_size = cur_size
            time.sleep(delta)


#
# Context Manager for connecting to a remote host.
#
class connect(object):
    def __init__(self, host, force_ssh=False, **kwargs):
        self.hostname = host
        self.options = kwargs
        self.force_ssh = force_ssh

    def __enter__(self):
        if self.hostname == 'localhost' and not self.force_ssh:
            self._host = Local()
        else:
            self._host = Remote()
            self._host.connect(self.hostname, **self.options)
        return self._host

    def __exit__(self, type, value, traceback):
        if self.hostname != 'localhost':
            self._host.disconnect()
            del self._host


#
# Class for managing a remote host with SSH (paramiko).
#
class Remote(Host):
    def __init__(self):
        Host.__init__(self)
        self.forward_agent = True
        self.ip = None
        self.ipv4 = None
        self.ipv6 = None
        self.fqdn = None
        self.username = None
        self._conn = None

    @staticmethod
    def clone(host):
        """Static method for enforcing clone of this object."""
        new_host = Remote()
        new_host.__dict__.update(return_code=host.return_code)
        new_host.__dict__.update(host.controls)
        attrs = ('ip', 'ipv4', 'ipv6', 'fqdn', 'username', 'default_shell')
        new_host.__dict__.update({attr: getattr(host, attr) for attr in attrs})
        if hasattr(host, '_conn'):
            new_host.__dict__.update(_conn=host._conn)
        return new_host

    def __ipv4(self):
        try:
            return socket.getaddrinfo(self.fqdn, 22, 2, 1, 6)[0][4][0]
        except socket.gaierror:
            return None

    def __ipv6(self):
        try:
            return socket.getaddrinfo(self.fqdn, 22, 10, 1, 6)[0][4][0]
        except socket.gaierror:
            return None

    def __fqdn(self):
        try:
            if self.ipv4:
                return socket.gethostbyaddr(self.ipv4)[0]
            elif self.ipv6:
                return socket.gethostbyadd(self.ipv6)[0]
            else:
                return None
        except socket.herror:
            return None

    def connect(self, host, **kwargs):
        keepalive = kwargs.pop('keepalive', 0)
        self.forward_agent = kwargs.pop('forward_agent', True)
        self.username = kwargs.pop('username', 'root')

        if _IPV4.match(host):
            self.ipv4 = host
            self.fqdn = self.__fqdn()
            self.ipv6 = self.__ipv6()
        elif _IPV6.match(host):
            self.ipv6 = host
            self.ipv4 = self.__ipv4()
            self.fqdn = self.__fqdn()
        else:
            self.fqdn = host
            self.ipv4 = self.__ipv4()
            self.ipv6 = self.__ipv6()
            self.fqdn = self.__fqdn()

        if not self.ipv4 and not self.ipv6:
            raise UnixError(_IP_ERR)
        self.ip = (self.ipv6 if self.ipv6 and kwargs.pop('ipv6', False)
                             else self.ipv4)

        params = {'username': self.username}
        for param, value in kwargs.items():
            params[param] = value
        self._conn = paramiko.SSHClient()
        try:
            self._conn.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._conn.connect(self.ip, **params)
        except Exception as err:
            raise UnixError(err)

        # Add keepalive on connection.
        self._conn.get_transport().set_keepalive(keepalive)

        # Optimizations for file transfert
        # (see https://github.com/paramiko/paramiko/issues/175)
        # From 6Mb/s to 12Mb/s => still very slow (scp = 40Mb/s)!
        self._conn.get_transport().window_size = 2147483647
        self._conn.get_transport().packetizer.REKEY_BYTES = pow(2, 40)
        self._conn.get_transport().packetizer.REKEY_PACKETS = pow(2, 40)

        # Get the default shell (without using any environments variables as
        # shells differ for managing them).
        with self.set_controls(locale='', envs={}):
            self.default_shell = self.execute('echo $0')[1].strip()

    def disconnect(self):
        self._conn.close()

    def is_connected(self):
        if self._conn is None or not self._conn.get_transport():
            raise UnixError(_NOT_CONNECTED_ERR)

    @contextmanager
    def _get_chan(self, get_pty=False):
        self.is_connected()
        chan = self._conn.get_transport().open_session()
        try:
            if get_pty:
                chan.get_pty()
            yield chan
        finally:
            chan.close()

    @contextmanager
    def _forward_agent(self, chan):
        forward = (paramiko.agent.AgentRequestHandler(chan)
                   if self.forward_agent
                   else None)
        try:
            yield forward
        finally:
            if forward:
                forward.close()

    def execute(self, command, *args, **options):
        with self._get_chan(options.pop('get_pty', False)) as chan:
            with self._forward_agent(chan):
                with timeout(self._timeout):
                    command = self._format_command(command, args, options)
                    chan.exec_command(command)
                    self.return_code = chan.recv_exit_status()
                    stdout = chan.makefile('rb', -1).read()
                    stderr = chan.makefile_stderr('rb', -1).read()
                    return [True if self.return_code == 0 else False,
                            self._manage_encoding(stdout),
                            self._manage_encoding(stderr)]

    def interactive(self, command, *args, **options):
        import termios
        import tty
        with self._get_chan(options.pop('get_pty', False)) as chan:
            with self._forward_agent(chan):
                with timeout(self._timeout):
                    command = self._format_command(command, args, options)

                    chan.get_pty()
                    oldtty = termios.tcgetattr(sys.stdin)
                    try:
                        tty.setraw(sys.stdin.fileno())
                        tty.setcbreak(sys.stdin.fileno())
                        chan.settimeout(0.0)
                        chan.exec_command(command)

                        while True:
                            rlist = select.select([chan, sys.stdin], [], [])[0]
                            if chan in rlist:
                                try:
                                    data = self._manage_encoding(chan.recv(1024))
                                    if len(data) == 0:
                                        break
                                    sys.stdout.write(data)
                                    sys.stdout.flush()
                                except socket.timeout:
                                    pass
                            if sys.stdin in rlist:
                                data = sys.stdin.read(1)
                                if len(data) == 0:
                                    break
                                chan.send(data)
                    finally:
                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, oldtty)

    def iter(self, command, *args, **options):
        with self._get_chan(options.pop('get_pty', False)) as chan:
            with self._forward_agent(chan):
                with timeout(self._timeout):
                    # Non-blocking mode.
                    chan.settimeout(0.0)

                    command = self._format_command(command, args, options)
                    chan.exec_command(command)
                    end = False
                    while not end:
                        try:
                            stdout = chan.recv(1024)
                            for line in stdout.splitlines():
                                yield (u('stdout'), self._manage_encoding(line))
                            if not stdout:
                                end = True
                        except socket.timeout:
                            pass

                        try:
                            for line in chan.recv_stderr(1024).splitlines():
                                yield (u('stderr'), self._manage_encoding(line))
                        except socket.timeout:
                            pass

                    self.return_code = chan.recv_exit_status()
                    yield ('status', True if self.return_code == 0 else False)

    def open(self, filepath, mode='r'):
        self.is_connected()
        sftp = paramiko.SFTPClient.from_transport(self._conn.get_transport())
        # File is always open in binary mode but 'readline' function decode
        # the line if the binary mode is not specified! So force the binary mode
        # for letting client program decoding lines.
        if 'b' not in mode:
            mode += 'b'
        return sftp.open(filepath, mode)

    def tail(self, filepath, delta=1):
        sftp = paramiko.SFTPClient.from_transport(self._conn.get_transport())

        prev_size = sftp.stat(filepath).st_size
        while 1:
            with timeout(self._timeout):
                cur_size = sftp.stat(filepath).st_size

                # File has been rotate.
                if cur_size < prev_size:
                    with self.open(filepath) as fhandler:
                        for line in fhandler.read().splitlines():
                            yield line.decode()
                else:
                    with self.open(filepath) as fhandler:
                        fhandler.seek(prev_size, 0)
                        for line in fhandler.read().splitlines():
                            yield line.decode()
                prev_size = cur_size
            time.sleep(delta)

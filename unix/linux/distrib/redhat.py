# -*- coding: utf-8 -*-

import unix
from .. import Linux, LinuxError

DISTRIBS = ('RedHat', 'CentOS')

def RedHat(host, root='', force=False):
    unix.isvalid(host)

    instances = unix.instances(host)
    if len(instances) >= 1:
        host = Linux(getattr(unix, instances[0]).clone(host), root)

    if host.distrib[0] not in DISTRIBS and not force:
        raise LinuxError('invalid distrib')

    class RedhatHost(host.__class__):
        def __init__(self, root=''):
            host.__class__.__init__(self, root)
            self.__dict__.update(host.__dict__)


        def list_packages(self):
            return self.execute('dpkg -l')


        def set_hostname(self, hostname):
            with self.open('/etc/hostname') as fhandler:
                fhandler.write(hostname)

    return RedhatHost(root)

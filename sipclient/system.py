
"""System utilities used by the sipclient scripts"""

__all__ = ['IPAddressMonitor', 'copy_default_certificates']

import os
import shutil

from application.notification import NotificationCenter, NotificationData
from application.system import host, makedirs
from eventlib import api

from sipclient.configuration import config_directory
from sipclient.configuration.datatypes import ResourcePath
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread


class IPAddressMonitor(object):
    """
    An object which monitors the IP address used for the default route of the
    host and posts a SystemIPAddressDidChange notification when a change is
    detected.
    """

    def __init__(self):
        self.greenlet = None

    @run_in_green_thread
    def start(self):
        notification_center = NotificationCenter()

        if self.greenlet is not None:
            return
        self.greenlet = api.getcurrent()

        current_address = host.default_ip
        while True:
            new_address = host.default_ip
            # make sure the address stabilized
            api.sleep(5)
            if new_address != host.default_ip:
                continue
            if new_address != current_address:
                notification_center.post_notification(name='SystemIPAddressDidChange', sender=self, data=NotificationData(old_ip_address=current_address, new_ip_address=new_address))
                current_address = new_address
            api.sleep(5)

    @run_in_twisted_thread
    def stop(self):
        if self.greenlet is not None:
            api.kill(self.greenlet, api.GreenletExit())
            self.greenlet = None


def copy_default_certificates():
    default_tls_certificate = ResourcePath('tls/default.crt').normalized
    local_tls_certificate = os.path.join(config_directory, 'tls/default.crt')

    if not os.path.isfile(local_tls_certificate):
        makedirs(os.path.join(config_directory, 'tls'))
        shutil.copy(default_tls_certificate, local_tls_certificate)

    default_tls_ca = ResourcePath('tls/ca.crt').normalized
    local_tls_ca = os.path.join(config_directory, 'tls/ca.crt')

    if not os.path.isfile(local_tls_ca):
        makedirs(os.path.join(config_directory, 'tls'))
        shutil.copy(default_tls_ca, local_tls_ca)

        
#!/usr/bin/python3

from distutils.core import setup
import os
import glob

import sipclient

setup(
    name='sipclients3',
    version=sipclient.__version__,

    description='SIP SIMPLE client',
    long_description='Python command line clients using the SIP SIMPLE framework',
    url='http://sipsimpleclient.org',

    author='AG Projects',
    author_email='support@ag-projects.com',

    platforms=['Platform Independent'],
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Service Providers',
        'License :: GNU Lesser General Public License (LGPL)',
        'Operating System :: OS Independent',
        'Programming Language :: Python'
    ],

    packages=['sipclient', 'sipclient.configuration'],
    data_files=[('share/sipclients3/sounds', glob.glob(os.path.join('resources', 'sounds', '*.wav'))), ('share/sipclients3/tls', ['resources/tls/ca.crt', 'resources/tls/default.crt'])],
    scripts=[
        'sip-audio-session3',
        'sip-message3',
        'sip-publish-presence3',
        'sip-register3',
        'sip-session3',
        'sip-settings3',
        'sip-subscribe-conference3',
        'sip-subscribe-mwi3',
        'sip-subscribe-presence3',
        'sip-subscribe-rls3',
        'sip-subscribe-winfo3',
        'sip-subscribe-xcap-diff3'
    ]
)

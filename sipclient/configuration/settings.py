
"""SIP SIMPLE Client settings extensions"""

__all__ = ['SIPSimpleSettingsExtension']

import os

from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension
from sipsimple.configuration.datatypes import Path, SampleRate
from sipsimple.configuration.settings import AudioSettings, LogsSettings


from sipclient.configuration.datatypes import SoundFile, UserDataPath, HTTPURL


class AudioSettingsExtension(AudioSettings):
    directory = Setting(type=UserDataPath, default=UserDataPath('history'))
    sample_rate = Setting(type=SampleRate, default=32000)
 

class LogsSettingsExtension(LogsSettings):
    directory = Setting(type=UserDataPath, default=UserDataPath('logs'))
    trace_notifications = Setting(type=bool, default=False)


class SoundsSettings(SettingsGroup):
    audio_inbound = Setting(type=SoundFile, default=SoundFile('sounds/ring_inbound.wav'), nillable=True)
    audio_outbound = Setting(type=SoundFile, default=SoundFile('sounds/ring_outbound.wav'), nillable=True)
    message_received = Setting(type=SoundFile, default=SoundFile('sounds/message_received.wav'), nillable=True)
    message_sent = Setting(type=SoundFile, default=SoundFile('sounds/message_sent.wav'), nillable=True)
    roger_beep = Setting(type=SoundFile, default=SoundFile('sounds/rogerbeep.wav'), nillable=True)
    file_received = Setting(type=SoundFile, default=SoundFile('sounds/file_received.wav'), nillable=True)
    file_sent = Setting(type=SoundFile, default=SoundFile('sounds/file_sent.wav'), nillable=True)


class EnrollmentSettings(SettingsGroup):
    default_domain = Setting(type=str, default='sip2sip.info', nillable=False)
    url = Setting(type=HTTPURL, default="https://blink.sipthor.net/enrollment.phtml", nillable=True)


class SIPSimpleSettingsExtension(SettingsObjectExtension):
    user_data_directory = Setting(type=Path, default=Path(os.path.expanduser('~/.sipclient')))
    resources_directory = Setting(type=Path, default=None, nillable=True)

    audio = AudioSettingsExtension
    logs = LogsSettingsExtension
    sounds = SoundsSettings
    enrollment = EnrollmentSettings


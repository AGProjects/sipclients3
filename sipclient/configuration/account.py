
"""SIP SIMPLE Client account settings extensions"""

__all__ = ['AccountExtension']

from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension
from sipsimple.account import RTPSettings

from sipclient.configuration.datatypes import AccountSoundFile


class RTPSettingsExtension(RTPSettings):
    inband_dtmf = Setting(type=bool, default=False)


class SoundsSettings(SettingsGroup):
    audio_inbound = Setting(type=AccountSoundFile, default=AccountSoundFile(AccountSoundFile.DefaultSoundFile('sounds.audio_inbound')), nillable=True)


class SMSSettings(SettingsGroup):
    use_cpim = Setting(type=bool, default=True)
    enable_composing = Setting(type=bool, default=True)
    enable_imdn = Setting(type=bool, default=True)
    enable_otr = Setting(type=bool, default=False)



class AccountExtension(SettingsObjectExtension):
    rtp = RTPSettingsExtension
    sounds = SoundsSettings
    sms = SMSSettings


    
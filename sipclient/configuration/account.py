
"""SIP SIMPLE Client account settings extensions"""

__all__ = ['AccountExtension', 'BonjourAccountExtension']

from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension
from sipsimple.account import RTPSettings, BonjourMSRPSettings, BonjourSIPSettings
from sipclient.configuration.datatypes import AccountSoundFile
from sipsimple.configuration.datatypes import MSRPTransport, SIPTransport


class RTPSettingsExtension(RTPSettings):
    inband_dtmf = Setting(type=bool, default=False)


class SoundsSettings(SettingsGroup):
    audio_inbound = Setting(type=AccountSoundFile, default=AccountSoundFile(AccountSoundFile.DefaultSoundFile('sounds.audio_inbound')), nillable=True)


class SMSSettings(SettingsGroup):
    use_cpim = Setting(type=bool, default=True)
    enable_composing = Setting(type=bool, default=True)
    enable_imdn = Setting(type=bool, default=True)
    enable_otr = Setting(type=bool, default=False)


class BonjourMSRPSettingsExtension(BonjourMSRPSettings):
    transport = Setting(type=MSRPTransport, default='tls')


class BonjourSIPSettingsExtension(BonjourSIPSettings):
    transport = Setting(type=SIPTransport, default='tls')
    tls_name = Setting(type=str, default='Blink')


class AccountExtension(SettingsObjectExtension):
    rtp = RTPSettingsExtension
    sounds = SoundsSettings
    sms = SMSSettings


class BonjourAccountExtension(SettingsObjectExtension):
    sip = BonjourSIPSettingsExtension
    msrp = BonjourMSRPSettingsExtension
    rtp = RTPSettingsExtension
    sounds = SoundsSettings
    sms = SMSSettings
    

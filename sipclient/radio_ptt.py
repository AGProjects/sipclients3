
"""Radio PTT — endpoint side and shared definitions.

See docs/radio-ptt.md in sylkserver for the protocol. This module holds:

  * CONTENT_TYPE, the MSRP (CPIM-wrapped) content type for commands and
    state reports, and FEATURE_TAG, the RFC 3840 Contact feature tag;
  * RadioPTTElement, the <agp-conf:radio_ptt> conference-info extension,
    registered on the sipsimple Endpoint element on import so a client can
    read the radio state from the conference NOTIFY;
  * RadioPTTEndpoint, the radio endpoint controller: runs the configured
    on/off commands, polls the status file (the hardware feedback) and
    reports state changes to the conference focus. Without a status file
    the command results stand in for the feedback: on_command exiting 0
    means transmitting, off_command exiting 0 means idle. Only on_command
    is required: without off_command the relay releases on its own timer
    and a release or timeout counts as done immediately.

All RadioPTTEndpoint state lives in the twisted reactor thread. Commands
run in a worker thread and hand their result back to the reactor.
"""

import json
import os
import subprocess
import uuid

from datetime import datetime
from threading import Thread

from sipsimple.payloads import XMLElement, XMLAttribute
from sipsimple.payloads.conference import ConferenceDocument, Endpoint, EndpointExtension, agp_conf_namespace
from twisted.internet import reactor
from twisted.internet.task import LoopingCall


__all__ = ['CONTENT_TYPE', 'FEATURE_TAG', 'RadioPTTElement', 'RadioPTTEndpoint', 'new_command_id']


CONTENT_TYPE = 'application/vnd.agp.radio-ptt+json'
FEATURE_TAG = '+g.agp.radio-ptt'   # RFC 3840 Contact feature tag

COMMAND_EXECUTION_LIMIT = 10  # seconds a relay script may run


class RadioPTTElement(XMLElement, EndpointExtension):
    _xml_tag = 'radio_ptt'
    _xml_namespace = agp_conf_namespace
    _xml_document = ConferenceDocument

    state = XMLAttribute('state', type=str, required=True, test_equal=False)
    by = XMLAttribute('by', type=str, required=False, test_equal=False)
    elapsed = XMLAttribute('elapsed', type=str, required=False, test_equal=False)
    timeout = XMLAttribute('timeout', type=str, required=False, test_equal=False)
    reason = XMLAttribute('reason', type=str, required=False, test_equal=False)

    def __init__(self, state, by=None, elapsed=None, timeout=None, reason=None):
        XMLElement.__init__(self)
        self.state = state
        self.by = by
        self.elapsed = elapsed
        self.timeout = timeout
        self.reason = reason


try:
    Endpoint.register_extension('radio_ptt', RadioPTTElement)
except ValueError:
    pass  # already registered (e.g. sylk.payloads loaded in the same process)


def new_command_id():
    return str(uuid.uuid4())


def _utcnow():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


class RadioPTTEndpoint(object):
    """Drives the radio relay and reports the on-air state.

    `log` is a callable taking one string (sip-session3 passes show_notice).
    Streams are sipsimple ChatStreams of sessions to a conference focus.
    """

    def __init__(self, on_command, off_command=None, status_file=None, timeout=30, confirm_timeout=2.0, poll_interval=0.1, log=print):
        self.on_command = on_command
        self.off_command = off_command
        self.status_file = os.path.expanduser(status_file) if status_file else None
        self.timeout = int(timeout)
        self.confirm_timeout = float(confirm_timeout)
        self.poll_interval = float(poll_interval)
        self._log = log

        self.state = 'idle'             # idle | keying | transmitting
        self.command_id = None
        self.by = None
        self.keyed_by_us = False        # we ran on_command for the current transmission
        self.release_reason = None      # set when we run off_command, consumed when the file vanishes
        self.present = False            # last observed status file presence
        self.streams = []               # chat streams of conference sessions to report to

        self._poller = None
        self._confirm_timer = None
        self._timeout_timer = None

    # -- lifecycle (call from any thread) ---------------------------------

    def start(self):
        reactor.callFromThread(self._start)

    def stop(self):
        reactor.callFromThread(self._stop)

    def attach(self, stream):
        reactor.callFromThread(self._attach, stream)

    def detach(self, stream):
        reactor.callFromThread(self._detach, stream)

    def handle_command(self, stream, content):
        reactor.callFromThread(self._handle_command, stream, content)

    # -- reactor thread ----------------------------------------------------

    def log(self, text):
        self._log('%s radio PTT %s' % (_utcnow(), text))

    def _start(self):
        if self.status_file is None:
            # No feedback: make sure the relay starts released.
            self.log('ready, no status file (state follows on/off command results), timeout %ds%s' % (self.timeout, '' if self.off_command else ', no off_command'))
            self._run(self.off_command, 'off', None)
            return
        self.present = os.path.exists(self.status_file)
        if self.present:
            # A relay left keyed (crash, power loss) — release it once.
            self.log('status file %s present at startup, running off_command' % self.status_file)
            self.release_reason = 'released'
            self._run(self.off_command, 'off', None)
            self.state = 'transmitting'
        self._poller = LoopingCall(self._poll)
        self._poller.start(self.poll_interval, now=False)
        self.log('ready, status file %s, timeout %ds%s' % (self.status_file, self.timeout, '' if self.off_command else ', no off_command'))

    def _stop(self):
        if self._poller is not None and self._poller.running:
            self._poller.stop()
        self._poller = None
        if self.keyed_by_us and self.state in ('keying', 'transmitting'):
            self.log('stopping while %s, running off_command' % self.state)
            self.release_reason = 'released'
            self._run(self.off_command, 'off', None)
        self._cancel_timers()

    def _attach(self, stream):
        if stream not in self.streams:
            self.streams.append(stream)
        # Initial report: tells the focus we are here and what the radio is doing now.
        if self.state == 'transmitting':
            self._send(stream, {'state': 'transmitting', 'id': self.command_id, 'timeout': self.timeout})
        else:
            self._send(stream, {'state': self.state, 'id': self.command_id})

    def _detach(self, stream):
        try:
            self.streams.remove(stream)
        except ValueError:
            return
        if not self.streams and self.keyed_by_us and self.state in ('keying', 'transmitting'):
            self.log('conference session ended while %s, running off_command' % self.state)
            self.release_reason = 'released'
            self._run(self.off_command, 'off', self.command_id)

    def _handle_command(self, stream, content):
        if isinstance(content, bytes):
            content = content.decode('utf-8', 'replace')
        try:
            payload = json.loads(content)
            action = payload['action']
        except (ValueError, KeyError, TypeError):
            self.log('ignoring malformed command: %r' % (content,))
            return
        command_id = payload.get('id')
        by = payload.get('by')
        if action == 'press':
            self._press(command_id, by)
        elif action == 'release':
            self._release(command_id, by)
        else:
            self.log('ignoring unknown action %r' % action)

    def _press(self, command_id, by):
        if self.state == 'transmitting':
            if self.keyed_by_us:
                self.log('press id=%s by=%s while on air, extending timeout to %ds' % (command_id, by, self.timeout))
                self.command_id, self.by = command_id, by
                self._start_timeout()
                self._report({'state': 'transmitting', 'id': command_id, 'timeout': self.timeout})
            else:
                self.log('press id=%s by=%s while keyed locally, ignored' % (command_id, by))
                self._report({'state': 'transmitting', 'id': None, 'timeout': self.timeout})
            return
        if self.state == 'keying':
            self.log('press id=%s by=%s while keying' % (command_id, by))
            self.command_id, self.by = command_id, by
            self._report({'state': 'keying', 'id': command_id, 'timeout': self.timeout})
            return
        self.log('press id=%s by=%s -> on_command' % (command_id, by))
        self.state = 'keying'
        self.command_id, self.by = command_id, by
        self.keyed_by_us = True
        self.release_reason = None
        self._report({'state': 'keying', 'id': command_id, 'timeout': self.timeout})
        if self.status_file is not None:
            self._confirm_timer = reactor.callLater(self.confirm_timeout, self._confirm_expired, command_id)
        self._run(self.on_command, 'on', command_id)

    def _release(self, command_id, by):
        self.log('release id=%s by=%s -> off_command' % (command_id, by))
        if self.state == 'keying' and not self.present:
            # Not on air yet: abort the keying attempt.
            self._cancel_timers()
            self.release_reason = None
            self.keyed_by_us = False
            self.state = 'idle'
            self._run(self.off_command, 'off', command_id)
            self._report({'state': 'idle', 'id': command_id, 'reason': 'released'})
            self.command_id = self.by = None
            return
        self.release_reason = 'released'
        self._run(self.off_command, 'off', command_id)

    def _confirm_expired(self, command_id):
        self._confirm_timer = None
        if self.state != 'keying' or self.present:
            return
        self.log('id=%s status file did not appear within %.1fs, running off_command' % (command_id, self.confirm_timeout))
        self.keyed_by_us = False
        self.state = 'idle'
        self.release_reason = None
        self._run(self.off_command, 'off', command_id)
        self._report({'state': 'error', 'id': command_id, 'reason': 'not_confirmed'})
        self.command_id = self.by = None

    def _timeout_expired(self):
        self._timeout_timer = None
        if self.state != 'transmitting':
            return
        self.log('id=%s timeout %ds reached, running off_command' % (self.command_id, self.timeout))
        self.release_reason = 'timeout'
        self._run(self.off_command, 'off', self.command_id)

    def _start_timeout(self):
        if self._timeout_timer is not None and self._timeout_timer.active():
            self._timeout_timer.cancel()
        self._timeout_timer = reactor.callLater(self.timeout, self._timeout_expired)

    def _cancel_timers(self):
        for name in ('_confirm_timer', '_timeout_timer'):
            timer = getattr(self, name)
            if timer is not None and timer.active():
                timer.cancel()
            setattr(self, name, None)

    def _poll(self):
        self._set_present(os.path.exists(self.status_file))

    def _set_present(self, present):
        if present == self.present:
            return
        self.present = present
        if present:
            if self._confirm_timer is not None and self._confirm_timer.active():
                self._confirm_timer.cancel()
            self._confirm_timer = None
            if not (self.keyed_by_us and self.state == 'keying'):
                # Keyed by hand at the radio (or a keying we already gave up on).
                self.keyed_by_us = False
                self.command_id, self.by = None, 'local'
            self.state = 'transmitting'
            if self.keyed_by_us:
                self._start_timeout()
            self.log('transmitting id=%s by=%s' % (self.command_id, self.by))
            self._report({'state': 'transmitting', 'id': self.command_id, 'timeout': self.timeout})
        else:
            reason = self.release_reason or 'hardware'
            command_id = self.command_id
            self._cancel_timers()
            self.state = 'idle'
            self.keyed_by_us = False
            self.release_reason = None
            self.command_id = self.by = None
            self.log('idle id=%s reason=%s' % (command_id, reason))
            self._report({'state': 'idle', 'id': command_id, 'reason': reason})

    # -- commands ------------------------------------------------------------

    def _run(self, command, which, command_id):
        if not command:
            # No off_command configured: nothing to run, the relay times out
            # by itself. Complete asynchronously, like a real command.
            reactor.callLater(0, self._command_done, which, command_id, 0, '')
            return
        env = dict(os.environ,
                   RADIO_PTT_ID=command_id or '',
                   RADIO_PTT_BY=self.by or '',
                   RADIO_PTT_TIMEOUT=str(self.timeout),
                   RADIO_PTT_ROOM=self._room_uri())
        Thread(target=self._run_worker, args=(command, which, command_id, env), daemon=True).start()

    def _run_worker(self, command, which, command_id, env):
        try:
            result = subprocess.run(command, shell=True, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=COMMAND_EXECUTION_LIMIT)
            code, output = result.returncode, result.stdout.decode('utf-8', 'replace').strip()
        except subprocess.TimeoutExpired:
            code, output = None, 'killed after %ds' % COMMAND_EXECUTION_LIMIT
        except Exception as e:
            code, output = None, str(e)
        reactor.callFromThread(self._command_done, which, command_id, code, output)

    def _command_done(self, which, command_id, code, output):
        if code == 0:
            if output:
                self.log('%s_command id=%s: %s' % (which, command_id, output))
            if self.status_file is None:
                # No hardware feedback: the command result is the feedback.
                if which == 'on' and self.state == 'keying' and command_id == self.command_id:
                    self._set_present(True)
                elif which == 'on' and self.state == 'idle':
                    # Released while on_command was still running: the relay
                    # may now be keyed with nobody watching it.
                    self.log('on_command id=%s finished after release, running off_command again' % command_id)
                    self._run(self.off_command, 'off', command_id)
                elif which == 'off' and self.state == 'transmitting':
                    self._set_present(False)
            return
        self.log('%s_command id=%s failed (%s): %s' % (which, command_id, 'exit %s' % code if code is not None else 'error', output))
        if which == 'on' and self.state == 'keying' and command_id == self.command_id and not self.present:
            if self._confirm_timer is not None and self._confirm_timer.active():
                self._confirm_timer.cancel()
            self._confirm_timer = None
            self.keyed_by_us = False
            self.state = 'idle'
            self._report({'state': 'error', 'id': command_id, 'reason': 'command_failed'})
            self.command_id = self.by = None
        # A failed off_command changes nothing: the status file still tells
        # whether the radio is on air, and the relay's own timer releases it.

    # -- reporting -------------------------------------------------------------

    def _room_uri(self):
        for stream in self.streams:
            try:
                uri = stream.session.remote_identity.uri
                return '%s@%s' % (uri.user.decode() if isinstance(uri.user, bytes) else uri.user,
                                  uri.host.decode() if isinstance(uri.host, bytes) else uri.host)
            except Exception:
                continue
        return ''

    def _report(self, payload):
        for stream in list(self.streams):
            self._send(stream, payload)

    def _send(self, stream, payload):
        try:
            stream.send_message(json.dumps(payload), content_type=CONTENT_TYPE)
        except Exception as e:
            self.log('could not send state report: %s' % e)

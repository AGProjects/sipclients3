#!/usr/bin/env python3

import pyaudio
import wave
from ctypes import *
from optparse import OptionParser
from pathlib import Path
import math
import platform
import struct
import wave
import time
import os
import sys
from datetime import datetime
import functools
print = functools.partial(print, flush=True)

# remove useless alsa logging
# From alsa-lib Git 3fd4ab9be0db7c7430ebd258f2717a976381715d
# $ grep -rn snd_lib_error_handler_t
# include/error.h:59:typedef void (*snd_lib_error_handler_t)(const char *file, int line, const char *function, int err, const char *fmt, ...) /* __attribute__ ((format (printf, 5, 6))) */;
# Define our error handler type
ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)
def py_error_handler(filename, line, function, err, fmt):
  pass
  #print('messages are yummy')

try:
    asound = cdll.LoadLibrary('libasound.so')
    # Set error handler
    c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
    asound.snd_lib_error_set_handler(c_error_handler)
except OSError:
    pass

#import sounddevice as sd
#devices = sd.query_devices()
#print(devices)

f_name_directory = '%s/.sipclient/spool/playback' % Path.home()
lock_file = '%s/.sipclient/spool/playback/playback.lock' % Path.home()

class Recorder:
    chunk = 1024
    channels = 1

    @staticmethod
    def rms(frame):
        SHORT_NORMALIZE = (1.0/32768.0)
        count = len(frame) / 2
        format = "%dh" % (count)
        shorts = struct.unpack(format, frame)

        sum_squares = 0.0
        for sample in shorts:
            n = sample * SHORT_NORMALIZE
            sum_squares += n * n
        rms = math.pow(sum_squares / count, 0.5)

        return rms * 1000

    def __init__(self, target, options):
        self.p = pyaudio.PyAudio()
        info = self.p.get_host_api_info_by_index(0)
        numdevices = info.get('deviceCount')

        self.target = target
        self.timeout_length = options.timeout
        self.rate = options.rate
        self.device = options.device
        self.min_rec_time = options.min_rec_time
        self.max_rec_time = options.max_rec_time
        self.external_lock_file = options.external_lock_file
        self.external_trigger_file = options.external_trigger_file
        self.level_lock_file = options.level_lock_file
        self.level_enable_file = options.level_enable_file
        self.quiet = options.quiet

        self.started_by_file = False
        self.started_by_level = False

        devices = {}
        devices_text = []
        for i in range(0, numdevices):
            if (self.p.get_device_info_by_host_api_device_index(0, i).get('maxInputChannels')) > 0:
                devices[i] = self.p.get_device_info_by_host_api_device_index(0, i).get('name')
                if devices[i] == str(self.device):
                    self.device = i
                elif str(i) == str(self.device):
                    self.device = i

                devices_text.append("%s) %s" % (i, devices[i]))

        print('Available devices: %s' % ", ".join(devices_text))
        try:
            print('Using audio device %s) %s at sample rate %d' % (self.device, devices[self.device], self.rate))
        except KeyError:
            print('Non existent audio device')

        self.threshold = 0 if target == 'test' else options.threshold
        self.stream = self.p.open(format=pyaudio.paInt16,
                                  channels=self.channels,
                                  rate=self.rate,
                                  input=True,
                                  output=True,
                                  input_device_index=self.device,
                                  frames_per_buffer=self.chunk)

    def listen(self):
        wait_print = False
        i = 0
        lock_print = False
        external_lock_print = False
        level_lock_print = False
        level_enable_print = False

        while True:
            i = i + 1 
            input = self.stream.read(self.chunk, exception_on_overflow = False)
            rms_val = self.rms(input)
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            if os.path.exists(lock_file):
                if not lock_print:
                    print("%s - lock file %s present, listen paused" % (now, lock_file))
        
                lock_print = True
                continue
            else:
                if lock_print:
                    print("%s - lock file %s absent, listen resumed" % (now, lock_file))
                lock_print = False    

            if self.external_lock_file:
                if os.path.exists(self.external_lock_file):
                    if not external_lock_print:
                        print("%s - external lock file %s present, listen paused" % (now, self.external_lock_file))

                    external_lock_print = True
                    continue
                else:
                    if external_lock_print:
                        print("%s - external lock file %s absent, listen resumed" % (now, self.external_lock_file))
                    external_lock_print = False

            if self.level_lock_file:
                if os.path.exists(self.level_lock_file):
                    if not level_lock_print:
                        print("%s - level lock file %s present, listen paused" % (now, self.level_lock_file))

                    level_lock_print = True
                    continue
                else:
                    if level_lock_print:
                        print("%s - level lock file %s absent, listen resumed" % (now, self.level_lock_file))
                    level_lock_print = False

            
            wait_print = False

            if self.external_trigger_file:
                if os.path.exists(self.external_trigger_file):
                    if not self.started_by_file:
                        print('%s - recording by file %s' % (now, self.external_trigger_file))
                    self.started_by_file = True
            
            if self.threshold and not self.started_by_file:
                if self.level_enable_file:
                    if not os.path.exists(self.level_enable_file):
                        if not level_enable_print:
                            print("%s - level enable file %s missing, listen paused" % (now, self.level_enable_file))

                        level_enable_print = True
                        print("%s - not listening, level %3d" % (now, rms_val), end='\r')
                        continue
                    else:
                        if level_enable_print:
                            print("%s - level enable file %s present, listen resumed" % (now, self.level_enable_file))
                        level_enable_print = False
                
                if rms_val >= self.threshold:
                    if not self.started_by_level:
                        print('%s - recording by level %3d > %d' % (now, rms_val, self.threshold))
                    self.started_by_level = True
            
            if self.started_by_level or self.started_by_file:
                self.record()
            else:
                if not self.quiet:
                    if self.external_trigger_file:
                        print("%s - listening, level %3d" % (now, rms_val), end='\r')
                    else:
                        print("%s - listening, level %3d < %d" % (now, rms_val, self.threshold), end='\r')

    def record(self):
        rec = []
        current = time.time()
        end = time.time() + self.timeout_length
        start_time = current

        recording = False
        i = 0 

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print('%s - now recording...' % now)

        if self.started_by_level:        
            while current <= end:
                i = i + 1 
                data = self.stream.read(self.chunk)
                rms_val = self.rms(data)
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                if rms_val >= self.threshold: end = time.time() + self.timeout_length
                    #if not recording:
                diff = time.time() - start_time
                if not self.quiet:
                    print('%s - recording at level %3d for %.1f seconds' % (now, rms_val, diff), end='\r')
                recording = True

                current = time.time()
                rec.append(data)

                rec_time = time.time() - start_time
                if rec_time > self.max_rec_time:
                    #print("%s - maximum recording time of %d seconds reached" % (now, rec_time))
                    break
                else: 
                    rec_time = time.time() - start_time - self.timeout_length
                
                if os.path.exists(lock_file):
                    print("%s - lock file %s detected" % (now, lock_file))
                    break

            if rec_time > self.min_rec_time:
                self.write(b''.join(rec), rec_time)
            else:
                if rec_time > 0:
                    print("%s - skip too short recording of %.1f seconds" % (now, rec_time))
                else:
                    print()

        if self.started_by_file:
            while os.path.exists(self.external_trigger_file):
                i = i + 1 
                data = self.stream.read(self.chunk)
                rms_val = self.rms(data)
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                if not self.quiet:
                    print('%s - recording by file at level %3d' % (now, rms_val), end='\r')
                recording = True
                current = time.time()
                rec.append(data)

                rec_time = time.time() - start_time
                if rec_time > self.max_rec_time:
                    break

                if os.path.exists(lock_file):
                    print("%s - lock file %s detected" % (now, lock_file))
                    break

            print("%s - recorded %.1f seconds" % (now, rec_time))
            self.write(b''.join(rec), rec_time)

        self.started_by_file = False
        self.started_by_level = False

    def play(self, file):
        p = pyaudio.PyAudio()
    
        wf = wave.open(file, 'rb')
        stream = p.open(
            format = p.get_format_from_width(wf.getsampwidth()),
            channels = wf.getnchannels(),
            rate = wf.getframerate(),
            output = True
        )

        data = wf.readframes(1024)

        while data != b'':
            stream.write(data)
            data = wf.readframes(1024)
        stream.close()
        p.terminate()

    def write(self, recording, duration):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if os.path.exists(lock_file):
            print("%s - lock file %s present, skip file" % (now, lock_file))
            return

        if self.external_lock_file and os.path.exists(self.external_lock_file):
            print("%s - external lock file %s present, skip file" % (now, self.external_lock_file))
            return

        if self.level_lock_file and os.path.exists(self.level_lock_file):
            print("%s - level lock file %s present, skip file" % (now, self.level_lock_file))
            return

        n_files = len(os.listdir(f_name_directory))

        tmp_filename = os.path.join(f_name_directory, '%s.tmp' % self.target)

        wf = wave.open(tmp_filename, 'wb')
        wf.setnchannels(self.channels)
        wf.setsampwidth(self.p.get_sample_size(pyaudio.paInt16))
        wf.setframerate(self.rate)
        wf.writeframes(recording)
        wf.close()
        filename = os.path.join(f_name_directory, '%s.wav' % self.target)
        os.rename(tmp_filename, filename)
        print('%s - saved %d seconds audio to %s' % (now, duration, filename))

        if self.target == 'test':
            self.play(filename)

    def end(self):
    # Reset to default error handler
        self.p.terminate()
        asound.snd_lib_error_set_handler(None)


if __name__ == '__main__':
    description = 'This script is a voice activate recorder that saves individual recording in the folder %s that is polled by sip-session to initiate an outgoing call and playback the file. The filename is in the format user@domain.wav. Use test argument to test audio level.' % f_name_directory
    usage = '%prog [options] [user@domain]'
    parser = OptionParser(usage=usage, description=description)
    parser.print_usage = parser.print_help
    os_type = platform.system()
    default_device = 'pulse' if os_type == 'Linux' else '1'
    
    parser.add_option('-r', '--sample_rate', type='int', default='16000', dest='rate', help='Audio sample rate')
    parser.add_option('-d', '--device', type='string', default=default_device, dest='device', help='Use selected input audio device')
    parser.add_option('-T', '--timeout', type='int', default=2, dest='timeout', help='Silence timeout to stop recording')
    parser.add_option('-m', '--min_rec_time', type='int', default=1, dest='min_rec_time', help='Minimum recording time to save recording')
    parser.add_option('-M', '--max_rec_time', type='int', default=5, dest='max_rec_time', help='Maximum recording time for each file')
    parser.add_option('-t', '--threshold', type='int', default=10, dest='threshold', help='Minimum signal level to start recording')
    parser.add_option('-l', '--level_lock_file', type='string', dest='level_lock_file', help='Skip level recording if file exists')
    parser.add_option('-L', '--level_enable_file', type='string', dest='level_enable_file', help='Enable level recording only if file exists')
    parser.add_option('-e', '--external_lock_file', type='string', dest='external_lock_file', help='Skip recording if file exists')
    parser.add_option('-E', '--external_trigger_file', type='string', dest='external_trigger_file', help='Start recording if file exists, regardless of level')
    parser.add_option('-q', '--quiet', action='store_true', dest='quiet', default=False, help='Minimize logging.')
    
    options, args = parser.parse_args()
    try:
        target = args[0]
    except IndexError:
        parser.print_help()
        sys.exit(1)
    
    try:
        a = Recorder(args[0], options)
        a.listen()
    except KeyboardInterrupt:
        a.end()
        print()
        sys.exit(0)
    except OSError as e:
        print("Error: %s" % str(e))
        sys.exit(0)


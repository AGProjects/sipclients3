#!/usr/bin/env python3
import pyaudio
from pathlib import Path
import math
import struct
import wave
import time
import os
import sys

if len(sys.argv) != 2:
    print("Please run with one argument: %s user@domain" % sys.argv[0])
    exit()

sip_uri = sys.argv[1]

Threshold = 10

SHORT_NORMALIZE = (1.0/32768.0)
chunk = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 11025
swidth = 2

TIMEOUT_LENGTH = 2

f_name_directory = '%s/.sipclient/spool/playback' % Path.home()

class Recorder:

    @staticmethod
    def rms(frame):
        count = len(frame) / swidth
        format = "%dh" % (count)
        shorts = struct.unpack(format, frame)

        sum_squares = 0.0
        for sample in shorts:
            n = sample * SHORT_NORMALIZE
            sum_squares += n * n
        rms = math.pow(sum_squares / count, 0.5)

        return rms * 1000

    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.stream = self.p.open(format=FORMAT,
                                  channels=CHANNELS,
                                  rate=RATE,
                                  input=True,
                                  output=True,
                                  frames_per_buffer=chunk)

    def record(self):
        rec = []
        current = time.time()
        end = time.time() + TIMEOUT_LENGTH

        recording = False
        i = 0 
        while current <= end:
            i = i + 1 
            data = self.stream.read(chunk)
            rms_val = self.rms(data)
            if rms_val >= Threshold: end = time.time() + TIMEOUT_LENGTH
            if not recording:
                print('Recording at level %d...' % rms_val)
            recording = True
            current = time.time()
            rec.append(data)
        self.write(b''.join(rec))

    def write(self, recording):
        n_files = len(os.listdir(f_name_directory))

        tmp_filename = os.path.join(f_name_directory, 'tmp1.wav')

        wf = wave.open(tmp_filename, 'wb')
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(self.p.get_sample_size(FORMAT))
        wf.setframerate(RATE)
        wf.writeframes(recording)
        wf.close()
        filename = os.path.join(f_name_directory, '%s.wav' % sip_uri)
        os.rename(tmp_filename, filename)
        print('Saved audio file to {}'.format(filename))

    def listen(self):
        wait_print = False
        i = 0
        while True:
            i = i + 1 
            input = self.stream.read(chunk)
            rms_val = self.rms(input)
            if rms_val > Threshold:
                wait_print = False
                self.record()
            else:
                if not wait_print:
                    print('Listening...')
                wait_print = True

if __name__ == '__main__':
    try:
        a = Recorder()
        a.listen()
    except KeyboardInterrupt:
        sys.exit(0)
        

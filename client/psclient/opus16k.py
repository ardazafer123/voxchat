"""Isolated aiortc adapter for the party's 16 kHz / 40 ms Opus profile.

This changes codec registration in the current process, not installed aiortc.
The local codec tests only cover this adapter, not a real call.
"""
from fractions import Fraction

import av
from aiortc.codecs.opus import OpusEncoder, OpusDecoder
from aiortc.rtcrtpparameters import RTCRtpCodecParameters

RATE = 16000
TIME_BASE = Fraction(1, RATE)


class Opus16kEncoder(OpusEncoder):
    def __init__(self):
        self.codec = av.CodecContext.create('libopus', 'w')
        self.codec.bit_rate = 40000
        self.codec.format = 's16'
        self.codec.layout = 'mono'
        self.codec.sample_rate = RATE
        self.codec.time_base = TIME_BASE
        self.codec.options = {'application': 'lowdelay', 'frame_duration': '40'}
        self.resampler = av.AudioResampler(format='s16', layout='mono', rate=RATE, frame_size=640)
        self.first_packet_pts = None

    def pack(self, packet):
        return [bytes(packet)], int(packet.pts * packet.time_base / TIME_BASE)


class Opus16kDecoder(OpusDecoder):
    def __init__(self):
        self.codec = av.CodecContext.create('libopus', 'r')
        self.codec.format = 's16'
        self.codec.layout = 'mono'
        self.codec.sample_rate = RATE

    def decode(self, encoded_frame):
        packet = av.Packet(encoded_frame.data)
        packet.pts = encoded_frame.timestamp
        packet.time_base = TIME_BASE
        return list(self.codec.decode(packet))


_installed = False


def install():
    global _installed
    if _installed:
        return
    import aiortc.codecs as codecs
    import aiortc.rtcrtpsender as sender
    import aiortc.rtcrtpreceiver as receiver
    original_encoder, original_decoder = sender.get_encoder, receiver.get_decoder

    def is_16k(codec):
        return codec.mimeType.lower() == 'audio/opus' and codec.clockRate == RATE

    sender.get_encoder = lambda c: Opus16kEncoder() if is_16k(c) else original_encoder(c)
    receiver.get_decoder = lambda c: Opus16kDecoder() if is_16k(c) else original_decoder(c)
    codecs.CODECS['audio'].insert(0, RTCRtpCodecParameters(
        mimeType='audio/opus', clockRate=RATE, channels=2, payloadType=97,
        parameters={'minptime': 10, 'stereo': 0, 'useinbandfec': 1}))
    _installed = True

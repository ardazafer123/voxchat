"""Voice through the RTC bridge (relay), used when direct P2P fails.

The bridge acts as a server-side peer: `POST /peers` returns an offer, we answer over REST,
and a local `bridge` data channel carries the DcApi JSON (`psclient.bridge_dc`). Each remote
publisher gets its own audio m-line; `offer.mids` maps every mid to a bridge peer id. Members
publish their own `bridgeId\\npeerId` in customData4[8] (`psclient.custom_data`), which maps a peer id to an account.
"""
import asyncio
import contextlib
import json
import os
import re
import socket
import struct

from aioice import stun
from aiortc import RTCSessionDescription, rtcdtlstransport
from aiortc.rtcsctptransport import RTCSctpTransport
from cryptography.hazmat.primitives import hashes

from live.transport import send_spec
from live.voice_peer import DEBUG, VoicePeer
from psclient import bridge_dc, custom_data, rest, signaling

# The bridge offers only a sha-1 DTLS fingerprint, which aiortc does not know.
# This also adds a sha-1 line to every local SDP (P2P too), which peers ignore or accept.
rtcdtlstransport.X509_DIGEST_ALGORITHMS.setdefault('sha-1', hashes.SHA1())
if 'GOOG-NETWORK-INFO' not in stun.ATTRIBUTES_BY_NAME:
    _entry = (0xC057, 'GOOG-NETWORK-INFO', stun.pack_bytes, stun.unpack_bytes)
    stun.ATTRIBUTES_BY_TYPE[0xC057] = stun.ATTRIBUTES_BY_NAME['GOOG-NETWORK-INFO'] = _entry

TITLE_ID = None  # shortcut: value unknown; the header is omitted when empty. Find it if the bridge ever refuses.


def member_bridge_peer(member, bridge_id):
    """The bridge peer id a member published in customData4[8], if it is on this bridge."""
    fields = custom_data.metadata_fields(member.get('customData4'))
    info = fields[custom_data.BRIDGE_INDEX] if len(fields) > custom_data.BRIDGE_INDEX else ''
    found = custom_data.parse_bridge_info(info) if isinstance(info, str) else None
    return found[1] if found and found[0] == bridge_id else None


def fix_offer_sdp(sdp):
    """Bridge `c=` lines put the address before the type (`c=IN <addr> IP4`)."""
    return re.sub(r'^c=IN ([^ \r\n]+) (IP[46])(?=\r?$)', r'c=IN \2 \1', sdp, flags=re.M)


async def resolve_candidates(sdp):
    """One bridge candidate is a hostname (`…-rtcb.np.communication.playstation.net`),
    which aioice drops. Replace it with its IPv4 address."""
    loop = asyncio.get_running_loop()
    names = set(re.findall(r'^a=candidate:\S+ \d+ \S+ \d+ ([^ \r\n]*[a-zA-Z][^ \r\n]*) \d+ typ', sdp, flags=re.M))
    for name in names:
        try:
            infos = await loop.getaddrinfo(name, None, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        except OSError:
            continue
        sdp = sdp.replace(f' {name} ', f' {infos[0][4][0]} ')
    return sdp


def add_network_info(conn):
    """The bridge (ice4j) answers a connectivity check only if it
    carries GOOG-NETWORK-INFO (0xC057), as libwebrtc sends. Without it ICE never completes."""
    build = conn.build_request

    def build_request(pair, nominate):
        request = build(pair, nominate)
        request.attributes['GOOG-NETWORK-INFO'] = struct.pack('!HH', 1, 10)  # network id, cost (libwebrtc LAN)
        return request

    conn.build_request = build_request
    # Live: ice4j never answers a check that reached it before it had checked us, and aioice
    # retransmits keep that transaction id, so the pair hangs. Wait for the bridge's first check.
    heard = asyncio.Event()
    received, check_start = conn.request_received, conn.check_start

    def request_received(message, addr, protocol, raw_data):
        heard.set()
        received(message, addr, protocol, raw_data)

    async def delayed_check_start(pair):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(heard.wait(), 5)
        await check_start(pair)

    conn.request_received = request_received
    conn.check_start = delayed_check_start


# The bridge ignores a ClientHello with only aiortc's four ECDSA suites (its certificate
# is likely RSA) and answers once RSA suites are offered. SECLEVEL=0 in case it signs with SHA-1.
BRIDGE_CIPHERS = (b'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES128-SHA:'
                  b'ECDHE-RSA-AES128-SHA:ECDHE-RSA-AES256-SHA:AES128-GCM-SHA256:AES128-SHA:AES256-SHA:@SECLEVEL=0')


def allow_rsa_suites(pc):
    """Widen the DTLS cipher list for this peer connection only (P2P keeps aiortc's defaults)."""
    for cert in pc._RTCPeerConnection__certificates:
        make = cert._create_ssl_context

        def create(srtp_profiles, make=make):
            ctx = make(srtp_profiles=srtp_profiles)
            ctx.set_cipher_list(BRIDGE_CIPHERS)
            return ctx

        cert._create_ssl_context = create


class BridgeSctp(RTCSctpTransport):
    """The bridge never sends an SCTP INIT, and aiortc only sends one when ICE-controlling
    (the bridge is). Open the association ourselves and, as the DTLS client, use even stream ids (RFC 8832)."""

    @property
    def is_server(self):
        return False

    async def start(self, remoteCaps, remotePort):
        await super().start(remoteCaps, remotePort)
        self._data_channel_id = 0


def send_peer(peer, mids, own_peer_id):
    """Pick the audio transceiver we publish on: our own mid, else the line the bridge only receives on."""
    audio = [t for t in peer.pc.getTransceivers() if t.kind == 'audio']
    return (next((t for t in audio if mids.get(t.mid) == own_peer_id), None)
            or next((t for t in audio if t._offerDirection == 'sendonly'), None)
            or (audio[0] if len(audio) == 1 else None))


def user_table(seq, bridge_peer_id, device_id, account_id):
    """Shape of the mobile app's `user_table_updated` on `<peerId>_0` (raw JSON, no framing).
    `peer_id` is the member's deviceUniqueId; platform 100001 is the mobile app (signaling.MOBILE)."""
    user = {'account_id': account_id, 'platform': signaling.MOBILE}
    return json.dumps({'global_seq_num': seq, 'peer': {
        'bridge_peer_id': bridge_peer_id,
        'medias': [{'media_records': [{'media_identifier': 'voice_chat', 'mid': ''}], 'msids': [''], 'user': user}],
        'miranda_signaling_version': 2, 'peer_id': device_id, 'users': [user]}, 'type': 'user_table_updated'},
        separators=(',', ':'))


class BridgeLink:
    def __init__(self, token, sid, bridge_id, bridge_token, source, output, relay, log, account_for_peer,
                 ice_servers=None, local_user=None):
        self.token, self.sid, self.bridge_id, self.bridge_token = token, sid, bridge_id, bridge_token
        self.log, self.account_for_peer = log, account_for_peer
        self.peer_id = None
        self.mids = {}
        self.upstream = set()
        self.source, self.relay = source, relay
        self.remote = set()
        self.ids = bridge_dc.RequestIds()
        self.channel = None
        self.peer_channels = {}  # label -> our side of a member's per-peer control channel
        self.heartbeat = None
        self.local_user = local_user  # (account id, deviceUniqueId) for user_table_updated
        self.table_seq = 0
        self.answering = asyncio.Lock()
        self.peer = VoicePeer(source, output, relay, on_state=log, send=False, ice_servers=ice_servers,
                              name=lambda mid: account_for_peer(self.mids.get(mid)) or f'bridge-{mid}')
        allow_rsa_suites(self.peer.pc)

    def connected(self):
        return self.peer.pc.connectionState == 'connected'

    def accounts(self):
        return {a for a in map(self.account_for_peer, self.remote) if a}

    async def _send(self, spec, what):
        result = await asyncio.to_thread(send_spec, spec, self.token)
        if not 200 <= result.status < 300:
            raise RuntimeError(f'Bridge {what} failed: HTTP {result.status}')
        return result.body if isinstance(result.body, dict) else {}

    async def join(self, known_peers):
        body = await self._send(rest.create_peer(self.bridge_id, self.bridge_token, TITLE_ID), 'peer create')
        self.peer_id, offer = rest.parse_peer_response(body)
        self.peer_id = str(self.peer_id)
        if offer is None:
            body = await self._send(rest.request_offer(self.bridge_id, self.peer_id, self.bridge_token, TITLE_ID), 'offer')
            offer = body.get('offer') or body.get('peer', {}).get('offer')
        # No event announces peers that joined before us; the bridge GET lists them.
        body = await self._send(rest.get_bridge(self.bridge_id, self.bridge_token, TITLE_ID), 'peer list')
        self.remote = {str(p) for p in known_peers} | set(rest.bridge_peer_ids(body))
        self.remote.discard(self.peer_id)
        self.log('Joined the relay; answering its offer.')
        # Members find each other's bridge peer through customData4[8]; the phone
        # does not route its audio to us until we publish ours.
        await self._send(rest.publish_bridge_peer(self.sid, 'me', self.bridge_id, self.peer_id), 'bridge info')
        await self.answer(offer, first=True)

    async def answer(self, offer, first=False):
        async with self.answering:
            self.mids = {str(m['mid']): str(m['peerId']) for m in offer.get('mids', [])}
            # Live: the first offer has only our own upstream lines (CELT 24 and 40), each marked `upstream`.
            self.upstream = {str(m['mid']) for m in offer.get('mids', []) if m.get('upstream')}
            self.remote |= {p for mid, p in self.mids.items() if mid not in self.upstream} - {self.peer_id}
            pc = self.peer.pc
            if DEBUG:
                # Local only: the offer holds relay addresses, no secrets.
                with open(os.path.join(os.environ.get('PARTY_VOICE_DUMP', '.'), 'bridge_offer.json'), 'w') as f:
                    json.dump(offer, f)
            await pc.setRemoteDescription(RTCSessionDescription(sdp=await resolve_candidates(fix_offer_sdp(offer['sdp'])), type='offer'))
            if first:
                for t in {id(t.sender.transport): t.sender.transport for t in pc.getTransceivers()}.values():
                    add_network_info(t.transport._connection)
            if DEBUG:
                self.log('BRIDGE offer lines: ' + ', '.join(
                    f'{t.kind}:{t.mid}:{t._offerDirection}' for t in pc.getTransceivers()))
            audio = [t for t in pc.getTransceivers() if t.kind == 'audio']
            targets = [t for t in audio if t.mid in self.upstream] or [send_peer(self.peer, self.mids, self.peer_id)]
            targets = [t for t in targets if t is not None]
            if not targets:
                self.log('Relay offer has no line to send on; listening only.')
            for i, target in enumerate(targets):
                if target.sender.track is None:
                    target.sender.replaceTrack(self.peer.mic if i == 0 else self.relay.subscribe(self.source))
                    target.direction = 'sendrecv'
            if first:
                # The answering side creates the `bridge` channel itself.
                self.channel = pc.createDataChannel(bridge_dc.LABEL, ordered=True)
                pc.sctp.__class__ = BridgeSctp
                self.channel.on('open', self._opened)
                self.channel.on('message', self._message)
            await pc.setLocalDescription(await pc.createAnswer())
            await self._send(rest.send_answer(self.bridge_id, self.peer_id, self.bridge_token, TITLE_ID,
                                              self.peer._local_sdp()), 'answer')

    def _request(self, text):
        if self.channel is not None and self.channel.readyState == 'open':
            self.channel.send(text)

    def _subscribe(self):
        self._request(bridge_dc.set_media_pub_sub_peers(self.ids.next(), sorted(self.remote)))

    def _opened(self):
        # Order: floor control, receiving capability, then pub/sub of every other peer.
        self.log('Relay channel open; subscribing to the other members.')
        self._request(bridge_dc.set_floor_control_parameters(self.ids.next()))
        self._request(bridge_dc.set_receiving_capability(self.ids.next()))
        self._subscribe()

    def _message(self, data):
        try:
            frame = bridge_dc.parse_frame(data if isinstance(data, str) else data.decode())
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            self.log('Ignored an unknown relay message.')
            return
        if isinstance(frame, bridge_dc.ErrorResponse):
            self.log(f'Relay refused request {frame.request_id}: {frame.code}')
        elif isinstance(frame, bridge_dc.Event):
            if DEBUG:
                channel = frame.payload.get('peer', {}).get('dataChannel') or {}
                self.log(f'BRIDGE event {frame.type} keys={sorted(frame.payload)} peer={frame.peer_id} '
                         f'channel={ {k: channel.get(k) for k in ("sid", "label", "channelType")} } '
                         f'others={len(frame.payload.get("otherPeers") or [])}')
            peer = frame.peer_id and str(frame.peer_id)
            if frame.type == 'peerCreated' and peer and peer != self.peer_id:
                self.remote.add(peer)
                self._subscribe()
            elif frame.type == 'peerDeleted' and peer in self.remote:
                self.remote.discard(peer)
                self._subscribe()
            elif frame.type == 'dataChannelCreated' and peer and peer != self.peer_id:
                self._pair_channel(peer, frame.payload['peer'].get('dataChannel') or {})
            elif frame.type == 'dataChannelDeleted' and peer:
                for label in [l for l in self.peer_channels if l.rpartition('_')[0] == peer]:
                    self.peer_channels.pop(label).close()
            elif frame.type == 'offerUpdated':
                task = asyncio.ensure_future(self._renegotiate(frame.payload))
                task.add_done_callback(lambda t: t.cancelled() or not t.exception()
                                       or self.log(f'Relay renegotiation failed: {t.exception()}'))

    def _pair_channel(self, peer, info):
        """A member opens `<ourPeerId>_<n>` and shows us as connecting (and retries
        `start_reconnection`) until we open the opposite `<theirPeerId>_<n>`."""
        prefix, _, index = str(info.get('label', '')).rpartition('_')
        label = f'{peer}_{index}'
        if prefix != self.peer_id or not index.isdigit() or label in self.peer_channels:
            return
        kind, param = int(info.get('channelType') or 0), info.get('reliabilityParameter')
        options = {'ordered': not kind & 0x80} if kind else {'ordered': True}
        if kind & 0x02:
            options['maxPacketLifeTime'] = param
        elif kind & 0x01:
            options['maxRetransmits'] = param
        channel = self.peer.pc.createDataChannel(label, **options)
        self.peer_channels[label] = channel
        seen = set()

        @channel.on('message')
        def received(data):
            if DEBUG and data not in seen and len(seen) < 16:
                seen.add(data)
                self.log(f'BRIDGE peer channel {index} {len(data)} bytes: {data[:300]!r}')
                # Local only: holds account and peer ids.
                with open(os.path.join(os.environ.get('PARTY_VOICE_DUMP', '.'), 'bridge_peer_channel.log'), 'a') as f:
                    f.write(f'{index} {data!r}\n')

        def opened():
            self.log(f'Relay peer channel {index} open.')
            if index == '0':
                self._send_user_table(channel)

        channel.on('open', opened)
        if self.heartbeat is None:
            self.heartbeat = asyncio.ensure_future(self._hello())
        if DEBUG:
            self.log(f'BRIDGE pairing channel {index} type={kind} param={param} options={options}')

    def _send_user_table(self, channel):
        if self.local_user and channel.readyState == 'open':
            self.table_seq += 1
            channel.send(user_table(self.table_seq, self.peer_id, self.local_user[1], self.local_user[0]))

    async def _hello(self):
        # A 5-byte `hello` heartbeat on per-peer control channels.
        # shortcut: 1 s interval is a guess; match the mobile app's cadence if it turns out different.
        tick = 0
        while True:
            for channel in list(self.peer_channels.values()):
                if channel.readyState == 'open':
                    channel.send('hello')
            # shortcut: 5 s user-table cadence is a guess (the app also sends on changes).
            if tick % 5 == 0:
                for label, channel in list(self.peer_channels.items()):
                    if label.endswith('_0'):
                        self._send_user_table(channel)
            tick += 1
            await asyncio.sleep(1)

    async def _renegotiate(self, payload):
        # The event normally carries the new offer; without one, request it over REST.
        offer = payload.get('offer') or payload.get('peer', {}).get('offer')
        if not offer:
            body = await self._send(rest.request_offer(self.bridge_id, self.peer_id, self.bridge_token, TITLE_ID), 'offer')
            offer = body.get('offer') or body.get('peer', {}).get('offer')
        if offer:
            await self.answer(offer)

    async def close(self):
        if self.heartbeat:
            self.heartbeat.cancel()
        if self.peer_id:
            result = await asyncio.to_thread(send_spec, rest.delete_peer(self.bridge_id, self.peer_id, TITLE_ID), self.token)
            self.log(f'Relay peer DELETE -> HTTP {result.status}')
        await self.peer.close()

"""Party voice from the command line. Ctrl+C leaves the party.

Uses the saved sign-in, discovers invitations, answers mobile offers and sends
offers to consoles.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import queue
import signal
import threading
import time

from aiortc import RTCIceServer
from aiortc.contrib.media import MediaRelay

from live.auth import CONFIG_DIR, is_acknowledged, sign_in
from live.bridge import BridgeLink, member_bridge_peer
from live.push_transport import PushConnection
from live.transport import get, send_spec
from live.voice_peer import DEBUG, VoicePeer
from live.whoami import account_id_from_token
from psclient import custom_data, rest, signaling
from psclient.audio import AudioInput, AudioOutput
from psclient import push as push_protocol


def initial_metadata():
    return custom_data.metadata()


def hosted_path(account):
    return os.path.join(CONFIG_DIR, f'hosted-{account}')


def hosted_ids(account):
    """Parties this account created here. The server lists only received invitations, so a party the
    desktop started and then left would otherwise vanish from the list while others are still in it."""
    try:
        with open(hosted_path(account)) as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


def save_hosted(account, ids):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd = os.open(hosted_path(account), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(''.join(i + '\n' for i in ids[-10:]))


async def discover(token):
    response = await asyncio.to_thread(get, 'sessionManager', '/v2/users/me/partySessionsInvitations', token)
    if response.status != 200 or not isinstance(response.body, dict):
        raise RuntimeError(f'Invitation discovery failed: HTTP {response.status}')
    sessions = set()

    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get('sessionId'), str):
                sessions.add(value['sessionId'])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(response.body.get('invitations', []))
    account = account_id_from_token(token)
    hosted = hosted_ids(account)
    parties, gone = [], set()
    for sid in sorted(sessions | set(hosted)):
        response = await asyncio.to_thread(send_spec, rest.get_party(sid), token)
        try:
            parties.append(session_from_response(response, sid))
        except RuntimeError:
            gone.add(sid)  # Expired invitation or ended party, no longer joinable.
    if gone & set(hosted):
        save_hosted(account, [i for i in hosted if i not in gone])
    return parties


class PartyVoice:
    def __init__(self, token, account, session, source, output, log=print):
        self.token, self.account, self.session = token, account, session
        self.sid = session['sessionId']
        self.source, self.output, self.log = source, output, log
        self.relay = MediaRelay()
        self.decoder = signaling.Decoder()
        self.peers = {}
        self.sequences = {}
        self.sequence = 0
        self.remote_ice = {}
        self.pending_offers = {}
        self.bridge = None
        self.bridge_failed = False
        self.fallback = False
        self.members = {}
        self._update_members(session)

    def _update_members(self, session):
        members = {str(m['accountId']): m for m in session.get('members', [])}
        if DEBUG:
            for account, member in members.items():
                old = self.members.get(account, {})
                changed = sorted(k for k in set(old) | set(member) if k != 'pushContexts' and old.get(k) != member.get(k))
                if old and changed:
                    self.log(f"MEMBER {member.get('onlineId', '?')} changed: " + ', '.join(
                        f'{k}={member.get(k)!r}' if isinstance(member.get(k), bool) else k for k in changed))
        self.members = members

    async def refresh(self):
        response = await asyncio.to_thread(send_spec, rest.get_party(self.sid), self.token)
        session = session_from_response(response, self.sid)
        self._update_members(session)
        await self.join_bridge(session)
        for account in list(self.peers):
            if account not in self.members:
                await self.peers.pop(account).close()
                self.pending_offers.pop(account, None)
                self.remote_ice.pop(account, None)
                self.sequences.pop(account, None)

    def bridge_peer_account(self, peer_id):
        bid = self.bridge and self.bridge.bridge_id
        return next((a for a, m in self.members.items() if peer_id and member_bridge_peer(m, bid) == peer_id), None)

    async def join_bridge(self, session):
        # The member whose P2P failed creates the bridge and sends a
        # `bridge_fallback` topology message; the others must join the same bridge (no reply exists).
        found = rest.bridge_from_session(session)
        if found is None or self.bridge is not None or self.bridge_failed:
            return
        bid, btoken = found
        waiting = {a: member_bridge_peer(m, bid) for a, m in self.members.items() if a != self.account}
        waiting = {a: p for a, p in waiting.items() if p and peer_state(self.peers.get(a)) != 'connected'}
        if not waiting and not self.fallback:
            return
        self.log('A member moved to the relay; joining it.')
        me = self.members.get(self.account, {})
        self.bridge = BridgeLink(self.token, self.sid, bid, btoken, self.source, self.output, self.relay,
                                 self.log, self.bridge_peer_account,
                                 local_user=(self.account, me.get('deviceUniqueId')) if me.get('deviceUniqueId') else None)
        try:
            await self.bridge.join(waiting.values())
        except Exception as exc:  # noqa: BLE001 - one attempt per party; report and keep P2P
            self.log(f'Relay join failed: {type(exc).__name__}: {exc}')
            if DEBUG:
                import traceback
                self.log(traceback.format_exc())
            self.bridge_failed = True
            bridge, self.bridge = self.bridge, None
            await bridge.close()

    def state(self, account):
        if self.bridge is not None and self.bridge.connected() and account in self.bridge.accounts():
            return 'connected'
        return peer_state(self.peers.get(account))

    async def start_console_offers(self):
        # Console flow: the desktop sends the offer to consoles. Keep the
        # previously verified mobile answer path unchanged.
        for account, member in list(self.members.items()):
            if account == self.account or member.get('platform') != 'PS5' or account in self.peers:
                continue
            if member.get('voiceChatActivated') is False:
                continue
            stun = await asyncio.to_thread(VoicePeer.stun_server)
            self.log('STUN: ' + ('network' if stun == VoicePeer.NETWORK_STUN else 'Google (network STUN did not answer)'))
            peer = VoicePeer(self.source, self.output, self.relay, on_state=self.log, name=account,
                             ice_servers=[RTCIceServer(stun)])
            self.peers[account] = peer
            self.log('Starting a voice offer to the console; gathering local ICE candidates.')
            sdp = await asyncio.wait_for(peer.offer(), 30)
            sequence = self.sequences.get(account, 0) + 1
            self.pending_offers[account] = sequence
            await self.send_description(account, signaling.identity(account, 5), sdp, 'offer', sequence)
            self.log('Fresh WebRTC offer delivered: HTTP 204. Waiting for the console to answer.')

    async def send_description(self, account, recipient, sdp, kind, sequence):
        self.sequence += 1
        peer = self.peers[account]
        audio = next(t for t in peer.pc.getTransceivers() if t.kind == 'audio')
        message = signaling.description(signaling.identity(self.account), recipient, sdp,
                                        kind, sequence, self.sequence, mid=audio.mid)
        for payload in signaling.payloads(message):
            sent = await asyncio.to_thread(send_spec, rest.custom_message(
                self.sid, account, payload, self.members[account]['platform']), self.token)
            if sent.status != 204:
                raise RuntimeError(f'{kind.title()} signaling failed: HTTP {sent.status}')

    async def process(self, event):
        data = event.get('body', {}).get('data', {})
        if data.get('sessionId') != self.sid:
            return
        if DEBUG:
            self.log('PUSH ' + event.get('dataType', '?') + (' (custom message)' if data.get('customMessage') else ''))
        custom = data.get('customMessage')
        if not custom:
            # Member property topics (`ps:m:voiceChatActivated:updated`, …) lack ':members:'.
            topic = event.get('dataType', '')
            if ':members:' in topic or ':bridges:' in topic or ':ps:m:' in topic:
                await self.refresh()
                await self.start_console_offers()
            return
        if custom.get('channel') != 'miranda:12':
            return
        try:
            decoded = self.decoder.feed(custom.get('payload'))
        except (ValueError, KeyError, TypeError):
            # Session messaging also carries control packet types we do not use.
            # Ignore unsupported/malformed messages without dropping live media.
            if topology_type(custom.get('payload')) == 'bridge_fallback':
                self.log('A member switched to the relay.')
                self.fallback = True
                await self.refresh()
                return
            self.log('Ignored an unsupported signaling/control packet.')
            if DEBUG:
                with contextlib.suppress(Exception):
                    raw = base64.b64decode((custom.get('payload') or '').removeprefix(signaling.PREFIX))
                    head = signaling.HEADER.unpack_from(raw) if len(raw) >= signaling.HEADER.size else ()
                    self.log(f'CONTROL header={head} body={raw[signaling.HEADER.size:][:96]!r}')
            return
        if decoded is None:
            return
        kind, message = decoded
        sender = message['from']
        account = sender['account_id']
        attributed_sender = data.get('customProperties', {}).get('from', {})
        if str(attributed_sender.get('accountId')) != account:
            self.log('Ignored a signaling message with inconsistent sender attribution.')
            return
        if account == self.account or message.get('to') != signaling.identity(self.account):
            return
        if account not in self.members:
            await self.refresh()
        if account not in self.members:
            return
        if kind != 1 or message.get('type') not in ('offer', 'answer'):
            self.log(f"Signaling received: type={message.get('type', 'unknown')} (not handled)")
            return
        try:
            sequence = int(message['sdp_sequence_number'])
        except (ValueError, KeyError, TypeError):
            self.log('Ignored an offer with an invalid sequence number.')
            return
        if message['type'] == 'answer':
            peer = self.peers.get(account)
            if self.pending_offers.get(account) != sequence or peer is None:
                self.log('Ignored an answer without a matching pending offer.')
                return
            if sender['platform'] != 5 or self.members[account].get('platform') != 'PS5':
                self.log('Ignored an answer with inconsistent peer platform.')
                return
            await asyncio.wait_for(peer.accept_answer(message['sdp']), 30)
            self.pending_offers.pop(account, None)
            self.sequences[account] = sequence
            self.log('Console answer accepted; establishing voice transport.')
            return
        if sequence <= self.sequences.get(account, -1):
            return
        platform = self.members[account].get('platform')
        if platform not in ('MOBILE_APP', 'PS5', 'PS4'):
            raise RuntimeError('Unsupported peer platform')
        peer = self.peers.get(account)
        if account in self.pending_offers:
            # A remote offer raced ours. Replace the pending transport rather
            # than attempting unsupported aiortc rollback.
            await peer.close()
            peer = None
            self.pending_offers.pop(account, None)
        # aiortc1.14 starts ICE once per transport. A remote restart needs
        # fresh transports; applying new credentials to the old PC isn't enough.
        from aiortc.sdp import SessionDescription
        parsed_offer = SessionDescription.parse(message['sdp'])
        credentials = tuple((m.ice.usernameFragment, m.ice.password) for m in parsed_offer.media)
        if peer is not None and (credentials != self.remote_ice.get(account)
                                 or peer.pc.connectionState in ('failed', 'closed')):
            self.log('Peer restarted ICE; replacing the old transport.')
            await peer.close()
            peer = None
        if peer is None:
            peer = VoicePeer(self.source, self.output, self.relay, on_state=self.log, name=account)
            self.peers[account] = peer
        self.remote_ice[account] = credentials
        self.log('Incoming offer; gathering local ICE candidates.')
        answer = await asyncio.wait_for(peer.answer(message['sdp']), 30)
        await self.send_description(account, sender, answer, 'answer', sequence)
        self.sequences[account] = sequence
        self.log('Fresh WebRTC answer delivered: HTTP 204.')

    async def close(self):
        closing = [peer.close() for peer in self.peers.values()] + ([self.bridge.close()] if self.bridge else [])
        await asyncio.gather(*closing, return_exceptions=True)
        self.source.stop()
        self.output.close()


def topology_type(payload):
    """`type` of a JSON topology message (Division `3/1/1\\n{...}` after the envelope header), else None."""
    try:
        raw = base64.b64decode(payload.removeprefix(signaling.PREFIX), validate=True)[signaling.HEADER.size:]
        kind, _, body = raw.partition(b'\n')
        return json.loads(body).get('type') if kind.startswith(b'3/') else None
    except (ValueError, AttributeError, TypeError):
        return None


def peer_state(peer):
    if peer is None or peer.pc.connectionState in ('new', 'connecting'):
        return 'connecting'
    return 'connected' if peer.pc.connectionState == 'connected' else 'disconnected'


# Mobile app identity headers; party creation expects them.
APP_HEADERS = {"X-PSN-APP-TYPE": "MOBILE_APP.PSAPP", "X-PSN-APP-VER": "26.9.2"}


def session_from_response(response, session_id):
    sessions = response.body.get("v2PartySessions", []) if isinstance(response.body, dict) else []
    if response.status != 200 or not isinstance(sessions, list):
        raise RuntimeError(f"Party read failed: HTTP {response.status}")
    for session in sessions:
        if isinstance(session, dict) and session.get("sessionId") == session_id:
            return session
    raise RuntimeError("Requested party missing from response")


def created_session_id(body):
    """The new party's id from a create response, else None."""
    if not isinstance(body, dict):
        return None
    sessions = body.get("v2PartySessions")
    if isinstance(sessions, list) and sessions and isinstance(sessions[0], dict):
        value = sessions[0].get("sessionId")
        return value if isinstance(value, str) and value else None
    return None

def mic_muted(member):
    """True when a member has muted their microphone or turned voice chat off.

    The mobile app's mute button changes only `customData4`, a base64 JSON
    list of strings, e.g. ["","AQAAAA==","1","0","6.2.0","0","0","0","",""]. Index 5 is "1" while muted.
    """
    if member.get('voiceChatActivated') is False:
        return True
    try:
        fields = json.loads(base64.b64decode(member.get('customData4') or ''))
        return isinstance(fields, list) and len(fields) > 5 and fields[5] == '1'
    except (ValueError, TypeError):
        return False


def account_allowed(account):
    """Only accounts whose user confirmed the ban-risk warning."""
    return is_acknowledged(account)


class SessionControl:
    """Thread-safe controls shared by CLI/desktop window and the async call."""
    def __init__(self, muted=False):
        self.stop = threading.Event()
        self.muted = threading.Event()
        if muted:
            self.muted.set()
        # Read-only snapshots for the UI, replaced whole by the call loop.
        self.members = []
        self.accounts = {}        # accountId -> onlineId of the members
        self.levels = {}
        self.mic_level = 0.0
        self.states = {}          # onlineId -> 'connecting' | 'connected' | 'disconnected'
        self.remote_muted = set()  # onlineIds that turned their voice chat off
        self.silenced = set()      # onlineIds the UI muted locally; replaced whole by the UI
        self.invites = queue.SimpleQueue()  # {'account_id', 'online_id'} from the UI thread


async def run(args, control=None, log=print):
    control = control or SessionControl(args.muted)
    tokens = await asyncio.to_thread(sign_in)
    token = tokens.access_token
    refresh_at = time.monotonic() + max(30, tokens.expires_in - 120)
    account = account_id_from_token(token)
    if not account_allowed(account):
        raise RuntimeError('This account has not confirmed the ban-risk warning; sign in from the desktop app')
    create = getattr(args, 'create', False)
    if create:
        session = None  # created after push connects
    elif args.session:
        session = session_from_response(await asyncio.to_thread(send_spec, rest.get_party(args.session), token), args.session)
    else:
        parties = await discover(token)
        if args.owner:
            parties = [p for p in parties if p.get('owner', {}).get('onlineId') == args.owner]
        if args.list:
            for party in parties:
                print(party['sessionId'], party.get('owner', {}).get('onlineId', '?'), len(party.get('members', [])), 'members')
            if not parties:
                print('No active party invitations. Invite this account from another device.')
            return
        if len(parties) != 1:
            raise RuntimeError(f'Found {len(parties)} invited parties; use --list, then --session ID to select one')
        session = parties[0]
    if session and args.owner and session.get('owner', {}).get('onlineId') != args.owner:
        raise RuntimeError('Party owner does not match the selected peer')
    if session and any(str(m.get('accountId')) == account for m in session.get('members', [])):
        raise RuntimeError('Already a member; leave the party on the other device first')
    # A stored bridge is only available topology, not proof it is selected.
    source = AudioInput(device=args.input_device, mode='silence' if args.silent else 'microphone')
    source.set_muted(control.muted.is_set())
    output = AudioOutput(device=args.output_device, enabled=not args.silent)
    voice = None
    sid = session['sessionId'] if session else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
    joined = False
    uncertain_join = False
    try:
        source.start()
        output.start()
        async with PushConnection(token, account) as push:
            context = rest.new_context_id()
            member = {'accountId': 'me', 'platform': 'me', 'voiceChatActivated': True,
                      'pushContexts': [{'contextId': context}], 'customData4': initial_metadata()}
            uncertain_join = True
            if create:
                # Create body: maxMembers 16, empty sessionName, member with customData4.
                result = await asyncio.to_thread(send_spec, rest.create_party(
                    context, member, max_members=16, session_name=''), token, extra_headers=APP_HEADERS)
                sid = created_session_id(result.body) if 200 <= result.status < 300 else None
                if 200 <= result.status < 300 and not sid:
                    raise RuntimeError('Party created but its id was unreadable; leave it from your console or phone')
            else:
                result = await asyncio.to_thread(send_spec, rest.join_party(sid, member), token)
            uncertain_join = False
            if not 200 <= result.status < 300:
                raise RuntimeError(f"Party {'create' if create else 'join'} failed: HTTP {result.status}")
            joined = True
            if create:
                save_hosted(account, hosted_ids(account) + [sid])
                session = session_from_response(await asyncio.to_thread(send_spec, rest.get_party(sid), token), sid)
            voice = PartyVoice(token, account, session, source, output, log=log)
            log(f"{'Created' if create else 'Joined'} party {sid}. Starting voice negotiation.")
            await voice.refresh()
            await voice.start_console_offers()
            started = last_stats = time.monotonic()
            gaps = {}
            while not stop.is_set() and not control.stop.is_set() and (not args.seconds or time.monotonic() - started < args.seconds):
                source.set_muted(control.muted.is_set())
                if account not in voice.members:
                    # PSN allows one party per account: a party started elsewhere with this
                    # account (another app or device) silently removes us from this one.
                    joined = False  # nothing left to leave
                    raise RuntimeError('This account was removed from the party, maybe because it joined '
                                       'or started another party somewhere else')
                names = {a: m.get('onlineId') or 'Player' for a, m in voice.members.items()}
                control.members = list(names.values())
                control.accounts = names
                control.levels = {names[a]: v for a, v in output.levels.items() if a in names}
                control.states = {names[a]: voice.state(a) for a in names if a != account}
                control.remote_muted = {names[a] for a, m in voice.members.items() if a != account and mic_muted(m)}
                output.silenced = {a for a, n in names.items() if n in control.silenced}
                if DEBUG:
                    now = time.monotonic()
                    for a, seen in list(output.last_frame.items()):
                        quiet = now - seen > 1
                        if quiet != gaps.get(a, False):
                            gaps[a] = quiet
                            log(f"AUDIO {names.get(a, '?')} {'stopped' if quiet else 'resumed'}")
                control.mic_level = 0.0 if control.muted.is_set() else source.level
                while True:
                    try:
                        friend = control.invites.get_nowait()
                    except queue.Empty:
                        break
                    sent = await asyncio.to_thread(send_spec, rest.invite_to_party(sid, [friend['account_id']]), token)
                    log(f"INVITE {friend['online_id']} -> HTTP {sent.status}")
                if time.monotonic() >= refresh_at:
                    renewed = await asyncio.to_thread(sign_in)
                    if account_id_from_token(renewed.access_token) != account:
                        raise RuntimeError('Account changed while renewing sign-in')
                    await push.request(lambda t: push_protocol.change_user_status('3.0', t, account, renewed.access_token))
                    token = voice.token = push.token = renewed.access_token
                    if voice.bridge:
                        voice.bridge.token = token
                    refresh_at = time.monotonic() + max(30, renewed.expires_in - 120)
                    log('Sign-in renewed.')
                try:
                    event = await push.receive(.1)
                except asyncio.TimeoutError:
                    event = None
                if event:
                    await voice.process(event)
                if time.monotonic() - last_stats >= 10:
                    states = ', '.join(p.pc.connectionState for p in voice.peers.values()) or 'waiting'
                    log(f'Voice: {states}; received audio frames={output.frames_received}; peak={output.peak}')
                    last_stats = time.monotonic()
    finally:
        try:
            if voice:
                await voice.close()
            else:
                source.stop()
                output.close()
        finally:
            try:
                if (joined or uncertain_join) and sid:
                    result = await asyncio.to_thread(send_spec, rest.leave_party(sid, 'me'), token)
                    log(f'LEAVE -> HTTP {result.status}')
                    # 404: already out (left elsewhere, or PSN moved this account to another party).
                    if result.status not in (204, 404):
                        raise RuntimeError(f'Party cleanup failed: HTTP {result.status}; session {sid}')
                elif uncertain_join:
                    log('Party create may have succeeded without a readable id; check your console or phone.')
            finally:
                for sig in installed:
                    loop.remove_signal_handler(sig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', help='Join this party instead of discovering an invitation')
    parser.add_argument('--owner', help='Filter/verify owner online ID')
    parser.add_argument('--list', action='store_true', help='List active invited parties and exit')
    parser.add_argument('--devices', action='store_true', help='List audio devices and exit')
    parser.add_argument('--input-device', type=int)
    parser.add_argument('--output-device', type=int)
    parser.add_argument('--muted', action='store_true', help='Send silence instead of microphone samples')
    parser.add_argument('--silent', action='store_true', help='No microphone or speaker access; signaling test')
    parser.add_argument('--seconds', type=float, default=0, help='Leave after this many seconds; 0 means until Ctrl+C')
    args = parser.parse_args()
    if args.devices:
        import sounddevice
        print(sounddevice.query_devices())
        return 0
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Library errors may include SDP, tokens or HTTP bodies. Report type only
        # except for our deliberately constructed status messages.
        message = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        print('Voice stopped:', message)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

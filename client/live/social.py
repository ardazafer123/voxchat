"""Messages and friends calls for the desktop client.

Every method returns plain dicts in the shapes the UI reads:

    conversation  {'id', 'name', 'members': [onlineId], 'last', 'unread'}
    message       {'id', 'sender': onlineId, 'body', 'time', 'system': bool}
    friend        {'account_id', 'online_id', 'presence': 'online'|'offline', 'status'}
    request       {'account_id', 'online_id'}
    result        {'account_id', 'online_id', 'relation': 'none'|'friend'|'requested'}

Request formats match what the mobile app sends.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from live.auth import sign_in
from live.whoami import account_id_from_token

API = 'https://m.np.playstation.com/api/'


class Social:
    def __init__(self):
        self.lock = threading.Lock()
        self.access_token = None
        self.expires = 0.0
        self.account_id = None
        self.avatar_urls = {}  # onlineId -> profile picture URL, filled by profiles()

    def token(self):
        with self.lock:
            if self.access_token is None or time.monotonic() > self.expires:
                tokens = sign_in()
                self.access_token = tokens.access_token
                self.account_id = account_id_from_token(tokens.access_token)
                # Renew a minute early; unknown lifetime means renew after 5 minutes.
                self.expires = time.monotonic() + max(60, (tokens.expires_in or 360) - 60)
            return self.access_token

    def call(self, method, path, query=None, body=None, headers=None):
        url = API + path + ('?' + urllib.parse.urlencode(query) if query else '')
        data = None if body is None else json.dumps(body).encode()
        headers = {'Authorization': 'Bearer ' + self.token(), 'Accept': 'application/json',
                   'Accept-Language': 'en-US', **(headers or {})}  # chat list rejects requests without it
        if data is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    # Helpers

    def me(self):
        self.token()
        return self.account_id

    def profiles(self, account_ids):
        """onlineId per account id; the response lists profiles in request order."""
        names = {}
        for start in range(0, len(account_ids), 100):
            ids = account_ids[start:start + 100]
            data = self.call('GET', 'userProfile/v1/internal/users/profiles', {'accountIds': ','.join(ids)})
            for account_id, profile in zip(ids, data.get('profiles', [])):
                names[account_id] = profile.get('onlineId') or account_id
                self.avatar_urls[names[account_id]] = avatar_url(profile)
        return names

    def faces(self, accounts):
        """Profile picture bytes per onlineId for {accountId: onlineId}; people without one are left out."""
        missing = [a for a, name in accounts.items() if name not in self.avatar_urls]
        if missing:
            try:
                self.profiles(missing)
            except OSError as exc:  # still show the pictures already known
                print('Profile lookup failed:', type(exc).__name__, flush=True)

        urls = {n: self.avatar_urls[n] for n in accounts.values() if self.avatar_urls.get(n)}
        images = fetch_images(urls.values())
        return {name: images[url] for name, url in urls.items() if url in images}

    # Profiles

    def profile(self, account_id):
        """Everything the profile page shows. Sections the owner keeps private come back as None."""
        me = self.me()
        jobs = {
            'profile': (f'userProfile/v1/internal/users/{account_id}/profiles', None),
            'summary': (f'trophy/v1/users/{account_id}/trophySummary', None),
            'titles': (f'trophy/v1/users/{account_id}/trophyTitles', {'limit': 100}),
            'games': (f'gamelist/v2/users/{account_id}/titles',
                      {'categories': GAME_CATEGORIES, 'limit': 100, 'offset': 0}),
            'friends': (f'userProfile/v1/internal/users/{account_id}/friends', {'limit': 1}),
        }
        if account_id != me:
            # shortcut: relation checks the first 1000 friends and sent requests; page if anyone has more.
            jobs['mine'] = (f'userProfile/v2/internal/users/{me}/friends', {'limit': 1000, 'offset': 0})
            jobs['sent'] = ('userProfile/v1/internal/users/me/friends/sentRequests', {'limit': 100})

        def get(key):
            path, query = jobs[key]
            try:
                return key, self.call('GET', path, query)
            except urllib.error.HTTPError as exc:
                if key == 'profile':
                    raise
                return key, None  # private section (403) or no data (404)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs) + 1) as pool:
            presence = pool.submit(self.presences, [account_id])
            data = dict(pool.map(get, jobs))
        raw = data['profile']
        detail = raw.get('personalDetail') or {}
        name = ' '.join(p for p in (detail.get('firstName'), detail.get('middleName'), detail.get('lastName')) if p)
        if account_id == me:
            relation = 'me'
        elif account_id in {str(f) for f in (data.get('mine') or {}).get('friends', [])}:
            relation = 'friend'
        elif account_id in {str(r.get('accountId')) for r in (data.get('sent') or {}).get('sentRequests', [])}:
            relation = 'requested'
        else:
            relation = 'none'
        summary = data['summary']
        games, titles = data['games'], data['titles']
        return {
            'account_id': account_id, 'online_id': raw.get('onlineId') or account_id, 'name': name,
            'about': raw.get('aboutMe') or '', 'avatar': avatar_url(raw), 'plus': bool(raw.get('isPlus')),
            'verified': bool(raw.get('isOfficiallyVerified')), 'languages': raw.get('languages') or [],
            'relation': relation, **presence.result().get(account_id, {'presence': 'offline', 'status': 'Offline'}),
            'trophies': summary_row(summary) if summary else None,
            'friends_count': (data['friends'] or {}).get('totalItemCount'),
            # The app lists recently played first; the API order is not by date.
            'games': sorted((game_row(g) for g in games.get('titles', [])), key=lambda g: g['last_played'] or '',
                            reverse=True) if games else None,
            'games_total': (games or {}).get('totalItemCount'),
            'titles': [title_row(t) for t in titles.get('trophyTitles', [])] if titles else None,
            'titles_total': (titles or {}).get('totalItemCount'),
        }

    def trophies(self, account_id, title):
        """One game's trophy list for one player, merged from the definitions and that player's progress."""
        base = f"trophyGroups/all/trophies"
        query = {'npServiceName': title['service']}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            defs = pool.submit(self.call, 'GET', f"trophy/v1/npCommunicationIds/{title['id']}/{base}", query)
            earned = pool.submit(self.call, 'GET',
                                 f"trophy/v1/users/{account_id}/npCommunicationIds/{title['id']}/{base}", query)
            return merge_trophies(defs.result().get('trophies', []), earned.result().get('trophies', []))

    @staticmethod
    def images(urls):
        return fetch_images(urls)

    def presences(self, account_ids):
        result = {}
        for start in range(0, len(account_ids), 100):
            data = self.call('GET', 'userProfile/v2/internal/users/basicPresences', {
                'accountIds': ','.join(account_ids[start:start + 100]), 'platforms': PLATFORMS,
                'type': 'primary', 'withOwnGameTitleInfo': 'true'})
            for entry in data.get('basicPresences', []):
                info = entry.get('primaryPlatformInfo') or {}
                titles = entry.get('gameTitleInfoList') or []
                online = info.get('onlineStatus') == 'online'
                status = ('Playing ' + titles[0]['titleName'] if online and titles and titles[0].get('titleName')
                          else 'Online on ' + PLATFORM_NAMES.get(info.get('platform'), 'console') if online
                          else 'Offline')
                result[str(entry.get('accountId'))] = {'presence': 'online' if online else 'offline', 'status': status,
                                                       'last_online': info.get('lastOnlineDate') or entry.get('lastAvailableDate')}
        return result

    # Messages

    def conversations(self):
        data = self.call('GET', 'gamingLoungeGroups/v1/members/me/groups', {
            'includeFields': GROUP_FIELDS, 'limit': 50, 'offset': 0})
        me = self.me()
        return [group_to_conversation(group, me) for group in data.get('groups', [])]

    def messages(self, conversation):
        if not conversation.get('thread'):
            return []
        thread = f"gamingLoungeGroups/v1/members/me/groups/{conversation['id']}/threads/{conversation['thread']}"
        data = self.call('GET', thread + '/messages', {'limit': 50})
        raw = data.get('messages', [])
        rows = sorted((message_row(m, conversation.get('names', {})) for m in raw), key=lambda m: m['time'])
        if rows and conversation.get('unread'):
            # Same read markers the mobile app sets when a chat is opened.
            self.call('PATCH', thread, body={'readMessageUid': rows[-1]['id']})
            self.call('DELETE', f"gamingLoungeGroups/v1/members/me/groups/{conversation['id']}/newArrival")
            conversation['unread'] = 0
        return rows

    def send(self, conversation, text):
        self.call('POST', f"gamingLoungeGroups/v1/groups/{conversation['id']}/threads/{conversation['thread']}/messages",
                  body={'messageType': 1, 'body': text})
        return None  # the UI shows the sent text as the signed-in user's bubble

    def open_direct(self, friend):
        data = self.call('POST', 'gamingLoungeGroups/v1/groups', body={'invitees': [{'accountId': friend['account_id']}]})
        name = friend['online_id']
        return {'id': data['groupId'], 'thread': (data.get('mainThread') or {}).get('threadId'), 'name': name,
                'members': [name], 'names': {friend['account_id']: name}, 'last': '', 'unread': 0}

    # Friends

    def friends(self):
        data = self.call('GET', f'userProfile/v2/internal/users/{self.me()}/friends', {
            'limit': 100, 'offset': 0, 'order': 'availability+realName+onlineId', 'platforms': PLATFORMS})
        ids = [str(f) for f in data.get('friends', [])]
        names, online = self.profiles(ids), self.presences(ids)
        return [{'account_id': i, 'online_id': names.get(i, i), **online.get(i, {'presence': 'offline'})} for i in ids]

    def requests(self):
        data = self.call('GET', 'userProfile/v1/internal/users/me/friends/receivedRequests', {'limit': 100})
        ids = [str(r['accountId']) for r in data.get('receivedRequests', []) if r.get('accountId')]
        names = self.profiles(ids) if ids else {}
        return [{'account_id': i, 'online_id': names.get(i, i)} for i in ids]

    def answer_request(self, account_id, accept):
        # Accept: PUT 204, decline: DELETE 204 (same path also removes a friend or cancels a sent request).
        self.call('PUT' if accept else 'DELETE', f'userProfile/v1/internal/users/me/friends/{account_id}')

    def send_request(self, account_id):
        self.call('POST', f'userProfile/v1/internal/users/me/friends/{account_id}', body={})

    def search(self, query):
        data = self.call('GET', 'graphql/v1/op', {
            'operationName': 'metGetContextSearchResults',
            'variables': json.dumps({'searchTerm': query, 'searchContext': 'MobileUniversalSearchSocial',
                                     'displayTitleLocale': 'en-US'}, separators=(',', ':')),
            'extensions': json.dumps({'persistedQuery': {'version': 1, 'sha256Hash': SEARCH_HASH}},
                                     separators=(',', ':'))}, headers=GRAPHQL_HEADERS)
        if data.get('errors') and not data.get('data'):
            raise RuntimeError('Player search failed: ' + str(data['errors'][0].get('message', 'unknown error')))
        me = self.me()
        rows = []
        domains = ((data.get('data') or {}).get('universalContextSearch') or {}).get('results') or []
        for domain in domains:
            if domain.get('domain') != 'SocialAllAccounts':
                continue
            for item in domain.get('searchResults') or []:
                player = item.get('result') or {}
                account_id = str(player.get('accountId') or '')
                if not account_id or account_id == me:
                    continue
                state = (player.get('relationshipState') or '').upper()
                # Search rows carry the picture already; saves a profile lookup per search.
                url = avatar_url({'avatars': [{'size': AVATAR_SIZE, 'url': player.get('avatarUrl')}]})
                if url and player.get('onlineId'):
                    self.avatar_urls[player['onlineId']] = url
                rows.append({'account_id': account_id, 'online_id': player.get('onlineId') or account_id,
                             'detail': player.get('displayName') or None,
                             'relation': 'friend' if state == 'FRIEND' else 'requested' if 'REQUEST' in state
                             else 'none'})
        return rows


PLATFORMS = 'PS4,PS5,MOBILE_APP,PSPC'
GAME_CATEGORIES = 'ps4_game,ps5_native_game'
AVATAR_SIZE = 'l'  # profiles list sizes s, m, l, xl; l stays sharp on the 96 px roster tile
MAX_AVATAR_BYTES = 2_000_000
PLATFORM_NAMES = {'PS4': 'PS4', 'PS5': 'PS5', 'MOBILE_APP': 'mobile', 'PSPC': 'PC'}
# Persisted GraphQL operation used for player search.
SEARCH_HASH = 'cc76714a33c219bc4fb065a4c85d7e2d46cd93fc518a8210d4fbe1623d59748c'
GRAPHQL_HEADERS = {'apollographql-client-name': 'PlayStationApp-Android', 'apollographql-client-version': '26.9.2',
                   'Content-Type': 'application/json'}
GROUP_FIELDS = ('groupName,groupIcon,members,mainThread,joinedTimestamp,modifiedTimestamp,'
                'isFavorite,existsNewArrival,notificationSetting,readReceipts')


def secure_url(url):
    if not isinstance(url, str):
        return None
    # Profiles list http:// URLs; the same host serves https. Anything else (file://, …) is refused.
    if url.startswith('http://'):
        url = 'https://' + url[len('http://'):]
    return url if url.startswith('https://') else None


def avatar_url(profile):
    urls = {a.get('size'): a.get('url') for a in profile.get('avatars') or [] if isinstance(a, dict)}
    return secure_url(urls.get(AVATAR_SIZE) or next((u for u in urls.values() if u), None))


def fetch_images(urls):
    """Image bytes per URL, in memory only. Broken or non-https URLs are left out; no token is sent."""
    def download(url):
        safe = secure_url(url)
        if not safe:
            return url, None
        try:
            with urllib.request.urlopen(safe, timeout=30) as response:
                return url, response.read(MAX_AVATAR_BYTES)
        except OSError as exc:
            print('Image failed:', type(exc).__name__, flush=True)
            return url, None

    # Parallel: a 48-result search took seconds one picture at a time.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return {url: data for url, data in pool.map(download, set(urls)) if data}


TROPHY_TYPES = ('platinum', 'gold', 'silver', 'bronze')


def counts(value):
    return {kind: int((value or {}).get(kind) or 0) for kind in TROPHY_TYPES}


def summary_row(raw):
    return {'level': raw.get('trophyLevel'), 'progress': raw.get('progress') or 0, 'tier': raw.get('tier') or 1,
            **counts(raw.get('earnedTrophies'))}


def play_time(value):
    """ISO 8601 duration ('PT334H10M40S') as '334 h 10 min'; '' when absent."""
    match = re.fullmatch(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:[\d.]+S)?)?', value or '')
    if not match:
        return ''
    days, hours, minutes = (int(g or 0) for g in match.groups())
    hours += days * 24
    return f'{hours} h {minutes} min' if hours else f'{minutes} min'


def game_row(raw):
    category = raw.get('category') or ''
    return {'id': raw.get('titleId'), 'name': raw.get('localizedName') or raw.get('name') or '?',
            'icon': raw.get('localizedImageUrl') or raw.get('imageUrl'),
            'platform': 'PS5' if category.startswith('ps5') else 'PS4' if category.startswith('ps4') else '',
            'last_played': raw.get('lastPlayedDateTime'), 'play_time': play_time(raw.get('playDuration'))}


def title_row(raw):
    return {'id': raw.get('npCommunicationId'), 'service': raw.get('npServiceName') or 'trophy',
            'name': raw.get('trophyTitleName') or '?', 'icon': raw.get('trophyTitleIconUrl'),
            'platform': raw.get('trophyTitlePlatform') or '', 'progress': raw.get('progress') or 0,
            'earned': counts(raw.get('earnedTrophies')), 'defined': counts(raw.get('definedTrophies')),
            'updated': raw.get('lastUpdatedDateTime')}


def merge_trophies(definitions, progress):
    mine = {t.get('trophyId'): t for t in progress}
    rows = []
    for trophy in definitions:
        got = mine.get(trophy.get('trophyId'), {})
        earned = bool(got.get('earned'))
        secret = trophy.get('trophyHidden') and not earned
        rows.append({'name': 'Hidden trophy' if secret else trophy.get('trophyName') or '?',
                     'detail': 'Keep playing to reveal this trophy.' if secret else trophy.get('trophyDetail') or '',
                     'icon': None if secret else trophy.get('trophyIconUrl'), 'type': trophy.get('trophyType'),
                     'earned': earned, 'earned_at': got.get('earnedDateTime'),
                     'rate': got.get('trophyEarnedRate')})
    # The app lists earned trophies first, newest on top; the rest keep the game's order.
    return sorted((r for r in rows if r['earned']), key=lambda r: r['earned_at'] or '', reverse=True) + \
        [r for r in rows if not r['earned']]


def text_of(value):
    """Group names and message bodies arrive either as strings or as {'value': ...} objects."""
    if isinstance(value, dict):
        value = value.get('value') or value.get('text')
    return value if isinstance(value, str) else ''


def group_to_conversation(group, me):
    # Keys of the group detail response (groupName.value, mainThread.latestMessage.body).
    members = [m for m in group.get('members', []) if isinstance(m, dict)]
    names = {str(m.get('accountId')): m.get('onlineId') or '?' for m in members}
    others = [name for account, name in names.items() if account != str(me)]
    thread = group.get('mainThread') or {}
    latest = thread.get('latestMessage') or group.get('latestMessage') or {}
    last = text_of(latest.get('body') or latest.get('messageText'))
    return {'id': group.get('groupId'), 'thread': thread.get('threadId'),
            'name': text_of(group.get('groupName')) or ', '.join(others) or 'Conversation',
            'members': others, 'names': names, 'last': last,
            'unread': 1 if group.get('existsNewArrival') else 0}


def message_row(message, names):
    sender = message.get('sender') or {}
    account = str(sender.get('accountId') or message.get('senderAccountId') or '')
    return {'id': message.get('messageUid'),
            'sender': sender.get('onlineId') or names.get(account) or '?',
            'body': text_of(message.get('body') or message.get('messageText')) or '(unsupported message)',
            'time': int(message.get('createdTimestamp') or 0),  # epoch milliseconds as a string
            # shortcut: only 2006 (group created / member added) has been seen; assume events stay in the 2000s.
            'system': int(message.get('messageType') or 0) >= 2000}

# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

# -- stdlib --
from collections import defaultdict
import logging
import time

# -- third party --
import gevent

# -- own --
from endpoint import Endpoint
from server.utils import command
from utils import BatchList
from utils.misc import throttle


# -- code --
log = logging.getLogger('Room')


class Room(object):

    def __init__(self, core):
        self.core = core

        core.events.user_state_transition += self.handle_user_state_transition
        core.events.game_started += self.handle_game_started
        core.events.game_joined += self.handle_game_joined
        core.events.game_left += self.handle_game_left
        core.events.game_ended += self.handle_game_ended

        _ = core.events.client_command
        _['room:create'] += self._create
        _['room:join'] += self._join
        _['room:leave'] += self._leave
        _['room:users'] += self._users
        _['room:get-ready'] += self._get_ready
        _['room:change-location'] += self._change_location
        _['room:cancel-ready'] += self._cancel_ready

        self.games = {}

    def handle_user_state_transition(self, ev):
        c, f, t = ev
        core = self.core

        if t == 'dropped' and f in ('room', 'ready', 'game'):
            self.exit_game(c)

        if f in ('room', 'ready', 'game') or \
           t in ('room', 'ready', 'game'):
            # TODO: order with core.game?
            g = core.game.current(c)
            g and self._notify(g)

        users = core.lobby.all_users()
        ul = [u for u in users if core.lobby.state_of(u) == 'lobby']
        self._notify_gamelist(ul)

        return ev

    def handle_game_joined(self, ev):
        g, c = ev
        g._[self]['left'][c] = False
        return ev

    def handle_game_left(self, ev):
        g, c = ev
        g, c = ev
        g._[self]['left'][c] = bool(g.greenlet)
        return ev

    def handle_game_started(self, g):
        core = self.core
        users = g._[self]['users']
        assert None not in users

        for u in users:
            assert core.lobby.state_of(u) == 'ready'

        for u in users:
            core.lobby.state_of(u).transit('game')

        g._[self]['start_time'] = time.time()
        return g

    def handle_game_ended(self, g):
        core = self.core
        self.games.pop(g._[self]['gid'], 0)

        online_users = self.online_users_of(g)
        if not online_users:
            return g

        old = g

        gid = self._new_gid()
        g = self.create_game(
            gid,
            old.__class__,
            old._[self]['name'],
            old._[self]['flags'],
        )

        core.events.game_successive_create.emit((old, g))

        for u in old._[self]['users']:
            if self.is_online(u):
                self.join_game(g, u)

        return old

    # ----- Client Commands -----
    @command(['lobby'], [basestring, unicode, dict])  # TODO dict is too coarse
    def _create(self, u, gametype, name, flags):
        core = self.core
        from thb import modes

        if gametype not in modes:
            return

        gid = self._new_gid()
        g = self.create_game(gid, gametype, name, flags)
        core.invite.add_invited(u)
        core.room.join_game(g, u)

    @command(['lobby', 'ob'], [int, int, object])
    def _join(self, user, gid, slot):
        g = self.games.get(gid)
        if not g:
            return

        log.info("join game")
        self.join_game(g, user, slot)

    @command(['room', 'ready', 'game'], [])
    def _leave(self, c, is_drop=False):
        core = self.core
        self.exit_game(c)
        core.lobby.state_of(c).transit('lobby')

    @command(['lobby'], [int])
    def _users(self, c, gid):
        g = self.games.get(gid)
        if not g:
            return

        self._send_users(g, c)

    @command(['room'], [])
    def _get_ready(self, u):
        core = self.core
        g = core.game.current(u)

        users = g._[self]['users']
        if u not in users:
            return

        core.lobby.state_of(u).transfer('ready')

        if all(core.lobby.state_of(u) == 'ready' for u in users):
            # prevent double starting
            if not g.greenlet:
                log.info("game starting")
                g.greenlet = gevent.spawn(g.run)

    @command(['ready'], [])
    def _cancel_ready(self, u):
        core = self.core
        if core.lobby.state_of(u) != 'ready':
            return

        g = core.game.current(u)
        users = g._[self]['users']
        if u not in users:
            log.error('User not in player list')
            return

        u.state.transfer('room')

    @command(['room', 'ob'], [int])
    def _change_location(self, user, loc):
        core = self.core

        if core.lobby.state_of(user) not in ('room', ):
            return

        core = self.core

        g = core.game.current(user)
        users = g._[self]['users']

        if (not 0 <= loc < len(users)) or (users[loc] is not None):
            return

        i = users.index(user)
        users[loc], users[i] = users[i], users[loc]

        core.events.game_change_location.emit(g)

    # ----- Public Methods -----
    def is_online(self, g, c):
        # XXX 'left' is not implemented yet
        rst = c is not None
        rst = rst and c in g._[self]['users']
        rst = rst and not g._[self]['left'][c]
        return rst

    def online_users_of(self, g):
        return [u for u in g._[self]['users'] if self.is_online(g, u)]

    def users_of(self, g):
        return g._[self]['users']

    def gid_of(self, g):
        return g._[self]['gid']

    def name_of(self, g):
        return g._[self]['name']

    def get_game(self, gid):
        return self.games.get(gid)

    def create_game(self, gamecls, name, flags):
        core = self.core
        gid = self._new_gid()
        g = core.game.create_game(gamecls)
        self.games[gid] = g

        g._[self] = {
            'gid': gid,
            'users': BatchList([None] * g.n_persons),
            'left': defaultdict(bool),
            'name': name,
            'flags': flags,  # {'match': 1, 'invite': 1}
            'start_time': 0,

            '_notifier': None,
        }

        ev = core.events.game_created.emit(g)
        assert ev
        return g

    def join_game(self, g, c, slot):
        core = self.core

        assert core.lobby.state_of(c) == 'lobby'

        slot = slot and self._next_slot(g)
        if not (0 <= slot < g.n_persons and g._[self]['users'][slot] is None):
            return

        g._[self]['users'][slot] = c

        core.lobby.state_of(c).transit('room')
        c.write(['game_joined', core.view.Game(g)])

        core.events.game_joined.emit((g, c))

    def exit_game(self, c):
        core = self.core

        g = core.game.current(c)
        assert c in g._[self]['users']
        c.write(['game_left', None])

        if self.game_started:
            log.info('player dropped')
        else:
            log.info('player leave')

        core.events.game_left.emit((g, c))

        gid = g._[self]['uid']
        if gid not in self.games:
            return

        users = self.online_users_of(g)

        if users:
            return

        if g.greenlet:
            log.info('game aborted')
            g.suicide = True  # game will kill itself in get_synctag()
            # And then triggers GAME_ENDED, so nothing to do here
            core.events.game_killed.emit(g)
        else:
            log.info('game canceled')
            self.games.pop(gid, 0)

    def send_room_users(self, g, to):
        core = self.core
        gid = g._[self]['gid']
        pl = [core.view.User(u) for u in g._[self]['users']]
        s = Endpoint.encode(['room_users', [gid, pl]])  # former `gameinfo` and `player_change`
        for u in to:
            u.raw_write(s)

    # ----- Methods -----
    def _notify(self, g):
        notifier = g._[self]['_notifier']

        if notifier:
            notifier()
            return

        @throttle(0.5)
        def notifier():
            gevent.spawn(self.send_room_users, g, g._[self]['users'])

        g._[self]['_notifier'] = notifier
        notifier()

    @throttle(3)
    def _notify_gamelist(self, ul):
        core = self.core

        lst = [core.view.Game(g) for g in self.games.values()]

        d = Endpoint.encode([
            ['current_games', lst],
        ], Endpoint.FMT_BULK_COMPRESSED)

        @gevent.spawn
        def do_send():
            for u in ul:
                u.write(d)

    def _next_slot(self, g):
        try:
            return g._[self]['users'].index(None)
        except ValueError:
            return None

    def _new_gid(self):
        core = self.core
        gid = core.backend.query('query { gameId }')['gameId']
        return gid

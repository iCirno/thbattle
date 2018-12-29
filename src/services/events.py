# -*- coding: utf-8 -*-

from gevent import monkey
monkey.patch_all()


# -- stdlib --
from collections import deque
from urllib import unquote
import argparse
import json
import logging
import sys
import time

# -- third party --
from bottle import request, response, route, run
from gevent.event import Event
import gevent

# -- own --
from utils.interconnect import RedisInterconnect
from account.forum_integration import Account

# -- code --
options = None
current_users = {}
event_waiters = set()
events_history = deque([[None, 0]] * 1000)
interconnect = None

logging.basicConfig()


class Interconnect(RedisInterconnect):
    def on_message(self, node, topic, message):
        if topic == 'current_users':
            # [[node, username, state], ...]
            current_users[node] = [
                (node, i['account'][2], i['state']) for i in message
            ]
            rst = []
            map(rst.__iadd__, current_users.values())
            self.notify('current_users', rst)

        elif topic == 'shutdown':
            # not implemented yet
            current_users[node] = []
            rst = []
            map(rst.__iadd__, current_users.values())
            self.notify('current_users', rst)

        if topic == 'speaker':
            # [node, username, content]
            message.insert(0, node)
            self.notify('speaker', message)

    def notify(self, key, message):
        @gevent.spawn
        def _notify():
            events_history.rotate()
            events_history[0] = [[key, message], time.time()]
            [evt.set() for evt in list(event_waiters)]


@route('/interconnect/onlineusers')
def onlineusers():
    rst = []
    map(rst.__iadd__, current_users.values())
    return json.dumps(rst)


@route('/interconnect/events')
def events():
    try:
        last = float(request.get_cookie('interconnect_last_event'))
    except Exception:
        last = time.time()

    evt = Event()

    events_history[0][1] > last and evt.set()

    event_waiters.add(evt)
    success = evt.wait(timeout=30)
    event_waiters.discard(evt)

    response.set_header('Content-Type', 'application/json')
    response.set_header('Pragma', 'no-cache')
    response.set_header('Cache-Control', 'no-cache, no-store, max-age=0, must-revalidate')
    response.set_header('Expires', 'Thu, 01 Dec 1994 16:00:00 GMT')
    success and response.set_cookie('interconnect_last_event', '%.5f' % time.time())

    data = []
    for e in events_history:
        if e[1] > last:
            data.append(e[0])
        else:
            break

    data = list(reversed(data))
    return json.dumps(data)


@route('/interconnect/speaker', method='POST')
def speaker():
    idx = {
        k.split('_')[-1]: k for k in request.cookies
        if k.startswith(options.discuz_cookiepre)
    }

    if not ('auth' in idx and 'saltkey' in idx):
        response.status = 403
        return

    auth = unquote(request.get_cookie(idx['auth']))
    saltkey = unquote(request.get_cookie(idx['saltkey']))
    uid, pwd = Account.decode_cookie(auth, saltkey)
    user = Account.find(uid)
    if not user:
        return 'false'

    if user.jiecao < 0:
        return 'false'

    message = request.forms.get('message').decode('utf-8', 'ignore')
    username = user.username.decode('utf-8', 'ignore')

    interconnect.publish('speaker', [username, message])

    return 'true'


def main():
    global options, interconnect
    parser = argparse.ArgumentParser(sys.argv[0])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=7001)
    parser.add_argument('--chat', default='tcp://localhost:6969')
    parser.add_argument('--member-service', default='localhost')
    parser.add_argument('--discuz-cookiepre', default='VfKd_')
    parser.add_argument('--discuz-authkey', default='Proton rocks')
    parser.add_argument('--db', default='sqlite:////dev/shm/thb.sqlite3')
    options = parser.parse_args()

    import options as opmod
    opmod.options = options

    interconnect = Interconnect.spawn('forum', options.redis_url)

    run(server='gevent', host=options.host, port=options.port)


if __name__ == '__main__':
    main()

#!/usr/bin/env python
#
# MIT
# 2019 Alexander Couzens <lynxis@fe80.eu>

import logging
import logging.config
import yaml
import asyncio
import async_timeout
from datetime import datetime
import pydle

import aioredis
import aiohttp

from hbmqtt.client import MQTTClient, ClientException
from hbmqtt.mqtt.constants import QOS_2

from configparser import ConfigParser

LOG = logging.getLogger("v4runa")

class MyOwnBot(pydle.Client):
    def __init__(self, *args, join_channels=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands = {}
        self.__join_channels = []
        if join_channels:
            self.__join_channels = join_channels

    async def on_connect(self):
        for channel in self.__join_channels:
            await self.join(channel)

    async def on_message(self, target, source, message):
        # empty message
        if not message:
            return

        # ignore message
        if message[0] != ".":
            return

        command = message[1:].split()[0]
        # empty command list
        if not command:
            return

        if command in self.commands:
            await self.commands[command](self, target, source, message)
        else:
            LOG.debug("Unknown command '%s'.", command)

    def register_command(self, command, func):
        """ only coroutine's are supported as func
            :param: command a string under which the function will be called
            :param: func must have the signature command(ownbot, target, source, message)
        """
        self.commands[command] = func

    def deregister_command(self, command):
        if command in self.commands:
            del self.commands[command]

class Store():
    def __init__(self, server="localhost"):
        self.server = server
        self._conn = None

    async def _connect(self):
        if self._conn is None:
            self._conn = await aioredis.create_redis_pool('redis://localhost')

    async def set(self, key, value):
        await self._connect()
        await self._conn.set(key, value)
        return

    async def get(self, key):
        await self._connect()
        return await self._conn.get(key)

    async def get_float(self, key, default=0.0):
        await self._connect()
        value = await self._conn.get(key)
        if value is None:
            return default

        return float(value)

_OPEN = 1
_CLOSED = 2
_UNKNOWN = 3

async def update_spaceapi(state, token):
    LOG.info("updating spaceapi to state %s", state)
    if state not in [_OPEN, _CLOSED]:
        # TODO: unknown states
        return

    state = 0 if state == _CLOSED else 1
    url = 'https://spaceapi.afra.fe80.eu/v1/status/{}/{}'.format(token, state)

    try:
        async with aiohttp.ClientSession(raise_for_status=True) as session:
            async with async_timeout.timeout(5):
                async with session.put(url) as resp:
                    return await resp.text()
    except:
        LOG.error("Could not update the spaceapi status.")
        LOG.exception("Fooo")
        return None

class V4runaBot():
    def __init__(self, configpath):
        self.mqcli = None

        config = ConfigParser()
        config.read(configpath)

        logging_file = config.get("logging", "yamlconfig", fallback=None)
        if logging_file:
            configyaml = yaml.load(open(logging_file, 'r'))
            logging.config.dictConfig(configyaml)

        self.user = config.get("irc", "user")
        self.realname = config.get("irc", "realname", fallback="real name")
        self.server = config.get("irc", "server")
        self.channels = config.get("irc", "channels", fallback="").split()

        password = config.get("irc", "password", fallback=None)
        if password is not None:
            self.irc = MyOwnBot(
                self.user,
                sasl_username=self.user,
                sasl_password=password,
                realname=self.realname,
                join_channels=self.channels)
        else:
            self.irc = MyOwnBot(self.user, realname=self.realname, join_channels=self.channels)

        asyncio.ensure_future(self.irc.connect(self.server, tls=True), loop=loop)

        self.spacetoken = config.get("spaceapi", "token")

        self.store = Store(self)
        self.irc.register_command("open?", self.command_is_open)
        self.irc.register_command("open!", self.command_open)
        self.irc.register_command("close!", self.command_close)
        self.irc.register_command("closed!", self.command_close)
        self.irc.register_command("who", self.command_who)
        self.irc.register_command("help", self.command_help)
        self.irc.register_command("commands", self.command_help)

    async def get_space(self):
        """ calculate by the timestamps if the space is open or not """
        irc_open = await self.store.get_float('door_irc_open_timestamp')
        irc_closed = await self.store.get_float('door_irc_closed_timestamp')
        kicked = await self.store.get_float('door_kicked_timestamp')

        if not irc_open and not irc_closed and not kicked:
            return (_UNKNOWN, '0.0')

        now = datetime.now().timestamp()
        if (irc_open > irc_closed) and \
                (irc_open + 4 * 60 * 60) > now:
            #                   4 h
            return (_OPEN, irc_open)
        elif (irc_closed + 20 * 60) > now:
            #                20 min
            return (_CLOSED, irc_closed)
        elif (kicked + 15 * 60) > now:
            #                 15 min
            return (_OPEN, kicked)
        else:
            stamp = irc_open
            if stamp < irc_closed:
                stamp = irc_closed
            if stamp < kicked:
                stamp = kicked
            return (_CLOSED, stamp)

    async def check_state_change(self):
        ts_state, _ = await self.get_space()
        state = await self.store.get('open')
        if str(ts_state) != str(state, 'utf-8'):
            await self.store.set('open', ts_state)
            await update_spaceapi(ts_state, self.spacetoken)
            await self.say_state(ts_state)

    async def say_state(self, state, target=None):
        human = {
            _OPEN: "open",
            _CLOSED: "closed",
            _UNKNOWN: "in an unknown state",
            }

        LOG.info("The space is now %s. With target=%s", human[state], target)
        if target:
            await self.irc.notice(target, "The space is now %s." % human[state])
        else:
            for channel in self.channels:
                await self.irc.notice(channel, "The space is now %s." % human[state])
                if state == _CLOSED:
                    await self.irc.notice(channel, ".purge")

    async def check_room_status(self):
        """
        Checks periodically if the space is open or closed.
        The Open status can be easily checked, because
        an event must happen.
        To close the afra without a command, timers must be checked.
        """
        while True:
            await self.check_state_change()
            await asyncio.sleep(60)

    async def wait_kick_space(self):
        """
        The external device will publish a mqtt event.
        This function handles this event.
        """

        while True:
            self.mqcli = MQTTClient()
            try:
                await self.mqcli.connect('mqtt://localhost/')
            except:
                await asyncio.sleep(10)
                continue

            try:
                await self.mqcli.subscribe([
                    ('afra/door', QOS_2),
                    ])
                await self.mqcli.deliver_message()
                # TODO: ignoring the payload for now
                await self.store.set('door_kicked_timestamp', datetime.now().timestamp())
                await self.check_state_change()
            except ClientException as ce:
                continue

    async def set_space(self, state):
        """ use when setting the space manually """
        # seconds ince epoch
        if state == _OPEN:
            await self.store.set('door_irc_open_timestamp', datetime.now().timestamp())
        else:
            await self.store.set('door_irc_closed_timestamp', datetime.now().timestamp())

    # commands
    async def command_is_open(self, irc, target, _source, _message):
        status, timestamp = await self.get_space()
        LOG.info("is open? %s, %s", status, timestamp)

        if status == _CLOSED:
            await self.irc.notice(target, "The space is closed.")
        elif status == _OPEN:
            await self.irc.notice(target, "The space is open.")
        else:
            await self.irc.notice(target, "Who knows if the space is open or not")

    # open!
    async def command_open(self, irc, target, _source, _message):
        await self.set_space(_OPEN)
        await self.check_state_change()
        await self.irc.notice(target, "Noted.")

    # closed! or close!
    async def command_close(self, irc, target, _source, _message):
        await self.set_space(_CLOSED)
        await self.check_state_change()
        await self.irc.notice(target, "Noted.")

    async def command_help(self, irc, target, source, _message):
        cmds = []
        for key in self.irc.commands.keys():
            cmds += [key]
        cmds.sort()
        cmds = " ".join(cmds)
        message = "I'm able to follow commands. They must start with a . (dot). E.g. \".who\". I can speak the following commands: " + cmds

        await self.irc.notice(target, message)

    async def command_who(self, irc, target, source, _message):
        # TODO: find out where the BTC is located
        # explicit using message here, because we're talking to a human.
        await self.irc.message(target,
                               "Hi %s, I'm v4runa, the main AI construct. I'm integrated into the Bureau of Technology"
                               "Control head quarters. You can read more about me in the book Influx." % source)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    loop = asyncio.get_event_loop()
    v4runa = V4runaBot(configpath="v4runa.cfg")
    asyncio.ensure_future(v4runa.wait_kick_space(), loop=loop)
    asyncio.ensure_future(v4runa.check_room_status(), loop=loop)
    loop.run_forever()

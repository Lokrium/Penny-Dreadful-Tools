import asyncio
import datetime
import logging
import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

import discord
from discord import Guild, Member, Role, VoiceState
from discord.activity import Streaming
from discord.enums import Status
from discord.errors import Forbidden, NotFound
from discord.ext import commands
from discord.message import Message
from discord.reaction import Reaction
from github.GithubException import GithubException

import discordbot.commands
from discordbot import command
from magic import fetcher, multiverse, oracle, rotation, seasons, tournaments, whoosh_write
from magic.models import Card
from shared import configuration, dtutil, fetch_tools, perf
from shared import redis_wrapper as redis
from shared import repo
from shared.container import Container

TASKS = []

def background_task(func: Callable) -> Callable:
    async def wrapper(self: discord.Client) -> None:
        try:
            await self.wait_until_ready()
            await func(self)
        except Exception:  # pylint: disable=broad-except
            await self.on_error(func.__name__)
    TASKS.append(wrapper)
    return wrapper


class Bot(commands.Bot):
    def __init__(self, **kwargs: Any) -> None:
        self.launch_time = perf.start()
        commit_id = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode()
        redis.store('discordbot:commit_id', commit_id)

        help_command = commands.DefaultHelpCommand(dm_help=None, no_category='Commands')
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True
        intents.typing = False

        super().__init__(command_prefix=commands.when_mentioned_or('!'), help_command=help_command, case_insensitive=True, intents=intents, **kwargs)
        super().load_extension('jishaku')
        self.voice = None
        self.achievement_cache: Dict[str, Dict[str, str]] = {}
        for task in TASKS:
            asyncio.ensure_future(task(self), loop=self.loop)
        discordbot.commands.setup(self)

    async def close(self) -> None:
        try:
            p = await asyncio.create_subprocess_shell('git pull')
            await p.wait()
            p = await asyncio.create_subprocess_shell(f'{sys.executable} -m pip install -U -r requirements.txt --no-cache')
            await p.wait()
        except Exception as c:  # pylint: disable=broad-except
            repo.create_issue('Bot error while closing', 'discord user', 'discordbot', 'PennyDreadfulMTG/perf-reports', exception=c)
        await super().close()

    async def on_ready(self) -> None:
        logging.info('Logged in as %s (%d)', self.user.name, self.user.id)
        names = ', '.join([guild.name or '' for guild in self.guilds])
        logging.info('Connected to %s', names)
        logging.info('--------')
        perf.check(self.launch_time, 'slow_bot_start', '', 'discordbot')

    async def on_message(self, message: Message) -> None:
        # We do not want the bot to reply to itself.
        if message.author == self.user:
            return
        if message.author.bot:
            return
        ctx = await self.get_context(message, cls=command.MtgContext)
        if ctx.valid:
            await self.invoke(ctx)
        else:
            await command.respond_to_card_names(message, self)

    async def on_voice_state_update(self, member: Member, before: VoiceState, after: VoiceState) -> None:
        # pylint: disable=unused-argument
        # If we're the only one left in a voice chat, leave the channel
        guild = getattr(after.channel, 'guild', None)
        if guild is None:
            return
        voice = guild.voice_client
        if voice is None or not voice.is_connected():
            return
        if len(voice.channel.voice_members) == 1:
            await voice.disconnect()

    async def on_member_join(self, member: Member) -> None:
        if member.bot:
            return
        # is_test_server = member.guild.id == 226920619302715392
        if is_pd_server(member.guild):  # or is_test_server:
            greeting = "Hey there {mention}, welcome to the Penny Dreadful community!  Be sure to set your nickname to your MTGO username, and check out <{url}> if you haven't already.".format(mention=member.mention, url=fetcher.decksite_url('/'))
            chan = member.guild.get_channel(207281932214599682)  # general (Yes, the guild ID is the same as the ID of it's first channel.  It's not a typo)
            await chan.send(greeting)

    async def on_member_update(self, before: Member, after: Member) -> None:
        if before.bot:
            return
        # streamers.
        streaming_role = await get_role(before.guild, 'Currently Streaming')
        if streaming_role:
            if not isinstance(after.activity, Streaming) and streaming_role in before.roles:
                await after.remove_roles(streaming_role)
            if isinstance(after.activity, Streaming) and not streaming_role in before.roles:
                await after.add_roles(streaming_role)
        # Achievements
        role = await get_role(before.guild, 'Linked Magic Online')
        if role and before.status == Status.offline and after.status == Status.online:
            data = None
            # Linked to PDM
            if role is not None and not role in before.roles:
                if data is None:
                    data = await fetcher.person_data_async(before.id)
                if data.get('id', None):
                    await after.add_roles(role)

            key = f'discordbot:achievements:players:{before.id}'
            if is_pd_server(before.guild) and not redis.get_bool(key) and not data:
                data = await fetcher.person_data_async(before.id)
                redis.store(key, True, ex=14400)

            # Trophies
            if is_pd_server(before.guild) and data is not None and data.get('achievements', None) is not None:
                expected: List[Role] = []
                remove: List[Role] = []

                async def achievement_name(key: str) -> str:
                    name = self.achievement_cache.get(key, None)
                    if name is None:
                        self.achievement_cache.update(await fetcher.achievement_cache_async())
                        name = self.achievement_cache[key]
                    return f'🏆 {name["title"]}'

                for name, count in data['achievements'].items():
                    if int(count) > 0:
                        trophy = await achievement_name(name)
                        role = await get_role(before.guild, trophy, create=True)
                        if role is not None:
                            expected.append(role)
                for role in before.roles:
                    if role in expected:
                        expected.remove(role)
                    elif '🏆' in role.name:
                        remove.append(role)
                await before.remove_roles(*remove)
                await before.add_roles(*expected)

    async def on_guild_join(self, server: Guild) -> None:
        for channel in server.text_channels:
            try:
                await channel.send("Hi, I'm mtgbot.  To look up cards, just mention them in square brackets. (eg `[Llanowar Elves] is better than [Elvish Mystic]`).")
                await channel.send("By default, I display Penny Dreadful legality. If you don't want or need that, just type `!notpenny`.")
                return
            except Forbidden:
                pass

    async def on_reaction_add(self, reaction: Reaction, author: Member) -> None:
        if reaction.message.author == self.user:
            c = reaction.count
            if reaction.me:
                c = c - 1
            if c > 0 and not reaction.custom_emoji and reaction.emoji == '❎':
                try:
                    await reaction.message.delete()
                except NotFound:  # Someone beat us to it?
                    pass
            elif c > 0 and 'Ambiguous name for ' in reaction.message.content and reaction.emoji in command.DISAMBIGUATION_EMOJIS_BY_NUMBER.values():
                async with reaction.message.channel.typing():
                    search = re.search(r'Ambiguous name for ([^\.]*)\. Suggestions: (.*)', reaction.message.content)
                    if search:
                        previous_command, suggestions = search.group(1, 2)
                        card = re.findall(r':[^:]*?: ([^:]*) ', suggestions + ' ')[command.DISAMBIGUATION_NUMBERS_BY_EMOJI[reaction.emoji] - 1]
                        # pylint: disable=protected-access
                        message = Container(content='!{c} {a}'.format(c=previous_command, a=card), channel=reaction.message.channel, author=author, reactions=[], _state=reaction.message._state)
                        await self.on_message(message)
                        await reaction.message.delete()

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        await super().on_error(event_method, args, kwargs)
        (_, exception, __) = sys.exc_info()
        try:
            content = [arg.content for arg in args if hasattr(arg, 'content')]  # The default string representation of a Message does not include the message content.
            repo.create_issue(f'Bot error {event_method}\n{args}\n{kwargs}\n{content}', 'discord user', 'discordbot', 'PennyDreadfulMTG/perf-reports', exception=exception)
        except GithubException as e:
            logging.error('Github error\n%s', e)

    @background_task
    async def background_task_tournaments(self) -> None:
        tournament_channel_id = configuration.get_int('tournament_reminders_channel_id')
        if not tournament_channel_id:
            logging.warning('tournament channel is not configured')
            return
        channel = self.get_channel(tournament_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            logging.warning('ERROR: could not find tournament_channel_id %d', tournament_channel_id)
            return
        while self.is_ready:
            info = tournaments.next_tournament_info()
            diff = info['next_tournament_time_precise']
            if info['sponsor_name']:
                message = 'A {sponsor} sponsored tournament'.format(sponsor=info['sponsor_name'])
            else:
                message = 'A free tournament'
            embed = discord.Embed(title=info['next_tournament_name'], description=message)
            if diff <= 1:
                embed.add_field(name='Starting now', value='Check <#334220558159970304> for further annoucements')
            elif diff <= 14400:
                embed.add_field(name='Starting in:', value=dtutil.display_time(diff, 2))
                embed.add_field(name='Pre-register now:', value='https://gatherling.com')

            if diff <= 14400:
                embed.set_image(url=fetcher.decksite_url('/favicon-152.png'))
                # See #2809.
                # pylint: disable=no-value-for-parameter,unexpected-keyword-arg
                await channel.send(embed=embed)

            if diff <= 300:
                # Five minutes, final warning.  Sleep until the tournament has started.
                timer = 301
            elif diff <= 1800:
                # Half an hour. Sleep until 5 minute warning.
                timer = diff - 300
            elif diff <= 3600:
                # One hour.  Sleep until half-hour warning.
                timer = diff - 1800
            else:
                # Sleep for one hour plus enough to have a whole number of hours left.
                timer = 3600 + diff % 3600
                if diff > 3600 * 6:
                    # The timer can afford to get off-balance by doing other background work.
                    await multiverse.update_bugged_cards_async()

            if timer < 300:
                timer = 300
            await asyncio.sleep(timer)
        logging.warning('naturally stopping tournament reminders')

    @background_task
    async def background_task_league_end(self) -> None:
        tournament_channel_id = configuration.get_int('tournament_reminders_channel_id')
        if not tournament_channel_id:
            logging.warning('tournament channel is not configured')
            return
        channel = self.get_channel(tournament_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            logging.warning('tournament channel could not be found')
            return

        while self.is_ready:
            try:
                league = await fetch_tools.fetch_json_async(fetcher.decksite_url('/api/league'))
            except fetch_tools.FetchException as e:
                err = '; '.join(str(x) for x in e.args)
                logging.error("Couldn't reach decksite or decode league json with error message(s) %s", err)
                logging.info('Sleeping for 5 minutes and trying again.')
                await asyncio.sleep(300)
                continue

            if not league:
                await asyncio.sleep(300)
                continue

            diff = round((dtutil.parse_rfc3339(league['end_date'])
                          - datetime.datetime.now(tz=datetime.timezone.utc))
                         / datetime.timedelta(seconds=1))

            embed = discord.Embed(title=league['name'], description='League ending soon - any active runs will be cut short.')
            if diff <= 60 * 60 * 24:
                embed.add_field(name='Ending in:', value=dtutil.display_time(diff, 2))
                embed.set_image(url=fetcher.decksite_url('/favicon-152.png'))
                # See #2809.
                # pylint: disable=no-value-for-parameter,unexpected-keyword-arg
                await channel.send(embed=embed)
            if diff <= 5 * 60:
                # Five minutes, final warning.
                timer = 301
            elif diff <= 1 * 60 * 60:
                # 1 hour. Sleep until five minute warning.
                timer = diff - 300
            elif diff <= 24 * 60 * 60:
                # 1 day.  Sleep until one hour warning.
                timer = diff - 1800
            else:
                # Sleep for 1 day, plus enough to leave us with a whole number of days
                timer = 24 * 60 * 60 + diff % (24 * 60 * 60)

            if timer < 300:
                timer = 300
            await asyncio.sleep(timer)
        logging.warning('naturally stopping league reminders')

    @background_task
    async def background_task_rotation_hype(self) -> None:
        rotation_hype_channel_id = configuration.get_int('rotation_hype_channel_id')
        if not rotation_hype_channel_id:
            logging.warning('rotation hype channel is not configured')
            return
        channel = self.get_channel(rotation_hype_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            logging.warning('rotation hype channel is not a text channel')
            return
        while self.is_ready():
            until_rotation = seasons.next_rotation() - dtutil.now()
            last_run_time = rotation.last_run_time()
            if until_rotation < datetime.timedelta(7) and last_run_time is not None:
                if dtutil.now() - last_run_time < datetime.timedelta(minutes=5):
                    hype = await rotation_hype_message()
                    if hype:
                        await channel.send(hype)
                timer = 5 * 60
            else:
                timer = int((until_rotation - datetime.timedelta(7)).total_seconds())
            await asyncio.sleep(timer)

    @background_task
    async def background_task_reboot(self) -> None:
        do_reboot_key = 'discordbot:do_reboot'
        if redis.get_bool(do_reboot_key):
            redis.clear(do_reboot_key)
        while self.is_ready():
            if redis.get_bool(do_reboot_key):
                logging.info('Got request to reboot from redis')
                await self.logout()
            await asyncio.sleep(60)

def init() -> None:
    client = Bot()
    logging.info('Initializing Cards DB')
    updated = multiverse.init()
    if updated:
        whoosh_write.reindex()
    asyncio.ensure_future(multiverse.update_bugged_cards_async())
    oracle.init()
    logging.info('Connecting to Discord')
    client.run(configuration.get_str('token'))

def is_pd_server(guild: Guild) -> bool:
    return guild.id == 207281932214599682  # or guild.id == 226920619302715392

async def get_role(guild: Guild, rolename: str, create: bool = False) -> Optional[Role]:
    for r in guild.roles:
        if r.name == rolename:
            return r
    if create:
        return await guild.create_role(name=rolename)
    return None

async def rotation_hype_message() -> Optional[str]:
    rotation.clear_redis()
    runs, runs_percent, cs = rotation.read_rotation_files()
    runs_remaining = rotation.TOTAL_RUNS - runs
    newly_legal = [c for c in cs if c.hit_in_last_run and c.hits == rotation.TOTAL_RUNS / 2]
    newly_eliminated = [c for c in cs if not c.hit_in_last_run and c.status == 'Not Legal' and c.hits_needed == runs_remaining + 1]
    newly_hit = [c for c in cs if c.hit_in_last_run and c.hits == 1]
    num_undecided = len([c for c in cs if c.status == 'Undecided'])
    num_legal_cards = len([c for c in cs if c.status == 'Legal'])
    s = f'Rotation run number {runs} completed. Rotation is {runs_percent}% complete. {num_legal_cards} cards confirmed.'
    if not newly_hit + newly_legal + newly_eliminated and runs != 1 and runs % 5 != 0 and runs < rotation.TOTAL_RUNS / 2:
        return None  # Sometimes there's nothing to report
    if len(newly_hit) > 0 and runs_remaining > runs:
        newly_hit_s = list_of_most_interesting(newly_hit)
        s += f'\nFirst hit for: {newly_hit_s}.'
    if len(newly_legal) > 0:
        newly_legal_s = list_of_most_interesting(newly_legal)
        s += f'\nConfirmed legal: {newly_legal_s}.'
    if len(newly_eliminated) > 0:
        newly_eliminated_s = list_of_most_interesting(newly_eliminated)
        s += f'\nEliminated: {newly_eliminated_s}.'
    s += f'\nUndecided: {num_undecided}.\n'
    if runs_percent >= 50:
        s += f"<{fetcher.decksite_url('/rotation/')}>"
    return s

# This does not currently actually find the most interesting just max 10 – only decksite knows about interestingness for now.
def list_of_most_interesting(cs: List[Card]) -> str:
    max_shown = 4
    if len(cs) > max_shown:
        return ', '.join(c.name for c in cs[0:max_shown]) + f' and {len(cs) - max_shown} more'
    return ', '.join(c.name for c in cs)

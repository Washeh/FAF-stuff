# vim: ts=4 et sw=4 sts=4
# -*- coding: utf-8 -*-
import random
import asyncio
import urllib
import re

import irc3
from irc3.plugins.command import command
from irc3.plugins.async import Whois
import time
import threading
import os
import codecs
import traceback
import json
import shutil

from twitch import twitchThread
from timed_input_accumulator import timedInputAccumulatorThread
from periodic_callback import periodicCallback
from markov import Markov
from points import Points
from events import Events
from poker import Poker
from bet import Bets
from mai.roasts import BHROASTS
from mai.questions import Questions


ADMINS = []
MAIN_CHANNEL = "#aeolus" #   shadows
POKER_CHANNEL = "#poker" #   shadows
IGNOREDUSERS = {}
CDPRIVILEDGEDUSERS = {}
NICKSERVIDENTIFIEDRESPONSES = {}
NICKSERVRESPONSESLOCK = None
TIMERS = {}
VARS = {}
REACTION_WORDS = {}
DEFAULTCD = False
DEFAULTVALUE = False

RENAME_API_URL = "https://api.faforever.com/data/player/{id}?include=names&fields[nameRecord]=name"
RENAME_API_URL_NAME = "https://api.faforever.com/data/player?filter=(login=={name})"

NICKSERV_WAIT_TICKS = 60

CHATLVL_COMMANDLOCK = False
CHATLVL_RESETNAME = '#reset'
CHATLVL_NORESETNAME = '#noreset'
CHATLVL_NORESETDISCOUNT = 0.5
CHATLVL_RESETCOUNT = 25000
CHATLVL_EPOCH = 1
CHATLVLWORDS = {}
POINTS_PER_CHATLVL = 5
CHATLVL_TOPPLAYERS = {}
CHATPOINTS_REMOVAL_IF_KICKED = 100
CHATPOINTS_DEFAULT_TOURNEY_START = 1000

useDebugPrint = False
useLSTM = False


@irc3.extend
def action(bot, *args):
    bot.privmsg(args[0], '\x01ACTION ' + args[1] + '\x01')

@irc3.plugin
class Plugin(object):

    requires = [
        'irc3.plugins.userlist',
    ]

    def __init__(self, bot):
        self.bot = bot
        self.timers = {}
        self.whois = Whois(bot)
        self.loop = asyncio.new_event_loop()
        #asyncio.set_event_loop(self.loop)
        #self.oldHelp = self.help
        global NICKSERVRESPONSESLOCK, CHATLVL_COMMANDLOCK
        CHATLVL_COMMANDLOCK = threading.Lock()
        NICKSERVRESPONSESLOCK = threading.Lock()

    def debugPrint(self, text):
        if useDebugPrint:
            print(text)

    @classmethod
    def reload(cls, old):
        return cls(old.bot)

    @irc3.event(irc3.rfc.CONNECTED)
    def nickserv_auth(self, *args, **kwargs):
        self.bot.privmsg('nickserv', 'identify %s' % self.bot.config['nickserv_password'])
        self.on_restart()

    @irc3.event(irc3.rfc.JOIN)
    def on_join(self, channel, mask):
        if mask.nick == self.bot.config['nick']:
            return
        global CHATLVL_TOPPLAYERS, MAIN_CHANNEL
        if channel != MAIN_CHANNEL:
            return
        nick = mask.nick
        msg, msgstrength = self.Chatpoints.getOnJoinMsgById(nick)
        if msgstrength < 3:
            if CHATLVL_TOPPLAYERS.get(nick, False):
                msg = "Behold! {name}, currently rank {rank} on the chatlvl ladder, joined this chat!"
        if msg and (not self.spam_protect('onjoin-'+nick, mask, channel, {}, specialSpamProtect='onjoin', ircSpamProtect=False)):
                self.bot.action(channel, msg.format(**{
                    "name" : nick,
                    "rank" : str(CHATLVL_TOPPLAYERS.get(nick, -1))
                }))

    def __addText(self, text):
        try:
            self.TEXT += str(text.encode('ascii', 'ignore')) + "\n"
            l = len(self.TEXT)
            self.TEXT = self.TEXT[max([l-40,0]):l]
        except Exception:
            #print(traceback.format_exc())
            pass

    @irc3.event(irc3.rfc.PRIVMSG)
    @asyncio.coroutine
    def on_privmsg(self, *args, **kwargs):
        msg, channel, sender = kwargs['data'], kwargs['target'], kwargs['mask']
        if self.bot.config['nick'] in sender.nick:
            return
        if sender.startswith("NickServ!"):
            self.__handleNickservMessage(msg)
            return
        #if not msg.startswith('!'):
        #    self.__addText(msg)
        global IGNOREDUSERS, MAIN_CHANNEL
        if channel == MAIN_CHANNEL and "undress MAI" in msg:
            if not self.spam_protect("undress", "setoner", MAIN_CHANNEL, args):
                self.bot.action(channel, "blushes and reveals http://i.imgur.com/IOnpStK.png")
            return
        if channel.startswith("#") and not sender.nick in IGNOREDUSERS.values():
            lowercase_msg = msg.lower()
            for reaction_word in REACTION_WORDS:
                if reaction_word in lowercase_msg:
                    if self.spam_protect('rword-' + reaction_word, sender, channel, args, ircSpamProtect=False):
                        continue
                    self.bot.privmsg(channel, REACTION_WORDS[reaction_word].format(**{
                        "sender": sender.nick,
                    }))
            self.update_chatlevels(sender, channel, msg)
#            if channel == MAIN_CHANNEL:
#                self.AeolusMarkov.addLine(msg)

    @irc3.event(irc3.rfc.KICK)
    @asyncio.coroutine
    def on_kick(self, *args, **kwargs):
        kicktarget = kwargs['target']
        global CHATPOINTS_REMOVAL_IF_KICKED
        if not (kicktarget == self.bot.config['nick']):
            self.Chatevents.addEvent('kick', {
                'target' : kicktarget,
                'points' : CHATPOINTS_REMOVAL_IF_KICKED
            })
            self.Chatpoints.updatePointsById(kicktarget, -CHATPOINTS_REMOVAL_IF_KICKED, partial=True)
            self.bot.privmsg(kicktarget, 'You got kicked from {channel} by {nick} with reason "{reason}" and lost up to {p} chatpoints!'.format(**{
                'channel': kwargs.get('channel', '?'),
                'nick': kwargs.get('mask').nick,
                'reason': kwargs.get('data', '?'),
                'p': str(CHATPOINTS_REMOVAL_IF_KICKED),
            }))

    @irc3.event(irc3.rfc.MODE)
    @asyncio.coroutine
    def on_mode(self, *args, **kwargs):
        print('MODE ', args, kwargs)
        """
        MODE  () {'modes': '+b', 'target': '#shadows', 'event': 'MODE', 'mask': 'Washy!Washy@Clk-4A328548.hsi13.unitymediagroup.de', 'data': '*!*@<ip/provider>'}
        -b
        """
        pass

    @asyncio.coroutine
    def __isNickservIdentified(self, nick):
        self.bot.privmsg('nickserv', "status {}".format(nick))
        global NICKSERV_WAIT_TICKS
        remainingTries = NICKSERV_WAIT_TICKS + 0
        while remainingTries > 0:
            if NICKSERVIDENTIFIEDRESPONSES.get(nick):
                value = NICKSERVIDENTIFIEDRESPONSES[nick]
                NICKSERVRESPONSESLOCK.acquire()
                del NICKSERVIDENTIFIEDRESPONSES[nick]
                NICKSERVRESPONSESLOCK.release()
                if int(value) == 3:
                    return True
                return False
            remainingTries -= 1
            yield from asyncio.sleep(0.1)
        return False

    def __handleNickservMessage(self, message):
        message = " ".join(message.split())
        NICKSERVRESPONSESLOCK.acquire()
        if message.startswith('STATUS'):
            words = message.split(" ")
            NICKSERVIDENTIFIEDRESPONSES[words[1]] = words[2]
        NICKSERVRESPONSESLOCK.release()

    """
    @command
    @asyncio.coroutine
    def help(self, mask, target, args):
        "" "Spam protected help

            %%help
        "" "
        if self.spam_protect("help", mask.nick, target, args):
            return
        commands = ["chain", "chainb", "chainf", "chainprob", "rearrange", "chatlvl", "chattip", "chatstats", "chatroulette/cbet"]
        return ", ".join(commands)
        #yield from command.help(args)"""

    @command(permission='admin', show_in_help_list=False, public=False)
    @asyncio.coroutine
    def restart(self, mask, target, args):
        """Restart stuff

            %%restart
        """
        self.on_restart()
        return "Restarted"

    def on_restart(self):
        time.clock()
        t0 = time.clock()
        global TIMERS, VARS, IGNOREDUSERS, DEFAULTC, CDPRIVILEDGEDUSERS, DEFAULTCD, DEFAULTVALUE, ADMINS, REACTION_WORDS
        global CHATLVLWORDS,  CHATLVLEVENTDATA, CHATLVL_TOPPLAYERS, CHATLVL_EPOCH
        ADMINS = [n.split('@')[0].replace('!', '').replace('*', '') for n, v in self.bot.config['irc3.plugins.command.masks'].items() if len(v) > 5]
        DEFAULTCD = self.bot.config.get('spam_protect_time', 600)
        DEFAULTVALUE = self.bot.config.get('default_command_point_requirement', 500)
        self.__dbAdd([], 'ignoredusers', {}, overwriteIfExists=False, save=False)
        self.__dbAdd([], 'cdprivilege', {}, overwriteIfExists=False, save=False)
        for t in ['chain', 'chainprob', 'textchange', 'twitchchain', 'generate', 'chattip', 'chatlvl', 'chatladder',
                  'chatgames', 'chatbet', 'toGroup', 'roast', 'question', 'question-tags', 'spam_cats', 'onjoin',
                  'font', 'rancaps', 'ranspace']:
            self.__dbAdd(['timers'], t, DEFAULTCD, overwriteIfExists=False, save=False)
        for t in ['cmd_chain_points_min', 'cmd_chainf_points_min', 'cmd_chainb_points_min', 'cmd_chain_points_min',
                  'cmd_rancaps_points_min', 'cmd_answer_qpoints_max', 'cmd_bhroast_points_min', 'cmd_rearrange_points_min',
                  'cmd_mgym_points_min', 'cmd_font_points_min',
                  'cmd_ranspace_points_min', 'cmd_ranspace_min_spaces', 'cmd_ranspace_max_spaces']:
            self.__dbAdd(['vars'], t, DEFAULTVALUE, overwriteIfExists=False, save=False)
        self.__dbAdd([], 'chatlvltopplayers', {}, overwriteIfExists=False, save=False)
        self.__dbAdd([], 'chatlvlwords', {}, overwriteIfExists=False, save=False)
        self.__dbAdd(['chatlvlmisc'], 'epoch', 1, overwriteIfExists=False, save=True)
        IGNOREDUSERS = self.__dbGet(['ignoredusers'])
        CHATLVL_TOPPLAYERS = self.__dbGet(['chatlvltopplayers'])
        TIMERS = self.__dbGet(['timers'])
        VARS = self.__dbGet(['vars'])
        CHATLVLWORDS = self.__dbGet(['chatlvlwords'])
        CHATLVLWORDS = self.__dbGet(['chatlvlwords'])
        CDPRIVILEDGEDUSERS = self.__dbGet(['cdprivilege'])
        CHATLVL_EPOCH = self.__dbGet(['chatlvlmisc', 'epoch'])
        REACTION_WORDS = self.__dbGet(['reactionwords', 'words'])
        self.AeolusMarkov = Markov(self, self.bot.config.get('markovwordsstorage_chat', './dbmarkovChat.json'))
        print('loaded aeolus markov, info:', self.AeolusMarkov.getInfo())
        self.ChangelogMarkov = Markov(self, self.bot.config.get('markovwordsstorage_changelog', './dbmarkovChangelogs.json'))
        print('loaded changelog markov, info:', self.ChangelogMarkov.getInfo())
        self.GymMarkov = Markov(self, self.bot.config.get('markovwordsstorage_gym', './dbmarkovGym.json'))
        print('loaded gym markov, info:', self.ChangelogMarkov.getInfo())
        self.Chatpoints = Points(self.bot.config.get('chatlevelstorage', './chatlevel.json'))
        self.Chatevents = Events(self.bot.config.get('chateventstorage', './chatevents.json'))
        self.Chatbets = Bets(self.bot, self.Chatpoints, self.Chatevents, self.bot.config.get('chatmiscstorage', './chatmisc.json'))
        self.Questions = Questions(self.bot, self.Chatpoints, self.Chatevents, self.bot.config.get('questions', './mai/questions.json'))
        self.Chatpoker = {}
        self.ChatpokerPrev = {}
        self.ChatgameTourneys = {}
        self.playerslists = {
            'poker' : self.__dbGet(['playerlists', 'poker'])
        }

        try:
            if self.chatroulettethreads:
                for t in self.chatroulettethreads.keys():
                    t.stop()
            self.timedSavingThread.stop()
            self.twitchthread.stop()
        except:
            pass
        self.chatroulettethreads = {}
        self.timedSavingThread = periodicCallback(self.save, isAsyncioCallback=False,
                                                  args={'path' : 'auto/', 'keep' : 72},
                                                  seconds=self.bot.config.get('autosave', 300))
        self.timedSavingThread.start()
        self.twitchthread = False

        if useLSTM:
            from LSTMGen import LSTMGen
            self.LSTMGen = LSTMGen(self.bot)
        self.TEXT = ""

        t1 = time.clock()
        print("Startup time: {t}".format(**{"t" : format(t1-t0, '.4f')}))

    @command(permission='admin', show_in_help_list=False)
    @asyncio.coroutine
    def join(self, mask, target, args):
        """Overtake the given channel

            %%join <channel>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        self.bot.join(args['<channel>'])

    @command(permission='admin', show_in_help_list=False)
    @asyncio.coroutine
    def leave(self, mask, target, args):
        """Leave the given channel

            %%leave
            %%leave <channel>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        channel = args['<channel>']
        if channel is None:
            channel = target
        self.bot.part(channel)

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def puppet(self, mask, target, args):
        """Puppet

            %%puppet <target> WORDS ...
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        t = args.get('<target>')
        m = " ".join(args.get('WORDS'))
        self.bot.privmsg(t, m)

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def puppeta(self, mask, target, args):
        """Puppet /me

            %%puppeta <target> WORDS ...
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        t = args.get('<target>')
        m = " ".join(args.get('WORDS'))
        print(t, m)
        self.bot.action(t, m)

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def mode(self, mask, target, args):
        """mode

            %%mode <channel> <mode> <nick>
        """
        #if not (yield from self.__isNickservIdentified(mask.nick)):
        #    return
        self.bot.send_line('MODE {} {} {}'.format(
            args.get('<channel>'),
            args.get('<mode>'),
            args.get('<nick>'),
        ), nowait=True)

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def reactionwords(self, mask, target, args):
        """Adds/removes a given keyword from the checklist.
        "{sender}" in the reply text will be replaced by the name of the person who triggered the response.
            %%reactionwords get
            %%reactionwords add <word> REPLY ...
            %%reactionwords del <word>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        global REACTION_WORDS
        add, delete, get, word, reply = args.get('add'), args.get('del'), args.get('get'), args.get('<word>'), " ".join(
            args.get('REPLY'))
        if add:
            try:
                REACTION_WORDS, _, _ = self.__dbAdd(['reactionwords', 'words'], word.lower(), reply)
                return 'Added "{word}" to watched reactionwords with reply: "{reply}"'.format(**{
                    "word": word,
                    "reply": reply,
                })
            except Exception as ex:
                return "Failed adding the word."
        elif delete:
            words = self.__dbGet(['reactionwords', 'words'])
            if words.get(word):
                REACTION_WORDS = self.__dbDel(['reactionwords', 'words'], word)
                return 'Removed "{word}" from watched reactionwords'.format(**{
                    "word": word,
                })
            else:
                return 'Word not found in the list.'
        elif get:
            words = self.__dbGet(['reactionwords', 'words'])
            self.bot.privmsg(mask.nick, str(len(words)) + " checked reactionwords:")
            for word in words.keys():
                self.bot.privmsg(mask.nick, '- word: "%s", reply: %s' % (word, words[word]))

    @command(show_in_help_list=False, public=False)
    @asyncio.coroutine
    def list(self, mask, target, args):
        """List <count> people in channel, starting at <offset>

            %%list <channel> <offset> <count>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        channel, offset, count = args['<channel>'], int(args['<offset>']), int(args['<count>'])
        channellist = sorted([user for user in self.bot.channels[channel]])
        channellist.pop(0)
        if offset > len(channellist):
            self.bot.privmsg(mask.nick, "Offset > amount of people in channel ({total})".format(**{
                "total" : str(len(channellist)),
            }))
            return
        NAMES_PER_PM = 30
        self.bot.privmsg(mask.nick, "Listing {count} of {total} people in {channel}:".format(**{
                "count" : str(min([count,len(channellist)])),
                "total" : str(len(channellist)),
                "channel" : channel,
            }))
        i = offset
        while True:
            self.bot.privmsg(mask.nick, ", ".join(channellist[i:min([i + NAMES_PER_PM, len(channellist), offset + count])]))
            i += NAMES_PER_PM
            if i >= offset + count:
                break

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def twitchjoin(self, mask, target, args):
        """Join given twitch channel

            %%twitchjoin <channel>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        self.createTwitchConIfNecessary()
        self.twitchthread.join(args.get('<channel>'))

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def twitchleave(self, mask, target, args):
        """Leave given twitch channel

            %%twitchleave <channel>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        self.createTwitchConIfNecessary()
        self.twitchthread.leave(args.get('<channel>'))

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def twitchstop(self, mask, target, args):
        """Ends all twitch connections

            %%twitchstop
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        if self.twitchthread:
            self.twitchthread.stop()
        self.twitchthread = False

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def twitchmsg(self, mask, target, args):
        """Write to the given twitch channel

            %%twitchmsg <channel> TEXT ...
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        self.createTwitchConIfNecessary()
        #self.twitchthread.join(args.get('<channel>'))
        self.twitchthread.message(args.get('<channel>'), " ".join(args.get('TEXT')))

    def createTwitchConIfNecessary(self):
        if not self.twitchthread:
            self.twitchthread = twitchThread(self.bot, self, self.AeolusMarkov)
            self.twitchthread.start()

    @command(permission='admin', show_in_help_list=False, public=False)
    @asyncio.coroutine
    def files(self, mask, target, args):
        """ To read files, no abuse please

            %%files get
            %%files parse log <chat/changelog/gym> <filename>
            %%files parse raw <chat/changelog/gym> <filename>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        get, parse, log, raw, filename, chatchangelog = args.get('get'), args.get('parse'), args.get('log'), args.get('raw'), args.get('<filename>'), args.get('<chat/changelog/gym>')
        if get:
            for dirname, dirnames, filenames in os.walk('./files'):
                for filename in filenames:
                    self.bot.privmsg(mask.nick, ' - ' + filename)
        if parse:
            try:
                filename = "./files/" + filename
                filetype = "LOG"
                if raw:
                    filetype = "RAW"
                if chatchangelog == "chat":
                    self.AeolusMarkov.addFile(filename, filetype=filetype)
                elif chatchangelog == "changelog":
                    self.ChangelogMarkov.addFile(filename, filetype=filetype)
                elif chatchangelog == "gym":
                    self.GymMarkov.addFile(filename, filetype=filetype)
                else:
                    self.bot.privmsg(mask.nick, '<chat/changelog/gym> needs to be either "chat" or "changelog" or "gym".')
                self.bot.privmsg(mask.nick, 'Succeeded parsing. Use !savedb to save progress.')
            except Exception:
                print(traceback.format_exc())
                self.bot.privmsg(mask.nick, 'Failed parsing.')

    @command(permission='admin', show_in_help_list=False)
    @asyncio.coroutine
    def cd(self, mask, target, args):
        """ Set cooldowns

            %%cd get
            %%cd get <timer>
            %%cd set <timer> <time>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        get, set, timer, time = args.get('get'), args.get('set'), args.get('<timer>'), args.get('<time>')
        global TIMERS, DEFAULTCD
        if get:
            if timer:
                self.bot.privmsg(mask.nick, 'The cooldown for "' + timer + '" is set to ' + str(TIMERS.get(timer, DEFAULTCD)))
            else:
                for key in TIMERS.keys():
                    self.bot.privmsg(mask.nick, 'The cooldown for "' + key + '" is set to ' + str(TIMERS.get(key, DEFAULTCD)))
        if set:
            TIMERS[timer] = int(time)
            self.__dbAdd(['timers'], timer, TIMERS[timer], save=True)
            self.bot.privmsg(mask.nick, 'The cooldown for "' + timer + '" is now changed to ' + str(TIMERS[timer]))

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def vars(self, mask, target, args):
        """ Set vars, mostly point requirements

            %%vars get
            %%vars get <var>
            %%vars set <var> <value>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        get, set, var, value = args.get('get'), args.get('set'), args.get('<var>'), args.get('<value>')
        global VARS, DEFAULTVALUE
        if get:
            if var:
                self.bot.privmsg(mask.nick, 'The value for "' + var + '" is set to ' + str(VARS.get(var, DEFAULTVALUE)))
            else:
                for key in VARS.keys():
                    self.bot.privmsg(mask.nick, 'The value for "' + key + '" is set to ' + str(VARS.get(key, DEFAULTVALUE)))
        if set:
            VARS[var] = int(value)
            self.__dbAdd(['vars'], var, VARS[var], save=True)
            self.bot.privmsg(mask.nick, 'The value for "' + var + '" is now changed to ' + str(VARS[var]))

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def savedb(self, mask, target, args):
        """ Saves to the db, takes a while, no abuse please

            %%savedb
            %%savedb all
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        all = args.get('all')
        t0 = time.clock()
        args = {
            'saveAeolusMarkov' : all,
            'saveChangelogMarkov' : all,
            'saveGymMarkov' : all,
            'path' : 'manual/',
            'keep' : 5,
        }
        self.save(args)
        t1 = time.clock()
        self.bot.privmsg(mask.nick, "Saving completed. ({t} seconds)".format(**{"t" : format(t1-t0, '.4f')}))

    def save(self, args={}):
        self.__dbSave()
        path = './backups/'+args.get('path', '')
        pathFull = path+str(int(time.time()))+"/"
        os.makedirs(pathFull, exist_ok=True)
        for obj in [self.Chatpoints, self.Chatevents, self.Chatbets]:
            obj.save()
            shutil.copy2("./"+obj.getFilePath(), pathFull)
        allRelevantBackups = [d[0] for d in os.walk(path)]
        for i in range(1, len(allRelevantBackups) - args.get('keep', 10)):
            shutil.rmtree(allRelevantBackups[i])
        if args.get('saveAeolusMarkov', False):
            self.AeolusMarkov.save()
        if args.get('saveChangelogMarkov', False):
            self.ChangelogMarkov.save()
        if args.get('saveGymMarkov', False):
            self.GymMarkov.save()
        return True

    def chatreset(self):
        # TODO while chatgames?
        global CHATLVL_EPOCH
        self.save(args = {
            'path' : 'reset/'+str(CHATLVL_EPOCH)+'/',
            'keep' : 100000,
        })
        self.Chatpoints.reset()
        self.Chatevents.reset()
        self.Chatbets.reset()
        CHATLVL_EPOCH += 1
        self.save(args = {
            'path' : 'post-reset/',
            'keep' : 5,
        })
        self.__dbAdd(['chatlvlmisc'], 'epoch', CHATLVL_EPOCH, overwriteIfExists=True, save=True)

    @command(permission='admin')
    @asyncio.coroutine
    def ignore(self, mask, target, args):
        """ Change the ignore list

            %%ignore get
            %%ignore add TEXT ...
            %%ignore del <ID>
        """
        response = self.__genericCommandManage(mask, target, args, ['ignoredusers'])
        global IGNOREDUSERS
        IGNOREDUSERS = self.__dbGet(['ignoredusers'])
        return response

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def cdprivilege(self, mask, target, args):
        """ Change the cdprivilege list, which shortens individual cooldowns

            %%cdprivilege get
            %%cdprivilege add <name> <time>
            %%cdprivilege del <name>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        add, delete, get, t, name = args.get('add'), args.get('del'), args.get('get'), args.get('<time>'), args.get('<name>')
        global CDPRIVILEDGEDUSERS
        if add:
            try:
                CDPRIVILEDGEDUSERS, _, _ = self.__dbAdd(['cdprivilege'], name, int(t), save=True)
                return "Added"
            except:
                return "Failed"
        if get:
            self.bot.privmsg(mask.nick, str(len(CDPRIVILEDGEDUSERS)) + " users:")
            for id in CDPRIVILEDGEDUSERS.keys():
                self.bot.privmsg(mask.nick, '%s: %s' % (id, CDPRIVILEDGEDUSERS[id]))
        if delete:
            CDPRIVILEDGEDUSERS = self.__dbDel(['cdprivilege'], name, save=True)
            return "Removed"

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def chatlvlwords(self, mask, target, args):
        """ Change the cdprivilege list, which shortens individual cooldowns

            %%chatlvlwords get
            %%chatlvlwords add <points> TEXT ...
            %%chatlvlwords addm <points> TEXT ...
            %%chatlvlwords del TEXT ...
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        add, addm, delete, get, points, text = args.get('add'), args.get('addm'), args.get('del'), args.get('get'), args.get('<points>'), " ".join(args.get('TEXT'))
        global CHATLVLWORDS
        if add or addm:
            try:
                p = int(points)
                if addm:
                    p *= -1
                CHATLVLWORDS, _, _ = self.__dbAdd(['chatlvlwords'], text, p, save=False)
                return "Added"
            except:
                return "Failed"
        if get:
            self.bot.privmsg(mask.nick, str(len(CHATLVLWORDS)) + " words:")
            words = ['"%s": %s' % (id, CHATLVLWORDS[id]) for id in CHATLVLWORDS.keys()]
            self.bot.privmsg(mask.nick, ', '.join(words))
        if delete:
            CHATLVLWORDS = self.__dbDel(['chatlvlwords'], text, save=False)
            return "Removed"

    @command()
    @asyncio.coroutine
    def poker(self, mask, target, args):
        """Join the poker community

            %%poker
        """
        if not self.playerslists.get('poker', {}).get(mask.nick, False):
            self.__dbAdd(['playerlists', 'poker'], mask.nick, True, save=True)
            self.playerslists = self.__dbGet(['playerlists'])
            self.bot.privmsg(mask.nick, "Welcome in the poker community!")

    @command()
    @asyncio.coroutine
    def unpoker(self, mask, target, args):
        """Leave the poker community

            %%unpoker
        """
        if self.playerslists.get('poker', {}).get(mask.nick, False):
            self.__dbDel(['playerlists', 'poker'], mask.nick, save=True)
            self.playerslists = self.__dbGet(['playerlists'])
            self.bot.privmsg(mask.nick, "Too bad you're leaving!")

    @command()
    @asyncio.coroutine
    def to(self, mask, target, args):
        """Inform your fellow players of important events

            %%to poker
            %%to TEXT ...
        """
        poker = args.get('poker')
        if not poker:
            return
        if not self.playerslists.get('poker', {}).get(mask.nick, False):
            self.bot.privmsg(mask.nick, "Only people on the poker list may use this command!")
            return
        if self.spam_protect('toGroup', mask, target, args, specialSpamProtect='toGroup'):
            return
        inChannel = self.__filterForPlayersInChannel(self.playerslists.get('poker', {}), target)
        viablePlayers = []
        requiredPoints = 0
        if self.Chatpoker.get(target, False):
            requiredPoints = self.Chatpoker[target].getMaxPoints()
        for name in inChannel:
            if self.Chatpoints.getById(name).get('p', 0) >= requiredPoints:
                viablePlayers.append(name)
        if len(viablePlayers) > 0:
            self.bot.privmsg(target, "Join poker! " + ", ".join(viablePlayers))
        else:
            self.bot.privmsg(target, "Nobody to join :(")

    @command()
    @asyncio.coroutine
    def rearrange(self, mask, target, args):
        """Rearrange letters in words

            %%rearrange TEXT ...
        """
        hp, _ = self.has_permissions(mask.nick,
                                     irc_msg_responses=True,
                                     all=[('chatpoints_min', VARS.get('cmd_rearrange_points_min', DEFAULTVALUE))],
                                     any=[('bot_admin', 0), ('is_in_top5', 0)])
        if not hp:
            return
        if self.spam_protect('textchange', mask, target, args, specialSpamProtect='rearrange'):
            return
        words = args.get('TEXT')
        for i in range(0,len(words)):
            if len(words[i]) > 2 and (not self.isInChannel(words[i], target)):
                w = words[i]
                wh = w[1:len(w)-1]
                words[i] = w[0] + ''.join(random.sample(wh, len(wh))) + w[len(w)-1]
        self.bot.privmsg(target, " ".join(words))

    @command()
    @asyncio.coroutine
    def bhroast(self, mask, target, args):
        """Roast Blackheart with original comments from e.g. youtube!
           (Name might contain a . to avoid pinging)

            %%bhroast
        """
        if self.spam_protect('roast', mask, target, args, specialSpamProtect='roast'):
            return
        hp, _ = self.has_permissions(mask.nick,
                                     irc_msg_responses=True,
                                     all=[('chatpoints_min', VARS.get('cmd_bhroast_points_min', DEFAULTVALUE))],
                                     any=[('bot_admin', 0), ('is_in_top5', 0)])
        if hp:
            self.bot.privmsg(target, "%s" % random.choice(BHROASTS))

    @command()
    @asyncio.coroutine
    def question(self, mask, target, args):
        """

            %%question tags
            %%question abandon
            %%question
            %%question TAGS...
        """
        get_tags, tags, abandon = args.get('tags'), args.get('TAGS'), args.get('abandon')
        global MAIN_CHANNEL
        if not target == MAIN_CHANNEL:
            return
        if get_tags:
            if self.spam_protect('question-tags', mask, target, args, specialSpamProtect='question-tags'):
                return
            self.Questions.get_tags(mask.nick, target)
        elif abandon:
            self.Questions.abandon_question(mask.nick, target)
        else:
            if not self.spam_protect('question', mask, target, args, specialSpamProtect='question', updateTimer=False):
                if self.Questions.question(mask.nick, target, tags=tags):
                    self.spam_protect('question', mask, target, args, specialSpamProtect='question')

    @command()
    @asyncio.coroutine
    def answer(self, mask, target, args):
        """

            %%answer TEXT ...
        """
        global MAIN_CHANNEL
        if not target == MAIN_CHANNEL:
            return
        hp, _ = self.has_permissions(mask.nick,
                                     irc_msg_responses=True,
                                     all=[('questionpoints_max', VARS.get('cmd_answer_qpoints_max', DEFAULTVALUE))],
                                     any=[('bot_admin', 0)])
        if not hp:
            return
        if self.Questions.answer(mask.nick, target, args.get('TEXT')):
            self.spam_protect('question', mask, target, args, specialSpamProtect='question', setToNow=True)
            self.save(args={
                'path' : 'questions/',
                'keep' : 1,
            })

    @command()
    @asyncio.coroutine
    def rancaps(self, mask, target, args):
        """Rearrange letters in words

            %%rancaps TEXT ...
        """
        hp, _ = self.has_permissions(mask.nick,
                                     irc_msg_responses=True,
                                     all=[('chatpoints_min', VARS.get('cmd_rancaps_points_min', DEFAULTVALUE))],
                                     any=[('bot_admin', 0), ('is_in_top5', 0)])
        if not hp:
            return
        if self.spam_protect('textchange', mask, target, args, specialSpamProtect='rancaps'):
            return
        text = " ".join(args.get('TEXT'))
        text = text.lower()
        rtext = ""
        for l in text:
            if random.random() < 0.5:
                rtext += l
            else:
                rtext += l.capitalize()
        self.bot.privmsg(target, rtext)

    @command()
    @asyncio.coroutine
    def ranspace(self, mask, target, args):
        """Add spaces between letters in words

            %%ranspace TEXT ...
        """
        if self.is_main_channel(mask, target, irc_pm_if_channel=True):
            return
        hp, _ = self.has_permissions(mask.nick,
                                     irc_msg_responses=True,
                                     all=[('chatpoints_min', VARS.get('cmd_ranspace_points_min', DEFAULTVALUE))],
                                     any=[('bot_admin', 0), ('is_in_top5', 0)])
        if not hp:
            return
        if self.spam_protect('textchange', mask, target, args, specialSpamProtect='ranspace'):
            return
        text = "".join(args.get('TEXT'))
        rtext = ""
        min_spaces, max_spaces = VARS.get('cmd_ranspace_min_spaces', 0), VARS.get('cmd_ranspace_max_spaces', 2)
        for l in text:
            rtext += " "*random.randint(min_spaces, max_spaces) + l
        self.bot.privmsg(target, rtext)

    @command(permission='admin')
    @asyncio.coroutine
    def font(self, mask, target, args):
        """Change the font!

            %%font TEXT ...
        """
        if self.is_main_channel(mask, target, irc_pm_if_channel=True):
            return
        hp, _ = self.has_permissions(mask.nick,
                                     irc_msg_responses=True,
                                     all=[('chatpoints_min', VARS.get('cmd_font_points_min', DEFAULTVALUE))],
                                     any=[('bot_admin', 0), ('is_in_top5', 0)])
        if not hp:
            return
        if self.spam_protect('textchange', mask, target, args, specialSpamProtect='font'):
            return
        text = " ".join(args.get('TEXT'))
        text = text.lower()
        std_font = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z']
        jap_font = ['卂', '乃', '匚', '刀', '乇', '下', '厶', '卄', '工', '丁', '长', '乚', '从', '𠘨', '口', '尸', '㔿', '尺', '丂', '丅', '凵', 'リ', '山', '乂', '丫', '乙']
        font = dict(zip(std_font, jap_font))
        rtext = "".join([font.get(l, l) for l in text])
        self.bot.privmsg(target, rtext)

    @command()
    @asyncio.coroutine
    def changelog(self, mask, target, args):
        """ See what the future will bring

            %%changelog
        """
        if self.spam_protect('changelog', mask, target, args, specialSpamProtect='changelog'):
            return
        self.bot.privmsg(target, self.ChangelogMarkov.forwardSentence(False, 30, target, includeWord=True))

    @command()
    @asyncio.coroutine
    def mgym(self, mask, target, args):
        """ Top gym quotes, all legit!

            %%mgym
        """
        if self.spam_protect('mgym', mask, target, args, specialSpamProtect='mgym'):
            return
        hp, _ = self.has_permissions(mask.nick,
                                     irc_msg_responses=True,
                                     all=[('chatpoints_min', VARS.get('cmd_mgym_points_min', DEFAULTVALUE))],
                                     any=[('bot_admin', 0), ('is_in_top5', 0)])
        if not hp:
            return
        self.bot.privmsg(target, self.GymMarkov.forwardSentence(False, 30, target, includeWord=True))

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def chainadmin(self, mask, target, args):
        """ Manage chains

            %%chainadmin del <word>
            %%chainadmin disable <word>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        if args.get("del"):
            done = self.AeolusMarkov.delWord(args.get("<word>", ""))
            if done: return "Deleted"
            return "Failed to delete"
        if args.get("disable"):
            self.AeolusMarkov.disableWord(args.get("<word>", ""))
            return "Disabled the word."

    @command()
    @asyncio.coroutine
    def chain(self, mask, target, args):
        """ Chain words both directions <3

            %%chain <word>
        """
        hp, _ = self.has_permissions(mask.nick,
                                     irc_msg_responses=True,
                                     all=[('chatpoints_min', VARS.get('cmd_chain_points_min', DEFAULTVALUE))],
                                     any=[('bot_admin', 0), ('is_in_top5', 0)])
        if not hp:
            return
        if self.spam_protect('chain', mask, target, args, specialSpamProtect='chain'):
            return
        #l = 30
        #lf = random.randint(MINCHAINLENGTH/2, l - MINCHAINLENGTH/2)
        #lb = l - lf
        word = args.get('<word>', False)
        forward = self.AeolusMarkov.forwardSentence(word, 20, target, includeWord=False)
        backward = self.AeolusMarkov.backwardSentence(word, 20, target, includeWord=True)
        self.bot.privmsg(target, backward + forward)

    if useLSTM:
        @command(public=False)
        @asyncio.coroutine
        def generate(self, mask, target, args):
            """ Generate a text based on LSTMs

                %%generate
                %%generate TEXT ...
            """
            if self.spam_protect('generate', mask, target, args, specialSpamProtect='generate'):
                return
            text =  " ".join(args.get('TEXT'))
            if text:
                self.__addText(text)
            gen = self.LSTMGen.generate(self.TEXT, 0.4, 100)
            self.bot.privmsg(target, gen)

    @command()
    @asyncio.coroutine
    def chainf(self, mask, target, args):
        """ Chain words forwards <3

            %%chainf <word>
        """
        hp, _ = self.has_permissions(mask.nick,
                                     irc_msg_responses=True,
                                     all=[('chatpoints_min', VARS.get('cmd_chainf_points_min', DEFAULTVALUE))],
                                     any=[('bot_admin', 0), ('is_in_top5', 0)])
        if not hp:
            return
        if self.spam_protect('chain', mask, target, args, specialSpamProtect='chain'):
            return
        word = args.get('<word>', False)
        self.bot.privmsg(target, self.AeolusMarkov.forwardSentence(word, 30, target, includeWord=True))

    @command()
    @asyncio.coroutine
    def chainb(self, mask, target, args):
        """ Chain words backwards <3

            %%chainb <word>
        """
        hp, _ = self.has_permissions(mask.nick,
                                     irc_msg_responses=True,
                                     all=[('chatpoints_min', VARS.get('cmd_chainb_points_min', DEFAULTVALUE))],
                                     any=[('bot_admin', 0), ('is_in_top5', 0)])
        if not hp:
            return
        if self.spam_protect('chain', mask, target, args, specialSpamProtect='chain'):
            return
        word = args.get('<word>', False)
        self.bot.privmsg(target, self.AeolusMarkov.backwardSentence(word, 30, target, includeWord=True))

    @command()
    @asyncio.coroutine
    def chainprob(self, mask, target, args):
        """ Retrieve the probability of words in order

            %%chainprob <word1> [<word2>]
        """
        if self.spam_protect('chainprob', mask, target, args, specialSpamProtect='chainprob'):
            return
        w1, w2 = args.get('<word1>'), args.get('<word2>')
        self.bot.privmsg(target, self.AeolusMarkov.chainprob(w1, w2))

    def update_chatlevels(self, mask, channel, msg):
        if msg.startswith('!'):
            return
        global CHATLVLWORDS, MAIN_CHANNEL, POKER_CHANNEL
        points, text = 0, msg.lower()
        for word in CHATLVLWORDS.keys():
            if word in text:
                points += CHATLVLWORDS[word]
        # wordcount = len(text.split())
        lettercount = len(text.replace(" ", ""))
        points += 0.1 * lettercount
        if channel in self.__dbGet(['chatlvlchannels']).values():
            self.Chatpoints.updatePointsById(mask.nick, points)
        if channel.startswith('#'):
            self.Chatpoints.updatePointsById(channel, points)

    def update_chatlvl(self, name, channel, points, addChangeTo=False):
        return self.Chatpoints.updatePointsById(name, points)

    def __chatLevelAndPoints(self, points):
        level = 1
        req = self.Chatpoints.getPointsForLevelUp(level)
        while points >= req:
            level += 1
            req = self.Chatpoints.getPointsForLevelUp(level)
        return level, points

    @command()
    @asyncio.coroutine
    def chatlvl(self, mask, target, args):
        """ Display chatlvl + points

            %%chatlvl [<name>]
        """
        location = target
        if self.spam_protect('chatlvl', mask, target, args, specialSpamProtect='chatlvl', ircSpamProtect=False):
            if location == MAIN_CHANNEL:
                location = mask.nick
        if not location.startswith("#"):
            location = mask.nick
        name = args.get('<name>', False)
        if not name:
            name = mask.nick
        data = self.Chatpoints.getPointDataById(name)
        tipstring, roulettestring, pokerstring = "", "", ""
        additions = ""
        if data.get('chattip', False):
            additions += ", " + format(data.get('chattip'), '.1f') + " from tips"
        if data.get('chatpoker', False):
            additions += ", " + format(data.get('chatpoker'), '.1f') + " from poker"
        if data.get('chatroulette', False):
            additions += ", " + format(data.get('chatroulette'), '.1f') + " from roulette"
        if data.get('questions', False):
            additions += ", " + format(data.get('questions'), '.1f') + " from questions"
        self.bot.privmsg(location, "{object}'s points: {total}, level {level}, {toUp} to next level{additions}".format(**{
                "object": name,
                "level": str(data.get('level', 1)),
                "points": format(data.get('points', 1), '.1f'),
                "toUp": format(data.get('tonext', 1), '.1f'),
                "total": format(data.get('p', 1), '.1f'),
                "additions": additions
            }))

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def chattipadmin(self, mask, target, args):
        """ Tip some chatlvl points to someone <3

            %%chattipadmin <channel> <giver> <name> [<points/all>]
        """
        yield from self.chattip(mask, target, args)

    @command()
    @asyncio.coroutine
    def chattip(self, mask, target, args):
        """ Tip some chatlvl points to someone <3

            %%chattip <name> [<points/all>]
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        global CHATLVL_COMMANDLOCK, CHATLVL_RESETNAME, CHATLVL_NORESETNAME, CHATLVL_RESETCOUNT, CHATLVL_NORESETDISCOUNT
        CHATLVL_COMMANDLOCK.acquire()
        self.debugPrint('commandlock acquire chattip')
        channel = target
        if self.spam_protect('chattip', mask, target, args, specialSpamProtect='chattip', ircSpamProtect=False):
            channel = mask.nick
        takername, points = args.get('<name>', False), args.get('<points/all>')
        givername = mask.nick
        if args.get('chattipadmin', False):
            givername = args.get('<giver>')
            channel = args.get('<channel>', channel)
        """
        if takername in IGNOREDUSERS.values():
            self.bot.privmsg(mask.nick, "This user is on the ignore list and can not be tipped.")
            return
        """
        if not points:
            points = 5
        try:
            if not points == 'all':
                points = abs(int(points))
        except:
            self.bot.action(channel, "Failed to send points! Are you sure you gave me a number?")
            CHATLVL_COMMANDLOCK.release()
            self.debugPrint('commandlock release chattip 1')
            return
        _, points = self.Chatpoints.transferPointsByIdsSimple(takername, givername, points, partial=True, addTo='chattip')
        if points < 1:
            CHATLVL_COMMANDLOCK.release()
            self.debugPrint('commandlock release chattip 2')
            return
        self.Chatevents.addEvent('chattip', {
            'giver' : givername,
            'taker' : takername,
            'points' : points,
        })
        addstring = ""
        if takername in [CHATLVL_RESETNAME, CHATLVL_NORESETNAME]:
            p = self.Chatpoints.getPointsById(CHATLVL_RESETNAME)
            rp = self.Chatpoints.getPointsById(CHATLVL_NORESETNAME) * CHATLVL_NORESETDISCOUNT
            resetNeeded = CHATLVL_RESETCOUNT + rp
            addstring = "{p} of {max} points for a reset collected!".format(**{
                "p": format(p, '.1f'),
                "max": str(resetNeeded),
            })
            channel = target
            if takername == CHATLVL_NORESETNAME:
                addstring = "Reset delayed! " + addstring
            elif (takername == CHATLVL_RESETNAME) and (p > resetNeeded):
                addstring = "Enough points to reset collected! RESETTING NOW!"
                self.chatreset()
        self.bot.action(channel, "{giver} tipped {p} points to {taker}! {add}".format(**{
                "giver": givername,
                "p": format(points, '.1f'),
                "taker": takername,
                "add": addstring,
            }))
        CHATLVL_COMMANDLOCK.release()
        self.debugPrint('commandlock release chattip eof')

    @command(public=False)
    @asyncio.coroutine
    def chattipinfo(self, mask, target, args):
        """ Info about chattips!

            %%chattipinfo <name>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        hp, _ = self.has_permissions(mask.nick,
                                     irc_msg_responses=True,
                                     all=[('is_in_top5', 0)],
                                     any=[('bot_admin', 0)])
        if hp:
            name = args.get('<name>', '')
            data = self.Chatevents.getFormattedChattips('chattip', name)
            sorted_data = sorted([(n, v) for n, v in data.items()], reverse=True, key=lambda x: x[1])
            self.bot.privmsg(mask.nick, 'Chattips of %s. Values >0 indicate that the player received tips from that person' % name)
            self.bot.privmsg(mask.nick, '; '.join(['%s: %i' % (n, v) for n, v in sorted_data if v != 0]))

    @asyncio.coroutine
    def __maskToFafId(self, mask):
        return mask.nick, True #TODO
        try:
            return str(mask.host.split('@')[0]), True
        except:
            return "-1", False

    @asyncio.coroutine
    def __nameToFafId(self, name):
        return name, True #TODO
        global MAIN_CHANNEL, CHATLVLS
        if name.startswith('#'):
            return name, True
        if not self.isInChannel(name, MAIN_CHANNEL):
            for v in CHATLVLS.keys():
                if CHATLVLS[v].get('n', False) == name:
                    #print('not in main, but v:', v)
                    return v, True
            return "-1", False
        whois = yield from self.whois(nick=name)
        return whois.get('username', False), whois.get('timeout', True) == False

    @command()
    @asyncio.coroutine
    def chatladder(self, mask, target, args):
        """ The names of the top ladder warriors

            %%chatladder
            %%chatladder all
            %%chatladder tip [rev]
            %%chatladder roulette [rev]
            %%chatladder poker [rev]
            %%chatladder questions
        """
        tip, roulette, poker, questions = args.get('tip'), args.get('roulette'), args.get('poker'), args.get('questions')
        rev, all = args.get('rev', False), args.get('all', False)
        if self.spam_protect('chatladder', mask, target, args, specialSpamProtect='chatladder'):
            return
        global CHATLVLS, CHATLVL_TOPPLAYERS
        ladder = []
        announceString = ""
        individualString = ""
        default = False
        if tip:
            ladder = self.Chatpoints.getSortedBy(by='chattip', reversed=(not rev))
            announceString = "Top tip receivers (received-sent): {list}"
            if rev:
                announceString = "Top tip givers (received-sent): {list}"
            individualString = "{name} with {chattip} points"
        elif roulette:
            ladder = self.Chatpoints.getSortedBy(by='chatroulette', reversed=(not rev))
            announceString = "Top roulette winners (won-lost): {list}"
            if rev:
                announceString = "Unlucky roulette players (won-lost): {list}"
            individualString = "{name} with {chatroulette} points"
        elif poker:
            ladder = self.Chatpoints.getSortedBy(by='chatpoker', reversed=(not rev))
            announceString = "Successful poker players (won-lost): {list}"
            if rev:
                announceString = "Unsuccessful poker players (won-lost): {list}"
            individualString = "{name} with {chatpoker} points"
        elif questions:
            ladder = self.Chatpoints.getSortedBy(by='questions', reversed=True)
            announceString = "Successful question snipers: {list}"
            individualString = "{name} with {questions} points"
        else:
            default = True
            ladder = self.Chatpoints.getSortedBy(by='p', reversed=True)
            announceString = "Top chatwarriors: {list}"
            individualString = "{name} (level {level})"
        announcePlayers = []
        top5 = {}
        announced = 0
        for i in range(len(ladder)):
            playerdata = self.Chatpoints.getPointDataById(ladder[i][0])
            name = playerdata.get('n','-')
            if all or (not (name.startswith('#') or name in IGNOREDUSERS.values())):
                announcePlayers.append(individualString.format(**{
                    "name": self.getUnpingableName(playerdata.get('n','-')),
                    "level": playerdata.get('level', 0),
                    "chattip": format(playerdata.get('chattip', 0), '.1f'),
                    "chatroulette": format(playerdata.get('chatroulette', 0), '.1f'),
                    "chatpoker": format(playerdata.get('chatpoker', 0), '.1f'),
                    "questions": format(playerdata.get('questions', 0), '.0f'),
                }))
                announced += 1
                top5[name] = announced
                if announced >= 5:
                    break
        if default and not all:
            CHATLVL_TOPPLAYERS = top5
            self.__dbAdd([], 'chatlvltopplayers', CHATLVL_TOPPLAYERS, overwriteIfExists=True, trySavingWithNewKey=False, save=True)
        self.bot.privmsg(target, announceString.format(**{
                "list": ", ".join(announcePlayers),
            }))

    @command()
    @asyncio.coroutine
    def chattourney(self, mask, target, args):
        """ The names of the top ladder warriors

            %%chattourney [<channel>]
            %%chattourney <channel> join
            %%chattourney <channel> leave
        """
        channel, join, leave = args.get('<channel>'), args.get('join'), args.get('leave')
        # use ladder spam protect key for now i guess
        if self.spam_protect('chatladder', mask, target, args, specialSpamProtect='chatladder'):
            return
        if not channel:
            tourneys = ["{} ({})".format(k, self.ChatgameTourneys[k].get('type', '')) for k in self.ChatgameTourneys.keys()]
            if len(tourneys) == 0:
                self.bot.privmsg(target, "There are no running chat tourneys!")
                return
            self.bot.privmsg(target, "Running chat tourneys: {}".format(", ".join(tourneys)))
        else:
            tourneydata = self.ChatgameTourneys.get(channel, False)
            if not tourneydata:
                self.bot.privmsg(target, "No tourney is going on there!")
                return
            if join:
                if not tourneydata.get('joinable', False):
                    self.bot.privmsg(channel, "It's not possible to join the tourney anymore!")
                elif self.__tourneyAdd(mask.nick, channel):
                    self.bot.privmsg(channel, "{} joined the tourney!".format(mask.nick))
                else:
                    self.bot.privmsg(mask.nick, "Joining failed! You're probably already signed up!")
            elif leave:
                if self.__tourneyRemove(mask.nick, channel):
                    self.bot.privmsg(channel, "{} left the tourney!".format(mask.nick))
                else:
                    self.bot.privmsg(mask.nick, "Leaving failed! Are you even in the tourney?")
            else:
                ladder = self.Chatpoints.getSortedByMultiple(byPositive=[tourneydata['pointkey'], tourneydata['pointreservedkey']], reversed=True)
                ladderstringsIn = []
                ladderstringsOut = []
                for name, points in ladder:
                    if points <= 0:
                        break
                    elif points <= tourneydata['minpoints']:
                        ladderstringsOut.append("{name}".format(**{
                            'name' : self.getUnpingableName(name),
                            'points' : format(points, '.1f'),
                        }))
                    else:
                        ladderstringsIn.append("{name} ({points}p)".format(**{
                            'name' : self.getUnpingableName(name),
                            'points' : format(points, '.1f'),
                        }))
                self.bot.privmsg(target, "A {type} tourney is running, currently requires {points}p per game, participants: [{participants}], out: [{out}]".format(**{
                    'points' : tourneydata['minpoints'],
                    'type' : tourneydata.get('type', ''),
                    'participants' : ', '.join(ladderstringsIn),
                    'out' : ', '.join(ladderstringsOut),
                }))
        pass

    @command()
    @asyncio.coroutine
    def chatstats(self, mask, target, args):
        """ The names of the top ladder warriors

            %%chatstats roulette [<name>]
            %%chatstats roulette minplayers <playercount>
            %%chatstats poker [<name>]
            %%chatstats poker minplayers <playercount>
            %%chatstats poker winningtype <fold/highest/2/2pair/3/straight/flush/fh/4/sflush/rsflush>
            %%chatstats questions
        """
        roulette, poker, questions = args.get('roulette'), args.get('poker'),args.get('questions')
        minplayers, name, playercount = args.get('minplayers'), args.get('<name>'), args.get('<playercount>')
        channel = target
        if self.spam_protect('chatstats', mask, target, args, specialSpamProtect='chatstats'):
            channel = mask.nick
        try:
            playercount = int(playercount)
        except:
            playercount = 2
        if roulette:
            data = self.Chatevents.getFormattedRouletteData('chatroulette', name, playercount)
            if len(data) < 1:
                return "There are no games to talk about!"
            data['hwinner'] = self.getUnpingableName(self.Chatpoints.getById(data['hwinner'])['n'])
            data['roiwinner'] = self.getUnpingableName(self.Chatpoints.getById(data['roiwinner'])['n'])
            self.bot.action(channel, "Chatroulette stats! Total games: {count}, total points bet: {totalpoints}, average points per game: {avg}, "\
                                    "highest stake game: {hpoints} points won by {hwinner}, "\
                                    "highest ROI game: (R={roiwin}; I={roibet}, ratio={roiratio}) by {roiwinner}".format(**data))
            return
        if poker:
            winningtype = False
            if args.get('winningtype'):
                winningtype = Poker.getSimpleCardEvalToNumber().get(args.get('<fold/highest/2/2pair/3/straight/flush/fh/4/sflush/rsflush>'), False)
            data = self.Chatevents.getFormattedPokerData('chatpoker', name, playercount, winningtype)
            if len(data) < 1:
                return "There are no games to talk about!"
            #data['hwinners'] = ", ".join([self.getUnpingableName(self.Chatpoints.getById(name)['n']) for name in data['hwinners']])    # to be used once ids are saved rather than names
            data['hwinners'] = ", ".join([self.getUnpingableName(name) for name in data['hwinners']])
            self.bot.action(channel, "Chatpoker stats! Total games: {count}, total points: {totalpoints}, average points per game: {avg}, "\
                                    "highest stake game: {hpoints} points won by {hwinners}".format(**data))
            return
        if questions:
            data = self.Chatevents.getFormattedQuestionData('question')
            if len(data) < 1:
                return "There are no stats to talk about!"
            self.bot.action(channel, "Questions stats! Total games: {count}, total points: {totalpoints}, "\
                                     "average points per game: {avg}".format(**data))
            return

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def chatlvlpoints(self, mask, target, args):
        """ Add/remove points of player

            %%chatlvlpoints add <name> <points> [<type>]
            %%chatlvlpoints remove <name> <points> [<type>]
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        global CHATLVL_COMMANDLOCK
        CHATLVL_COMMANDLOCK.acquire()
        self.debugPrint('commandlock acquire chatlvlpoints')
        points, type = args.get('<points>'), args.get('<type>')
        if not type:
            type = 'p'
        try:
            points = int(points)
        except:
            self.bot.action(mask.nick, "Failed to send points! Are you sure you gave me a number?")
            points = 0
        if args.get('remove'):
            points *= -1
        self.Chatpoints.updateById(args.get('<name>'), delta={type : points}, allowNegative=False, partial=True)
        self.bot.action(mask.nick, "Done!")
        CHATLVL_COMMANDLOCK.release()
        self.debugPrint('commandlock release chatlvlpoints eof')

    @command(permission='admin', show_in_help_list=False)
    @asyncio.coroutine
    def chatslap(self, mask, target, args):
        """ Slap someone and remove some of his points

            %%chatslap <name>
            %%chatslap <name> <points>
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        global CHATLVL_COMMANDLOCK
        CHATLVL_COMMANDLOCK.acquire()
        self.debugPrint('commandlock acquire chatslap')
        name, points = args.get('<name>'), args.get('<points>')
        try:
            points = abs(int(points))
        except:
            points = 5
        self.Chatpoints.updateById(name, delta={'p' : -points}, allowNegative=False, partial=True)
        self.bot.action(target, "slaps {name}, causing them to lose {points} points".format(**{
                "name": name,
                "points": str(points),
            }))
        self.Chatevents.addEvent('chatslap', {
            'by' : mask.nick,
            'target' : name,
            'points' : points,
        })
        CHATLVL_COMMANDLOCK.release()
        self.debugPrint('commandlock release chatslap eof')

    def __tourneyAdd(self, id, channel):
        tourneydata = self.ChatgameTourneys.get(channel, False)
        if tourneydata:
            if self.ChatgameTourneys[channel]['players'].get(id, False):
                return False
            self.Chatpoints.updateById(id, data={tourneydata['pointkey']: CHATPOINTS_DEFAULT_TOURNEY_START})
            self.ChatgameTourneys[channel]['players'][id] = 1
            return True
        return False

    def __tourneyRemove(self, id, channel):
        tourneydata = self.ChatgameTourneys.get(channel, False)
        if tourneydata:
            if self.ChatgameTourneys[channel]['players'].get(id, False):
                del self.ChatgameTourneys[channel]['players'][id]
                self.Chatpoints.updateById(id, delta={tourneydata['pointkey']: -999999}, allowNegative=False, partial=True)
                return True
        return False

    @command(permission='admin', show_in_help_list=False, public=False)
    def chatgamesadmin(self, mask, target, args):
        """ To restore reserved points

            %%chatgamesadmin restore roulette
            %%chatgamesadmin restore poker
            %%chatgamesadmin tourney get
            %%chatgamesadmin tourney <channel> start poker
            %%chatgamesadmin tourney <channel> add <name>
            %%chatgamesadmin tourney <channel> remove <name>
            %%chatgamesadmin tourney <channel> end
        """
        global CHATLVL_COMMANDLOCK, CHATPOINTS_DEFAULT_TOURNEY_START
        CHATLVL_COMMANDLOCK.acquire()
        self.debugPrint('commandlock acquire chatgamesadmin')
        restore, roulette, poker = args.get('restore'), args.get('roulette'), args.get('poker')
        tourney, start, get, add, remove, end = args.get('tourney'), args.get('start'), args.get('get'), args.get('add'), args.get('remove'), args.get('end')
        name, channel = args.get('<name>'), args.get('<channel>')
        if restore:
            keyFrom, keyTo = 'reserved', 'p'
            if roulette: keyFrom = 'chatroulette-reserved'
            if poker: keyFrom = 'chatpoker-reserved'
            self.Chatpoints.transferBetweenKeysForAll(keyFrom, keyTo, 99999999999, deleteOld=True)
            self.bot.privmsg(mask.nick, "Done!")
        if tourney:
            # new tourney
            if start and poker:
                pointkey = 'pokertourney-'+channel
                self.ChatgameTourneys[channel] = {
                    'joinable' : True,
                    'minpoints' : 200,
                    'minpincreasemult' : 1.02,
                    'minpincreaseadd' : 10,
                    'type' : 'poker',
                    'pointkey' : pointkey,
                    'pointreservedkey' : pointkey+'-reserved',
                    'statisticskey' : 'pokertourney',
                    'players' : {},
                    'ante' : 5,
                }
                self.bot.privmsg(mask.nick, "Starting poker tourney in {}! Pointkey: '{}'!".format(channel, pointkey))
            # existing tourney
            tourneydata = self.ChatgameTourneys.get(channel, False)
            if tourneydata:
                if add:
                    self.__tourneyAdd(name, channel)
                    self.bot.privmsg(mask.nick, "Gave {} 1000 points!".format(name))
                elif remove:
                    if self.__tourneyRemove(name, channel):
                        self.bot.privmsg(mask.nick, "Removed {}!".format(name))
                    else:
                        self.bot.privmsg(mask.nick, "{} is not in the tourney!".format(name))
                # make sure new tourneys start clean
                if start or end:
                    self.Chatpoints.transferBetweenKeysForAll(tourneydata['pointkey'], False, 99999999999, deleteOld=True)
                    self.Chatpoints.transferBetweenKeysForAll(tourneydata['pointreservedkey'], False, 99999999999, deleteOld=True)
                if end:
                    self.ChatgameTourneys[channel] = False
                    del self.ChatgameTourneys[channel]
                    self.bot.privmsg(mask.nick, "Ended the tourney!")
            else:
                self.bot.privmsg(mask.nick, "There is no tourney in this channel!")
        if tourney and get:
            self.bot.privmsg(mask.nick, "Running tourneys: {}".format(", ".join([k for k in self.ChatgameTourneys.keys()])))
        CHATLVL_COMMANDLOCK.release()
        self.debugPrint('commandlock release chatgamesadmin eof')

    @command(permission='admin', show_in_help_list=False, public=False)
    def chatbetadmin(self, mask, target, args):
        """ To manage chatbets

            %%chatbetadmin restore
            %%chatbetadmin addbet <channel> <betname> TEXT ...
            %%chatbetadmin addoptions <betname> TEXT ...
            %%chatbetadmin closebet <betname>
            %%chatbetadmin endbet <betname> <winningoption>
        """
        global CHATLVL_COMMANDLOCK
        CHATLVL_COMMANDLOCK.acquire()
        self.debugPrint('commandlock acquire chatbetadmin')
        restore, addbet, addoptions, closebet, deletebet, endbet = args.get('restore'), args.get('addbet'), args.get('addoptions'), args.get('closebet'), args.get('deletebet'), args.get('endbet')
        channel, betname, TEXT, winningoption = args.get('<channel>'), args.get('<betname>'), " ".join(args.get('TEXT')), args.get('<winningoption>')
        if betname and not addbet:
            if not self.Chatbets.betExists(betname):
                self.bot.privmsg(mask.nick, "betname does not exist!")
                CHATLVL_COMMANDLOCK.release()
                self.debugPrint('commandlock release chatbetadmin 1')
                return
        if restore:
            self.Chatpoints.transferBetweenKeysForAll('chatbet-reserved', 'p', 99999999999, deleteOld=True)
            self.bot.privmsg(mask.nick, "Done!")
        if addbet:
            self.Chatbets.createBet(betname, TEXT, channel=channel)
            self.bot.privmsg(mask.nick, "Done!")
        if addoptions:
            self.Chatbets.addOptions(betname, TEXT)
            self.bot.privmsg(mask.nick, "Done!")
        if closebet:
            self.Chatbets.closeBet(betname)
            self.bot.privmsg(mask.nick, "Done!")
        if endbet:
            if self.Chatbets.endBet(betname, winningoption):
                self.bot.privmsg(mask.nick, "Done!")
            else:
                self.bot.privmsg(mask.nick, "That is not an existing option!")
        CHATLVL_COMMANDLOCK.release()
        self.debugPrint('commandlock release chatbetadmin eof')

    @command(permission='admin', show_in_help_list=False, public=False)
    def onjoinmsgadmin(self, mask, target, args):
        """ To manage join messages for the main chat
            usually setting with strength 2, which is below top5 announcements (which has 3)

            %%onjoinmsgadmin get <name>
            %%onjoinmsgadmin del <name>
            %%onjoinmsgadmin set <name> <strength> TEXT ...
        """
        get, delete, set = args.get('get'), args.get('del'), args.get('set')
        name, strength, text = args.get('<name>'), args.get('<strength>', 2), ' '.join(args.get('TEXT'))
        if get:
            msg, strength = self.Chatpoints.getOnJoinMsgById(name)
            if msg:
                self.bot.privmsg(mask.nick, 'User "{name}" has on_join message "{msg}" set with strength {str}'.format(**{
                    'name': name,
                    'msg': msg,
                    'str': strength,
                }))
            else:
                self.bot.privmsg(mask.nick, 'There is no on_join message for this user!')
        if delete:
            self.Chatpoints.setOnJoinMsgById(name, '', delete=True)
            self.bot.privmsg(mask.nick, 'The on_join message for this user was removed!')
        if set:
            if not '{name}' in text:
                self.bot.privmsg(mask.nick, 'The on_join does not contain "{name}"! This is required!')
                return
            try:
                strength = int(strength)
            except:
                strength = 2
                self.bot.privmsg(mask.nick, 'Failed reading strength! Set to default 2!')
            ans = self.Chatpoints.setOnJoinMsgById(name, text, writeStrength=strength,
                                                   announcementStrength=strength, delete=False)
            if ans:
                self.bot.privmsg(mask.nick, 'The on_join message for this user was set successfullly!')
            else:
                self.bot.privmsg(mask.nick, 'Something went wrong! (probably lower writing strength than needed)')

    @command
    @asyncio.coroutine
    def chatbet(self, mask, target, args):
        """ Betting!

            %%chatbet
            %%chatbet <betname> <option> <points/all>
        """
        global CHATLVL_COMMANDLOCK
        CHATLVL_COMMANDLOCK.acquire()
        self.debugPrint('commandlock acquire chatbet')
        betname, option, points = args.get('<betname>'), args.get('<option>'), args.get('<points/all>')
        allpoints = (points == 'all')
        bet = bool(points) and bool(betname)
        if points:
            try:
                points = int(points)
            except:
                if allpoints: points = 9999999999
                else: points = 0
        if betname:
            if not self.Chatbets.betExists(betname):
                CHATLVL_COMMANDLOCK.release()
                self.debugPrint('commandlock release chatbet 1')
                self.bot.privmsg(target, "Bet with selected name does not exist!")
                return
        if bet:
            id, _ = yield from self.__nameToFafId(mask.nick)
            self.Chatbets.addBet(betname, target, option, id, points, allpoints=allpoints)
        else:
            # printing out the options, need spam protect only for this
            if self.spam_protect('chatbet', mask, target, args, specialSpamProtect='chatbet'):
                return
            count = self.Chatbets.count()
            strings = self.Chatbets.asStrings()
            if count == 0:
                self.bot.privmsg(target, "There are currently no bets!")
            else:
                self.bot.privmsg(target, "There are " + str(count) + " bets going on!")
                for string in strings:
                    self.bot.privmsg(target, "- " + string)
        CHATLVL_COMMANDLOCK.release()
        self.debugPrint('commandlock release chatbet eof')

    def __textToPokerCommand(self, text):
        # TODO raises
        text = text.lower()
        for word in ["join"]:
            if word in text:
                return {'join' : True}
        for word in ["fold", "dansgame"]:
            if word in text:
                return {'fold' : True}
        for word in ["call"]:
            if word in text:
                return {'call' : True}
        for word in ["start"]:
            if word in text:
                return {'start' : True}
        for word in ["reveal", "show"]:
            if word in text:
                return {'reveal' : True}
        return {}

    @command(show_in_help_list=False)
    @asyncio.coroutine
    def cp(self, mask, target, args):
        """ %%cp join [<points>]
            %%cp signup [<points>]
            %%cp fold
            %%cp call
            %%cp raise <points>
            %%cp start
            %%cp reveal
            %%cp TEXT ...
        """
        return (yield from self.cpoker(mask, target, args))

    @command
    @asyncio.coroutine
    def cpoker(self, mask, target, args):
        """ %%cpoker join [<points>]
            %%cpoker signup [<points>]
            %%cpoker fold
            %%cpoker call
            %%cpoker raise <points>
            %%cpoker start
            %%cpoker reveal
            %%cpoker TEXT ...
        """
        global CHATLVL_COMMANDLOCK, MAIN_CHANNEL, POKER_CHANNEL
        """
        if (target == MAIN_CHANNEL):
            self.bot.privmsg(mask.nick, "Poker is heavily limited in {main} atm, due to the spam! ''!join {channel}'' to play with others!".format(**{
                "main" : MAIN_CHANNEL,
                "channel": POKER_CHANNEL,
            }))
            return
        """
        CHATLVL_COMMANDLOCK.acquire()
        if self.chatroulettethreads.get(target, False):
            CHATLVL_COMMANDLOCK.release()
            return "Another game is in progress!"
        self.debugPrint('commandlock acquire chatpoker')
        points = args.get('<points>')
        textcommands = self.__textToPokerCommand(" ".join(args.get('TEXT')))
        createdGame = False
        if points:
            try:
                points = abs(int(points))
            except:
                CHATLVL_COMMANDLOCK.release()
                self.debugPrint('commandlock release chatpoker 2')
                return "Failed setting points! Are you sure you gave me a number?"
        if (args.get('reveal') or textcommands.get('reveal')) and self.ChatpokerPrev.get(target, False):
            self.ChatpokerPrev[target].reveal(mask.nick)
            CHATLVL_COMMANDLOCK.release()
            return
        if self.spam_protect('chatgames', mask, target, args, specialSpamProtect='chatgames', updateTimer=False):  # TODO check, different timers?
            CHATLVL_COMMANDLOCK.release()
            self.debugPrint('commandlock release chatpoker spam')
            return
        if not self.Chatpoker.get(target, False):
            tourneydata = self.ChatgameTourneys.get(target, False)
            if tourneydata:
                self.Chatpoker[target] = Poker(self.bot, self.on_cpoker_done, self.Chatpoints, self.Chatevents,
                                               target,
                                               tourneydata['minpoints'],
                                               gamecost = 0,
                                               gamecostreceiver=target,
                                               chatpointsDefaultKey=tourneydata['pointkey'],
                                               chatpointsReservedKey=tourneydata['pointreservedkey'],
                                               chatpointsStatisticsKey=tourneydata['statisticskey'])
                for name in tourneydata['players'].keys():
                    self.Chatpoker[target].sponsor(name, tourneydata['ante'] * tourneydata['players'][name])
                self.ChatgameTourneys[target]['minpoints'] = int(self.ChatgameTourneys[target]['minpoints'] * tourneydata['minpincreasemult'] + tourneydata['minpincreaseadd'])
            else:
                if not points: points = 50
                else: points = max([points, 20])
                self.Chatpoker[target] = Poker(self.bot, self.on_cpoker_done, self.Chatpoints, self.Chatevents, target, maxpoints=points)
            createdGame = True
        if args.get('start') or textcommands.get('start'):
            self.Chatpoker[target].beginFirstRound(mask.nick)
        if args.get('call') or textcommands.get('call'):
            self.Chatpoker[target].call(mask.nick)
        if args.get('fold') or textcommands.get('fold'):
            self.Chatpoker[target].fold(mask.nick)
        if args.get('join') or args.get('signup') or textcommands.get('join'):
            worked = self.Chatpoker[target].signup(mask.nick)
            if createdGame and (not worked):
                self.Chatpoker[target] = False
                del self.Chatpoker[target]
                self.bot.privmsg(target, "Removed poker game again.")
        if args.get('raise'):
            self.Chatpoker[target].raise_(mask.nick, points)
        CHATLVL_COMMANDLOCK.release()

    def on_cpoker_done(self, args={}):
        # CHATLVL_COMMANDLOCK protected unless ends with timeout fold
        # TODO lock safety when timeout fold
        channel = args.get('channel', POKER_CHANNEL)
        # in case of tourney, update ante punishments
        tourneydata = self.ChatgameTourneys.get(channel, False)
        if tourneydata:
            self.ChatgameTourneys[channel]['joinable'] = False
            for name in tourneydata['players'].keys():
                if name in args.get('participants'): self.ChatgameTourneys[channel]['players'][name] = 1
                else: self.ChatgameTourneys[channel]['players'][name] = self.ChatgameTourneys[channel]['players'].get(name, 0) + 1
        self.ChatpokerPrev[channel] = self.Chatpoker[channel]
        self.Chatpoker[channel] = False
        del self.Chatpoker[channel]
        print("poker game duration:", time.time() - args.get('starttime')) # TODO nice time spam protection?
        self.spam_protect('chatgames', self.bot.config['nick'], channel, {}, specialSpamProtect='chatgames', setToNow=True)
        self.save(args={
            'path' : 'poker/',
            'keep' : 5,
        })

    @command
    @asyncio.coroutine
    def cr(self, mask, target, args):
        """ Shortcut to the chatroulette command

            %%cr <points/all>
        """
        yield from self.chatroulette(mask, target, args)

    @command
    @asyncio.coroutine
    def chatroulette(self, mask, target, args):
        """ Play the chat point roulette! Bet points, 20s after the initial roll, a winner is chosen.
            Probability scales with points bet. The winner gets all points.

            %%chatroulette <points/all>
        """
        if self.spam_protect('chatgames', mask, target, args, specialSpamProtect='chatgames', updateTimer=False):
            return
        global CHATLVL_COMMANDLOCK
        CHATLVL_COMMANDLOCK.acquire()
        if self.Chatpoker.get(target, False):
            CHATLVL_COMMANDLOCK.release()
            return "Another game is in progress!"
        self.debugPrint('commandlock acquire chatroulette')
        points, use = args.get('<points/all>'), False
        allin = points in ["all", "allin"]
        if allin:
           points = 99999999999999
        else:
            try:
                points = abs(int(points))
            except:
                CHATLVL_COMMANDLOCK.release()
                self.debugPrint('commandlock release chatroulette 1')
                return
        worked, points = self.Chatpoints.transferBetweenKeysById(mask.nick, 'p', 'chatroulette-reserved', points, partial=allin)
        if not worked:
            self.bot.action(target, "You have too few points to bet this sum! ({name})".format(**{
                    "name": mask.nick,
                }))
            CHATLVL_COMMANDLOCK.release()
            self.debugPrint('commandlock release chatroulette 2')
            return
        points = int(points)
        if points < 1:
            CHATLVL_COMMANDLOCK.release()
            self.debugPrint('commandlock release chatroulette 3')
            return
        seconds = 20
        addedSeconds = min([10, points])  # to roulette timer
        if (not self.chatroulettethreads.get(target)):
            self.chatroulettethreads[target] = timedInputAccumulatorThread(callbackf=self.on_chatroulette_finished_noasync, args={"channel":target}, seconds=seconds, maxduration=60)
            self.chatroulettethreads[target].start()
            self.bot.privmsg(target, "{name} is starting a chat roulette! Quickly, bet your points! ({seconds} seconds, betting is dangerous and can be addicting)".format(**{
                    "name": mask.nick,
                    "seconds": seconds,
                }))
        else:
            self.bot.action(mask.nick, "noted {name}'s bet (timer extended by {seconds} second(s))".format(**{
                    "name": mask.nick,
                    "seconds": str(addedSeconds),
                }))
        self.chatroulettethreads[target].addInput((mask.nick, points), addSeconds=addedSeconds)
        if allin:
            self.bot.action(target, "{name} is going all in with {points} points!".format(**{
                    "name": mask.nick,
                    "points": str(points),
                }))
        CHATLVL_COMMANDLOCK.release()
        self.debugPrint('commandlock release chatroulette eof')

    def on_chatroulette_finished_noasync(self, args, inputs):
        self.loop.run_until_complete(self.on_chatroulette_finished(args, inputs))

    @asyncio.coroutine
    def on_chatroulette_finished(self, args, inputs):
        global CHATLVL_COMMANDLOCK
        CHATLVL_COMMANDLOCK.acquire()
        self.debugPrint('commandlock acquire roulettefinished')
        result = {}
        # let bot join the roulette... for free
        for i in inputs:
            result[i[0]] = result.get(i[0], 0) + i[1]
        totalpoints = sum(result.values())
        maibet = 0.5 + int(totalpoints/50)
        result[self.bot.config['nick']] = maibet
        winner, _ = self.pickWeightedRandom(result)
        print('- roulette done!', winner, args.get('channel'), totalpoints)
        print('- result: ', result)
        # winner print
        #TODO write stuff non-delayed :(
        if ((len(result) == 2 and maibet) or (len(result) == 1 and not maibet)) and (not (winner == self.bot.config['nick'])):
            self.bot.privmsg(args.get('channel'), 'The roulette ended without competition (points returned)')
        else:
            endstring = ""
            if winner == self.bot.config['nick']:
                endstring = random.choice(["Thanks for the tip :)", "Get rekt!", "HAHAHAHAHA!", "Thanks for the points!", "Thanks, losers >:)", "Thanks <3"])
            else:
                roi = totalpoints / result.get(winner, 1)
                if roi > 10:
                    endstring = random.choice(["Wew, lucky!", "Damn, so many points!", "Lucky! :)", "Congrats!"])
                elif roi > 3:
                    endstring = random.choice(["Surprising result!", "Nice!", "Well done!", "Lucky!", ":)", "Congrats!"])
                else:
                    endstring = random.choice(["Congratulations!", "Well done!", "As expected!", "The farming proceeds."])
            self.bot.privmsg(args.get('channel'), "The chat roulette ended! {name} won {totalpoints} points (bet: {bet})! {end}".format(**{
                    "name": winner,
                    "totalpoints": str(totalpoints),
                    "bet": str(result[winner]),
                    "end": endstring,
                }))
        # juggle points, remove MAI from the betting list
        del result[self.bot.config['nick']]
        self.Chatpoints.transferByIds(winner, result, receiverKey='p', giverKey='chatroulette-reserved', allowNegative=False, partial=False)
        self.Chatpoints.transferByIds(winner, result, receiverKey='chatroulette', giverKey='chatroulette', allowNegative=True, partial=False)
        #self.Chatpoints.transferBetweenKeysForAll('chatroulette-reserved', 'p', 99999999999, deleteOld=False) # recover original points which might lost to hickup etc
        # cooldown, data
        if self.chatroulettethreads.get(args.get('channel'), False):
            self.chatroulettethreads[args.get('channel')].stop()
            del self.chatroulettethreads[args.get('channel')]
        self.Chatevents.addEvent('chatroulette', {
            'winner' : winner,
            'bets' : result
        })
        self.save(args={
            'path' : 'roulette/',
            'keep' : 5,
        })
        CHATLVL_COMMANDLOCK.release()
        self.debugPrint('commandlock release roulettefinished eof')
        self.spam_protect('chatgames', self.bot.config['nick'], args.get('channel'), args, specialSpamProtect='chatgames', setToNow=True)

    if False:
        @command(public=False, show_in_help_list=False)
        @asyncio.coroutine
        def maibotapi(self, mask, target, args):
            """ Enabling chat based data transfer

                %%maibotapi chatlvl <name>
                %%maibotapi pointcost <name> <points>
            """
            pass
            """
            if not (yield from self.__isNickservIdentified(mask.nick)):
                return
            print('MAIBOTAPI called by', mask.nick)
            if not (mask.nick in ["TheSetoner", "Giebmasse", "Giebmasse_irc", "Washy", "Purpleheart"]):
                print('abandoning')
                return
            chatlvl, pointcost, name, points = args.get('chatlvl'), args.get('pointcost'), args.get('<name>'), args.get('<points>', False)
            if points:
                try:
                    points = int(points)
                except:
                    self.bot.privmsg(mask.nick, "Failed: points not convertible to int")
                    return
            sid, data, use = -1, {}, False
            if chatlvl:
                self.update_chatlvl(mask.nick, mask.nick, 0)
                sid, data, use = yield from self.__chatlvlget(name=name)
                self.bot.privmsg(mask.nick, "{use}, level={level}, points={points}".format(**{
                        "use": str(use),
                        "level": str(data.get('l')),
                        "points": str(format(data.get('p', 0), '.1f')),
                    }))
                return
            if pointcost:
                use = self.update_chatlvl(name, name, -points)
                self.bot.privmsg(mask.nick, "{use}".format(**{
                        "use": str(use)
                    }))
                return
            self.bot.privmsg(mask.nick, "Failed")
            """

    @command(permission='admin', show_in_help_list=False)
    @asyncio.coroutine
    def maitest(self, mask, target, args):
        """ Test functionality

            %%maitest <name>
        """
        self.Chatpoints.merge("test54", "Washy")
        """
        name = args.get('<name>')
        #print('.')
        whois = yield from self.whois(nick=name)
        print(whois.get('username', False))
        return
        self.bot.action(target, "{msg}".format(**{
                "msg": "<3",
            }))
        """

    @command(public=False)
    @asyncio.coroutine
    def helpirenamed(self, mask, target, args):
        """ Merges data that's attached to your previous name to your current.

            %%helpirenamed
        """
        global RENAME_API_URL, RENAME_API_URL_NAME
        past_names = []
        try:
            user_id = int(str(mask).split('@')[0].split('!')[1])
            with urllib.request.urlopen(RENAME_API_URL.format(**{
                'id': user_id
            })) as response:
                ans = json.loads(response.read().decode())
                for name in ans['included']:
                    if name['type'] == 'nameRecord':
                        past_names.append(name['attributes']['name'])
        except:
            pass
        if len(past_names) < 1:
            self.bot.privmsg(mask.nick, 'You have not changed your name, or FAF does not know about you.')
            return
        previous_name = past_names[-1]
        try:
            # check if the name is taken by someone
            with urllib.request.urlopen(RENAME_API_URL_NAME.format(**{
                'name': previous_name
            })) as response:
                ans = json.loads(response.read().decode())
                if ans is None or (not ans.get('data', False)):
                    self.bot.privmsg(mask.nick, 'Confirmed! Merging with data of ' + previous_name + '!')
                    self.Chatpoints.merge(mask.nick, previous_name)
                else:
                    self.bot.privmsg(mask.nick, 'Your previous name "{}" is currently taken!'.format(previous_name))
        except:
            self.bot.privmsg(mask.nick, 'Something went wrong :(')
            return

    def getUnpingableName(self, name):
        return name[0:len(name)-1] + '.' + name[len(name)-1]

    def spam_protect(self, cmd, mask, target, args, updateTimer=True, specialSpamProtect=None, ircSpamProtect=True, setToNow=False):
        if setToNow:
            if not cmd in self.timers:
                self.timers[cmd] = {}
            self.timers[cmd][target] = time.time()
            return
        nick = mask
        if type(mask) is not str:
            nick = mask.nick
        if nick in IGNOREDUSERS.values():
            if ircSpamProtect:
                self.bot.privmsg(nick, "You are on the ignore list, commands will not be executed.")
            return True
        if ircSpamProtect:
            if not target == MAIN_CHANNEL:
                return False
        if not cmd in self.timers:
            self.timers[cmd] = {}
        if not target in self.timers[cmd]:
            self.timers[cmd][target] = 0
        global TIMERS, DEFAULTCD, CDPRIVILEDGEDUSERS
        timer = TIMERS.get(specialSpamProtect,
                           self.bot.config.get(specialSpamProtect,
                                               DEFAULTCD))
        remTime = timer - (time.time() - self.timers[cmd][target]) - CDPRIVILEDGEDUSERS.get(nick, 0)
        if remTime > 0:
            if ircSpamProtect:
                self.bot.privmsg(nick, "Wait another " + str(int(remTime)+1) + " seconds before trying again.")
            return True
        if updateTimer:
            self.timers[cmd][target] = time.time()
        return False

    def is_main_channel(self, mask, target, irc_pm_if_channel=True):
        global MAIN_CHANNEL
        if target == MAIN_CHANNEL:
            if irc_pm_if_channel:
                self.bot.privmsg(mask.nick, "This command is not available in {}!".format(MAIN_CHANNEL))
            return True
        return False

    def has_permissions(self, id, irc_msg_responses=True, all=[], any=[]):
        """
        :param irc_msg_responses: message responses if permission is not granted
        :param any: list of options - having any of these returns True, regardless of required
        :param all: list of options - not having all of these returns False
        these lists use (requirement_name, variable)
        """
        data = self.Chatpoints.getPointDataById(id)
        responses = []
        lists = [all, any]
        counters = [0 for lst in lists]
        nick = id

        def inc_counter_or_response(bool, req_var, index, response):
            if bool: counters[index] += 1
            else: responses.append(response.format(req_var))

        for i in range(0, len(lists)):
            lst = lists[i]
            for req_name, req_var in lst:
                if req_name == 'chatpoints_min':
                    inc_counter_or_response(req_var <= data.get('p', 999999), req_var, i, 'Not enough chatpoints (min {})')
                elif req_name == 'chatpoints_max':
                    inc_counter_or_response(req_var >= data.get('p', 0), req_var, i, 'Too many chatpoints (max {})')
                elif req_name == 'is_in_top5':
                    inc_counter_or_response(CHATLVL_TOPPLAYERS.get(id, False), req_var, i, 'Not in the list of top chatters')
                elif req_name == 'questionpoints_max':
                    inc_counter_or_response(req_var >= data.get('questions', 0), req_var, i, 'Already got too many points with questions (max {})')
                elif req_name == 'bot_admin':
                    global ADMINS
                    inc_counter_or_response(id in ADMINS, req_var, i, 'Not an admin')
        granted = (counters[0] == len(all)) or (counters[1] >= 1)
        if (not granted) and irc_msg_responses:
            self.bot.privmsg(id, 'Permission to the command not granted due to one or more of the following reasons:')
            self.bot.privmsg(id, ', '.join(responses))
        return granted, responses

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def chattest(self, mask, target, args):
        """ Testing!

            %%chattest
        """
        hp, resp = self.has_permissions(mask.nick,
                                        irc_msg_responses=True,
                                        all=[('chatpoints_min', 1000),
                                             ('chatpoints_min', 2000),
                                             ('is_in_top5', 0)],
                                        any=[('bot_admin', 0)])


    def pickWeightedRandom(self, dct):
        total = sum(dct.values())
        v = random.random() * total
        for key in dct.keys():
            v -= dct[key]
            if v <= 0:
                return key, total
        return dct.keys()[len(dct)-1], total

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def chatlvlchannels(self, mask, target, args):
        """Adds/removes a given channel to those which points can be farmed in
            %%chatlvlchannels get
            %%chatlvlchannels add TEXT ...
            %%chatlvlchannels del <ID>
        """
        return self.__genericCommandManage(mask, target, args, ['chatlvlchannels'])

    @command
    @asyncio.coroutine
    def cats(self, mask, target, args):
        """Show a cats image
            %%cats
        """
        self.__genericSpamCommand(mask, target, args, ['spam', 'cats'], specialSpamProtect='spam_cats')

    @command(permission='admin', public=False, show_in_help_list=False)
    @asyncio.coroutine
    def catsadmin(self, mask, target, args):
        """Adds/removes a given text from the quotelist.
            %%catsadmin get
            %%catsadmin add TEXT ...
            %%catsadmin del <ID>
        """
        return self.__genericCommandManage(mask, target, args, ['spam', 'cats'])

    def __genericSpamCommand(self, mask, target, args, path, specialSpamProtect=None):
        if self.spam_protect("-".join(path), mask, target, args, specialSpamProtect=specialSpamProtect):
            return
        try:
            elem = random.choice(list(self.__dbGet(path).values()))
            self.bot.privmsg(target, elem)
        except:
            self.debugPrint('__genericSpamCommand: Trying to sample from empty list: ' + repr(path))

    def __genericCommandManage(self, mask, target, args, path, allowSameValue=False):
        """
        Generic managing of adding/removing/getting
        Needs: add,del,get,<ID>,TEXT
        """
        if not (yield from self.__isNickservIdentified(mask.nick)):
            return
        add, delete, get, id, text = args.get('add'), args.get('del'), args.get('get'), args.get('<ID>'), " ".join(
            args.get('TEXT'))
        dict = self.__dbGet(path)
        if add:
            if not allowSameValue:
                entries = self.__dbGet(path)
                for e in entries.values():
                    if e == text:
                        return "This already exists, so it won't be added."
            try:
                id = self.__getNextDictIncremental(dict)
                self.__dbAdd(path, id, text, save=True)
                return 'Added to the list.'
            except:
                return "Failed adding."
        elif delete:
            try:
                if dict.get(id):
                    dict = self.__dbDel(path, id, save=True)
                    return 'Removed element of ID "{id}".'.format(**{
                        "id": id,
                    })
                else:
                    return 'ID not found in the list.'
            except:
                return "Failed deleting."
        elif get:
            self.bot.privmsg(mask.nick, str(len(dict)) + " elements:")
            for id in dict.keys():
                self.bot.privmsg(mask.nick, '<%s>: %s' % (id, dict[id]))

    def isInChannel(self, player, channel):
        if isinstance(channel, str):
            channel = self.bot.channels[channel]
        if player in channel:
            return True
        return False

    def __filterForPlayersInChannel(self, playerlist, channelname):
        players = {}
        if not channelname in self.bot.channels:
            return players
        channel = self.bot.channels[channelname]
        for p in playerlist.keys():
            if self.isInChannel(p, channel):
                players[p] = True
        return players

    def __getNextDictIncremental(self, dict):
        for i in range(0, 99999999):
            if not dict.get(str(i), False):
                return str(i)
        return "-1"

    @command(permission='admin', public=False)
    @asyncio.coroutine
    def hidden(self, mask, target, args):
        """Actually shows hidden commands
            %%hidden
        """
        words = ["join", "leave", "files", "cd", "vars", "savedb", "twitchjoin", "twitchleave",\
                 "twitchmsg", "list", "ignore", "cdprivilege", "chainadmin", "catsadmin",\
                 "chatlvlwords", "chatlvlpoints", "chatslap", "maibotapi", "restart", "reactionwords",\
                 "chatgamesadmin", "chatlvlchannels", "chattipadmin", "chatbetadmin", "onjoinmsgadmin"]
        self.bot.privmsg(mask.nick, "Hidden commands (!help <command> for more info):")
        #for word in words:
        #    self.bot.privmsg(mask.nick, "- " + word)
        self.bot.privmsg(mask.nick, ", ".join(words))

    def __dbAdd(self, path, key, value, overwriteIfExists=True, trySavingWithNewKey=False, save=True):
        cur = self.bot.db
        for p in path:
            if p not in cur:
                cur[p] = {}
            cur = cur[p]
        exists, addedWithNewKey = cur.get(key), False
        if overwriteIfExists:
            cur[key] = value
        elif not exists:
            cur[key] = value
        elif exists and trySavingWithNewKey:
            for i in range(0, 1000):
                if not cur.get(key+str(i)):
                    cur[key+str(i)] = value
                    addedWithNewKey = True
                    break
        if save:
            self.__dbSave()
        return cur, exists, addedWithNewKey

    def __dbDel(self, path, key, save=True):
        cur = self.bot.db
        for p in path:
            cur = cur.get(p, {})
        if not cur.get(key) is None:
            del cur[key]
            if save:
                self.__dbSave()
        return cur

    def __dbGet(self, path):
        reply = self.bot.db
        for p in path:
            reply = reply.get(p, {})
        return reply

    def __dbSave(self):
        self.bot.db.set('misc', lastSaved=time.time())
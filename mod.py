import asyncio
from collections import defaultdict
from urllib.parse import unquote
from copy import deepcopy
import datetime
import typing

from ext.utils.timed_events import parse_time, spool_reminder
from ext.utils.embed_utils import paginate
from discord.ext import commands
import discord


# TODO: Find a way to use a custom converter for temp mute/ban and merge into main command.

async def get_prefix(bot, message):
    if message.guild is None:
        pref = [".tb ", "!", "-", "`", "!", "?", ""]
    else:
        pref = bot.prefix_cache[message.guild.id]
    if not pref:
        pref = [".tb "]
    return commands.when_mentioned_or(*pref)(bot, message)


class Mod(commands.Cog):
    """ Guild Moderation Commands """
    
    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.update_cache())
        self.bot.prefix_cache = defaultdict(list)
        self.bot.loop.create_task(self.update_prefixes())
        self.bot.command_prefix = get_prefix
    
    def me_or_mod(self):
        def predicate(ctx):
            return ctx.author.permissions_in(ctx.channel).manage_channels or ctx.author.id == self.bot.owner_id
        return commands.check(predicate)
    
    # Listeners
    @commands.Cog.listener()
    async def on_message(self, message):
        ctx = await self.bot.get_context(message)
        if ctx.message.content == ctx.me.mention:
            await ctx.send(f"Forgot your prefixes? They're ```css"
                           f"\n{', '.join(self.bot.prefix_cache[message.guild.id])}```")
    
    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        connection = await self.bot.db.acquire()
        await connection.execute("""
        with gid as (
                INSERT INTO guild_settings (guild_id) VALUES ($1)
        RETURNING guild_id
        )
        INSERT INTO prefixes (prefix, guild_id)
        VALUES
        ( $2, (SELECT guild_id FROM gid)
        );
        """, guild.id,  '.tb ')
        await self.bot.db.release(connection)
        print(f"Guild Join: Default prefix set for {guild.id}")
        await self.update_prefixes()

    async def update_prefixes(self):
        self.bot.prefix_cache.clear()
        connection = await self.bot.db.acquire()
        records = await connection.fetch("""SELECT * FROM prefixes""")
        await self.bot.db.release(connection)
        
        for r in records:
            guild_id = r["guild_id"]
            prefix = r["prefix"]
            self.bot.prefix_cache[guild_id].append(prefix)
        
        # Items ending in space must come first.
        for guild, pref_list in self.bot.prefix_cache.items():
            for i in range(len(pref_list)):
                if pref_list[i].endswith(' '):
                    pref_list = [pref_list[i]] + pref_list[:i] + pref_list[i + 1:]
            self.bot.prefix_cache[guild] = pref_list
            
    async def update_cache(self):
        self.bot.disabled_cache = {}
        connection = await self.bot.db.acquire()
        records = await connection.fetch("""SELECT * FROM disabled_commands""")
        await self.bot.db.release(connection)
        
        for r in records:
            try:
                self.bot.disabled_cache[r["guild_id"]].append(r["command"])
            except KeyError:
                self.bot.disabled_cache.update({r["guild_id"]: [r["command"]]})
    
    @commands.command()
    @commands.has_permissions(kick_members=True)
    async def leave(self, ctx):
        """ Politely ask me to leave the server. """
        m = await ctx.send('Are you sure you want me to go? All of your settings will be wiped.')
        await m.add_reaction('✅')
        await m.add_reaction('🚫')
        
        def check(reaction, user):
            if reaction.message.id == m.id and user == ctx.author:
                emoji = str(reaction.emoji)
                return emoji.startswith(('✅', '🚫'))
            
        try:
            res = await self.bot.wait_for("reaction_add", check=check, timeout=30)
        except asyncio.TimeoutError:
            return await ctx.send("Response timed out after 30 seconds, I'm staying.", delete_after=15)
        res = res[0]

        if res.emoji.startswith('✅'):
            await ctx.send('Farewell!')
            await ctx.guild.leave()
        else:
            await ctx.send("Okay, I'll stick around a bit longer then.")
            await m.remove_reaction('✅', ctx.me)

        
    @commands.command(aliases=['nick'])
    @commands.has_permissions(manage_nicknames=True)
    async def name(self, ctx, *, new_name: str):
        """ Rename the bot for your server. """
        await ctx.me.edit(nick=new_name)
    
    @commands.command(usage="say <Channel (optional)< <what you want the bot to say>")
    @commands.check(me_or_mod)
    async def say(self, ctx, destination: typing.Optional[discord.TextChannel] = None, *, msg):
        """ Say something as the bot in specified channel """
        if destination is None:
            destination = ctx.channel
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass
        await destination.send(msg)
    
    @commands.command(usage="topic <New Channel Topic>")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def topic(self, ctx, *, new_topic):
        """ Set the topic for the current channel """
        await ctx.channel.edit(topic=new_topic)
        await ctx.send(f"Topic changed to: '{new_topic}'")
    
    @commands.command(usage="pin <(Message ID you want pinned) or (new message to pin.)>")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def pin(self, ctx, *, message: typing.Union[discord.Message, int, str]):
        """ Pin a message to the current channel """
        if isinstance(message, int):
            message = await ctx.channel.fetch_message(message)
        elif isinstance(message, str):
            message = await ctx.send(message)
        await message.pin()
        await ctx.message.delete()
    
    @commands.command(usage="rename <member> <new name>")
    @commands.has_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def rename(self, ctx, member: discord.Member, nickname: commands.clean_content):
        """ Rename a member """
        try:
            await member.edit(nick=nickname)
        except discord.Forbidden:
            await ctx.send("⛔ I can't change that member's nickname.")
        except discord.HTTPException:
            await ctx.send("❔ Member edit failed.")
        else:
            await ctx.send(f"{member.mention} has been renamed.")
    
    @commands.command(usage="delete_empty_roles")
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def delete_empty_roles(self, ctx):
        """ Delete any unused roles on the server """
        targets = [i for i in ctx.guild.roles if i.name.lower() != "muted" and not i.members]
        deleted = []
        for i in targets:
            deleted.append(i.name)
            await i.delete()
        await ctx.send(f'Found and deleted {len(deleted)} empty roles: {", ".join(deleted)}')
    
    @commands.command(usage="kick <@member1  @member2 @member3> <reason>")
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx, members: commands.Greedy[discord.Member], *, reason="unspecified reason."):
        """ Kicks the user from the server """
        replies = []
        for i in members:
            try:
                await i.kick(reason=f"{ctx.author.name}: {reason}")
            except discord.Forbidden:
                replies.append(f"⛔ I can't kick {i.mention}.")
            except discord.HTTPException:
                replies.append(f'⚠ Kicking failed for {ctx.author.name}.')
            else:
                replies.append(f"✅ {i.mention} was kicked by {ctx.author} for: \"{reason}\".")
        await ctx.send("\n".join(replies))
    
    @commands.command(usage="ban <@member1 user_id2 @member3 @member4> "
                            "<(Optional: Days to delete messages from)> <(Optional: reason)>",
                      aliases=["hackban"])
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx, targets: commands.Greedy[typing.Union[discord.Member, int]],
                  delete_days: typing.Optional[int] = 0, *, reason="Not specified"):
        """ Bans a list of members (or User IDs) from the server, deletes all messages for the last x days """
        replies = []
        for i in targets:
            if isinstance(i, discord.Member):
                try:
                    await i.ban(reason=f"{ctx.author.name}: {reason}", delete_message_days=delete_days)
                except discord.Forbidden:
                    replies.append(f"⛔ Sorry, I can't ban {i.mention}.")
                except discord.HTTPException:
                    replies.append(f"⚠ Banning failed for {i.mention}.")
                else:
                    replies.append(f"☠ {i.mention} was banned by {ctx.author} for: \"{reason}\".")
            else:
                try:
                    await self.bot.http.ban(i, ctx.message.guild.id)
                    target = await self.bot.fetch_user(i)
                    replies.append(f"☠ UserID {i} {target} was banned")
                except discord.HTTPException:
                    replies.append(f"⚠ Banning failed for UserID# {i}.")
                except Exception as e:
                    print(e)
                    print("Failed while banning ID#.")
        await ctx.send("\n".join(replies))
    
    @commands.command()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx, *, who):
        """ Unbans a user from the server (use name#discrim or userid)"""
        # Try to get by user_id.
        if who.isdigit():
            who = self.bot.get_user(int(who))
            try:
                await self.bot.http.unban(who.id, ctx.guild.id)
            except discord.Forbidden:
                await ctx.send("⛔ I can't unban that user.")
            except discord.HTTPException:
                await ctx.send("❔ Unban failed.")
            else:
                await ctx.send(f"🆗 {who} was unbanned")
        else:
            try:
                un, discrim = who.split('#')
                for i in await ctx.guild.bans():
                    if i.user.display_name == un:
                        if i.discriminator == discrim:
                            try:
                                await self.bot.http.unban(i.user.id, ctx.guild.id)
                            except discord.Forbidden:
                                await ctx.send("⛔ I can't unban that user.")
                            except discord.HTTPException:
                                await ctx.send("❔ Unban failed.")
                            else:
                                await ctx.send(f"🆗 {who} was unbanned")
            except ValueError:
                for i in await ctx.guild.bans():
                    if i.user.name == who:
                        try:
                            await self.bot.http.unban(i.user.id, ctx.guild.id)
                        except discord.Forbidden:
                            await ctx.send("⛔ I can\'t unban that user.")
                        except discord.HTTPException:
                            await ctx.send("❔ Unban failed.")
                        else:
                            await ctx.send(f"🆗 {who} was unbanned")
    
    @commands.command(aliases=['bans'])
    @commands.has_permissions(view_audit_log=True)
    @commands.bot_has_permissions(view_audit_log=True)
    async def banlist(self, ctx):
        """ Show the banlist for the server """
        banlist = await ctx.guild.bans()
        banpages = []
        banembeds = []
        if len(banlist) == 0:
            banpages = "☠ No bans found!"
        else:
            this_page = ""
            for x in banlist:
                a = x.user.name
                b = x.user.discriminator
                if len(unquote("\💀 {a}#{b}: {x.reason}\n")) + len(this_page) > 1200:
                    banpages.append(this_page)
                    this_page = ""
                this_page += unquote(f"\💀 {a}#{b}: {x.reason}\n")
            banpages.append(this_page)
            
        for i in banpages:
            e = discord.Embed(color=0x111)
            n = f"≡ {ctx.guild.name} discord ban list"
            e.set_author(name=n, icon_url=ctx.guild.icon_url)
            e.set_thumbnail(url="https://i.ytimg.com/vi/eoTDquDWrRI/hqdefault.jpg")
            e.title = "User (Reason)"
            e.description = i
            banembeds.append(deepcopy(e))
        await paginate(ctx, banpages)
    
    ### Mutes & Blocks
    @commands.command(usage="Block <Optional: #channel> <@member1 @member2> <Optional: reason>")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def block(self, ctx, channel: typing.Optional[discord.TextChannel], members: commands.Greedy[discord.Member]):
        """ Block a user from seeing or talking in this channel  """
        if channel is None:
            channel = ctx.channel

        ow = discord.PermissionOverwrite(read_messages=False, send_messages=False)
        for i in members:
            await channel.set_permissions(i, overwrite=ow)
        
        await ctx.send(f'Blocked {" ,".join([i.mention for i in members])} from {channel.mention}')

    @commands.command(usage="unblock <Optional: #channel> <@member1 @member2> <Optional: reason>")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def unblock(self, ctx, channel:typing.Optional[discord.TextChannel], members:commands.Greedy[discord.Member]):
        if channel is None:
            channel = ctx.channel
            
        for i in members:
            await channel.set_permissions(i, overwrite=None)

        await ctx.send(f'Unblocked {" ,".join([i.mention for i in members])} from {channel.mention}')
        
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    @commands.command(usage="mute <@user1 @user2 @user3> <reason>")
    async def mute(self, ctx, members: commands.Greedy[discord.Member], *, reason="No reason given."):
        """ Prevent member(s) from talking on your server. """
        muted_role = discord.utils.get(ctx.guild.roles, name='Muted')
        if not muted_role:
            muted_role = await ctx.guild.create_role(name="Muted")  # Read Messages / Read mesasge history.
            await muted_role.edit(position=ctx.me.top_role.position - 1)
            m_overwrite = discord.PermissionOverwrite(add_reactions=False, send_messages=False)
            for i in ctx.guild.text_channels:
                await i.set_permissions(muted_role, overwrite=m_overwrite)
        
        muted = []
        not_muted = []
        for i in members:
            if i.top_role >= ctx.me.top_role:
                members.remove(i)
                not_muted.append(i)
                continue
            else:
                muted.append(i)
                await i.add_roles(muted_role, reason=f"{ctx.author}: {reason}")
        muted = f"Muted {', '.join([i.mention for i in muted])} for {reason}" if muted else ""
        not_muted = f"⚠ Could not mute {', '.join([i.mention for i in not_muted])}," \
                    f" they are the same or higher role than me." if not_muted else ""
        await ctx.send("\n".join([i for i in [muted, not_muted] if i]))
        
                
    @commands.command()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unmute(self, ctx, members: commands.Greedy[discord.Member]):
        """ Allow members to talk again. """
        muted_role = discord.utils.get(ctx.guild.roles, name='Muted')
        if not muted_role:
            return await ctx.send(f"No 'muted' role found on {ctx.guild.name}")
        
        success, fail = [], []
        for i in members:
            try:
                await i.remove_roles(muted_role)
            except discord.Forbidden:
                fail.append(i.mention)
            else:
                success.append(i.mention)

        success = f"🆗 Unmuted {', '.join(success)}" if success else ""
        fail = f"🚫 Could not unmute {', '.join(fail)}" if fail else ""
        await ctx.send("\n".join(i for i in [success, fail] if i))
        
    
    @commands.command(aliases=["clear"])
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def clean(self, ctx, number: int = 100):
        """ Deletes my messages from the last x messages in channel"""
        try:
            prefixes = tuple(self.bot.prefix_cache[ctx.guild.id])
        except KeyError:
            prefixes = ctx.prefix
        
        def is_me(m):
            return m.author == ctx.me or m.content.startswith(prefixes)
        
        try:
            deleted = await ctx.channel.purge(limit=number, check=is_me)
            s = "s" if len(deleted) > 1 else ""
            await ctx.send(f'♻ Deleted {len(deleted)} bot and command messages{s}', delete_after=10)
        except discord.NotFound:
            await ctx.send('⚠ An error occurred, someone else deleted those messages before I did.', delete_after=5)
    
    @commands.group(invoke_without_command=True, usage='prefix: List all prefixes for the server')
    @commands.guild_only()
    async def prefix(self, ctx):
        """ Add, remove, or List bot prefixes for this server."""
        try:
            prefixes = self.bot.prefix_cache[ctx.guild.id]
        except KeyError:
            prefixes = ['.tb ']
            connection = await self.bot.db.acquire()
            await connection.execute("""INSERT INTO prefixes (guild_id,prefix) VALUES ($1,$2)""", ctx.guild.id, '.tb ')
            await self.bot.db.release(connection)
            await self.update_prefixes()
        
        prefixes = ', '.join([f"'{i}'" for i in prefixes])
        await ctx.send(f"Current Command prefixes for this server: ```{prefixes}```")
    
    @prefix.command(name="add", aliases=["set"])
    @commands.has_permissions(manage_guild=True)
    async def pref_add(self, ctx, prefix):
        try:
            prefixes = self.bot.prefix_cache[ctx.guild.id]
        except KeyError:
            prefixes = ['.tb ']
        
        if prefix not in prefixes:
            connection = await self.bot.db.acquire()
            await connection.execute("""INSERT INTO prefixes (guild_id,prefix) VALUES ($1,$2) """, ctx.guild.id,
                                     prefix)
            await self.bot.db.release(connection)
            await ctx.send(f"Added '{prefix}' to {ctx.guild.name}'s prefixes list.")
            await self.update_prefixes()
        else:
            await ctx.send(f"'{prefix}' was already in {ctx.guild.name}'s prefix list")
        
        prefixes = ', '.join([f"'{i}'" for i in self.bot.prefix_cache[ctx.guild.id]])
        await ctx.send(f"Current Command prefixes for this server: ```{prefixes}```")
    
    @prefix.command(name="remove", aliases=["delete"])
    @commands.has_permissions(manage_guild=True)
    async def pref_del(self, ctx, prefix):
        try:
            prefixes = self.bot.prefix_cache[ctx.guild.id]
        except KeyError:
            prefixes = ['.tb ']
        if prefix in prefixes:
            connection = await self.bot.db.acquire()
            await connection.execute("""DELETE FROM prefixes WHERE (guild_id,prefix) = ($1,$2) """, ctx.guild.id,
                                     prefix)
            await self.bot.db.release(connection)
            await ctx.send(f"Deleted '{prefix}' from {ctx.guild.name}'s prefixes list.")
            await self.update_prefixes()
        else:
            await ctx.send(f"'{prefix}' was not in {ctx.guild.name}'s prefix list")
        
        prefixes = ', '.join([f"'{i}'" for i in self.bot.prefix_cache[ctx.guild.id]])
        await ctx.send(f"Current Command prefixes for this server: ```{prefixes}```")
    
    @commands.command(aliases=["enable"], usage="<'disable' or 'enable'> <command name>")
    @commands.has_permissions(manage_guild=True)
    async def disable(self, ctx, command: str):
        """Disables a command for this server."""
        command = command.lower()
        
        if ctx.invoked_with == "enable":
            if command not in self.bot.disabled_cache[ctx.guild.id]:
                return await ctx.send("That command isn't disabled on this server.")
            else:
                connection = await self.bot.db.acquire()
                async with connection.transaction():
                    await connection.execute("""
                        DELETE FROM disabled_commands WHERE (guild_id,command) = ($1,$2)
                        """, ctx.guild.id, command)
                await self.bot.db.release(connection)
                await self.update_cache()
                return await ctx.send(f"The {command} command was re-enabled for {ctx.guild.name}")
        
        if command in ('disable', 'enable'):
            return await ctx.send('Cannot disable the disable command.')
        elif command not in [i.name for i in list(self.bot.commands)]:
            return await ctx.send('Unrecognised command name.')
        
        connection = await self.bot.db.acquire()
        await connection.execute(""" INSERT INTO disabled_commands (guild_id,command) VALUES ($1,$2) """,
                                 ctx.guild.id, command)
        await self.bot.db.release(connection)
        await self.update_cache()
        return await ctx.send(f"The {command} command was disabled for {ctx.guild.name}")
    
    @commands.command(usage="disabled")
    @commands.has_permissions(manage_guild=True)
    async def disabled(self, ctx):
        """ Check which commands are disabled on this server """
        try:
            disabled = self.bot.disabled_cache[ctx.guild.id]
            await ctx.send(f"The following commands are disabled on this server: ```{' ,'.join(disabled)}```")
        except KeyError:
            return await ctx.send(f'No commands are currently disabled on {ctx.guild.name}')

    @commands.command(usage="tempban <members: @member1 @member2> <time (e.g. 1d1h1m1s)> <(Optional: reason)>")
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def tempban(self, ctx,  members: commands.Greedy[discord.Member], time, *,
                      reason: commands.clean_content = None):
        """ Temporarily ban member(s) """
        if not members:
            return await ctx.send('🚫 You need to specify which users to ban.')
    
        delta = await parse_time(time.lower())
        remind_at = datetime.datetime.now() + delta
        human_time = datetime.datetime.strftime(remind_at, "%H:%M:%S on %a %d %b")
    
        for i in members:
            try:
                await ctx.guild.ban(i, reason=reason)
            except discord.Forbidden:
                await ctx.send("🚫 I can't ban {i.mention}}.")
                continue
        
            connection = await self.bot.db.acquire()
            record = await connection.fetchrow(""" INSERT INTO reminders (message_id, channel_id, guild_id,
            reminder_content,
            created_time, target_time. user_id, mod_action, mod_target) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *""", ctx.message.id, ctx.channel.id, ctx.guild.id, reason, datetime.datetime.now(), remind_at,
                                              ctx.author.id, "unban", i.id)
            await self.bot.db.release(connection)
            self.bot.reminders.append(self.bot.loop.create_task(spool_reminder(ctx.bot, record)))
    
        e = discord.Embed()
        e.title = "⏰ User banned"
        e.description = f"{[i.mention for i in members]} will be unbanned for \n{reason}\nat\n {human_time}"
        e.colour = 0x00ffff
        e.timestamp = remind_at
        await ctx.send(embed=e)

    @commands.command(usage="tempmute <members: @member1 @member2> <time (e.g. 1d1h1m1s)> <(Optional: reason)>")
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def tempmute(self, ctx, members: commands.Greedy[discord.Member], time,
                       *, reason: commands.clean_content = None):
        """ Temporarily mute member(s) """
        delta = await parse_time(time.lower())
        remind_at = datetime.datetime.now() + delta
        human_time = datetime.datetime.strftime(remind_at, "%H:%M:%S on %a %d %b")
    
        # Role.
        muted_role = discord.utils.get(ctx.guild.roles, name='Muted')
        if not muted_role:
            muted_role = await ctx.guild.create_role(name="Muted")  # Read Messages / Read mesasge history.
            await muted_role.edit(position=ctx.me.top_role.position - 1)
            m_overwrite = discord.PermissionOverwrite(add_reactions=False, send_messages=False)
        
            for i in ctx.guild.text_channels:
                await i.set_permissions(muted_role, overwrite=m_overwrite)

        # Mute
        for i in members:
            await i.add_roles(muted_role, reason=f"{ctx.author}: {reason}")
            connection = await self.bot.db.acquire()
            record = await connection.fetchrow(""" INSERT INTO reminders
            (message_id, channel_id, guild_id, reminder_content,
             created_time, target_time, user_id, mod_action, mod_target)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *""",
            ctx.message.id, ctx.channel.id, ctx.guild.id, reason,
            ctx.message.created_at, remind_at, ctx.author.id, "unmute", i.id)
            await self.bot.db.release(connection)
            self.bot.reminders.append(self.bot.loop.create_task(spool_reminder(ctx.bot, record)))
    
        e = discord.Embed()
        e.title = "⏰ User muted"
        e.description = f"{', '.join([i.mention for i in members])} temporarily muted:"
        e.add_field(name="Until", value=human_time)
        if reason is not None:
            e.add_field(name="Reason", value=reason)
        e.colour = 0x00ffff
        e.timestamp = remind_at
        await ctx.send(embed=e)

    @commands.command(usage="tempblock <members: @member1 @member2> <time (e.g. 1d1h1m1s)> <(Optional: reason)>")
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def tempblock(self, ctx, channel: typing.Optional[discord.TextChannel],
                        members: commands.Greedy[discord.Member], time, *, reason: commands.clean_content = None):
        """ Temporarily mute member(s) """
        if channel is None:
            channel = ctx.channel
    
        delta = await parse_time(time.lower())
        remind_at = datetime.datetime.now() + delta
        human_time = datetime.datetime.strftime(remind_at, "%H:%M:%S on %a %d %b")
    
        ow = discord.PermissionOverwrite(read_messages=False, send_messages=False)
    
        # Mute, send to notification channel if exists.
        for i in members:
            await channel.set_permissions(i, overwrite=ow)
        
            connection = await self.bot.db.acquire()
            record = await connection.fetchval(""" INSERT INTO reminders (message_id, channel_id, guild_id,
            reminder_content,
            created_time, target_time. user_id, mod_action, mod_target) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *""", ctx.message.id, channel.id, ctx.guild.id, reason, datetime.datetime.now(), remind_at,
                                              ctx.author.id, "unblock", i.id)
            await self.bot.db.release(connection)
            self.bot.reminders.append(self.bot.loop.create_task(spool_reminder(ctx.bot, record)))
    
        e = discord.Embed()
        e.title = "⏰ User blocked"
        e.description = f"{', '.join([i.mention for i in members])} will be blocked from {channel.mention} " \
                        f"\n{reason}\nuntil\n {human_time}"
        e.colour = 0x00ffff
        e.timestamp = remind_at
        await ctx.send(embed=e)

def setup(bot):
    bot.add_cog(Mod(bot))

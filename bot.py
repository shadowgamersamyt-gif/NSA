import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, View, Select
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
import json
import asyncio
from flask import Flask
import threading
import os
# -------------------------
# FLASK KEEP-ALIVE SETUP
# -------------------------
app = Flask("")

@app.route("/")
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# Start Flask in a separate thread
threading.Thread(target=run_flask).start()

# -------------------------
# DISCORD INTENTS
# -------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Your two server IDs
GUILD_IDS = [1381367766535372903, 1415839232672403468]

async def clear_and_resync_commands():
    # 1. Clear all global commands (removes leftovers)
    await bot.tree.clear_commands(guild=None)
    await bot.tree.sync()

    # 2. Clear and resync per-guild commands
    for guild_id in GUILD_IDS:
        guild = discord.Object(id=guild_id)
        await bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"🗑️ Cleared and re-synced commands for guild {guild_id}")

@bot.event
async def on_ready():
    await clear_and_resync_commands()
    print(f"✅ Logged in as {bot.user}")

# -------------------------
# GLOBAL BOT MESSAGE FILTER
# -------------------------
@bot.check
async def ignore_bot_commands(ctx):
    # Prevent commands from running if the author is a bot
    return not ctx.author.bot

def get_db():
    return psycopg2.connect(os.getenv('DATABASE_URL'))

def parse_time_string(time_str):
    """Parse natural language time like '2 hours', '30 minutes', '1 day' into seconds or absolute datetime"""
    import re
    from dateutil import parser as dateparser
    
    time_str = time_str.strip()
    
    match = re.match(r'(\d+)\s*(hour|hr|h|minute|min|m|day|d|week|w)s?', time_str.lower())
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        
        if unit in ['hour', 'hr', 'h']:
            return ('relative', value * 3600)
        elif unit in ['minute', 'min', 'm']:
            return ('relative', value * 60)
        elif unit in ['day', 'd']:
            return ('relative', value * 86400)
        elif unit in ['week', 'w']:
            return ('relative', value * 604800)
    
    try:
        parsed_dt = dateparser.parse(time_str, fuzzy=True)
        if parsed_dt:
            return ('absolute', parsed_dt)
    except:
        pass
    
    return None

def log_event(guild_id, event_type, target_user_id=None, actor_user_id=None, details=None):
    """Log an event to the database"""
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            INSERT INTO activity_logs (guild_id, event_type, target_user_id, actor_user_id, details, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (guild_id, event_type, target_user_id, actor_user_id, json.dumps(details) if details else None, datetime.now()))
        
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f'Error logging event: {e}')

async def send_global_log(guild, event_type, embed):
    """Send log to global log channel if configured"""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute('SELECT * FROM global_log_config WHERE guild_id = %s AND enabled = true', (guild.id,))
        config = cur.fetchone()
        
        cur.close()
        conn.close()
        
        if config and config['channel_id']:
            channel = guild.get_channel(config['channel_id'])
            if channel:
                await channel.send(embed=embed)
    except Exception as e:
        print(f'Error sending global log: {e}')

async def check_raid_pattern(guild, member):
    """Check if there's a raid pattern and alert if necessary"""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute('SELECT * FROM security_config WHERE guild_id = %s AND anti_raid_enabled = true', (guild.id,))
        config = cur.fetchone()
        
        if not config:
            cur.close()
            conn.close()
            return
        
        account_age_days = (datetime.now() - member.created_at.replace(tzinfo=None)).days
        is_suspicious = account_age_days < config['min_account_age']
        
        cur.execute('''
            INSERT INTO raid_tracking (guild_id, user_id, account_created_at, is_suspicious)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET joined_at = CURRENT_TIMESTAMP
        ''', (guild.id, member.id, member.created_at, is_suspicious))
        
        cur.execute('''
            SELECT COUNT(*) as join_count FROM raid_tracking 
            WHERE guild_id = %s AND joined_at > NOW() - INTERVAL '%s seconds'
        ''', (guild.id, config['raid_time_window']))
        
        join_count = cur.fetchone()['join_count']
        
        conn.commit()
        
        if join_count >= config['raid_threshold']:
            embed = discord.Embed(
                title="🚨 RAID DETECTED",
                description=f"Detected {join_count} members joining within {config['raid_time_window']} seconds!",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            embed.add_field(name="Latest Member", value=f"{member.mention} ({member.id})", inline=False)
            embed.add_field(name="Account Age", value=f"{account_age_days} days", inline=True)
            embed.add_field(name="Suspicious", value="Yes" if is_suspicious else "No", inline=True)
            
            if config['alert_role_id']:
                alert_role = guild.get_role(config['alert_role_id'])
                if alert_role:
                    embed.description = f"{alert_role.mention}\n\n" + embed.description
            
            await send_global_log(guild, 'raid_detected', embed)
            
            if config['auto_lockdown']:
                lockdown_config = cur.execute('SELECT * FROM lockdown_config WHERE guild_id = %s', (guild.id,))
                if lockdown_config and not lockdown_config.get('is_active'):
                    embed.add_field(name="Auto-Response", value="🔒 Initiating automatic lockdown...", inline=False)
        
        cur.close()
        conn.close() 
        
    except Exception as e:
        print(f'Error checking raid pattern: {e}')

def init_db():
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('DROP TABLE IF EXISTS staff_points CASCADE')
    cur.execute('DROP TABLE IF EXISTS rank_config CASCADE')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS welcome_config (
            guild_id BIGINT PRIMARY KEY,
            channel_id BIGINT,
            message TEXT,
            auto_role_id BIGINT
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS training_config (
            guild_id BIGINT,
            training_type TEXT,
            channel_id BIGINT,
            message TEXT,
            PRIMARY KEY (guild_id, training_type)
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS monthly_awards (
            guild_id BIGINT,
            award_type TEXT,
            channel_id BIGINT,
            message TEXT,
            PRIMARY KEY (guild_id, award_type)
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS warnings (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT,
            user_id BIGINT,
            warning_number INTEGER,
            reason TEXT,
            issued_by BIGINT,
            issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS training_messages (
            message_id BIGINT PRIMARY KEY,
            guild_id BIGINT,
            channel_id BIGINT,
            training_type TEXT,
            message_template TEXT,
            training_time TEXT,
            host_id BIGINT
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS training_roles (
            guild_id BIGINT PRIMARY KEY,
            helper_role_id BIGINT
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS reaction_role_groups (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT,
            group_name TEXT,
            message_id BIGINT,
            channel_id BIGINT,
            description TEXT,
            is_exclusive BOOLEAN DEFAULT true
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS reaction_role_options (
            id SERIAL PRIMARY KEY,
            group_id INTEGER REFERENCES reaction_role_groups(id) ON DELETE CASCADE,
            role_id BIGINT,
            button_label TEXT,
            button_style TEXT DEFAULT 'primary'
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS agent_files (
            guild_id BIGINT,
            user_id BIGINT,
            agent_name TEXT,
            division TEXT,
            rank TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS duty_status (
            guild_id BIGINT,
            user_id BIGINT,
            is_on_duty BOOLEAN DEFAULT false,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by BIGINT,
            PRIMARY KEY (guild_id, user_id)
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS polls (
            poll_id SERIAL PRIMARY KEY,
            guild_id BIGINT,
            channel_id BIGINT,
            message_id BIGINT,
            question TEXT,
            options TEXT,
            created_by BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            is_active BOOLEAN DEFAULT true
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS poll_votes (
            poll_id INTEGER REFERENCES polls(poll_id) ON DELETE CASCADE,
            user_id BIGINT,
            option_index INTEGER,
            voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (poll_id, user_id)
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS activity_logs (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT,
            event_type TEXT,
            target_user_id BIGINT,
            actor_user_id BIGINT,
            details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS log_channels (
            guild_id BIGINT,
            event_type TEXT,
            channel_id BIGINT,
            PRIMARY KEY (guild_id, event_type)
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS lockdown_config (
            guild_id BIGINT PRIMARY KEY,
            is_active BOOLEAN DEFAULT false,
            director_role_id BIGINT,
            announcement_channel_id BIGINT,
            roles_to_ping TEXT,
            initiated_by BIGINT,
            initiated_at TIMESTAMP
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS lockdown_permissions (
            guild_id BIGINT,
            channel_id BIGINT,
            permissions_json TEXT,
            PRIMARY KEY (guild_id, channel_id)
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS presence_config (
            guild_id BIGINT PRIMARY KEY,
            activity_type TEXT DEFAULT 'playing',
            status_message TEXT DEFAULT 'Managing the Agency',
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS duty_role_config (
            guild_id BIGINT PRIMARY KEY,
            on_duty_role_id BIGINT
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS global_log_config (
            guild_id BIGINT PRIMARY KEY,
            channel_id BIGINT,
            enabled BOOLEAN DEFAULT true
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS security_config (
            guild_id BIGINT PRIMARY KEY,
            anti_raid_enabled BOOLEAN DEFAULT false,
            raid_threshold INTEGER DEFAULT 5,
            raid_time_window INTEGER DEFAULT 30,
            min_account_age INTEGER DEFAULT 7,
            auto_lockdown BOOLEAN DEFAULT false,
            alert_role_id BIGINT,
            permission_guard_enabled BOOLEAN DEFAULT false,
            trusted_role_ids TEXT
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS raid_tracking (
            guild_id BIGINT,
            user_id BIGINT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            account_created_at TIMESTAMP,
            is_suspicious BOOLEAN DEFAULT false,
            PRIMARY KEY (guild_id, user_id)
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS permission_changes (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT,
            role_id BIGINT,
            changed_by BIGINT,
            changes TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reverted BOOLEAN DEFAULT false
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS training_attendance (
            message_id BIGINT,
            user_id BIGINT,
            reaction_type TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, user_id, reaction_type)
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS role_request_config (
            guild_id BIGINT PRIMARY KEY,
            panel_channel_id BIGINT,
            review_channel_id BIGINT,
            probationary_private_role_id BIGINT,
            private_role_id BIGINT,
            private_agent_role_id BIGINT
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS role_requests (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT,
            user_id BIGINT,
            requested_role TEXT,
            training_officer_id BIGINT,
            screenshot_url TEXT,
            status TEXT DEFAULT 'pending',
            review_message_id BIGINT,
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP,
            reviewed_by BIGINT
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS admin_roles (
            guild_id BIGINT,
            role_id BIGINT,
            PRIMARY KEY (guild_id, role_id)
        )
    ''')
    
    conn.commit()
    cur.close()
    conn.close()

async def update_training_message(message_id, guild):
    """Update training message with current attendance and helpers"""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute('SELECT * FROM training_messages WHERE message_id = %s', (message_id,))
        training_msg = cur.fetchone()
        
        if not training_msg:
            cur.close()
            conn.close()
            return
        
        cur.execute('''
            SELECT user_id, reaction_type FROM training_attendance 
            WHERE message_id = %s
        ''', (message_id,))
        attendance = cur.fetchall()
        
        cur.close()
        conn.close()
        
        attending = []
        helping = []
        
        for record in attendance:
            if record['reaction_type'] == 'attending':
                attending.append(f"<@{record['user_id']}>")
            elif record['reaction_type'] == 'helping':
                helping.append(f"<@{record['user_id']}>")
        
        channel = guild.get_channel(training_msg['channel_id'])
        if not channel:
            return
        
        try:
            message = await channel.fetch_message(message_id)
        except:
            return
        
        parsed_time = parse_time_string(training_msg['training_time'])
        if parsed_time:
            time_type, time_value = parsed_time
            if time_type == 'relative':
                target_time = datetime.now() + timedelta(seconds=time_value)
                discord_timestamp = f"<t:{int(target_time.timestamp())}:F>"
            else:
                discord_timestamp = f"<t:{int(time_value.timestamp())}:F>"
        else:
            discord_timestamp = training_msg['training_time']
        
        host = guild.get_member(training_msg['host_id'])
        host_mention = host.mention if host else f"<@{training_msg['host_id']}>"
        
        training_display = training_msg['training_type'].replace('_', ' ').title()
        
        embed = discord.Embed(
            title=f"🎓 {training_display} Training",
            description=f"**Time:** {discord_timestamp}\n**Host:** {host_mention}",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        if attending:
            embed.add_field(
                name=f"✅ Attending ({len(attending)})",
                value=', '.join(attending),
                inline=False
            )
        else:
            embed.add_field(
                name="✅ Attending (0)",
                value="None yet",
                inline=False
            )
        
        if helping:
            embed.add_field(
                name=f"🦅 Helping ({len(helping)})",
                value=', '.join(helping),
                inline=False
            )
        else:
            embed.add_field(
                name="🦅 Helping (0)",
                value="None yet",
                inline=False
            )
        
        embed.set_footer(text="React with ✅ to attend or 🦅 to help")
        
        await message.edit(embed=embed)
        
    except Exception as e:
        print(f'Error updating training message: {e}')

class PollView(View):
    def __init__(self, poll_id: int, options: list):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        
        for i, option in enumerate(options):
            button = Button(
                label=option,
                style=discord.ButtonStyle.primary,
                custom_id=f"poll_{poll_id}_{i}"
            )
            button.callback = self.create_callback(i)
            self.add_item(button)
    
    def create_callback(self, option_index: int):
        async def button_callback(interaction: discord.Interaction):
            conn = get_db()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            cur.execute('SELECT is_active FROM polls WHERE poll_id = %s', (self.poll_id,))
            poll = cur.fetchone()
            
            if not poll or not poll['is_active']:
                await interaction.response.send_message('❌ This poll is no longer active!', ephemeral=True)
                cur.close()
                conn.close()
                return
            
            cur.execute('SELECT * FROM poll_votes WHERE poll_id = %s AND user_id = %s', 
                       (self.poll_id, interaction.user.id))
            existing_vote = cur.fetchone()
            
            if existing_vote:
                cur.execute('''
                    UPDATE poll_votes SET option_index = %s, voted_at = %s
                    WHERE poll_id = %s AND user_id = %s
                ''', (option_index, datetime.now(), self.poll_id, interaction.user.id))
                await interaction.response.send_message(f'✅ Vote updated!', ephemeral=True)
            else:
                cur.execute('''
                    INSERT INTO poll_votes (poll_id, user_id, option_index, voted_at)
                    VALUES (%s, %s, %s, %s)
                ''', (self.poll_id, interaction.user.id, option_index, datetime.now()))
                await interaction.response.send_message(f'✅ Vote recorded!', ephemeral=True)
            
            conn.commit()
            cur.close()
            conn.close()
        
        return button_callback

class ReactionRoleView(View):
    def __init__(self, group_id: int, options: list, is_exclusive: bool):
        super().__init__(timeout=None)
        self.group_id = group_id
        self.is_exclusive = is_exclusive
        
        for option in options:
            button = Button(
                label=option['button_label'],
                style=self.get_button_style(option['button_style']),
                custom_id=f"reaction_role_{group_id}_{option['role_id']}"
            )
            button.callback = self.create_callback(option['role_id'])
            self.add_item(button)
    
    def get_button_style(self, style_name: str):
        styles = {
            'primary': discord.ButtonStyle.primary,
            'secondary': discord.ButtonStyle.secondary,
            'success': discord.ButtonStyle.success,
            'danger': discord.ButtonStyle.danger
        }
        return styles.get(style_name, discord.ButtonStyle.primary)
    
    def create_callback(self, role_id: int):
        async def button_callback(interaction: discord.Interaction):
            role = interaction.guild.get_role(role_id)
            if not role:
                await interaction.response.send_message('❌ Role not found!', ephemeral=True)
                return
            
            if self.is_exclusive:
                conn = get_db()
                cur = conn.cursor(cursor_factory=RealDictCursor)
                
                cur.execute('''
                    SELECT role_id FROM reaction_role_options 
                    WHERE group_id = %s
                ''', (self.group_id,))
                
                group_roles = cur.fetchall()
                cur.close()
                conn.close()
                
                roles_to_remove = []
                for role_data in group_roles:
                    group_role = interaction.guild.get_role(role_data['role_id'])
                    if group_role and group_role in interaction.user.roles and group_role.id != role_id:
                        roles_to_remove.append(group_role)
                
                if roles_to_remove:
                    await interaction.user.remove_roles(*roles_to_remove)
            
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role)
                await interaction.response.send_message(f'✅ Removed {role.name} role!', ephemeral=True)
            else:
                await interaction.user.add_roles(role)
                await interaction.response.send_message(f'✅ Added {role.name} role!', ephemeral=True)
        
        return button_callback

async def check_admin_permission(interaction: discord.Interaction) -> bool:
    """Check if user has admin permissions based on configured admin roles"""
    if interaction.user.guild_permissions.administrator:
        return True
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT role_id FROM admin_roles WHERE guild_id = %s', (interaction.guild.id,))
    admin_roles = cur.fetchall()
    
    cur.close()
    conn.close()
    
    user_role_ids = [role.id for role in interaction.user.roles]
    for admin_role in admin_roles:
        if admin_role['role_id'] in user_role_ids:
            return True
    
    return False

async def process_role_request_screenshot(request_id: int, screenshot_url: str, guild):
    """Process a role request with screenshot and send to review channel"""
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('''
        UPDATE role_requests 
        SET screenshot_url = %s, status = 'pending'
        WHERE id = %s
    ''', (screenshot_url, request_id))
    
    cur.execute('SELECT * FROM role_requests WHERE id = %s', (request_id,))
    request = cur.fetchone()
    
    cur.execute('SELECT * FROM role_request_config WHERE guild_id = %s', (guild.id,))
    config = cur.fetchone()
    
    conn.commit()
    
    if config and config['review_channel_id'] and request:
        review_channel = guild.get_channel(config['review_channel_id'])
        if review_channel:
            member = guild.get_member(request['user_id'])
            embed = discord.Embed(
                title="🎭 New Role Request",
                description=f"{member.mention if member else 'Unknown User'} has requested a role!",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            embed.add_field(name="Requested Role", value=request['requested_role'], inline=True)
            embed.add_field(name="Training Officer", value=f"<@{request['training_officer_id']}>", inline=True)
            embed.add_field(name="User", value=f"{member.mention if member else 'Unknown'} ({request['user_id']})", inline=False)
            embed.set_image(url=screenshot_url)
            embed.set_footer(text=f"Request ID: {request_id}")
            
            view = RoleRequestReviewView(request_id)
            review_message = await review_channel.send(embed=embed, view=view)
            
            cur.execute('''
                UPDATE role_requests 
                SET review_message_id = %s 
                WHERE id = %s
            ''', (review_message.id, request_id))
            conn.commit()
    
    cur.close()
    conn.close()

class RoleRequestReviewView(View):
    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id
        
        approve_button = Button(
            label='Approve',
            style=discord.ButtonStyle.success,
            custom_id=f'role_request_approve_{request_id}'
        )
        approve_button.callback = self.approve_callback
        
        deny_button = Button(
            label='Deny',
            style=discord.ButtonStyle.danger,
            custom_id=f'role_request_deny_{request_id}'
        )
        deny_button.callback = self.deny_callback
        
        self.add_item(approve_button)
        self.add_item(deny_button)
    
    async def approve_callback(self, interaction: discord.Interaction):
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute('SELECT * FROM role_requests WHERE id = %s', (self.request_id,))
        request = cur.fetchone()
        
        if not request:
            await interaction.response.send_message('❌ Request not found!', ephemeral=True)
            cur.close()
            conn.close()
            return
        
        if request['status'] != 'pending':
            await interaction.response.send_message('❌ This request has already been reviewed!', ephemeral=True)
            cur.close()
            conn.close()
            return
        
        cur.execute('SELECT * FROM role_request_config WHERE guild_id = %s', (interaction.guild.id,))
        config = cur.fetchone()
        
        if not config:
            await interaction.response.send_message('❌ Role request system not configured!', ephemeral=True)
            cur.close()
            conn.close()
            return
        
        role_mapping = {
            'Probationary Private': config['probationary_private_role_id'],
            'Private': config['private_role_id'],
            'Private Agent': config['private_agent_role_id']
        }
        
        role_id = role_mapping.get(request['requested_role'])
        if not role_id:
            await interaction.response.send_message('❌ Role not found in configuration!', ephemeral=True)
            cur.close()
            conn.close()
            return
        
        role = interaction.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message('❌ Role not found in server!', ephemeral=True)
            cur.close()
            conn.close()
            return
        
        member = interaction.guild.get_member(request['user_id'])
        if not member:
            await interaction.response.send_message('❌ Member not found!', ephemeral=True)
            cur.close()
            conn.close()
            return
        
        await member.add_roles(role)
        
        cur.execute('''
            UPDATE role_requests 
            SET status = 'approved', reviewed_at = %s, reviewed_by = %s 
            WHERE id = %s
        ''', (datetime.now(), interaction.user.id, self.request_id))
        conn.commit()
        
        cur.close()
        conn.close()
        
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(name="Status", value=f"✅ Approved by {interaction.user.mention}", inline=False)
        
        await interaction.message.edit(embed=embed, view=None)
        await interaction.response.send_message(f'✅ Request approved! {role.mention} role has been given to {member.mention}', ephemeral=True)
        
        try:
            await member.send(f'✅ Your role request for **{request["requested_role"]}** has been approved! You have been given the {role.mention} role.')
        except:
            pass
    
    async def deny_callback(self, interaction: discord.Interaction):
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute('SELECT * FROM role_requests WHERE id = %s', (self.request_id,))
        request = cur.fetchone()
        
        if not request:
            await interaction.response.send_message('❌ Request not found!', ephemeral=True)
            cur.close()
            conn.close()
            return
        
        if request['status'] != 'pending':
            await interaction.response.send_message('❌ This request has already been reviewed!', ephemeral=True)
            cur.close()
            conn.close()
            return
        
        cur.execute('''
            UPDATE role_requests 
            SET status = 'denied', reviewed_at = %s, reviewed_by = %s 
            WHERE id = %s
        ''', (datetime.now(), interaction.user.id, self.request_id))
        conn.commit()
        
        cur.close()
        conn.close()
        
        member = interaction.guild.get_member(request['user_id'])
        
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(name="Status", value=f"❌ Denied by {interaction.user.mention}", inline=False)
        
        await interaction.message.edit(embed=embed, view=None)
        await interaction.response.send_message(f'❌ Request denied.', ephemeral=True)
        
        if member:
            try:
                await member.send(f'❌ Your role request for **{request["requested_role"]}** has been denied.')
            except:
                pass

class MemberSelect(discord.ui.Select):
    def __init__(self, members: list, requester: discord.Member):
        allowed_role_ids = [1423052817496674304, 1422375802011389982, 1410404396675895406]

        options = []
        count = 0

        for member in members:
            if member.bot:
                continue  # skip bots
            if member == requester:
                continue  # exclude requester
            if any(role.id in allowed_role_ids for role in member.roles):
                options.append(discord.SelectOption(
                    label=member.display_name[:100],
                    value=str(member.id),
                    description=f"ID: {member.id}"
                ))
                count += 1
                if count >= 25:  # Discord dropdown limit
                    break

        super().__init__(
            placeholder="Select your training officer...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="training_officer_select"
        )
    
    async def callback(self, interaction: discord.Interaction):
        selected_member_id = int(self.values[0])
        
        selected_role = None
        for item in self.view.children:
            if isinstance(item, RoleSelect):
                if hasattr(item, 'selected_value'):
                    selected_role = item.selected_value
                    break
        
        if not selected_role:
            await interaction.response.send_message('❌ Please select a role first!', ephemeral=True)
            return
        
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute('''
            INSERT INTO role_requests (guild_id, user_id, requested_role, training_officer_id, status)
            VALUES (%s, %s, %s, %s, 'awaiting_screenshot')
            RETURNING id
        ''', (interaction.guild.id, interaction.user.id, selected_role, selected_member_id))
        
        request_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        
        training_officer = interaction.guild.get_member(selected_member_id)
        officer_name = training_officer.display_name if training_officer else "Unknown"
        
        await interaction.response.send_message(
            f'✅ Role request created!\n\n'
            f'**Selected Role:** {selected_role}\n'
            f'**Training Officer:** {officer_name}\n\n'
            f'📸 **Next Step:** Please upload your screenshot as an attachment in your next message in this channel.\n'
            f'The screenshot will be automatically added to your request.',
            ephemeral=True
        )

class RoleSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Probationary Private", value="Probationary Private", emoji="🎖️"),
            discord.SelectOption(label="Private", value="Private", emoji="🎖️"),
            discord.SelectOption(label="Private Agent", value="Private Agent", emoji="🎖️")
        ]
        super().__init__(
            placeholder="Select the role you want to request...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="role_select"
        )
        self.selected_value = None
    
    async def callback(self, interaction: discord.Interaction):
        self.selected_value = self.values[0]
        await interaction.response.send_message(f'✅ Selected: **{self.selected_value}**. Now select your training officer from the dropdown below.', ephemeral=True)

class RoleRequestPanelView(View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild
        
        role_select = RoleSelect()
        self.add_item(role_select)

    
    async def setup_hook(self):
        print("Initializing database...")
        init_db()
        print("Loading persistent views...")
        await self.load_persistent_views()
        print("Syncing commands with Discord...")
        await self.tree.sync()
        print("Commands synced!")
        
        self.presence_update_loop.start()
    
    async def load_persistent_views(self):
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute('SELECT * FROM reaction_role_groups WHERE message_id IS NOT NULL')
        groups = cur.fetchall()
        
        for group in groups:
            cur.execute('SELECT * FROM reaction_role_options WHERE group_id = %s', (group['id'],))
            options = cur.fetchall()
            
            if options:
                view = ReactionRoleView(group['id'], options, group['is_exclusive'])
                self.add_view(view)
        
        cur.execute('SELECT * FROM polls WHERE is_active = true')
        polls = cur.fetchall()
        
        for poll in polls:
            options = json.loads(poll['options'])
            view = PollView(poll['poll_id'], options)
            self.add_view(view)
        
        cur.close()
        conn.close()
    
    @tasks.loop(minutes=5)
    async def presence_update_loop(self):
        """Keep bot presence updated"""
        try:
            conn = get_db()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            cur.execute('SELECT * FROM presence_config LIMIT 1')
            config = cur.fetchone()
            
            cur.close()
            conn.close()
            
            if config:
                activity_text = config.get('status_message', 'Managing the Agency')
                activity_type = config.get('activity_type', 'playing')
                
                if activity_type == 'playing':
                    activity = discord.Game(name=activity_text)
                elif activity_type == 'watching':
                    activity = discord.Activity(type=discord.ActivityType.watching, name=activity_text)
                elif activity_type == 'listening':
                    activity = discord.Activity(type=discord.ActivityType.listening, name=activity_text)
                else:
                    activity = discord.Game(name=activity_text)
                
                await self.change_presence(status=discord.Status.online, activity=activity)
            else:
                await self.change_presence(status=discord.Status.online, activity=discord.Game(name="Managing the Agency"))
        except Exception as e:
            print(f'Error updating presence: {e}')
    
    @presence_update_loop.before_loop
    async def before_presence_loop(self):
        await self.wait_until_ready()

@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.online, activity=discord.Game(name="Managing the Agency"))
    print(f'{bot.user} has connected to Discord!')
    print(f'Bot is in {len(bot.guilds)} servers')

@bot.event
async def on_disconnect():
    print('Bot disconnected! Attempting to reconnect...')

@bot.event
async def on_resumed():
    print('Bot reconnected successfully!')

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM training_messages WHERE message_id = %s', (reaction.message.id,))
    training_msg = cur.fetchone()
    
    if not training_msg:
        cur.close()
        conn.close()
        return
    
    reaction_type = None
    if str(reaction.emoji) == '✅':
        reaction_type = 'attending'
    elif str(reaction.emoji) == '🦅':
        reaction_type = 'helping'
    
    if reaction_type:
        cur.execute('''
            INSERT INTO training_attendance (message_id, user_id, reaction_type)
            VALUES (%s, %s, %s)
            ON CONFLICT (message_id, user_id, reaction_type) DO NOTHING
        ''', (reaction.message.id, user.id, reaction_type))
        
        conn.commit()
        cur.close()
        conn.close()
        
        await update_training_message(reaction.message.id, reaction.message.guild)
    else:
        cur.close()
        conn.close()

@bot.event
async def on_reaction_remove(reaction, user):
    if user.bot:
        return
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM training_messages WHERE message_id = %s', (reaction.message.id,))
    training_msg = cur.fetchone()
    
    if not training_msg:
        cur.close()
        conn.close()
        return
    
    reaction_type = None
    if str(reaction.emoji) == '✅':
        reaction_type = 'attending'
    elif str(reaction.emoji) == '🦅':
        reaction_type = 'helping'
    
    if reaction_type:
        cur.execute('''
            DELETE FROM training_attendance 
            WHERE message_id = %s AND user_id = %s AND reaction_type = %s
        ''', (reaction.message.id, user.id, reaction_type))
        
        conn.commit()
        cur.close()
        conn.close()
        
        await update_training_message(reaction.message.id, reaction.message.guild)
    else:
        cur.close()
        conn.close()

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    if not message.guild:
        return
    
    if not message.attachments:
        return
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('''
        SELECT * FROM role_requests 
        WHERE guild_id = %s AND user_id = %s AND status = 'awaiting_screenshot'
        ORDER BY requested_at DESC
        LIMIT 1
    ''', (message.guild.id, message.author.id))
    
    pending_request = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if pending_request:
        image_attachment = None
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith('image/'):
                image_attachment = attachment
                break
        
        if image_attachment:
            try:
                await message.add_reaction('✅')
                await process_role_request_screenshot(pending_request['id'], image_attachment.url, message.guild)
                await message.delete()

                try:
                    await message.author.send(
                        f'✅ Your role request has been submitted for review!\n\n'
                        f'**Requested Role:** {pending_request["requested_role"]}\n'
                        f'**Training Officer:** <@{pending_request["training_officer_id"]}>\n\n'
                        f'You will be notified when your request is reviewed.'
                    )
                except:
                    await message.channel.send(
                        f'{message.author.mention} ✅ Your role request has been submitted for review!',
                        delete_after=10
                    )
            except Exception as e:
                print(f'Error processing screenshot: {e}')
                await message.add_reaction('❌')
        else:
            await message.add_reaction('❌')
            try:
                await message.author.send('❌ Please upload an image file (PNG, JPG, etc.) for your role request screenshot.')
            except:
                pass

@bot.event
async def on_member_join(member):
    log_event(member.guild.id, 'member_join', target_user_id=member.id, details={'username': str(member)})
    
    account_age_days = (datetime.now() - member.created_at.replace(tzinfo=None)).days
    
    embed = discord.Embed(
        title="👋 Member Joined",
        description=f"{member.mention} joined the server",
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    embed.add_field(name="User", value=str(member), inline=True)
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.add_field(name="Account Age", value=f"{account_age_days} days", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    
    await send_global_log(member.guild, 'member_join', embed)
    await check_raid_pattern(member.guild, member)
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT channel_id FROM log_channels WHERE guild_id = %s AND event_type = %s', 
                (member.guild.id, 'member_join'))
    log_channel_config = cur.fetchone()
    
    if log_channel_config:
        log_channel = member.guild.get_channel(log_channel_config['channel_id'])
        if log_channel:
            await log_channel.send(embed=embed)
    
    cur.execute('SELECT * FROM welcome_config WHERE guild_id = %s', (member.guild.id,))
    config = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if config:
        if config['channel_id']:
            channel = member.guild.get_channel(config['channel_id'])
            if channel and config['message']:
                message = config['message'].replace('{user}', member.mention).replace('{server}', member.guild.name)
                await channel.send(message)
        
        if config['auto_role_id']:
            role = member.guild.get_role(config['auto_role_id'])
            if role:
                await member.add_roles(role)

@bot.event
async def on_member_remove(member):
    log_event(member.guild.id, 'member_leave', target_user_id=member.id, details={'username': str(member)})
    
    embed = discord.Embed(
        title="👋 Member Left",
        description=f"{member.mention} left the server",
        color=discord.Color.red(),
        timestamp=datetime.now()
    )
    embed.add_field(name="User", value=str(member), inline=True)
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    
    await send_global_log(member.guild, 'member_leave', embed)
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT channel_id FROM log_channels WHERE guild_id = %s AND event_type = %s', 
                (member.guild.id, 'member_leave'))
    log_channel_config = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if log_channel_config:
        log_channel = member.guild.get_channel(log_channel_config['channel_id'])
        if log_channel:
            await log_channel.send(embed=embed)

@bot.event
async def on_member_update(before, after):
    if before.roles != after.roles:
        added_roles = [r for r in after.roles if r not in before.roles]
        removed_roles = [r for r in before.roles if r not in after.roles]
        
        log_event(after.guild.id, 'member_role_update', target_user_id=after.id, 
                 details={'added': [r.name for r in added_roles], 'removed': [r.name for r in removed_roles]})
        
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute('SELECT channel_id FROM log_channels WHERE guild_id = %s AND event_type = %s', 
                    (after.guild.id, 'member_role_update'))
        log_channel_config = cur.fetchone()
        
        cur.close()
        conn.close()
        
        if log_channel_config:
            log_channel = after.guild.get_channel(log_channel_config['channel_id'])
            if log_channel:
                embed = discord.Embed(
                    title="Member Roles Updated",
                    description=f"{after.mention}'s roles were changed",
                    color=discord.Color.blue(),
                    timestamp=datetime.now()
                )
                if added_roles:
                    embed.add_field(name="Added Roles", value=", ".join([r.mention for r in added_roles]), inline=False)
                if removed_roles:
                    embed.add_field(name="Removed Roles", value=", ".join([r.mention for r in removed_roles]), inline=False)
                await log_channel.send(embed=embed)

@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return
    
    log_event(message.guild.id, 'message_delete', target_user_id=message.author.id, actor_user_id=message.author.id,
             details={'content': message.content[:500], 'channel': message.channel.name})
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT channel_id FROM log_channels WHERE guild_id = %s AND event_type = %s', 
                (message.guild.id, 'message_delete'))
    log_channel_config = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if log_channel_config:
        log_channel = message.guild.get_channel(log_channel_config['channel_id'])
        if log_channel and log_channel.id != message.channel.id:
            embed = discord.Embed(
                title="Message Deleted",
                color=discord.Color.orange(),
                timestamp=datetime.now()
            )
            embed.add_field(name="Author", value=message.author.mention, inline=True)
            embed.add_field(name="Channel", value=message.channel.mention, inline=True)
            if message.content:
                embed.add_field(name="Content", value=message.content[:1024], inline=False)
            await log_channel.send(embed=embed)

@bot.event
async def on_message_edit(before, after):
    if before.author.bot or before.content == after.content:
        return
    
    log_event(after.guild.id, 'message_edit', target_user_id=after.author.id, actor_user_id=after.author.id,
             details={'before': before.content[:500], 'after': after.content[:500], 'channel': after.channel.name})
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT channel_id FROM log_channels WHERE guild_id = %s AND event_type = %s', 
                (after.guild.id, 'message_edit'))
    log_channel_config = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if log_channel_config:
        log_channel = after.guild.get_channel(log_channel_config['channel_id'])
        if log_channel:
            embed = discord.Embed(
                title="Message Edited",
                color=discord.Color.gold(),
                timestamp=datetime.now()
            )
            embed.add_field(name="Author", value=after.author.mention, inline=True)
            embed.add_field(name="Channel", value=after.channel.mention, inline=True)
            if before.content:
                embed.add_field(name="Before", value=before.content[:1024], inline=False)
            if after.content:
                embed.add_field(name="After", value=after.content[:1024], inline=False)
            embed.add_field(name="Jump to Message", value=f"[Click here]({after.jump_url})", inline=False)
            await log_channel.send(embed=embed)

@bot.event
async def on_guild_role_update(before, after):
    if before.permissions != after.permissions:
        try:
            conn = get_db()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            cur.execute('SELECT * FROM security_config WHERE guild_id = %s AND permission_guard_enabled = true', (after.guild.id,))
            config = cur.fetchone()
            
            if config:
                async for entry in after.guild.audit_logs(limit=1, action=discord.AuditLogAction.role_update):
                    changed_by = entry.user
                    
                    changes = []
                    dangerous_perms = ['administrator', 'manage_guild', 'manage_roles', 'manage_channels', 'kick_members', 'ban_members']
                    
                    for perm in dangerous_perms:
                        before_val = getattr(before.permissions, perm)
                        after_val = getattr(after.permissions, perm)
                        if before_val != after_val:
                            changes.append(f"{perm}: {before_val} → {after_val}")
                    
                    if changes:
                        changes_str = json.dumps(changes)
                        cur.execute('''
                            INSERT INTO permission_changes (guild_id, role_id, changed_by, changes)
                            VALUES (%s, %s, %s, %s)
                        ''', (after.guild.id, after.id, changed_by.id, changes_str))
                        conn.commit()
                        
                        embed = discord.Embed(
                            title="⚠️ Role Permissions Changed",
                            description=f"Dangerous permissions were modified for {after.mention}",
                            color=discord.Color.orange(),
                            timestamp=datetime.now()
                        )
                        embed.add_field(name="Role", value=after.mention, inline=True)
                        embed.add_field(name="Changed By", value=changed_by.mention, inline=True)
                        embed.add_field(name="Changes", value="\n".join(changes), inline=False)
                        
                        if config['alert_role_id']:
                            alert_role = after.guild.get_role(config['alert_role_id'])
                            if alert_role:
                                embed.description = f"{alert_role.mention}\n\n" + embed.description
                        
                        await send_global_log(after.guild, 'permission_change', embed)
                    break
            
            cur.close()
            conn.close()
        except Exception as e:
            print(f'Error tracking permission change: {e}')

@bot.event
async def on_member_ban(guild, user):
    log_event(guild.id, 'member_ban', target_user_id=user.id, details={'username': str(user)})
    
    embed = discord.Embed(
        title="🔨 Member Banned",
        description=f"{user.mention} was banned from the server",
        color=discord.Color.red(),
        timestamp=datetime.now()
    )
    embed.add_field(name="User", value=str(user), inline=True)
    embed.add_field(name="ID", value=user.id, inline=True)
    
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
            if entry.target.id == user.id:
                embed.add_field(name="Banned By", value=entry.user.mention, inline=True)
                if entry.reason:
                    embed.add_field(name="Reason", value=entry.reason, inline=False)
                break
    except:
        pass
    
    await send_global_log(guild, 'member_ban', embed)

@bot.event
async def on_member_unban(guild, user):
    log_event(guild.id, 'member_unban', target_user_id=user.id, details={'username': str(user)})
    
    embed = discord.Embed(
        title="✅ Member Unbanned",
        description=f"{user.mention} was unbanned from the server",
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    embed.add_field(name="User", value=str(user), inline=True)
    embed.add_field(name="ID", value=user.id, inline=True)
    
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.unban):
            if entry.target.id == user.id:
                embed.add_field(name="Unbanned By", value=entry.user.mention, inline=True)
                break
    except:
        pass
    
    await send_global_log(guild, 'member_unban', embed)

@bot.event
async def on_guild_channel_create(channel):
    log_event(channel.guild.id, 'channel_create', details={'channel_name': channel.name, 'channel_type': str(channel.type)})
    
    embed = discord.Embed(
        title="📝 Channel Created",
        description=f"A new channel was created: {channel.mention}",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.add_field(name="Channel", value=channel.name, inline=True)
    embed.add_field(name="Type", value=str(channel.type), inline=True)
    
    await send_global_log(channel.guild, 'channel_create', embed)

@bot.event
async def on_guild_channel_delete(channel):
    log_event(channel.guild.id, 'channel_delete', details={'channel_name': channel.name, 'channel_type': str(channel.type)})
    
    embed = discord.Embed(
        title="🗑️ Channel Deleted",
        description=f"Channel **{channel.name}** was deleted",
        color=discord.Color.red(),
        timestamp=datetime.now()
    )
    embed.add_field(name="Channel", value=channel.name, inline=True)
    embed.add_field(name="Type", value=str(channel.type), inline=True)
    
    await send_global_log(channel.guild, 'channel_delete', embed)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f'Command error: {type(error).__name__}: {str(error)}')
    
    if isinstance(error, app_commands.errors.MissingPermissions):
        if not interaction.response.is_done():
            await interaction.response.send_message('❌ You don\'t have permission to use this command!', ephemeral=True)
    elif isinstance(error, app_commands.errors.CommandInvokeError):
        print(f'Command invoke error details: {error.original}')
        if not interaction.response.is_done():
            await interaction.response.send_message(f'❌ An error occurred: {str(error.original)}', ephemeral=True)
    else:
        print(f'Unexpected error: {error}')
        if not interaction.response.is_done():
            await interaction.response.send_message(f'❌ An unexpected error occurred!', ephemeral=True)

@bot.tree.command(name="setuprolerequest", description="Configure the role request system")
@app_commands.describe(
    panel_channel="Channel where the role request panel will be posted",
    review_channel="Channel where role requests will be sent for review",
    probationary_private_role="The Probationary Private role",
    private_role="The Private role",
    private_agent_role="The Private Agent role"
)
async def setup_role_request(
    interaction: discord.Interaction,
    panel_channel: discord.TextChannel,
    review_channel: discord.TextChannel,
    probationary_private_role: discord.Role,
    private_role: discord.Role,
    private_agent_role: discord.Role
):
    if not await check_admin_permission(interaction):
        await interaction.response.send_message('❌ You do not have permission to use this command!', ephemeral=True)
        return
    
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO role_request_config (
            guild_id, panel_channel_id, review_channel_id,
            probationary_private_role_id, private_role_id, private_agent_role_id
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (guild_id) DO UPDATE SET
            panel_channel_id = EXCLUDED.panel_channel_id,
            review_channel_id = EXCLUDED.review_channel_id,
            probationary_private_role_id = EXCLUDED.probationary_private_role_id,
            private_role_id = EXCLUDED.private_role_id,
            private_agent_role_id = EXCLUDED.private_agent_role_id
    ''', (
        interaction.guild.id,
        panel_channel.id,
        review_channel.id,
        probationary_private_role.id,
        private_role.id,
        private_agent_role.id
    ))
    
    conn.commit()
    cur.close()
    conn.close()
    
    embed = discord.Embed(
        title="✅ Role Request System Configured",
        description="The role request system has been set up successfully!",
        color=discord.Color.green()
    )
    embed.add_field(name="Panel Channel", value=panel_channel.mention, inline=True)
    embed.add_field(name="Review Channel", value=review_channel.mention, inline=True)
    embed.add_field(name="Probationary Private", value=probationary_private_role.mention, inline=False)
    embed.add_field(name="Private", value=private_role.mention, inline=False)
    embed.add_field(name="Private Agent", value=private_agent_role.mention, inline=False)
    embed.add_field(name="Next Step", value="Use `/createrolepanel` to post the role request panel", inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="createrolepanel", description="Create and post the role request panel")
async def create_role_panel(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        await interaction.response.send_message('❌ You do not have permission to use this command!', ephemeral=True)
        return

    # Get all non-bot members
    members = [m for m in interaction.guild.members if not m.bot]

    # Create dropdown for training officers
    member_select = MemberSelect(members, requester=interaction.user)

    # Load role request config from DB
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute('SELECT * FROM role_request_config WHERE guild_id = %s', (interaction.guild.id,))
    config = cur.fetchone()
    cur.close()
    conn.close()

    if not config or not config['panel_channel_id']:
        await interaction.response.send_message('❌ Role request system not configured! Use `/setuprolerequest` first.', ephemeral=True)
        return

    panel_channel = interaction.guild.get_channel(config['panel_channel_id'])
    if not panel_channel:
        await interaction.response.send_message('❌ Panel channel not found!', ephemeral=True)
        return

    embed = discord.Embed(
        title="🎭 Role Request System",
        description=(
            "Welcome to the role request system!\n\n"
            "**How to request a role:**\n"
            "1. Select the role you want from the dropdown below\n"
            "2. Select your training officer from the member list\n"
            "3. Upload your screenshot as an image attachment in this channel\n"
            "4. Wait for review and approval\n\n"
            "**Available Roles:**\n"
            "🎖️ Probationary Private\n"
            "🎖️ Private\n"
            "🎖️ Private Agent"
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.set_footer(text="Use the dropdowns below to start your request")

    # Combine role request panel + training officer dropdown
    view = RoleRequestPanelView(interaction.guild)
    view.add_item(member_select)

    # Send panel
    await panel_channel.send(embed=embed, view=view)
    await interaction.response.send_message(f'✅ Role request panel posted in {panel_channel.mention}!', ephemeral=True)

@bot.tree.command(name="addadminrole", description="Add a role that can use admin commands")
@app_commands.describe(role="The role to give admin permissions")
async def add_admin_role(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message('❌ Only server administrators can use this command!', ephemeral=True)
        return
    
    conn = get_db()
    cur = conn.cursor()
    
    try:
        cur.execute('''
            INSERT INTO admin_roles (guild_id, role_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
        ''', (interaction.guild.id, role.id))
        conn.commit()
        
        if cur.rowcount > 0:
            await interaction.response.send_message(f'✅ {role.mention} now has admin permissions for bot commands!', ephemeral=True)
        else:
            await interaction.response.send_message(f'⚠️ {role.mention} already has admin permissions!', ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f'❌ Error adding admin role: {str(e)}', ephemeral=True)
    finally:
        cur.close()
        conn.close()

@bot.tree.command(name="removeadminrole", description="Remove admin permissions from a role")
@app_commands.describe(role="The role to remove admin permissions from")
async def remove_admin_role(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message('❌ Only server administrators can use this command!', ephemeral=True)
        return
    
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('DELETE FROM admin_roles WHERE guild_id = %s AND role_id = %s', (interaction.guild.id, role.id))
    conn.commit()
    
    if cur.rowcount > 0:
        await interaction.response.send_message(f'✅ Removed admin permissions from {role.mention}!', ephemeral=True)
    else:
        await interaction.response.send_message(f'⚠️ {role.mention} did not have admin permissions!', ephemeral=True)
    
    cur.close()
    conn.close()

@bot.tree.command(name="listadminroles", description="List all roles with admin permissions")
async def list_admin_roles(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message('❌ Only server administrators can use this command!', ephemeral=True)
        return
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT role_id FROM admin_roles WHERE guild_id = %s', (interaction.guild.id,))
    admin_roles = cur.fetchall()
    
    cur.close()
    conn.close()
    
    if not admin_roles:
        await interaction.response.send_message('No admin roles configured. Use `/addadminrole` to add one.', ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🛡️ Admin Roles",
        description="Roles with admin permissions for bot commands:",
        color=discord.Color.blue()
    )
    
    role_mentions = []
    for admin_role in admin_roles:
        role = interaction.guild.get_role(admin_role['role_id'])
        if role:
            role_mentions.append(role.mention)
        else:
            role_mentions.append(f"Deleted Role (ID: {admin_role['role_id']})")
    
    embed.add_field(name="Admin Roles", value="\n".join(role_mentions) if role_mentions else "None", inline=False)
    embed.set_footer(text="Server Administrators always have full access")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setbotactivity", description="Set what the bot is playing/watching (Director only)")
@app_commands.describe(
    activity_type="Type of activity",
    message="What the bot should be doing"
)
@app_commands.choices(activity_type=[
    app_commands.Choice(name="Playing", value="playing"),
    app_commands.Choice(name="Watching", value="watching"),
    app_commands.Choice(name="Listening to", value="listening")
])
@app_commands.checks.has_permissions(administrator=True)
async def set_bot_activity(interaction: discord.Interaction, activity_type: str, message: str):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO presence_config (guild_id, activity_type, status_message, last_updated)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (guild_id) DO UPDATE 
        SET activity_type = %s, status_message = %s, last_updated = %s
    ''', (interaction.guild.id, activity_type, message, datetime.now(), activity_type, message, datetime.now()))
    
    conn.commit()
    cur.close()
    conn.close()
    
    if activity_type == 'playing':
        activity = discord.Game(name=message)
    elif activity_type == 'watching':
        activity = discord.Activity(type=discord.ActivityType.watching, name=message)
    elif activity_type == 'listening':
        activity = discord.Activity(type=discord.ActivityType.listening, name=message)
    else:
        activity = discord.Game(name=message)
    
    await bot.change_presence(status=discord.Status.online, activity=activity)
    await interaction.response.send_message(f'✅ Bot activity set to: {activity_type} {message}')

@bot.tree.command(name="registeragent", description="Register your agent file")
@app_commands.describe(
    agent_name="Your agent name",
    division="Your division",
    rank="Your rank"
)
async def register_agent(interaction: discord.Interaction, agent_name: str, division: str, rank: str):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO agent_files (guild_id, user_id, agent_name, division, rank, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (guild_id, user_id) DO UPDATE 
        SET agent_name = %s, division = %s, rank = %s, updated_at = %s
    ''', (interaction.guild.id, interaction.user.id, agent_name, division, rank, datetime.now(),
          agent_name, division, rank, datetime.now()))
    
    conn.commit()
    cur.close()
    conn.close()
    
    log_event(interaction.guild.id, 'agent_registered', target_user_id=interaction.user.id, actor_user_id=interaction.user.id,
             details={'agent_name': agent_name, 'division': division, 'rank': rank})
    
    embed = discord.Embed(
        title="Agent File Registered",
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    embed.add_field(name="Agent Name", value=agent_name, inline=False)
    embed.add_field(name="Division", value=division, inline=True)
    embed.add_field(name="Rank", value=rank, inline=True)
    embed.set_footer(text=f"Registered by {interaction.user}")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="viewagent", description="View an agent file")
@app_commands.describe(member="The member to view (leave empty for yourself)")
async def view_agent(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM agent_files WHERE guild_id = %s AND user_id = %s',
                (interaction.guild.id, target.id))
    agent = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if not agent:
        await interaction.response.send_message(f'❌ No agent file found for {target.mention}!', ephemeral=True)
        return
    
    embed = discord.Embed(
        title=f"Agent File: {agent['agent_name']}",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Discord User", value=target.mention, inline=False)
    embed.add_field(name="Division", value=agent['division'], inline=True)
    embed.add_field(name="Rank", value=agent['rank'], inline=True)
    embed.add_field(name="Registered", value=agent['created_at'].strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    embed.add_field(name="Last Updated", value=agent['updated_at'].strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="listagents", description="List all registered agents")
@app_commands.checks.has_permissions(manage_guild=True)
async def list_agents(interaction: discord.Interaction):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM agent_files WHERE guild_id = %s ORDER BY agent_name', (interaction.guild.id,))
    agents = cur.fetchall()
    
    cur.close()
    conn.close()
    
    if not agents:
        await interaction.response.send_message('❌ No agents registered yet!')
        return
    
    embed = discord.Embed(
        title=f"Registered Agents ({len(agents)})",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    for agent in agents[:25]:
        user = interaction.guild.get_member(agent['user_id'])
        user_str = user.mention if user else f"<@{agent['user_id']}>"
        embed.add_field(
            name=agent['agent_name'],
            value=f"{user_str} | {agent['division']} | {agent['rank']}",
            inline=False
        )
    
    if len(agents) > 25:
        embed.set_footer(text=f"Showing first 25 of {len(agents)} agents")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="deleteagent", description="Delete an agent file (Admin only)")
@app_commands.describe(member="The member whose agent file to delete")
@app_commands.checks.has_permissions(administrator=True)
async def delete_agent(interaction: discord.Interaction, member: discord.Member):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('DELETE FROM agent_files WHERE guild_id = %s AND user_id = %s',
                (interaction.guild.id, member.id))
    
    if cur.rowcount > 0:
        conn.commit()
        log_event(interaction.guild.id, 'agent_deleted', target_user_id=member.id, actor_user_id=interaction.user.id)
        await interaction.response.send_message(f'✅ Deleted agent file for {member.mention}!')
    else:
        await interaction.response.send_message(f'❌ No agent file found for {member.mention}!', ephemeral=True)
    
    cur.close()
    conn.close()

@bot.tree.command(name="dutyon", description="Go on duty")
async def duty_on(interaction: discord.Interaction):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('''
        INSERT INTO duty_status (guild_id, user_id, is_on_duty, last_updated, updated_by)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (guild_id, user_id) DO UPDATE 
        SET is_on_duty = true, last_updated = %s, updated_by = %s
    ''', (interaction.guild.id, interaction.user.id, True, datetime.now(), interaction.user.id,
          datetime.now(), interaction.user.id))
    
    cur.execute('SELECT on_duty_role_id FROM duty_role_config WHERE guild_id = %s', (interaction.guild.id,))
    role_config = cur.fetchone()
    
    conn.commit()
    cur.close()
    conn.close()
    
    if role_config and role_config['on_duty_role_id']:
        duty_role = interaction.guild.get_role(role_config['on_duty_role_id'])
        if duty_role:
            try:
                await interaction.user.add_roles(duty_role)
            except Exception as e:
                print(f'Error adding duty role: {e}')
    
    log_event(interaction.guild.id, 'duty_on', target_user_id=interaction.user.id, actor_user_id=interaction.user.id)
    
    await interaction.response.send_message(f'✅ {interaction.user.mention} is now **ON DUTY** 🟢')

@bot.tree.command(name="dutyoff", description="Go off duty")
async def duty_off(interaction: discord.Interaction):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('''
        INSERT INTO duty_status (guild_id, user_id, is_on_duty, last_updated, updated_by)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (guild_id, user_id) DO UPDATE 
        SET is_on_duty = false, last_updated = %s, updated_by = %s
    ''', (interaction.guild.id, interaction.user.id, False, datetime.now(), interaction.user.id,
          datetime.now(), interaction.user.id))
    
    cur.execute('SELECT on_duty_role_id FROM duty_role_config WHERE guild_id = %s', (interaction.guild.id,))
    role_config = cur.fetchone()
    
    conn.commit()
    cur.close()
    conn.close()
    
    if role_config and role_config['on_duty_role_id']:
        duty_role = interaction.guild.get_role(role_config['on_duty_role_id'])
        if duty_role:
            try:
                await interaction.user.remove_roles(duty_role)
            except Exception as e:
                print(f'Error removing duty role: {e}')
    
    log_event(interaction.guild.id, 'duty_off', target_user_id=interaction.user.id, actor_user_id=interaction.user.id)
    
    await interaction.response.send_message(f'✅ {interaction.user.mention} is now **OFF DUTY** 🔴')

@bot.tree.command(name="dutystatus", description="Check duty status")
@app_commands.describe(member="The member to check (leave empty for yourself)")
async def duty_status(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM duty_status WHERE guild_id = %s AND user_id = %s',
                (interaction.guild.id, target.id))
    status = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if not status:
        await interaction.response.send_message(f'{target.mention} has no duty status recorded. Default: **OFF DUTY** 🔴')
        return
    
    if status['is_on_duty']:
        status_text = "**ON DUTY** 🟢"
        color = discord.Color.green()
    else:
        status_text = "**OFF DUTY** 🔴"
        color = discord.Color.red()
    
    embed = discord.Embed(
        title=f"Duty Status for {target.display_name}",
        description=status_text,
        color=color,
        timestamp=datetime.now()
    )
    embed.add_field(name="Last Updated", value=status['last_updated'].strftime('%Y-%m-%d %H:%M:%S'), inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="dutylist", description="List all on-duty members")
@app_commands.checks.has_permissions(manage_guild=True)
async def duty_list(interaction: discord.Interaction):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM duty_status WHERE guild_id = %s AND is_on_duty = true ORDER BY last_updated DESC',
                (interaction.guild.id,))
    on_duty = cur.fetchall()
    
    cur.close()
    conn.close()
    
    if not on_duty:
        await interaction.response.send_message('❌ No members are currently on duty!')
        return
    
    embed = discord.Embed(
        title=f"On-Duty Members ({len(on_duty)})",
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    
    for status in on_duty[:25]:
        user = interaction.guild.get_member(status['user_id'])
        if user:
            embed.add_field(
                name=user.display_name,
                value=f"{user.mention} | Since {status['last_updated'].strftime('%H:%M:%S')}",
                inline=False
            )
    
    if len(on_duty) > 25:
        embed.set_footer(text=f"Showing first 25 of {len(on_duty)} members")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setdutyrole", description="Set the role that's given when going on duty")
@app_commands.describe(role="The role to assign when on duty")
@app_commands.checks.has_permissions(administrator=True)
async def set_duty_role(interaction: discord.Interaction, role: discord.Role):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO duty_role_config (guild_id, on_duty_role_id)
        VALUES (%s, %s)
        ON CONFLICT (guild_id) DO UPDATE SET on_duty_role_id = %s
    ''', (interaction.guild.id, role.id, role.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(f'✅ On-duty role set to {role.mention}! Members will now receive this role when they go on duty.')

@bot.tree.command(name="createpoll", description="Create a poll with voting buttons")
@app_commands.describe(
    question="The poll question",
    option1="First option",
    option2="Second option",
    option3="Third option (optional)",
    option4="Fourth option (optional)",
    option5="Fifth option (optional)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def create_poll(interaction: discord.Interaction, question: str, option1: str, option2: str, 
                     option3: str = None, option4: str = None, option5: str = None):
    options = [option1, option2]
    if option3:
        options.append(option3)
    if option4:
        options.append(option4)
    if option5:
        options.append(option5)
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('''
        INSERT INTO polls (guild_id, channel_id, question, options, created_by, is_active)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING poll_id
    ''', (interaction.guild.id, interaction.channel.id, question, json.dumps(options), 
          interaction.user.id, True))
    
    poll_id = cur.fetchone()['poll_id']
    conn.commit()
    
    embed = discord.Embed(
        title="📊 " + question,
        description="Click the buttons below to vote!",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.set_footer(text=f"Poll created by {interaction.user} | Poll ID: {poll_id}")
    
    view = PollView(poll_id, options)
    
    await interaction.response.send_message(embed=embed, view=view)
    
    message = await interaction.original_response()
    
    cur.execute('UPDATE polls SET message_id = %s WHERE poll_id = %s', (message.id, poll_id))
    conn.commit()
    
    cur.close()
    conn.close()
    
    log_event(interaction.guild.id, 'poll_created', actor_user_id=interaction.user.id,
             details={'question': question, 'options': options, 'poll_id': poll_id})

@bot.tree.command(name="closepoll", description="Close a poll and show results")
@app_commands.describe(poll_id="The ID of the poll to close")
@app_commands.checks.has_permissions(manage_guild=True)
async def close_poll(interaction: discord.Interaction, poll_id: int):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM polls WHERE poll_id = %s AND guild_id = %s', (poll_id, interaction.guild.id))
    poll = cur.fetchone()
    
    if not poll:
        await interaction.response.send_message(f'❌ Poll #{poll_id} not found!', ephemeral=True)
        cur.close()
        conn.close()
        return
    
    cur.execute('UPDATE polls SET is_active = false WHERE poll_id = %s', (poll_id,))
    
    cur.execute('''
        SELECT option_index, COUNT(*) as vote_count
        FROM poll_votes
        WHERE poll_id = %s
        GROUP BY option_index
        ORDER BY option_index
    ''', (poll_id,))
    
    results = cur.fetchall()
    conn.commit()
    
    options = json.loads(poll['options'])
    
    embed = discord.Embed(
        title="📊 Poll Results: " + poll['question'],
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    
    total_votes = sum([r['vote_count'] for r in results])
    
    vote_counts = {r['option_index']: r['vote_count'] for r in results}
    
    for i, option in enumerate(options):
        votes = vote_counts.get(i, 0)
        percentage = (votes / total_votes * 100) if total_votes > 0 else 0
        bar_length = int(percentage / 5)
        bar = "█" * bar_length + "░" * (20 - bar_length)
        embed.add_field(
            name=option,
            value=f"{bar} {votes} votes ({percentage:.1f}%)",
            inline=False
        )
    
    embed.set_footer(text=f"Total votes: {total_votes} | Poll closed by {interaction.user}")
    
    await interaction.response.send_message(embed=embed)
    
    cur.close()
    conn.close()
    
    log_event(interaction.guild.id, 'poll_closed', actor_user_id=interaction.user.id,
             details={'poll_id': poll_id, 'total_votes': total_votes})

@bot.tree.command(name="setgloballog", description="Set a unified logging channel for ALL server events")
@app_commands.describe(channel="The channel where all logs will be sent")
@app_commands.checks.has_permissions(administrator=True)
async def set_global_log(interaction: discord.Interaction, channel: discord.TextChannel):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO global_log_config (guild_id, channel_id, enabled)
        VALUES (%s, %s, true)
        ON CONFLICT (guild_id) DO UPDATE SET channel_id = %s, enabled = true
    ''', (interaction.guild.id, channel.id, channel.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(
        f'✅ Global logging enabled! All server events will now be logged to {channel.mention}\n\n'
        f'Events tracked: Member joins/leaves, bans, role changes, message edits/deletes, '
        f'channel create/delete, permission changes, and more!'
    )

@bot.tree.command(name="disablegloballog", description="Disable the unified logging system")
@app_commands.checks.has_permissions(administrator=True)
async def disable_global_log(interaction: discord.Interaction):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        UPDATE global_log_config SET enabled = false WHERE guild_id = %s
    ''', (interaction.guild.id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message('✅ Global logging disabled!')

@bot.tree.command(name="configsecurity", description="Configure anti-raid and permission guard settings")
@app_commands.describe(
    anti_raid="Enable anti-raid detection",
    raid_threshold="Number of joins to trigger raid alert (default: 5)",
    raid_window="Time window in seconds for raid detection (default: 30)",
    min_account_age="Minimum account age in days (default: 7)",
    auto_lockdown="Automatically activate lockdown when raid detected",
    permission_guard="Enable permission guard to monitor role permission changes",
    alert_role="Role to ping for security alerts"
)
@app_commands.checks.has_permissions(administrator=True)
async def config_security(interaction: discord.Interaction, 
                         anti_raid: bool = None,
                         raid_threshold: int = None,
                         raid_window: int = None,
                         min_account_age: int = None,
                         auto_lockdown: bool = None,
                         permission_guard: bool = None,
                         alert_role: discord.Role = None):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM security_config WHERE guild_id = %s', (interaction.guild.id,))
    existing = cur.fetchone()
    
    if not existing:
        cur.execute('''
            INSERT INTO security_config (guild_id) VALUES (%s)
        ''', (interaction.guild.id,))
        conn.commit()
    
    updates = []
    params = []
    
    if anti_raid is not None:
        updates.append("anti_raid_enabled = %s")
        params.append(anti_raid)
    if raid_threshold is not None:
        updates.append("raid_threshold = %s")
        params.append(raid_threshold)
    if raid_window is not None:
        updates.append("raid_time_window = %s")
        params.append(raid_window)
    if min_account_age is not None:
        updates.append("min_account_age = %s")
        params.append(min_account_age)
    if auto_lockdown is not None:
        updates.append("auto_lockdown = %s")
        params.append(auto_lockdown)
    if permission_guard is not None:
        updates.append("permission_guard_enabled = %s")
        params.append(permission_guard)
    if alert_role is not None:
        updates.append("alert_role_id = %s")
        params.append(alert_role.id)
    
    if updates:
        params.append(interaction.guild.id)
        cur.execute(f'''
            UPDATE security_config SET {", ".join(updates)}
            WHERE guild_id = %s
        ''', params)
        conn.commit()
    
    cur.execute('SELECT * FROM security_config WHERE guild_id = %s', (interaction.guild.id,))
    config = cur.fetchone()
    
    cur.close()
    conn.close()
    
    embed = discord.Embed(
        title="🛡️ Security Configuration Updated",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    embed.add_field(
        name="Anti-Raid Protection",
        value=f"{'✅ Enabled' if config['anti_raid_enabled'] else '❌ Disabled'}\n"
              f"Threshold: {config['raid_threshold']} joins in {config['raid_time_window']}s\n"
              f"Min Account Age: {config['min_account_age']} days\n"
              f"Auto-Lockdown: {'✅ Yes' if config['auto_lockdown'] else '❌ No'}",
        inline=False
    )
    
    embed.add_field(
        name="Permission Guard",
        value=f"{'✅ Enabled' if config['permission_guard_enabled'] else '❌ Disabled'}",
        inline=False
    )
    
    if config['alert_role_id']:
        alert_role_obj = interaction.guild.get_role(config['alert_role_id'])
        embed.add_field(name="Alert Role", value=alert_role_obj.mention if alert_role_obj else "Not set", inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="securitystatus", description="View current security settings and stats")
@app_commands.checks.has_permissions(manage_guild=True)
async def security_status(interaction: discord.Interaction):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM security_config WHERE guild_id = %s', (interaction.guild.id,))
    config = cur.fetchone()
    
    cur.execute('SELECT * FROM global_log_config WHERE guild_id = %s', (interaction.guild.id,))
    log_config = cur.fetchone()
    
    cur.execute('''
        SELECT COUNT(*) as suspicious_count FROM raid_tracking 
        WHERE guild_id = %s AND is_suspicious = true AND joined_at > NOW() - INTERVAL '24 hours'
    ''', (interaction.guild.id,))
    suspicious = cur.fetchone()
    
    cur.execute('''
        SELECT COUNT(*) as recent_joins FROM raid_tracking 
        WHERE guild_id = %s AND joined_at > NOW() - INTERVAL '1 hour'
    ''', (interaction.guild.id,))
    recent = cur.fetchone()
    
    cur.close()
    conn.close()
    
    recommendations = []
    protection_score = 0
    
    embed = discord.Embed(
        title="🛡️ Security System Status",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    if log_config and log_config['enabled'] and log_config['channel_id']:
        log_channel = interaction.guild.get_channel(log_config['channel_id'])
        embed.add_field(
            name="📝 Global Logging",
            value=f"✅ **Enabled**\nChannel: {log_channel.mention if log_channel else 'Channel not found'}\n"
                  f"All server events are being tracked!",
            inline=False
        )
        protection_score += 25
    else:
        embed.add_field(
            name="📝 Global Logging", 
            value="❌ **Not configured**\nYou're missing out on a complete audit trail of server activity.",
            inline=False
        )
        recommendations.append("Set up global logging with `/setgloballog`")
    
    if config:
        if config['anti_raid_enabled']:
            embed.add_field(
                name="🛡️ Anti-Raid System",
                value=f"✅ **Active**\n"
                      f"• Trigger: {config['raid_threshold']} joins in {config['raid_time_window']}s\n"
                      f"• Min Account Age: {config['min_account_age']} days\n"
                      f"• Auto-Lockdown: {'✅ Enabled' if config['auto_lockdown'] else '❌ Disabled'}",
                inline=False
            )
            protection_score += 35
            if not config['auto_lockdown']:
                recommendations.append("Enable auto-lockdown for automatic raid response: `/configsecurity auto_lockdown:True`")
        else:
            embed.add_field(
                name="🛡️ Anti-Raid System",
                value="❌ **Inactive**\nYour server is vulnerable to raid attacks.",
                inline=False
            )
            recommendations.append("Enable anti-raid protection: `/configsecurity anti_raid:True`")
        
        if config['permission_guard_enabled']:
            embed.add_field(
                name="🔐 Permission Guard",
                value="✅ **Active**\nMonitoring changes to dangerous role permissions.",
                inline=False
            )
            protection_score += 25
        else:
            embed.add_field(
                name="🔐 Permission Guard",
                value="❌ **Inactive**\nUnauthorized permission changes won't be detected.",
                inline=False
            )
            recommendations.append("Enable permission guard: `/configsecurity permission_guard:True`")
        
        if config['alert_role_id']:
            alert_role = interaction.guild.get_role(config['alert_role_id'])
            embed.add_field(
                name="🔔 Alert Role",
                value=f"✅ **Configured**\n{alert_role.mention if alert_role else 'Role not found'} will be pinged for security events.",
                inline=False
            )
            protection_score += 15
        else:
            embed.add_field(
                name="🔔 Alert Role",
                value="❌ **Not set**\nNo one will be notified of security events.",
                inline=False
            )
            recommendations.append("Set an alert role: `/configsecurity alert_role:@YourRole`")
    else:
        embed.add_field(
            name="Security Features", 
            value="❌ **Not configured**\nYour server has no active security protection!",
            inline=False
        )
        recommendations.append("Run `/setupguide` to get started with security setup")
    
    suspicious_count = suspicious['suspicious_count'] if suspicious else 0
    recent_count = recent['recent_joins'] if recent else 0
    
    activity_status = "🟢 Normal" if suspicious_count == 0 else ("🟡 Moderate" if suspicious_count < 5 else "🔴 High Alert")
    
    embed.add_field(
        name="📊 Recent Activity (Last 24 hours)",
        value=f"Status: {activity_status}\n"
              f"• Suspicious joins: {suspicious_count}\n"
              f"• Joins in last hour: {recent_count}",
        inline=False
    )
    
    if protection_score == 100:
        embed.add_field(
            name="🏆 Protection Level",
            value="**Excellent!** Your server has full security protection enabled.",
            inline=False
        )
        embed.color = discord.Color.green()
    elif protection_score >= 60:
        embed.add_field(
            name="⚠️ Protection Level",
            value=f"**Good** ({protection_score}% protected) - A few improvements recommended.",
            inline=False
        )
        embed.color = discord.Color.gold()
    else:
        embed.add_field(
            name="🚨 Protection Level",
            value=f"**Needs Improvement** ({protection_score}% protected) - Your server is at risk!",
            inline=False
        )
        embed.color = discord.Color.red()
    
    if recommendations:
        embed.add_field(
            name="💡 Recommendations",
            value="\n".join(f"• {rec}" for rec in recommendations[:5]),
            inline=False
        )
    
    embed.set_footer(text="💡 Use /security for detailed setup instructions | /setupguide for step-by-step help")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setlogchannel", description="Set a channel for logging events")
@app_commands.describe(
    event_type="Type of events to log",
    channel="Channel to send logs to"
)
@app_commands.choices(event_type=[
    app_commands.Choice(name="Member Joins", value="member_join"),
    app_commands.Choice(name="Member Leaves", value="member_leave"),
    app_commands.Choice(name="Role Changes", value="member_role_update"),
    app_commands.Choice(name="Message Deletes", value="message_delete"),
    app_commands.Choice(name="Message Edits", value="message_edit")
])
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, event_type: str, channel: discord.TextChannel):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO log_channels (guild_id, event_type, channel_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (guild_id, event_type) DO UPDATE SET channel_id = %s
    ''', (interaction.guild.id, event_type, channel.id, channel.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    event_names = {
        'member_join': 'Member Joins',
        'member_leave': 'Member Leaves',
        'member_role_update': 'Role Changes',
        'message_delete': 'Message Deletes',
        'message_edit': 'Message Edits'
    }
    
    await interaction.response.send_message(
        f'✅ {event_names.get(event_type, event_type)} will now be logged to {channel.mention}!'
    )

@bot.tree.command(name="viewlogs", description="View recent activity logs")
@app_commands.describe(
    event_type="Type of events to view (leave empty for all)",
    limit="Number of logs to show (max 25)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def view_logs(interaction: discord.Interaction, event_type: str = None, limit: int = 10):
    if limit > 25:
        limit = 25
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    if event_type:
        cur.execute('''
            SELECT * FROM activity_logs 
            WHERE guild_id = %s AND event_type = %s
            ORDER BY timestamp DESC 
            LIMIT %s
        ''', (interaction.guild.id, event_type, limit))
    else:
        cur.execute('''
            SELECT * FROM activity_logs 
            WHERE guild_id = %s
            ORDER BY timestamp DESC 
            LIMIT %s
        ''', (interaction.guild.id, limit))
    
    logs = cur.fetchall()
    
    cur.close()
    conn.close()
    
    if not logs:
        await interaction.response.send_message('❌ No logs found!', ephemeral=True)
        return
    
    embed = discord.Embed(
        title=f"Activity Logs ({len(logs)})",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    for log in logs:
        target = f"<@{log['target_user_id']}>" if log['target_user_id'] else "N/A"
        actor = f"<@{log['actor_user_id']}>" if log['actor_user_id'] else "N/A"
        details = log['details'][:100] if log['details'] else "N/A"
        
        embed.add_field(
            name=f"{log['event_type']} - {log['timestamp'].strftime('%m/%d %H:%M:%S')}",
            value=f"Target: {target} | Actor: {actor}",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setlockdownconfig", description="Configure emergency lockdown settings (Director only)")
@app_commands.describe(
    director_role="The role that can activate lockdown",
    announcement_channel="Channel for lockdown announcements"
)
@app_commands.checks.has_permissions(administrator=True)
async def set_lockdown_config(interaction: discord.Interaction, director_role: discord.Role, 
                              announcement_channel: discord.TextChannel):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO lockdown_config (guild_id, director_role_id, announcement_channel_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (guild_id) DO UPDATE 
        SET director_role_id = %s, announcement_channel_id = %s
    ''', (interaction.guild.id, director_role.id, announcement_channel.id, 
          director_role.id, announcement_channel.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(
        f'✅ Lockdown configured!\nDirector Role: {director_role.mention}\nAnnouncement Channel: {announcement_channel.mention}'
    )

@bot.tree.command(name="lockdown", description="Activate emergency lockdown (Director only)")
@app_commands.describe(reason="Reason for lockdown")
async def lockdown(interaction: discord.Interaction, reason: str):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM lockdown_config WHERE guild_id = %s', (interaction.guild.id,))
    config = cur.fetchone()
    
    if not config:
        await interaction.response.send_message('❌ Lockdown not configured! Use /setlockdownconfig first.', ephemeral=True)
        cur.close()
        conn.close()
        return
    
    director_role = interaction.guild.get_role(config['director_role_id'])
    
    if director_role not in interaction.user.roles:
        await interaction.response.send_message('❌ Only the Director can activate lockdown!', ephemeral=True)
        cur.close()
        conn.close()
        return
    
    if config['is_active']:
        await interaction.response.send_message('❌ Lockdown is already active!', ephemeral=True)
        cur.close()
        conn.close()
        return
    
    cur.execute('''
        UPDATE lockdown_config 
        SET is_active = true, initiated_by = %s, initiated_at = %s
        WHERE guild_id = %s
    ''', (interaction.user.id, datetime.now(), interaction.guild.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message('🚨 **INITIATING EMERGENCY LOCKDOWN...** 🚨', ephemeral=True)
    
    announcement_channel = interaction.guild.get_channel(config['announcement_channel_id'])
    
    if announcement_channel:
        embed = discord.Embed(
            title="🚨 EMERGENCY LOCKDOWN ACTIVATED 🚨",
            description=f"**The server is now under emergency lockdown.**\n\nAll non-essential communications are restricted.",
            color=discord.Color.red(),
            timestamp=datetime.now()
        )
        embed.add_field(name="Initiated By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Instructions", value="Stay calm and await further instructions from leadership.", inline=False)
        embed.set_footer(text="This is an emergency protocol")
        
        if director_role:
            await announcement_channel.send(f'{director_role.mention} @everyone', embed=embed)
        else:
            await announcement_channel.send('@everyone', embed=embed)
    
    conn = get_db()
    cur = conn.cursor()
    
    for channel in interaction.guild.text_channels:
        try:
            overwrites = channel.overwrites
            
            all_overwrites = {}
            for target, overwrite in overwrites.items():
                if isinstance(target, discord.Role):
                    target_type = 'role'
                    target_id = target.id
                elif isinstance(target, discord.Member):
                    target_type = 'member'
                    target_id = target.id
                else:
                    continue
                
                perms_dict = {}
                for perm_name in dir(overwrite):
                    if not perm_name.startswith('_'):
                        perm_value = getattr(overwrite, perm_name, None)
                        if perm_value is not None and isinstance(perm_value, bool):
                            perms_dict[perm_name] = perm_value
                
                all_overwrites[f'{target_type}_{target_id}'] = perms_dict
            
            cur.execute('''
                INSERT INTO lockdown_permissions (guild_id, channel_id, permissions_json)
                VALUES (%s, %s, %s)
                ON CONFLICT (guild_id, channel_id) DO UPDATE SET permissions_json = %s
            ''', (interaction.guild.id, channel.id, json.dumps(all_overwrites), json.dumps(all_overwrites)))
            
            if channel.id == config['announcement_channel_id']:
                overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=False,
                    add_reactions=False
                )
            else:
                overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(
                    view_channel=False
                )
            
            await channel.edit(overwrites=overwrites)
        except Exception as e:
            print(f'Error locking channel {channel.name}: {e}')
    
    conn.commit()
    cur.close()
    conn.close()
    
    log_event(interaction.guild.id, 'lockdown_activated', actor_user_id=interaction.user.id,
             details={'reason': reason})

@bot.tree.command(name="unlockdown", description="Deactivate emergency lockdown (Director only)")
async def unlockdown(interaction: discord.Interaction):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM lockdown_config WHERE guild_id = %s', (interaction.guild.id,))
    config = cur.fetchone()
    
    if not config:
        await interaction.response.send_message('❌ Lockdown not configured!', ephemeral=True)
        cur.close()
        conn.close()
        return
    
    director_role = interaction.guild.get_role(config['director_role_id'])
    
    if director_role not in interaction.user.roles:
        await interaction.response.send_message('❌ Only the Director can deactivate lockdown!', ephemeral=True)
        cur.close()
        conn.close()
        return
    
    if not config['is_active']:
        await interaction.response.send_message('❌ Lockdown is not active!', ephemeral=True)
        cur.close()
        conn.close()
        return
    
    cur.execute('UPDATE lockdown_config SET is_active = false WHERE guild_id = %s', (interaction.guild.id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message('✅ **Deactivating lockdown...** Please wait.', ephemeral=True)
    
    announcement_channel = interaction.guild.get_channel(config['announcement_channel_id'])
    
    if announcement_channel:
        embed = discord.Embed(
            title="✅ LOCKDOWN DEACTIVATED",
            description="The emergency lockdown has been lifted. Normal operations may resume.",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        embed.add_field(name="Deactivated By", value=interaction.user.mention, inline=True)
        embed.set_footer(text="Emergency protocol ended")
        
        await announcement_channel.send(embed=embed)
    
    conn_perms = get_db()
    cur_perms = conn_perms.cursor(cursor_factory=RealDictCursor)
    
    for channel in interaction.guild.text_channels:
        try:
            cur_perms.execute('''
                SELECT permissions_json FROM lockdown_permissions 
                WHERE guild_id = %s AND channel_id = %s
            ''', (interaction.guild.id, channel.id))
            
            stored_perms = cur_perms.fetchone()
            
            if stored_perms and stored_perms['permissions_json']:
                perms_json = stored_perms['permissions_json']
                
                try:
                    all_overwrites = json.loads(perms_json)
                    new_overwrites = {}
                    
                    for target_key, perms_dict in all_overwrites.items():
                        parts = target_key.split('_', 1)
                        if len(parts) != 2:
                            continue
                        
                        target_type, target_id_str = parts
                        target_id = int(target_id_str)
                        target = None
                        
                        if target_type == 'role':
                            target = interaction.guild.get_role(target_id)
                        elif target_type == 'member':
                            target = interaction.guild.get_member(target_id)
                            if not target:
                                try:
                                    target = await interaction.guild.fetch_member(target_id)
                                except:
                                    pass
                        
                        if not target:
                            continue
                        
                        restored_overwrite = discord.PermissionOverwrite()
                        for perm_name, perm_value in perms_dict.items():
                            try:
                                setattr(restored_overwrite, perm_name, perm_value)
                            except:
                                pass
                        new_overwrites[target] = restored_overwrite
                    
                    await channel.edit(overwrites=new_overwrites)
                except Exception as e:
                    print(f'Error restoring permissions for {channel.name}: {e}')
                    overwrites = channel.overwrites
                    if interaction.guild.default_role in overwrites:
                        del overwrites[interaction.guild.default_role]
                        await channel.edit(overwrites=overwrites)
            else:
                overwrites = channel.overwrites
                if interaction.guild.default_role in overwrites:
                    del overwrites[interaction.guild.default_role]
                    await channel.edit(overwrites=overwrites)
        except Exception as e:
            print(f'Error unlocking channel {channel.name}: {e}')
    
    cur_perms.execute('DELETE FROM lockdown_permissions WHERE guild_id = %s', (interaction.guild.id,))
    conn_perms.commit()
    cur_perms.close()
    conn_perms.close()
    
    log_event(interaction.guild.id, 'lockdown_deactivated', actor_user_id=interaction.user.id)

@bot.tree.command(name="setwelcomechannel", description="Set the channel where welcome messages will be sent")
@app_commands.describe(channel="The channel for welcome messages")
@app_commands.checks.has_permissions(administrator=True)
async def set_welcome_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO welcome_config (guild_id, channel_id)
        VALUES (%s, %s)
        ON CONFLICT (guild_id) DO UPDATE SET channel_id = %s
    ''', (interaction.guild.id, channel.id, channel.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(f'✅ Welcome channel set to {channel.mention}!')

@bot.tree.command(name="setwelcomemessage", description="Set the welcome message for new members")
@app_commands.describe(message="The welcome message (use {user} for mention, {server} for server name)")
@app_commands.checks.has_permissions(administrator=True)
async def set_welcome_message(interaction: discord.Interaction, message: str):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO welcome_config (guild_id, message)
        VALUES (%s, %s)
        ON CONFLICT (guild_id) DO UPDATE SET message = %s
    ''', (interaction.guild.id, message, message))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(f'✅ Welcome message set!')

@bot.tree.command(name="setautorole", description="Set a role to automatically assign to new members")
@app_commands.describe(role="The role to auto-assign")
@app_commands.checks.has_permissions(administrator=True)
async def set_auto_role(interaction: discord.Interaction, role: discord.Role):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO welcome_config (guild_id, auto_role_id)
        VALUES (%s, %s)
        ON CONFLICT (guild_id) DO UPDATE SET auto_role_id = %s
    ''', (interaction.guild.id, role.id, role.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(f'✅ Auto-role set to {role.mention}!')

@bot.tree.command(name="testwelcome", description="Test the welcome message")
@app_commands.checks.has_permissions(administrator=True)
async def test_welcome(interaction: discord.Interaction):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM welcome_config WHERE guild_id = %s', (interaction.guild.id,))
    config = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if not config or not config['channel_id']:
        await interaction.response.send_message('❌ Welcome channel not configured!')
        return
    
    channel = interaction.guild.get_channel(config['channel_id'])
    if not channel:
        await interaction.response.send_message('❌ Welcome channel not found!')
        return
    
    message = config.get('message', 'Welcome {user} to {server}!')
    formatted_message = message.replace('{user}', interaction.user.mention).replace('{server}', interaction.guild.name)
    
    await channel.send(formatted_message)
    await interaction.response.send_message(f'✅ Test welcome message sent to {channel.mention}!')

# ✅ SET TRAINING CHANNEL
@bot.tree.command(name="settrainingchannel", description="Set the channel for training notifications")
@app_commands.describe(
    training_type="Type of training",
    channel="Channel to send notifications"
)
@app_commands.choices(training_type=[
    app_commands.Choice(name="Civilian", value="civilian"),
    app_commands.Choice(name="Probationary Private", value="probationary_private"),
    app_commands.Choice(name="Private", value="private")
])
@app_commands.checks.has_permissions(administrator=True)
async def set_training_channel(interaction: discord.Interaction, training_type: str, channel: discord.TextChannel):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO training_config (guild_id, training_type, channel_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (guild_id, training_type) DO UPDATE SET channel_id = %s
    ''', (interaction.guild.id, training_type, channel.id, channel.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    training_display = training_type.replace('_', ' ').title()
    await interaction.response.send_message(f'✅ {training_display} training channel set to {channel.mention}!')


# ✅ SET TRAINING MESSAGE
@bot.tree.command(name="settrainingmessage", description="Set the message template for training notifications")
@app_commands.describe(
    training_type="Type of training",
    message="Message template (use {time} for time, {host} for host mention)"
)
@app_commands.choices(training_type=[
    app_commands.Choice(name="Civilian", value="civilian"),
    app_commands.Choice(name="Probationary Private", value="probationary_private"),
    app_commands.Choice(name="Private", value="private")
])
@app_commands.checks.has_permissions(administrator=True)
async def set_training_message(interaction: discord.Interaction, training_type: str, message: str):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO training_config (guild_id, training_type, message)
        VALUES (%s, %s, %s)
        ON CONFLICT (guild_id, training_type) DO UPDATE SET message = %s
    ''', (interaction.guild.id, training_type, message, message))
    
    conn.commit()
    cur.close()
    conn.close()
    
    training_display = training_type.replace('_', ' ').title()
    await interaction.response.send_message(f'✅ {training_display} training message set!')


# ✅ SCHEDULE TRAINING
@bot.tree.command(name="scheduletraining", description="Send a training notification")
@app_commands.describe(
    training_type="Type of training",
    time="When is the training?"
)
@app_commands.choices(
    training_type=[
        app_commands.Choice(name="Civilian", value="civilian"),
        app_commands.Choice(name="Probationary Private", value="probationary_private"),
        app_commands.Choice(name="Private", value="private")
    ],
    time=[
        app_commands.Choice(name="In 15 minutes", value="15 minutes"),
        app_commands.Choice(name="In 30 minutes", value="30 minutes"),
        app_commands.Choice(name="In 45 minutes", value="45 minutes"),
        app_commands.Choice(name="In 1 hour", value="1 hour"),
        app_commands.Choice(name="In 1.5 hours", value="90 minutes"),
        app_commands.Choice(name="In 2 hours", value="2 hours"),
        app_commands.Choice(name="In 3 hours", value="3 hours"),
        app_commands.Choice(name="In 4 hours", value="4 hours"),
        app_commands.Choice(name="In 6 hours", value="6 hours"),
        app_commands.Choice(name="In 12 hours", value="12 hours"),
        app_commands.Choice(name="In 24 hours (Tomorrow)", value="1 day"),
        app_commands.Choice(name="In 2 days", value="2 days"),
        app_commands.Choice(name="In 3 days", value="3 days"),
        app_commands.Choice(name="In 1 week", value="1 week")
    ]
)
@app_commands.checks.has_permissions(manage_guild=True)
async def schedule_training(interaction: discord.Interaction, training_type: str, time: str):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM training_config WHERE guild_id = %s AND training_type = %s',
                (interaction.guild.id, training_type))
    config = cur.fetchone()
    
    if not config or not config['channel_id']:
        training_display = training_type.replace('_', ' ').title()
        cur.close()
        conn.close()
        await interaction.response.send_message(f'❌ {training_display} training channel not configured!')
        return
    
    channel = interaction.guild.get_channel(config['channel_id'])
    if not channel:
        cur.close()
        conn.close()
        await interaction.response.send_message('❌ Training channel not found!')
        return
    
    parsed_time = parse_time_string(time)
    if parsed_time:
        time_type, time_value = parsed_time
        if time_type == 'relative':
            target_time = datetime.now() + timedelta(seconds=time_value)
            discord_timestamp = f"<t:{int(target_time.timestamp())}:F>"
        else:
            discord_timestamp = f"<t:{int(time_value.timestamp())}:F>"
    else:
        discord_timestamp = time
    
    message_template = config.get('message', '🎓 Training scheduled for {time}! Hosted by {host}')
    
    training_display = training_type.replace('_', ' ').title()
    
    embed = discord.Embed(
        title=f"🎓 {training_display} Training",
        description=f"**Time:** {discord_timestamp}\n**Host:** {interaction.user.mention}",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    embed.add_field(
        name="✅ Attending (0)",
        value="None yet",
        inline=False
    )
    
    embed.add_field(
        name="🦅 Helping (0)",
        value="None yet",
        inline=False
    )
    
    embed.set_footer(text="React with ✅ to attend or 🦅 to help")
    
    # Send message tagging @everyone
    sent_message = await channel.send(
        content="@everyone",
        embed=embed,
        allowed_mentions=discord.AllowedMentions(everyone=True)
    )
    
    await sent_message.add_reaction('✅')
    await sent_message.add_reaction('🦅')
    
    cur.execute('''
        INSERT INTO training_messages (message_id, guild_id, channel_id, training_type, message_template, training_time, host_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    ''', (sent_message.id, interaction.guild.id, channel.id, training_type, message_template, time, interaction.user.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(f'✅ {training_display} training scheduled and posted to {channel.mention}!')

@bot.tree.command(name="sethelperrole", description="Set the helper role for training")
@app_commands.describe(role="The role for training helpers")
@app_commands.checks.has_permissions(administrator=True)
async def set_helper_role(interaction: discord.Interaction, role: discord.Role):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO training_roles (guild_id, helper_role_id)
        VALUES (%s, %s)
        ON CONFLICT (guild_id) DO UPDATE SET helper_role_id = %s
    ''', (interaction.guild.id, role.id, role.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(f'✅ Training helper role set to {role.mention}!')

@bot.tree.command(name="warn", description="Issue a warning to a user")
@app_commands.describe(
    user="The user to warn",
    reason="Reason for the warning"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT COUNT(*) as count FROM warnings WHERE guild_id = %s AND user_id = %s',
                (interaction.guild.id, user.id))
    warning_count = cur.fetchone()['count'] + 1
    
    cur.execute('''
        INSERT INTO warnings (guild_id, user_id, warning_number, reason, issued_by)
        VALUES (%s, %s, %s, %s, %s)
    ''', (interaction.guild.id, user.id, warning_count, reason, interaction.user.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    log_event(interaction.guild.id, 'warning_issued', target_user_id=user.id, actor_user_id=interaction.user.id,
             details={'reason': reason, 'warning_number': warning_count})
    
    embed = discord.Embed(
        title="⚠️ Warning Issued",
        color=discord.Color.orange(),
        timestamp=datetime.now()
    )
    embed.add_field(name="User", value=user.mention, inline=True)
    embed.add_field(name="Warning #", value=warning_count, inline=True)
    embed.add_field(name="Issued by", value=interaction.user.mention, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="clearwarnings", description="Clear all warnings for a user")
@app_commands.describe(user="The user to clear warnings for")
@app_commands.checks.has_permissions(administrator=True)
async def clear_warnings(interaction: discord.Interaction, user: discord.Member):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('DELETE FROM warnings WHERE guild_id = %s AND user_id = %s',
                (interaction.guild.id, user.id))
    
    deleted_count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    
    log_event(interaction.guild.id, 'warnings_cleared', target_user_id=user.id, actor_user_id=interaction.user.id,
             details={'count': deleted_count})
    
    await interaction.response.send_message(f'✅ Cleared {deleted_count} warning(s) for {user.mention}!')

@bot.tree.command(name="viewwarnings", description="View warnings for a user")
@app_commands.describe(user="The user to check warnings for")
async def view_warnings(interaction: discord.Interaction, user: discord.Member):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM warnings WHERE guild_id = %s AND user_id = %s ORDER BY issued_at DESC',
                (interaction.guild.id, user.id))
    warnings = cur.fetchall()
    
    cur.close()
    conn.close()
    
    if not warnings:
        await interaction.response.send_message(f'{user.mention} has no warnings!', ephemeral=True)
        return
    
    embed = discord.Embed(
        title=f"Warnings for {user.display_name}",
        description=f"Total warnings: {len(warnings)}",
        color=discord.Color.orange(),
        timestamp=datetime.now()
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    
    for warning in warnings[:10]:
        issuer = interaction.guild.get_member(warning['issued_by'])
        issuer_str = issuer.mention if issuer else f"<@{warning['issued_by']}>"
        
        embed.add_field(
            name=f"Warning #{warning['warning_number']} - {warning['issued_at'].strftime('%Y-%m-%d')}",
            value=f"**Reason:** {warning['reason']}\n**Issued by:** {issuer_str}",
            inline=False
        )
    
    if len(warnings) > 10:
        embed.set_footer(text=f"Showing 10 of {len(warnings)} warnings")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setawardchannel", description="Set the channel for monthly awards")
@app_commands.describe(
    award_type="Type of award",
    channel="Channel to post awards"
)
@app_commands.choices(award_type=[
    app_commands.Choice(name="Employee of the Month", value="employee"),
    app_commands.Choice(name="Agent of the Month", value="agent")
])
@app_commands.checks.has_permissions(administrator=True)
async def set_award_channel(interaction: discord.Interaction, award_type: str, channel: discord.TextChannel):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO monthly_awards (guild_id, award_type, channel_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (guild_id, award_type) DO UPDATE SET channel_id = %s
    ''', (interaction.guild.id, award_type, channel.id, channel.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    award_display = "Employee of the Month" if award_type == "employee" else "Agent of the Month"
    await interaction.response.send_message(f'✅ {award_display} channel set to {channel.mention}!')

@bot.tree.command(name="setawardmessage", description="Set the message for monthly awards")
@app_commands.describe(
    award_type="Type of award",
    message="Message template (use {user} for winner mention)"
)
@app_commands.choices(award_type=[
    app_commands.Choice(name="Employee of the Month", value="employee"),
    app_commands.Choice(name="Agent of the Month", value="agent")
])
@app_commands.checks.has_permissions(administrator=True)
async def set_award_message(interaction: discord.Interaction, award_type: str, message: str):
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute('''
        INSERT INTO monthly_awards (guild_id, award_type, message)
        VALUES (%s, %s, %s)
        ON CONFLICT (guild_id, award_type) DO UPDATE SET message = %s
    ''', (interaction.guild.id, award_type, message, message))
    
    conn.commit()
    cur.close()
    conn.close()
    
    award_display = "Employee of the Month" if award_type == "employee" else "Agent of the Month"
    await interaction.response.send_message(f'✅ {award_display} message set!')

@bot.tree.command(name="sendmonthlyaward", description="Send a monthly award")
@app_commands.describe(
    award_type="Type of award",
    winner="The winner of the award"
)
@app_commands.choices(award_type=[
    app_commands.Choice(name="Employee of the Month", value="employee"),
    app_commands.Choice(name="Agent of the Month", value="agent")
])
@app_commands.checks.has_permissions(administrator=True)
async def send_monthly_award(interaction: discord.Interaction, award_type: str, winner: discord.Member):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM monthly_awards WHERE guild_id = %s AND award_type = %s',
                (interaction.guild.id, award_type))
    config = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if not config or not config['channel_id']:
        await interaction.response.send_message('❌ Award channel not configured!')
        return
    
    channel = interaction.guild.get_channel(config['channel_id'])
    if not channel:
        await interaction.response.send_message('❌ Award channel not found!')
        return
    
    message = config.get('message', '🏆 Congratulations to {user} for being awarded this month!')
    formatted_message = message.replace('{user}', winner.mention)
    
    award_display = "Employee of the Month" if award_type == "employee" else "Agent of the Month"
    
    embed = discord.Embed(
        title=f"🏆 {award_display}",
        description=formatted_message,
        color=discord.Color.gold(),
        timestamp=datetime.now()
    )
    embed.set_thumbnail(url=winner.display_avatar.url)
    
    await channel.send(embed=embed)
    await interaction.response.send_message(f'✅ Award sent to {channel.mention}!')

@bot.tree.command(name="createreactionrole", description="Create a reaction role group with buttons")
@app_commands.describe(
    group_name="Name for this reaction role group",
    description="Description of the role selection",
    exclusive="Can users only have one role from this group? (default: yes)"
)
@app_commands.checks.has_permissions(administrator=True)
async def create_reaction_role(interaction: discord.Interaction, group_name: str, description: str, exclusive: bool = True):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('''
        INSERT INTO reaction_role_groups (guild_id, group_name, description, is_exclusive)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    ''', (interaction.guild.id, group_name, description, exclusive))
    
    group_id = cur.fetchone()['id']
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(
        f'✅ Created reaction role group "{group_name}" (ID: {group_id})!\n'
        f'Next, add roles to this group using `/addreactionroleoption`'
    )

@bot.tree.command(name="addreactionroleoption", description="Add a role option to a reaction role group")
@app_commands.describe(
    group_id="The ID of the reaction role group",
    role="The role to assign",
    button_label="Text to display on the button",
    button_style="Button color style"
)
@app_commands.choices(button_style=[
    app_commands.Choice(name="Blue (Primary)", value="primary"),
    app_commands.Choice(name="Gray (Secondary)", value="secondary"),
    app_commands.Choice(name="Green (Success)", value="success"),
    app_commands.Choice(name="Red (Danger)", value="danger")
])
@app_commands.checks.has_permissions(administrator=True)
async def add_reaction_role_option(interaction: discord.Interaction, group_id: int, role: discord.Role, button_label: str, button_style: str = "primary"):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM reaction_role_groups WHERE id = %s AND guild_id = %s', (group_id, interaction.guild.id))
    group = cur.fetchone()
    
    if not group:
        cur.close()
        conn.close()
        await interaction.response.send_message(f'❌ Reaction role group with ID {group_id} not found!')
        return
    
    cur.execute('''
        INSERT INTO reaction_role_options (group_id, role_id, button_label, button_style)
        VALUES (%s, %s, %s, %s)
    ''', (group_id, role.id, button_label, button_style))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(f'✅ Added {role.mention} to group "{group["group_name"]}" with button "{button_label}"!')

@bot.tree.command(name="postreactionrole", description="Post the reaction role message with buttons")
@app_commands.describe(
    group_id="The ID of the reaction role group",
    channel="Channel to post in (leave empty for current channel)"
)
@app_commands.checks.has_permissions(administrator=True)
async def post_reaction_role(interaction: discord.Interaction, group_id: int, channel: discord.TextChannel = None):
    target_channel = channel or interaction.channel
    
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM reaction_role_groups WHERE id = %s AND guild_id = %s', (group_id, interaction.guild.id))
    group = cur.fetchone()
    
    if not group:
        cur.close()
        conn.close()
        await interaction.response.send_message(f'❌ Reaction role group with ID {group_id} not found!')
        return
    
    cur.execute('SELECT * FROM reaction_role_options WHERE group_id = %s', (group_id,))
    options = cur.fetchall()
    
    if not options:
        cur.close()
        conn.close()
        await interaction.response.send_message(f'❌ No role options configured for this group! Use `/addreactionroleoption` first.')
        return
    
    embed = discord.Embed(
        title=group['group_name'],
        description=group['description'],
        color=discord.Color.blue()
    )
    
    if group['is_exclusive']:
        embed.set_footer(text="You can only have one role from this group at a time.")
    else:
        embed.set_footer(text="Click buttons to toggle roles on and off.")
    
    view = ReactionRoleView(group_id, options, group['is_exclusive'])
    
    message = await target_channel.send(embed=embed, view=view)
    
    cur.execute('''
        UPDATE reaction_role_groups 
        SET message_id = %s, channel_id = %s 
        WHERE id = %s
    ''', (message.id, target_channel.id, group_id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(f'✅ Reaction role message posted in {target_channel.mention}!')

@bot.tree.command(name="listreactionroles", description="List all reaction role groups in this server")
@app_commands.checks.has_permissions(administrator=True)
async def list_reaction_roles(interaction: discord.Interaction):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM reaction_role_groups WHERE guild_id = %s', (interaction.guild.id,))
    groups = cur.fetchall()
    
    if not groups:
        cur.close()
        conn.close()
        await interaction.response.send_message('No reaction role groups configured yet!')
        return
    
    embed = discord.Embed(title="Reaction Role Groups", color=discord.Color.blue())
    
    for group in groups:
        cur.execute('SELECT COUNT(*) as count FROM reaction_role_options WHERE group_id = %s', (group['id'],))
        option_count = cur.fetchone()['count']
        
        status = "✅ Posted" if group['message_id'] else "⚠️ Not posted"
        exclusive = "Yes" if group['is_exclusive'] else "No"
        
        embed.add_field(
            name=f"ID {group['id']}: {group['group_name']}",
            value=f"Status: {status}\nOptions: {option_count}\nExclusive: {exclusive}",
            inline=False
        )
    
    cur.close()
    conn.close()
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="deletereactionrole", description="Delete a reaction role group")
@app_commands.describe(group_id="The ID of the reaction role group to delete")
@app_commands.checks.has_permissions(administrator=True)
async def delete_reaction_role(interaction: discord.Interaction, group_id: int):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT * FROM reaction_role_groups WHERE id = %s AND guild_id = %s', (group_id, interaction.guild.id))
    group = cur.fetchone()
    
    if not group:
        cur.close()
        conn.close()
        await interaction.response.send_message(f'❌ Reaction role group with ID {group_id} not found!')
        return
    
    if group['message_id'] and group['channel_id']:
        try:
            channel = interaction.guild.get_channel(group['channel_id'])
            if channel:
                message = await channel.fetch_message(group['message_id'])
                await message.delete()
        except:
            pass
    
    cur.execute('DELETE FROM reaction_role_groups WHERE id = %s', (group_id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    await interaction.response.send_message(f'✅ Deleted reaction role group "{group["group_name"]}"!')

@bot.tree.command(name="testreactionrole", description="Test the reaction role system")
@app_commands.checks.has_permissions(administrator=True)
async def test_reaction_role(interaction: discord.Interaction):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute('SELECT COUNT(*) as count FROM reaction_role_groups WHERE guild_id = %s', (interaction.guild.id,))
    group_count = cur.fetchone()['count']
    
    cur.close()
    conn.close()
    
    embed = discord.Embed(title="Reaction Role System Test", color=discord.Color.green())
    embed.add_field(name="Status", value="✅ Reaction role system is operational!", inline=False)
    embed.add_field(name="Configured Groups", value=f"{group_count} groups", inline=True)
    embed.add_field(name="Available Commands", value=(
        "/createreactionrole - Create a new role group\n"
        "/addreactionroleoption - Add a role option\n"
        "/postreactionrole - Post the role message\n"
        "/listreactionroles - List all groups\n"
        "/deletereactionrole - Delete a group"
    ), inline=False)
    embed.add_field(name="Features", value=(
        "✅ Mutually exclusive roles (only one at a time)\n"
        "✅ Non-exclusive roles (multiple allowed)\n"
        "✅ Customizable button colors and labels\n"
        "✅ Persistent buttons (work after bot restarts)"
    ), inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="sendembed", description="Send a custom embed message")
@app_commands.describe(
    title="The title of the embed",
    description="The description/content of the embed",
    color="Hex color code (e.g., #FF5733)"
)
@app_commands.checks.has_permissions(administrator=True)
async def send_embed(interaction: discord.Interaction, title: str, description: str, color: str = "#0099ff"):
    try:
        color_int = int(color.replace('#', ''), 16)
        embed = discord.Embed(title=title, description=description, color=discord.Color(color_int))
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f'❌ Error creating embed: {str(e)}')

@bot.tree.command(name="wakeup", description="Wake up the bot (set status to online)")
@app_commands.checks.has_permissions(administrator=True)
async def wakeup(interaction: discord.Interaction):
    await bot.change_presence(status=discord.Status.online, activity=discord.Game(name="Managing the Agency"))
    await interaction.response.send_message('✅ Bot is now online!')

@bot.tree.command(name="purge", description="Delete multiple messages at once")
@app_commands.describe(
    amount="Number of messages to delete (1-100)",
    user="Only delete messages from this user (optional)"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int, user: discord.User = None):
    if amount < 1 or amount > 100:
        await interaction.response.send_message('❌ Please specify a number between 1 and 100!', ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        if user:
            def check_user(message):
                return message.author.id == user.id
            
            deleted = await interaction.channel.purge(limit=amount, check=check_user)
            await interaction.followup.send(f'✅ Successfully deleted {len(deleted)} message(s) from {user.mention}!', ephemeral=True)
            
            log_event(interaction.guild.id, 'purge_messages', target_user_id=user.id, actor_user_id=interaction.user.id,
                     details={'amount': len(deleted), 'channel': interaction.channel.name})
        else:
            deleted = await interaction.channel.purge(limit=amount)
            await interaction.followup.send(f'✅ Successfully deleted {len(deleted)} message(s)!', ephemeral=True)
            
            log_event(interaction.guild.id, 'purge_messages', actor_user_id=interaction.user.id,
                     details={'amount': len(deleted), 'channel': interaction.channel.name})
    except discord.Forbidden:
        await interaction.followup.send('❌ I don\'t have permission to delete messages!', ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f'❌ An error occurred: {str(e)}', ephemeral=True)

@bot.tree.command(name="security", description="Complete guide to security features and setup")
async def security_guide(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ Security System Guide",
        description="Your server has powerful security features to protect against raids and unauthorized changes.\n\n**Quick Start:** Use `/setupguide` for step-by-step setup!",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    embed.add_field(
        name="1️⃣ Unified Logging",
        value="**What it does:** Sends ALL server events to one channel\n"
              "**Setup:** `/setgloballog` and choose a channel\n"
              "**Events tracked:** Member joins/leaves, bans, role changes, message edits/deletes, channel changes, permission modifications, and more!\n"
              "**Turn off:** `/disablegloballog`",
        inline=False
    )
    
    embed.add_field(
        name="2️⃣ Anti-Raid Protection",
        value="**What it does:** Automatically detects when too many people join at once\n"
              "**Setup:** `/configsecurity anti_raid:True`\n"
              "**Customize:** Set how many joins trigger an alert\n"
              "• `/configsecurity raid_threshold:10` - Alert if 10+ people join\n"
              "• `/configsecurity raid_window:60` - Within 60 seconds\n"
              "• `/configsecurity min_account_age:7` - Flag accounts under 7 days old\n"
              "**Auto-lockdown:** `/configsecurity auto_lockdown:True` to automatically lock the server during a raid",
        inline=False
    )
    
    embed.add_field(
        name="3️⃣ Permission Guard",
        value="**What it does:** Monitors when someone changes important role permissions\n"
              "**Setup:** `/configsecurity permission_guard:True`\n"
              "**Tracks:** Administrator, Manage Server, Manage Roles, Manage Channels, Kick/Ban permissions\n"
              "**Alerts:** Sends warnings when dangerous permissions are modified",
        inline=False
    )
    
    embed.add_field(
        name="4️⃣ Alert Role",
        value="**What it does:** Pings a specific role when security events happen\n"
              "**Setup:** `/configsecurity alert_role:@YourRole`\n"
              "**When it pings:** Raid detected, permission changes, and other suspicious activity",
        inline=False
    )
    
    embed.add_field(
        name="📊 Check Your Settings",
        value="`/securitystatus` - See what's enabled and recent activity\n"
              "`/logevents` - See all events you can track",
        inline=False
    )
    
    embed.set_footer(text="💡 Tip: Use /setupguide for an easy step-by-step setup process")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setupguide", description="Step-by-step guide to set up your server security")
@app_commands.checks.has_permissions(administrator=True)
async def setup_guide(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🚀 Quick Setup Guide",
        description="Follow these steps to fully protect your server:",
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    
    embed.add_field(
        name="Step 1: Set Up Logging 📝",
        value="Run `/setgloballog` and pick a channel where all events will be logged.\n"
              "This creates a complete record of everything happening in your server.",
        inline=False
    )
    
    embed.add_field(
        name="Step 2: Enable Anti-Raid Protection 🛡️",
        value="Run `/configsecurity anti_raid:True`\n"
              "**Recommended settings for most servers:**\n"
              "• `/configsecurity raid_threshold:5` (alert if 5+ join)\n"
              "• `/configsecurity raid_window:30` (within 30 seconds)\n"
              "• `/configsecurity min_account_age:7` (flag new accounts)",
        inline=False
    )
    
    embed.add_field(
        name="Step 3: Enable Permission Guard 🔐",
        value="Run `/configsecurity permission_guard:True`\n"
              "This will alert you if anyone tries to give themselves or others dangerous permissions.",
        inline=False
    )
    
    embed.add_field(
        name="Step 4: Set Up Alert Role 🔔",
        value="Run `/configsecurity alert_role:@YourModRole`\n"
              "Choose a mod/admin role to be pinged when security events happen.\n"
              "Make sure this role can see your log channel!",
        inline=False
    )
    
    embed.add_field(
        name="Step 5: Test Everything ✅",
        value="Run `/securitystatus` to verify all your settings are correct.\n"
              "Your server is now protected!",
        inline=False
    )
    
    embed.add_field(
        name="⚙️ Optional: Configure Lockdown",
        value="Set up emergency lockdown for serious situations:\n"
              "`/setlockdownconfig` - Set director role and announcement channel\n"
              "`/configsecurity auto_lockdown:True` - Auto-lockdown during raids",
        inline=False
    )
    
    embed.set_footer(text="Need more details? Use /security to see what each feature does")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="logevents", description="See all available log events and what they track")
async def log_events_list(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📋 Available Log Events",
        description="Here are all the events that can be tracked by the logging system:",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    embed.add_field(
        name="👥 Member Events",
        value="**Member Joins** - New members joining the server\n"
              "**Member Leaves** - Members leaving the server\n"
              "**Member Bans** - Members being banned\n"
              "**Member Unbans** - Members being unbanned\n"
              "**Role Changes** - When members get or lose roles",
        inline=False
    )
    
    embed.add_field(
        name="💬 Message Events",
        value="**Message Deletes** - When messages are deleted\n"
              "**Message Edits** - When messages are edited\n"
              "**Message Purge** - When multiple messages are bulk deleted",
        inline=False
    )
    
    embed.add_field(
        name="📁 Channel Events",
        value="**Channel Create** - New channels being created\n"
              "**Channel Delete** - Channels being deleted",
        inline=False
    )
    
    embed.add_field(
        name="🛡️ Security Events",
        value="**Raid Detected** - When anti-raid system triggers\n"
              "**Permission Changes** - When role permissions are modified\n"
              "**Lockdown Activated** - When emergency lockdown is enabled\n"
              "**Lockdown Deactivated** - When lockdown is lifted",
        inline=False
    )
    
    embed.add_field(
        name="How to Use",
        value="**Option 1:** Use `/setgloballog` to log ALL events to one channel (recommended)\n"
              "**Option 2:** Use `/setlogchannel` to send specific events to different channels",
        inline=False
    )
    
    embed.set_footer(text="💡 Tip: Global logging is easier to manage and gives you a complete audit trail")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="commands", description="List all available commands")
async def commands_list(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📋 Available Commands",
        description="Here are all the commands organized by category:",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )

    # Define your command categories and commands
    categories = {
        "🛡️ Security System (Start Here!)": [
            "security", "setupguide", "securitystatus", "logevents", 
            "configsecurity", "setgloballog", "disablegloballog"
        ],
        "🎖️ Agent Management": [
            "registeragent", "viewagent", "listagents", "deleteagent"
        ],
        "⚡ Duty System": [
            "dutyon", "dutyoff", "dutystatus", "dutylist", "setdutyrole"
        ],
        "📊 Polls & Voting": [
            "createpoll", "closepoll"
        ],
        "📝 Logging System": [
            "setlogchannel", "viewlogs"
        ],
        "🚨 Emergency Lockdown": [
            "setlockdownconfig", "lockdown", "unlockdown"
        ],
        "👋 Welcome System": [
            "setwelcomechannel", "setwelcomemessage", "setautorole", "testwelcome"
        ],
        "🎓 Training System": [
            "settrainingchannel", "settrainingmessage", "scheduletraining", "sethelperrole"
        ],
        "🎭 Role Request System": [
            "setuprolerequest", "createrolepanel"
        ],
        "⚠️ Warnings": [
            "warn", "clearwarnings", "viewwarnings"
        ],
        "🏆 Monthly Awards": [
            "setawardchannel", "setawardmessage", "sendmonthlyaward"
        ],
        "🎭 Reaction Roles": [
            "createreactionrole", "addreactionroleoption", "postreactionrole",
            "listreactionroles", "deletereactionrole", "testreactionrole"
        ],
        "⚙️ Utilities": [
            "setbotactivity", "sendembed", "wakeup", "purge", "commands"
        ]
    }

    for category_name, command_list in categories.items():
        value_lines = []
        for command_name in command_list:
            cmd = bot.tree.get_command(command_name)
            if cmd:
                value_lines.append(f"`/{cmd.name}` - {cmd.description}")
            else:
                value_lines.append(f"`/{command_name}` - No description set")
        embed.add_field(name=category_name, value="\n".join(value_lines), inline=False)

    embed.set_footer(text=f"Requested by {interaction.user}")

    await interaction.response.send_message(embed=embed)

token = os.getenv('DISCORD_BOT_TOKEN')
if token:
    bot.run(token)
else:
    print('ERROR: DISCORD_BOT_TOKEN not found in environment variables!')

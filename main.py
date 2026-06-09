import discord
from discord.ext import commands
from discord import app_commands
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
import aiohttp
import sqlite3
import json

TOKEN = os.getenv("TOKEN")
LOSTARK_API_KEY = os.getenv("LOSTARK_API_KEY")
KST = ZoneInfo("Asia/Seoul")

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
recruitments = {}

conn = sqlite3.connect("lostark_bot.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS rosters (
    user_id INTEGER PRIMARY KEY,
    roster_data TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    recruit_channel_id INTEGER,
    archive_channel_id INTEGER,
    verify_channel_id INTEGER,
    intro_channel_id INTEGER,
    selfrole_channel_id INTEGER,
    member_role_id INTEGER,
    newbie_role_id INTEGER
)
""")
conn.commit()


def ensure_column(table, column, col_type):
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()


ensure_column("guild_settings", "verify_channel_id", "INTEGER")
ensure_column("guild_settings", "intro_channel_id", "INTEGER")
ensure_column("guild_settings", "selfrole_channel_id", "INTEGER")
ensure_column("guild_settings", "member_role_id", "INTEGER")
ensure_column("guild_settings", "newbie_role_id", "INTEGER")
ensure_column("guild_settings", "archive_channel_id", "INTEGER")

RAIDS = [
    "서막:에키드나",
    "1막:에기르",
    "2막:아브렐슈드",
    "3막:모르둠",
    "4막:아르모체",
    "종막:카제로스",
    "세르카",
    "지평의 성당"
]

DIFFICULTIES = ["노말", "하드", "나이트메어"]
SKILLS = ["트라이", "클경", "반숙", "숙련"]

RAID_LIMITS = {
    "서막:에키드나": {"dealer": 6, "support": 2},
    "1막:에기르": {"dealer": 6, "support": 2},
    "2막:아브렐슈드": {"dealer": 6, "support": 2},
    "3막:모르둠": {"dealer": 6, "support": 2},
    "4막:아르모체": {"dealer": 6, "support": 2},
    "종막:카제로스": {"dealer": 6, "support": 2},
    "세르카": {"dealer": 3, "support": 1},
    "지평의 성당": {"dealer": 3, "support": 1}
}

POSITION_ROLES = ["⚔️ 딜러", "🎵 서포터"]
TIME_ROLES = ["🌙 밤반", "☀️ 낮반", "🌌 새벽반"]
VOICE_ROLES = ["🎤 음성 가능", "🔇 듣코", "🚫 음성·듣코 불가"]
STYLE_ROLES = ["🐣 트라이 선호", "🔥 숙련 선호", "🤝 숙련도 상관없음"]


def set_guild_value(guild_id, key, value):
    cursor.execute(
        f"""
        INSERT INTO guild_settings (guild_id, {key})
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET {key}=excluded.{key}
        """,
        (guild_id, value)
    )
    conn.commit()


def get_guild_value(guild_id, key):
    cursor.execute(
        f"SELECT {key} FROM guild_settings WHERE guild_id = ?",
        (guild_id,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def get_item_level(character):
    return (
        character.get("ItemMaxLevel")
        or character.get("ItemAvgLevel")
        or character.get("ItemLevel")
        or "레벨없음"
    )


def load_roster(user_id):
    cursor.execute("SELECT roster_data FROM rosters WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return json.loads(row[0]) if row else []


def save_roster(user_id, new_data):
    existing = load_roster(user_id)
    existing_names = {c["CharacterName"] for c in existing}

    for char in new_data:
        if char["CharacterName"] not in existing_names:
            existing.append(char)

    cursor.execute(
        "REPLACE INTO rosters (user_id, roster_data) VALUES (?, ?)",
        (user_id, json.dumps(existing, ensure_ascii=False))
    )
    conn.commit()


def clear_roster(user_id):
    cursor.execute("DELETE FROM rosters WHERE user_id = ?", (user_id,))
    conn.commit()


async def fetch_lostark_siblings(character_name):
    url = f"https://developer-lostark.game.onstove.com/characters/{character_name}/siblings"
    headers = {
        "accept": "application/json",
        "authorization": LOSTARK_API_KEY
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                print("LostArk API status:", response.status)
                print(await response.text())
                return None
            return await response.json()


def find_role(guild, role_name):
    return discord.utils.get(guild.roles, name=role_name)


async def set_single_role(member, role_name, group):
    guild = member.guild
    add_role = find_role(guild, role_name)

    if not add_role:
        return False, f"`{role_name}` 역할을 찾을 수 없습니다."

    remove_roles = [
        role for name in group
        if (role := find_role(guild, name))
        and role in member.roles
        and role.name != role_name
    ]

    if remove_roles:
        await member.remove_roles(*remove_roles)

    if add_role in member.roles:
        await member.remove_roles(add_role)
        return True, f"{add_role.mention} 역할을 해제했습니다."

    await member.add_roles(add_role)
    return True, f"{add_role.mention} 역할을 선택했습니다."


async def toggle_role(member, role_name):
    role = find_role(member.guild, role_name)

    if not role:
        return False, f"`{role_name}` 역할을 찾을 수 없습니다."

    if role in member.roles:
        await member.remove_roles(role)
        return True, f"{role.mention} 역할을 해제했습니다."

    await member.add_roles(role)
    return True, f"{role.mention} 역할을 선택했습니다."


def make_member_text(members):
    if not members:
        return "-"

    return "\n".join([
        f"• **{m['character']}** / {m.get('class_name', '직업없음')} / Lv.{m.get('item_level', '레벨없음')} <@{m['user_id']}>"
        for m in members
    ])


def make_recruit_embed(data):
    is_closed = data.get("closed", False)
    is_full = (
        len(data["dealer"]) >= data["max_dealer"]
        and len(data["support"]) >= data["max_support"]
    )

    if is_closed:
        status = "🔒 모집 마감"
        color = discord.Color.dark_gray()
    elif is_full:
        status = "✅ 모집 완료"
        color = discord.Color.green()
    else:
        status = "🟢 모집 중"
        color = discord.Color.blurple()

    embed = discord.Embed(
        title=f"⚔️ {data['raid']} 레이드 모집",
        description=(
            f"**{status}**\n\n"
            f"🗓️ 출발 시간\n"
            f"> `{data['start_time_text']}`\n\n"
            f"🎚️ 난이도 `{data['difficulty']}` | 📘 숙련도 `{data['skill']}`"
        ),
        color=color
    )

    embed.add_field(
        name=f"🔥 딜러 {len(data['dealer'])}/{data['max_dealer']}",
        value=make_member_text(data["dealer"]),
        inline=False
    )

    embed.add_field(
        name=f"💚 서포터 {len(data['support'])}/{data['max_support']}",
        value=make_member_text(data["support"]),
        inline=False
    )

    embed.set_footer(text=f"생성자: {data['creator_name']} · 사뭇 레이드 모집")
    return embed


async def reminder_task(message_id):
    data = recruitments.get(message_id)
    if not data:
        return

    wait_seconds = (data["start_time"] - timedelta(minutes=10) - datetime.now(KST)).total_seconds()
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)

    data = recruitments.get(message_id)
    if not data or data.get("closed"):
        return

    members = data["dealer"] + data["support"]
    if not members:
        return

    mentions = " ".join([f"<@{m['user_id']}>" for m in members])
    channel = bot.get_channel(data["channel_id"])

    if channel:
        await channel.send(
            f"⏰ {mentions}\n"
            f"**{data['raid']} {data['difficulty']}** 출발 10분 전입니다!"
        )


async def close_recruitment_task(message_id):
    data = recruitments.get(message_id)
    if not data:
        return

    wait_seconds = (data["start_time"] - datetime.now(KST)).total_seconds()
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)

    data = recruitments.get(message_id)
    if not data:
        return

    data["closed"] = True
    channel = bot.get_channel(data["channel_id"])

    if not channel:
        return

    try:
        msg = await channel.fetch_message(message_id)
        await msg.edit(embed=make_recruit_embed(data), view=None)

        archive_channel_id = get_guild_value(data["guild_id"], "archive_channel_id")
        if archive_channel_id:
            archive_channel = bot.get_channel(archive_channel_id)
            if archive_channel:
                await archive_channel.send(embed=make_recruit_embed(data))

        await asyncio.sleep(5)
        await msg.delete()

    except discord.NotFound:
        return


class DateTimeModal(discord.ui.Modal, title="출발 날짜/시간 입력"):
    date = discord.ui.TextInput(label="날짜", placeholder="예: 2026-06-09", required=True)
    time = discord.ui.TextInput(label="시간", placeholder="예: 21:30", required=True)

    def __init__(self, target_view):
        super().__init__()
        self.target_view = target_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            dt = datetime.strptime(
                f"{self.date.value} {self.time.value}",
                "%Y-%m-%d %H:%M"
            ).replace(tzinfo=KST)
        except ValueError:
            await interaction.response.send_message("날짜/시간 형식이 틀렸어요.", ephemeral=True)
            return

        self.target_view.start_time = dt
        self.target_view.start_time_text = dt.strftime("%Y-%m-%d %H:%M")

        await interaction.response.send_message(
            f"출발 시간 설정 완료: {self.target_view.start_time_text}",
            ephemeral=True
        )


class VerifyModal(discord.ui.Modal, title="사뭇 길드 인증 신청"):
    nickname = discord.ui.TextInput(label="닉네임", placeholder="예: 하도앵", required=True, max_length=30)
    main_class = discord.ui.TextInput(label="본캐 직업", placeholder="예: 바드", required=True, max_length=30)
    item_level = discord.ui.TextInput(label="템렙", placeholder="예: 1710", required=True, max_length=20)
    active_time = discord.ui.TextInput(label="주 활동 시간", placeholder="예: 평일 저녁 ~ 새벽", required=True, max_length=50)
    comment = discord.ui.TextInput(
        label="한마디",
        placeholder="예: 오래 재밌게 같이 하고 싶어요!",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=300
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user

        member_role_id = get_guild_value(guild.id, "member_role_id")
        newbie_role_id = get_guild_value(guild.id, "newbie_role_id")
        intro_channel_id = get_guild_value(guild.id, "intro_channel_id")
        selfrole_channel_id = get_guild_value(guild.id, "selfrole_channel_id")

        member_role = guild.get_role(member_role_id) if member_role_id else None
        newbie_role = guild.get_role(newbie_role_id) if newbie_role_id else None
        intro_channel = guild.get_channel(intro_channel_id) if intro_channel_id else None
        selfrole_channel = guild.get_channel(selfrole_channel_id) if selfrole_channel_id else None

        if not member_role:
            await interaction.response.send_message(
                "길드원 역할이 설정되지 않았습니다. 관리자에게 문의해주세요.",
                ephemeral=True
            )
            return

        await member.add_roles(member_role)

        if newbie_role and newbie_role in member.roles:
            await member.remove_roles(newbie_role)

        embed = discord.Embed(
            title="✨ 새로운 길드원이 합류했습니다!",
            color=discord.Color.gold()
        )
        embed.add_field(name="닉네임", value=self.nickname.value, inline=True)
        embed.add_field(name="본캐 직업", value=self.main_class.value, inline=True)
        embed.add_field(name="템렙", value=self.item_level.value, inline=True)
        embed.add_field(name="주 활동 시간", value=self.active_time.value, inline=False)
        embed.add_field(name="한마디", value=self.comment.value, inline=False)
        embed.set_footer(text="🎉 모두 따뜻하게 환영해주세요!")

        if intro_channel:
            await intro_channel.send(
                content=f"{member.mention} 님이 인증을 완료했습니다!",
                embed=embed
            )

        msg = "✅ 인증이 완료되었습니다!\n길드원 역할이 지급되었고, 신입소개 채널에 자기소개가 등록되었습니다."

        if selfrole_channel:
            msg += f"\n\n🎭 다음으로 {selfrole_channel.mention} 에서 본인에게 맞는 셀프 역할을 선택해주세요."

        await interaction.response.send_message(msg, ephemeral=True)


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ 인증 신청",
        style=discord.ButtonStyle.success,
        custom_id="verify_apply_button"
    )
    async def verify_apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerifyModal())


class SelfRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚔️ 딜러", style=discord.ButtonStyle.primary, custom_id="role_dealer")
    async def role_dealer(self, interaction, button):
        ok, msg = await set_single_role(interaction.user, "⚔️ 딜러", POSITION_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🎵 서포터", style=discord.ButtonStyle.primary, custom_id="role_support")
    async def role_support(self, interaction, button):
        ok, msg = await set_single_role(interaction.user, "🎵 서포터", POSITION_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🌙 밤반", style=discord.ButtonStyle.secondary, custom_id="role_night")
    async def role_night(self, interaction, button):
        ok, msg = await toggle_role(interaction.user, "🌙 밤반")
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="☀️ 낮반", style=discord.ButtonStyle.secondary, custom_id="role_day")
    async def role_day(self, interaction, button):
        ok, msg = await toggle_role(interaction.user, "☀️ 낮반")
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🌌 새벽반", style=discord.ButtonStyle.secondary, custom_id="role_dawn")
    async def role_dawn(self, interaction, button):
        ok, msg = await toggle_role(interaction.user, "🌌 새벽반")
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🎤 음성 가능", style=discord.ButtonStyle.success, custom_id="role_voice")
    async def role_voice(self, interaction, button):
        ok, msg = await set_single_role(interaction.user, "🎤 음성 가능", VOICE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🔇 듣코", style=discord.ButtonStyle.success, custom_id="role_listen")
    async def role_listen(self, interaction, button):
        ok, msg = await set_single_role(interaction.user, "🔇 듣코", VOICE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🚫 음성·듣코 불가", style=discord.ButtonStyle.success, custom_id="role_no_voice")
    async def role_no_voice(self, interaction, button):
        ok, msg = await set_single_role(interaction.user, "🚫 음성·듣코 불가", VOICE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🐣 트라이 선호", style=discord.ButtonStyle.danger, custom_id="role_try")
    async def role_try(self, interaction, button):
        ok, msg = await set_single_role(interaction.user, "🐣 트라이 선호", STYLE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🔥 숙련 선호", style=discord.ButtonStyle.danger, custom_id="role_exp")
    async def role_exp(self, interaction, button):
        ok, msg = await set_single_role(interaction.user, "🔥 숙련 선호", STYLE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🤝 숙련도 상관없음", style=discord.ButtonStyle.danger, custom_id="role_any")
    async def role_any(self, interaction, button):
        ok, msg = await set_single_role(interaction.user, "🤝 숙련도 상관없음", STYLE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)


class RosterRegisterSelect(discord.ui.Select):
    def __init__(self, characters):
        self.characters = characters[:25]

        options = [
            discord.SelectOption(
                label=c["CharacterName"],
                description=f"{c.get('CharacterClassName', '직업없음')} / Lv.{get_item_level(c)}"
            )
            for c in self.characters
        ]

        super().__init__(
            placeholder="등록할 캐릭터 선택",
            min_values=1,
            max_values=len(options),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        selected_characters = [
            c for c in self.characters
            if c["CharacterName"] in self.values
        ]

        save_roster(interaction.user.id, selected_characters)
        await interaction.response.send_message("캐릭터 등록 완료!", ephemeral=True)


class RosterRegisterView(discord.ui.View):
    def __init__(self, characters):
        super().__init__(timeout=180)
        self.add_item(RosterRegisterSelect(characters))


class CharacterSelect(discord.ui.Select):
    def __init__(self, message_id, role_type, user_id):
        self.message_id = message_id
        self.role_type = role_type
        self.user_id = user_id

        roster = load_roster(user_id)

        options = [
            discord.SelectOption(
                label=c["CharacterName"],
                description=f"{c.get('CharacterClassName', '직업없음')} / Lv.{get_item_level(c)}"
            )
            for c in roster[:25]
        ]

        super().__init__(placeholder="신청 캐릭터 선택", options=options)

    async def callback(self, interaction: discord.Interaction):
        data = recruitments.get(self.message_id)

        if not data:
            await interaction.response.send_message("모집 정보 없음", ephemeral=True)
            return

        if data.get("closed"):
            await interaction.response.send_message("이미 마감된 모집입니다.", ephemeral=True)
            return

        char_name = self.values[0]

        data["dealer"] = [m for m in data["dealer"] if m["user_id"] != self.user_id]
        data["support"] = [m for m in data["support"] if m["user_id"] != self.user_id]

        roster = load_roster(self.user_id)
        selected_char = next((c for c in roster if c["CharacterName"] == char_name), None)

        member_data = {
            "user_id": self.user_id,
            "character": char_name,
            "class_name": selected_char.get("CharacterClassName", "직업없음"),
            "item_level": get_item_level(selected_char)
        }

        if self.role_type == "dealer":
            if len(data["dealer"]) >= data["max_dealer"]:
                await interaction.response.send_message("딜러 인원 마감", ephemeral=True)
                return
            data["dealer"].append(member_data)
        else:
            if len(data["support"]) >= data["max_support"]:
                await interaction.response.send_message("서포터 인원 마감", ephemeral=True)
                return
            data["support"].append(member_data)

        msg_channel = bot.get_channel(data["channel_id"])
        msg = await msg_channel.fetch_message(self.message_id)

        await msg.edit(embed=make_recruit_embed(data), view=JoinView(self.message_id))
        await interaction.response.send_message(f"{char_name} 신청 완료!", ephemeral=True)


class CharacterSelectView(discord.ui.View):
    def __init__(self, message_id, role_type, user_id):
        super().__init__(timeout=60)
        self.add_item(CharacterSelect(message_id, role_type, user_id))


class RaidSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.raid = None
        self.difficulty = None
        self.skill = None
        self.start_time = None
        self.start_time_text = None

    @discord.ui.select(placeholder="레이드 선택", options=[discord.SelectOption(label=r) for r in RAIDS])
    async def raid_select(self, interaction, select):
        self.raid = select.values[0]
        await interaction.response.send_message(f"레이드 선택: {self.raid}", ephemeral=True)

    @discord.ui.select(placeholder="난이도 선택", options=[discord.SelectOption(label=d) for d in DIFFICULTIES])
    async def difficulty_select(self, interaction, select):
        self.difficulty = select.values[0]
        await interaction.response.send_message(f"난이도 선택: {self.difficulty}", ephemeral=True)

    @discord.ui.select(placeholder="숙련도 선택", options=[discord.SelectOption(label=s) for s in SKILLS])
    async def skill_select(self, interaction, select):
        self.skill = select.values[0]
        await interaction.response.send_message(f"숙련도 선택: {self.skill}", ephemeral=True)

    @discord.ui.button(label="날짜/시간 입력", style=discord.ButtonStyle.secondary)
    async def set_datetime(self, interaction, button):
        await interaction.response.send_modal(DateTimeModal(self))

    @discord.ui.button(label="모집글 생성", style=discord.ButtonStyle.primary)
    async def create_recruitment(self, interaction, button):
        if not all([self.raid, self.difficulty, self.skill, self.start_time]):
            await interaction.response.send_message("모든 항목 선택 필요", ephemeral=True)
            return

        channel_id = get_guild_value(interaction.guild.id, "recruit_channel_id")

        if not channel_id:
            await interaction.response.send_message("먼저 /모집채널설정 해주세요.", ephemeral=True)
            return

        target_channel = interaction.guild.get_channel(channel_id)

        if not target_channel:
            await interaction.response.send_message("설정된 모집 채널을 찾을 수 없습니다.", ephemeral=True)
            return

        limits = RAID_LIMITS[self.raid]

        data = {
            "raid": self.raid,
            "difficulty": self.difficulty,
            "skill": self.skill,
            "start_time": self.start_time,
            "start_time_text": self.start_time_text,
            "dealer": [],
            "support": [],
            "max_dealer": limits["dealer"],
            "max_support": limits["support"],
            "creator_id": interaction.user.id,
            "creator_name": interaction.user.display_name,
            "channel_id": target_channel.id,
            "guild_id": interaction.guild.id,
            "closed": False
        }

        msg = await target_channel.send(embed=make_recruit_embed(data))
        recruitments[msg.id] = data

        asyncio.create_task(reminder_task(msg.id))
        asyncio.create_task(close_recruitment_task(msg.id))

        await msg.edit(view=JoinView(msg.id))

        await interaction.response.send_message(
            f"{target_channel.mention} 에 모집글 생성 완료!",
            ephemeral=True
        )


class JoinView(discord.ui.View):
    def __init__(self, message_id):
        super().__init__(timeout=None)
        self.message_id = message_id

    async def open_character_select(self, interaction, role_type):
        roster = load_roster(interaction.user.id)

        if not roster:
            await interaction.response.send_message("먼저 /대표캐릭등록 해주세요.", ephemeral=True)
            return

        await interaction.response.send_message(
            "캐릭터 선택",
            view=CharacterSelectView(self.message_id, role_type, interaction.user.id),
            ephemeral=True
        )

    @discord.ui.button(label="딜러 신청", style=discord.ButtonStyle.red)
    async def dealer_join(self, interaction, button):
        await self.open_character_select(interaction, "dealer")

    @discord.ui.button(label="서폿 신청", style=discord.ButtonStyle.green)
    async def support_join(self, interaction, button):
        await self.open_character_select(interaction, "support")


@bot.tree.command(name="모집채널설정")
@app_commands.checks.has_permissions(administrator=True)
async def 모집채널설정(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_value(interaction.guild.id, "recruit_channel_id", channel.id)
    await interaction.response.send_message(f"모집 채널 설정 완료: {channel.mention}", ephemeral=True)


@bot.tree.command(name="모집기록채널설정")
@app_commands.checks.has_permissions(administrator=True)
async def 모집기록채널설정(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_value(interaction.guild.id, "archive_channel_id", channel.id)
    await interaction.response.send_message(f"모집 기록 채널 설정 완료: {channel.mention}", ephemeral=True)


@bot.tree.command(name="인증채널설정")
@app_commands.checks.has_permissions(administrator=True)
async def 인증채널설정(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_value(interaction.guild.id, "verify_channel_id", channel.id)
    await interaction.response.send_message(f"인증 채널 설정 완료: {channel.mention}", ephemeral=True)


@bot.tree.command(name="신입소개채널설정")
@app_commands.checks.has_permissions(administrator=True)
async def 신입소개채널설정(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_value(interaction.guild.id, "intro_channel_id", channel.id)
    await interaction.response.send_message(f"신입소개 채널 설정 완료: {channel.mention}", ephemeral=True)


@bot.tree.command(name="셀프역할채널설정")
@app_commands.checks.has_permissions(administrator=True)
async def 셀프역할채널설정(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_value(interaction.guild.id, "selfrole_channel_id", channel.id)
    await interaction.response.send_message(f"셀프역할 채널 설정 완료: {channel.mention}", ephemeral=True)


@bot.tree.command(name="길드원역할설정")
@app_commands.checks.has_permissions(administrator=True)
async def 길드원역할설정(interaction: discord.Interaction, role: discord.Role):
    set_guild_value(interaction.guild.id, "member_role_id", role.id)
    await interaction.response.send_message(f"길드원 역할 설정 완료: {role.mention}", ephemeral=True)


@bot.tree.command(name="신입역할설정")
@app_commands.checks.has_permissions(administrator=True)
async def 신입역할설정(interaction: discord.Interaction, role: discord.Role):
    set_guild_value(interaction.guild.id, "newbie_role_id", role.id)
    await interaction.response.send_message(f"신입 역할 설정 완료: {role.mention}", ephemeral=True)


@bot.tree.command(name="인증패널생성")
@app_commands.checks.has_permissions(administrator=True)
async def 인증패널생성(interaction: discord.Interaction):
    channel_id = get_guild_value(interaction.guild.id, "verify_channel_id")
    target_channel = interaction.guild.get_channel(channel_id) if channel_id else interaction.channel

    embed = discord.Embed(
        title="🎉 사뭇 길드 인증 안내",
        description=(
            "아래 버튼을 눌러 길드 인증을 진행해주세요!\n\n"
            "인증 완료 시:\n"
            "• ✅ 길드원 역할이 자동 지급됩니다.\n"
            "• 📝 작성한 자기소개가 신입소개 채널에 자동 등록됩니다.\n"
            "• 🎭 인증 후 셀프 역할 채널도 이용 부탁드립니다.\n\n"
            "편하게 작성해주세요 😊"
        ),
        color=discord.Color.green()
    )

    await target_channel.send(embed=embed, view=VerifyView())
    await interaction.response.send_message(f"인증 패널 생성 완료: {target_channel.mention}", ephemeral=True)


@bot.tree.command(name="셀프역할패널생성")
@app_commands.checks.has_permissions(administrator=True)
async def 셀프역할패널생성(interaction: discord.Interaction):
    channel_id = get_guild_value(interaction.guild.id, "selfrole_channel_id")
    target_channel = interaction.guild.get_channel(channel_id) if channel_id else interaction.channel

    embed = discord.Embed(
        title="🎭 사뭇 셀프 역할 선택",
        description=(
            "아래 버튼을 눌러 본인에게 맞는 역할을 선택해주세요.\n\n"
            "⚔️ **포지션** — 단일 선택\n"
            "🌙 **플레이 시간대** — 중복 선택 가능\n"
            "🎧 **플레이 스타일** — 단일 선택\n"
            "🔥 **레이드 성향** — 단일 선택"
        ),
        color=discord.Color.blurple()
    )

    await target_channel.send(embed=embed, view=SelfRoleView())
    await interaction.response.send_message(f"셀프 역할 패널 생성 완료: {target_channel.mention}", ephemeral=True)


@bot.tree.command(name="대표캐릭등록")
async def 대표캐릭등록(interaction: discord.Interaction, 캐릭터명: str):
    await interaction.response.defer(ephemeral=True)

    siblings = await fetch_lostark_siblings(캐릭터명)

    if not siblings:
        await interaction.followup.send("캐릭터 조회 실패", ephemeral=True)
        return

    await interaction.followup.send("등록할 캐릭터 선택", view=RosterRegisterView(siblings), ephemeral=True)


@bot.tree.command(name="내캐릭터")
async def 내캐릭터(interaction: discord.Interaction):
    roster = load_roster(interaction.user.id)

    if not roster:
        await interaction.response.send_message("등록된 캐릭터 없음", ephemeral=True)
        return

    names = "\n".join([
        f"{c['CharacterName']} / {c.get('CharacterClassName', '직업없음')} / Lv.{get_item_level(c)}"
        for c in roster[:25]
    ])

    await interaction.response.send_message(names, ephemeral=True)


@bot.tree.command(name="캐릭터초기화")
async def 캐릭터초기화(interaction: discord.Interaction):
    clear_roster(interaction.user.id)
    await interaction.response.send_message("캐릭터 초기화 완료", ephemeral=True)


@bot.tree.command(name="모집")
async def 모집(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚔️ 레이드 모집 설정",
        description=(
            "1️⃣ 레이드 선택\n"
            "2️⃣ 난이도 선택\n"
            "3️⃣ 숙련도 선택\n"
            "4️⃣ 날짜/시간 입력\n"
            "5️⃣ 모집글 생성"
        ),
        color=discord.Color.purple()
    )

    await interaction.response.send_message(
        embed=embed,
        view=RaidSetupView(),
        ephemeral=True
    )


@bot.event
async def on_member_join(member):
    newbie_role_id = get_guild_value(member.guild.id, "newbie_role_id")
    newbie_role = member.guild.get_role(newbie_role_id) if newbie_role_id else None

    if newbie_role:
        try:
            await member.add_roles(newbie_role)
        except discord.Forbidden:
            print("신입 역할 지급 실패: 봇 역할 순서/권한 확인 필요")


@bot.event
async def on_ready():
    bot.add_view(VerifyView())
    bot.add_view(SelfRoleView())

    try:
        synced = await bot.tree.sync()
        print(f"슬래시 명령어 동기화 완료: {len(synced)}개")
    except Exception as e:
        print(e)

    print(f"{bot.user} 로그인 완료")


bot.run(TOKEN)
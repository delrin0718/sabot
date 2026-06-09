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
    archive_channel_id INTEGER
)
""")

conn.commit()

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


def set_recruit_channel(guild_id, channel_id):
    cursor.execute("""
    INSERT INTO guild_settings (guild_id, recruit_channel_id)
    VALUES (?, ?)
    ON CONFLICT(guild_id)
    DO UPDATE SET recruit_channel_id=excluded.recruit_channel_id
    """, (guild_id, channel_id))
    conn.commit()


def set_archive_channel(guild_id, channel_id):
    cursor.execute("""
    INSERT INTO guild_settings (guild_id, archive_channel_id)
    VALUES (?, ?)
    ON CONFLICT(guild_id)
    DO UPDATE SET archive_channel_id=excluded.archive_channel_id
    """, (guild_id, channel_id))
    conn.commit()


def get_recruit_channel(guild_id):
    cursor.execute("""
    SELECT recruit_channel_id
    FROM guild_settings
    WHERE guild_id = ?
    """, (guild_id,))

    row = cursor.fetchone()

    if row:
        return row[0]

    return None


def get_archive_channel(guild_id):
    cursor.execute("""
    SELECT archive_channel_id
    FROM guild_settings
    WHERE guild_id = ?
    """, (guild_id,))

    row = cursor.fetchone()

    if row:
        return row[0]

    return None


def get_item_level(character):
    return (
        character.get("ItemMaxLevel")
        or character.get("ItemAvgLevel")
        or character.get("ItemLevel")
        or "레벨없음"
    )


def load_roster(user_id):
    cursor.execute(
        "SELECT roster_data FROM rosters WHERE user_id = ?",
        (user_id,)
    )

    row = cursor.fetchone()

    if not row:
        return []

    return json.loads(row[0])


def save_roster(user_id, new_data):
    existing = load_roster(user_id)

    existing_names = {
        c["CharacterName"]
        for c in existing
    }

    for char in new_data:
        if char["CharacterName"] not in existing_names:
            existing.append(char)

    cursor.execute(
        "REPLACE INTO rosters (user_id, roster_data) VALUES (?, ?)",
        (user_id, json.dumps(existing, ensure_ascii=False))
    )

    conn.commit()


def clear_roster(user_id):
    cursor.execute(
        "DELETE FROM rosters WHERE user_id = ?",
        (user_id,)
    )

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
            f"🎚️ 난이도 `{data['difficulty']}` | "
            f"📘 숙련도 `{data['skill']}`"
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

    embed.set_footer(
        text=f"생성자: {data['creator_name']} · 사뭇 레이드 모집"
    )

    return embed


async def reminder_task(message_id):
    data = recruitments.get(message_id)

    if not data:
        return

    wait_seconds = (
        data["start_time"]
        - timedelta(minutes=10)
        - datetime.now(KST)
    ).total_seconds()

    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)

    data = recruitments.get(message_id)

    if not data or data.get("closed"):
        return

    members = data["dealer"] + data["support"]

    if not members:
        return

    mentions = " ".join([
        f"<@{m['user_id']}>"
        for m in members
    ])

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

    wait_seconds = (
        data["start_time"]
        - datetime.now(KST)
    ).total_seconds()

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

        await msg.edit(
            embed=make_recruit_embed(data),
            view=None
        )

        archive_channel_id = get_archive_channel(data["guild_id"])

        if archive_channel_id:
            archive_channel = bot.get_channel(archive_channel_id)

            if archive_channel:
                await archive_channel.send(
                    embed=make_recruit_embed(data)
                )

        await asyncio.sleep(5)

        await msg.delete()

    except discord.NotFound:
        return


class DateTimeModal(discord.ui.Modal, title="출발 날짜/시간 입력"):

    date = discord.ui.TextInput(
        label="날짜",
        placeholder="예: 2026-06-09",
        required=True
    )

    time = discord.ui.TextInput(
        label="시간",
        placeholder="예: 21:30",
        required=True
    )

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
            await interaction.response.send_message(
                "날짜/시간 형식이 틀렸어요.",
                ephemeral=True
            )
            return

        self.target_view.start_time = dt
        self.target_view.start_time_text = dt.strftime("%Y-%m-%d %H:%M")

        await interaction.response.send_message(
            f"출발 시간 설정 완료: {self.target_view.start_time_text}",
            ephemeral=True
        )


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

        selected_names = self.values

        selected_characters = [
            c for c in self.characters
            if c["CharacterName"] in selected_names
        ]

        save_roster(interaction.user.id, selected_characters)

        await interaction.response.send_message(
            "캐릭터 등록 완료!",
            ephemeral=True
        )


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

        super().__init__(
            placeholder="신청 캐릭터 선택",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        data = recruitments.get(self.message_id)

        if not data:
            await interaction.response.send_message(
                "모집 정보 없음",
                ephemeral=True
            )
            return

        if data.get("closed"):
            await interaction.response.send_message(
                "이미 마감된 모집입니다.",
                ephemeral=True
            )
            return

        char_name = self.values[0]

        data["dealer"] = [
            m for m in data["dealer"]
            if m["user_id"] != self.user_id
        ]

        data["support"] = [
            m for m in data["support"]
            if m["user_id"] != self.user_id
        ]

        roster = load_roster(self.user_id)

        selected_char = next(
            (
                c for c in roster
                if c["CharacterName"] == char_name
            ),
            None
        )

        member_data = {
            "user_id": self.user_id,
            "character": char_name,
            "class_name": selected_char.get("CharacterClassName", "직업없음"),
            "item_level": get_item_level(selected_char)
        }

        if self.role_type == "dealer":

            if len(data["dealer"]) >= data["max_dealer"]:
                await interaction.response.send_message(
                    "딜러 인원 마감",
                    ephemeral=True
                )
                return

            data["dealer"].append(member_data)

        else:

            if len(data["support"]) >= data["max_support"]:
                await interaction.response.send_message(
                    "서포터 인원 마감",
                    ephemeral=True
                )
                return

            data["support"].append(member_data)

        msg_channel = bot.get_channel(data["channel_id"])
        msg = await msg_channel.fetch_message(self.message_id)

        await msg.edit(
            embed=make_recruit_embed(data),
            view=JoinView(self.message_id)
        )

        await interaction.response.send_message(
            f"{char_name} 신청 완료!",
            ephemeral=True
        )


class CharacterSelectView(discord.ui.View):

    def __init__(self, message_id, role_type, user_id):
        super().__init__(timeout=60)
        self.add_item(
            CharacterSelect(
                message_id,
                role_type,
                user_id
            )
        )


class RaidSetupView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=180)

        self.raid = None
        self.difficulty = None
        self.skill = None
        self.start_time = None
        self.start_time_text = None

    @discord.ui.select(
        placeholder="레이드 선택",
        options=[
            discord.SelectOption(label=r)
            for r in RAIDS
        ]
    )
    async def raid_select(self, interaction, select):
        self.raid = select.values[0]

        await interaction.response.send_message(
            f"레이드 선택: {self.raid}",
            ephemeral=True
        )

    @discord.ui.select(
        placeholder="난이도 선택",
        options=[
            discord.SelectOption(label=d)
            for d in DIFFICULTIES
        ]
    )
    async def difficulty_select(self, interaction, select):
        self.difficulty = select.values[0]

        await interaction.response.send_message(
            f"난이도 선택: {self.difficulty}",
            ephemeral=True
        )

    @discord.ui.select(
        placeholder="숙련도 선택",
        options=[
            discord.SelectOption(label=s)
            for s in SKILLS
        ]
    )
    async def skill_select(self, interaction, select):
        self.skill = select.values[0]

        await interaction.response.send_message(
            f"숙련도 선택: {self.skill}",
            ephemeral=True
        )

    @discord.ui.button(
        label="날짜/시간 입력",
        style=discord.ButtonStyle.secondary
    )
    async def set_datetime(self, interaction, button):
        await interaction.response.send_modal(
            DateTimeModal(self)
        )

    @discord.ui.button(
        label="모집글 생성",
        style=discord.ButtonStyle.primary
    )
    async def create_recruitment(self, interaction, button):

        if not all([
            self.raid,
            self.difficulty,
            self.skill,
            self.start_time
        ]):
            await interaction.response.send_message(
                "모든 항목 선택 필요",
                ephemeral=True
            )
            return

        channel_id = get_recruit_channel(interaction.guild.id)

        if not channel_id:
            await interaction.response.send_message(
                "먼저 /모집채널설정 해주세요.",
                ephemeral=True
            )
            return

        target_channel = interaction.guild.get_channel(channel_id)

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

        msg = await target_channel.send(
            embed=make_recruit_embed(data)
        )

        recruitments[msg.id] = data

        asyncio.create_task(reminder_task(msg.id))
        asyncio.create_task(close_recruitment_task(msg.id))

        await msg.edit(
            view=JoinView(msg.id)
        )

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
            await interaction.response.send_message(
                "먼저 /대표캐릭등록 해주세요.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "캐릭터 선택",
            view=CharacterSelectView(
                self.message_id,
                role_type,
                interaction.user.id
            ),
            ephemeral=True
        )

    @discord.ui.button(
        label="딜러 신청",
        style=discord.ButtonStyle.red
    )
    async def dealer_join(self, interaction, button):
        await self.open_character_select(
            interaction,
            "dealer"
        )

    @discord.ui.button(
        label="서폿 신청",
        style=discord.ButtonStyle.green
    )
    async def support_join(self, interaction, button):
        await self.open_character_select(
            interaction,
            "support"
        )


@bot.tree.command(name="모집채널설정")
@app_commands.checks.has_permissions(administrator=True)
async def 모집채널설정(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):

    set_recruit_channel(
        interaction.guild.id,
        channel.id
    )

    await interaction.response.send_message(
        f"모집 채널 설정 완료: {channel.mention}",
        ephemeral=True
    )


@bot.tree.command(name="모집기록채널설정")
@app_commands.checks.has_permissions(administrator=True)
async def 모집기록채널설정(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):

    set_archive_channel(
        interaction.guild.id,
        channel.id
    )

    await interaction.response.send_message(
        f"모집 기록 채널 설정 완료: {channel.mention}",
        ephemeral=True
    )


@bot.tree.command(name="대표캐릭등록")
async def 대표캐릭등록(
    interaction: discord.Interaction,
    캐릭터명: str
):

    await interaction.response.defer(
        ephemeral=True
    )

    siblings = await fetch_lostark_siblings(
        캐릭터명
    )

    if not siblings:
        await interaction.followup.send(
            "캐릭터 조회 실패",
            ephemeral=True
        )
        return

    await interaction.followup.send(
        "등록할 캐릭터 선택",
        view=RosterRegisterView(siblings),
        ephemeral=True
    )


@bot.tree.command(name="내캐릭터")
async def 내캐릭터(interaction: discord.Interaction):

    roster = load_roster(interaction.user.id)

    if not roster:
        await interaction.response.send_message(
            "등록된 캐릭터 없음",
            ephemeral=True
        )
        return

    names = "\n".join([
        f"{c['CharacterName']} / {c.get('CharacterClassName', '직업없음')} / Lv.{get_item_level(c)}"
        for c in roster[:25]
    ])

    await interaction.response.send_message(
        names,
        ephemeral=True
    )


@bot.tree.command(name="캐릭터초기화")
async def 캐릭터초기화(interaction: discord.Interaction):

    clear_roster(interaction.user.id)

    await interaction.response.send_message(
        "캐릭터 초기화 완료",
        ephemeral=True
    )


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
async def on_ready():

    try:
        synced = await bot.tree.sync()
        print(f"슬래시 명령어 동기화 완료: {len(synced)}개")

    except Exception as e:
        print(e)

    print(f"{bot.user} 로그인 완료")


bot.run(TOKEN)
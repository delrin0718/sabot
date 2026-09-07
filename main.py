import os
import json
import asyncio
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands

TOKEN = os.getenv("TOKEN")
LOSTARK_API_KEY = os.getenv("LOSTARK_API_KEY")
KST = ZoneInfo("Asia/Seoul")

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
recruitments = {}
recruitment_task_handles = {}

DB_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".") + "/lostark_bot.db"
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# =========================
# DB
# =========================
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
    schedule_channel_id INTEGER,
    schedule_message_id INTEGER,
    suggestion_channel_id INTEGER,
    suggestion_staff_role_id INTEGER,
    suggestion_panel_message_id INTEGER,
    archive_channel_id INTEGER,
    verify_channel_id INTEGER,
    intro_channel_id INTEGER,
    selfrole_channel_id INTEGER,
    member_role_id INTEGER,
    newbie_role_id INTEGER,
    guest_role_id INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS recruitments (
    message_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    data_json TEXT NOT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS suggestion_counters (
    guild_id INTEGER PRIMARY KEY,
    last_number INTEGER NOT NULL DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS suggestions (
    thread_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    suggestion_number INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    message_id INTEGER
)
""")
conn.commit()


def ensure_column(table, column, col_type):
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()


for col in [
    "recruit_channel_id",
    "schedule_channel_id",
    "schedule_message_id",
    "suggestion_channel_id",
    "suggestion_staff_role_id",
    "suggestion_panel_message_id",
    "archive_channel_id",
    "verify_channel_id",
    "intro_channel_id",
    "selfrole_channel_id",
    "member_role_id",
    "newbie_role_id",
    "guest_role_id",
]:
    ensure_column("guild_settings", col, "INTEGER")

# =========================
# Constants
# =========================
RAIDS = [
    "1막 : 에기르",
    "2막 : 아브렐슈드",
    "3막 : 모르둠",
    "종막 : 카제로스",
    "세르카",
    "지평의 성당",
    "벨가르딘",
]

DIFFICULTIES = ["노말", "하드", "나이트메어"]
SKILLS = ["트라이", "클경", "반숙", "숙련"]
PARTY_LIMITS = {
    4: {"dealer": 3, "support": 1},
    8: {"dealer": 6, "support": 2},
}

POSITION_ROLES = ["⚔️ 딜러", "🎵 서포터"]
TIME_ROLES = ["🌙 밤반", "☀️ 낮반", "🌌 새벽반"]
VOICE_ROLES = ["🎤 음성 가능", "🔇 듣코", "🚫 음성·듣코 불가"]
STYLE_ROLES = ["🐣 트라이 선호", "🔥 숙련 선호", "🤝 숙련도 상관없음"]

# =========================
# Generic helpers
# =========================
def set_guild_value(guild_id, key, value):
    cursor.execute(
        f"""
        INSERT INTO guild_settings (guild_id, {key})
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET {key}=excluded.{key}
        """,
        (guild_id, value),
    )
    conn.commit()


def get_guild_value(guild_id, key):
    cursor.execute(f"SELECT {key} FROM guild_settings WHERE guild_id = ?", (guild_id,))
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
    by_name = {c["CharacterName"]: c for c in existing}
    for char in new_data:
        by_name[char["CharacterName"]] = char

    merged = list(by_name.values())
    cursor.execute(
        "REPLACE INTO rosters (user_id, roster_data) VALUES (?, ?)",
        (user_id, json.dumps(merged, ensure_ascii=False)),
    )
    conn.commit()


def clear_roster(user_id):
    cursor.execute("DELETE FROM rosters WHERE user_id = ?", (user_id,))
    conn.commit()


async def fetch_lostark_siblings(character_name):
    url = f"https://developer-lostark.game.onstove.com/characters/{character_name}/siblings"
    headers = {"accept": "application/json", "authorization": LOSTARK_API_KEY}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                print("LostArk API status:", response.status)
                print(await response.text())
                return None
            return await response.json()


async def fetch_character_profile(character_name):
    """
    캐릭터 상세 프로필 조회.
    Lost Ark Open API의 CombatPower 값을 사용합니다.
    """
    url = f"https://developer-lostark.game.onstove.com/armories/characters/{character_name}/profiles"
    headers = {"accept": "application/json", "authorization": LOSTARK_API_KEY}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    print(f"프로필 조회 실패 {character_name}: {response.status}")
                    return None
                return await response.json()
    except Exception as e:
        print(f"프로필 조회 오류 {character_name}: {e}")
        return None


def format_combat_power(value):
    if value is None or value == "":
        return "조회 불가"

    try:
        # API에서 숫자 또는 문자열 형태로 올 수 있는 경우 모두 처리
        number = float(str(value).replace(",", ""))
        return f"{int(number):,}"
    except (ValueError, TypeError):
        return str(value)


def find_role(guild, role_name):
    return discord.utils.get(guild.roles, name=role_name)


async def set_single_role(member, role_name, group):
    add_role = find_role(member.guild, role_name)
    if not add_role:
        return False, f"`{role_name}` 역할을 찾을 수 없습니다."

    remove_roles = [
        role
        for name in group
        if (role := find_role(member.guild, name))
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

# =========================
# Recruitment persistence
# =========================
def serialize_recruitment(data):
    copy = dict(data)
    copy["start_time"] = data["start_time"].isoformat()
    return json.dumps(copy, ensure_ascii=False)


def deserialize_recruitment(raw):
    data = json.loads(raw)
    data["start_time"] = datetime.fromisoformat(data["start_time"])
    if data["start_time"].tzinfo is None:
        data["start_time"] = data["start_time"].replace(tzinfo=KST)
    return data


def save_recruitment(message_id, data):
    recruitments[message_id] = data
    cursor.execute(
        """
        INSERT INTO recruitments (message_id, guild_id, channel_id, data_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            guild_id=excluded.guild_id,
            channel_id=excluded.channel_id,
            data_json=excluded.data_json
        """,
        (message_id, data["guild_id"], data["channel_id"], serialize_recruitment(data)),
    )
    conn.commit()


def load_recruitments_from_db():
    cursor.execute("SELECT message_id, data_json FROM recruitments")
    result = {}
    for message_id, raw in cursor.fetchall():
        try:
            result[message_id] = deserialize_recruitment(raw)
        except Exception as e:
            print(f"모집 데이터 로드 실패 {message_id}: {e}")
    return result


def delete_recruitment(message_id):
    recruitments.pop(message_id, None)
    cursor.execute("DELETE FROM recruitments WHERE message_id = ?", (message_id,))
    conn.commit()


def recruitment_is_full(data):
    return (
        len(data["dealer"]) >= data["max_dealer"]
        and len(data["support"]) >= data["max_support"]
    )


def recruitment_status(data):
    if data.get("closed") or datetime.now(KST) >= data["start_time"]:
        return "completed"
    if recruitment_is_full(data):
        return "scheduled"
    return "recruiting"


def status_label(data):
    status = recruitment_status(data)
    if status == "completed":
        return "⚫ 완료"
    if status == "scheduled":
        return "🟡 예정"
    return "🟢 모집중"


def status_color(data):
    status = recruitment_status(data)
    if status == "completed":
        return discord.Color.dark_gray()
    if status == "scheduled":
        return discord.Color.gold()
    return discord.Color.green()


def make_member_text(members):
    if not members:
        return "-"
    return "\n".join(
        f"• **{m['character']}** · {m.get('class_name', '직업없음')} · Lv.{m.get('item_level', '레벨없음')} <@{m['user_id']}>"
        for m in members
    )


def make_recruit_embed(data):
    total = len(data["dealer"]) + len(data["support"])
    max_total = data["max_dealer"] + data["max_support"]
    remaining = max_total - total

    if recruitment_status(data) == "completed":
        info_line = "이 모집은 종료되었습니다."
    elif recruitment_is_full(data):
        info_line = "✅ 모집 인원이 모두 모였습니다."
    else:
        info_line = f"출발까지 **{remaining}자리** 남았습니다."

    embed = discord.Embed(
        title=f"⚔️ {data['raid']} · {data['difficulty']}",
        description=(
            f"**{status_label(data)}**\n\n"
            f"🕘 **{data['start_time'].strftime('%m월 %d일 (%a) %H:%M')}**\n"
            f"🎯 **{data['skill']}**\n"
            f"👑 모집자 : <@{data['creator_id']}>\n\n"
            f"👥 참가 인원 **{total} / {max_total}**\n"
            f"🗡️ 딜러 **{len(data['dealer'])} / {data['max_dealer']}**\n"
            f"🎵 서포터 **{len(data['support'])} / {data['max_support']}**\n\n"
            f"{info_line}"
        ),
        color=status_color(data),
    )
    embed.set_footer(text="사뭇 레이드 모집")
    return embed


async def update_recruit_message(message_id):
    data = recruitments.get(message_id)
    if not data:
        return
    channel = bot.get_channel(data["channel_id"])
    if not channel:
        return
    try:
        msg = await channel.fetch_message(message_id)
        view = None if recruitment_status(data) == "completed" else RecruitView(message_id)
        await msg.edit(embed=make_recruit_embed(data), view=view)
    except discord.NotFound:
        pass


def user_is_recruit_manager(interaction, data):
    if interaction.user.id == data["creator_id"]:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


async def archive_recruitment(data):
    archive_channel_id = get_guild_value(data["guild_id"], "archive_channel_id")
    if not archive_channel_id:
        return
    channel = bot.get_channel(archive_channel_id)
    if channel:
        await channel.send(embed=make_recruit_embed(data))


async def remove_recruitment_message(message_id, data):
    """
    완료된 모집은 기록 채널에 보관한 뒤 모집 채널의 원본 메시지를 삭제합니다.
    """
    channel = bot.get_channel(data["channel_id"])
    if not channel:
        return
    try:
        msg = await channel.fetch_message(message_id)
        await msg.delete()
    except discord.NotFound:
        pass
    except discord.Forbidden:
        print(f"모집 메시지 삭제 권한 없음: {message_id}")


async def finalize_recruitment(message_id, data):
    """
    모집 완료 공통 처리:
    1) 완료 상태 저장
    2) 기록 채널에 아카이브
    3) 모집 채널 원본 삭제
    4) 이번 주 일정 갱신
    5) DB에서 모집 제거
    """
    data["closed"] = True
    save_recruitment(message_id, data)
    await archive_recruitment(data)
    await remove_recruitment_message(message_id, data)
    await update_weekly_schedule(data["guild_id"])
    delete_recruitment(message_id)


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

    if datetime.now(KST) >= data["start_time"]:
        return

    members = data["dealer"] + data["support"]
    if not members:
        return

    mentions = " ".join(f"<@{m['user_id']}>" for m in members)
    channel = bot.get_channel(data["channel_id"])
    if channel:
        await channel.send(
            f"🔔 {mentions}\n"
            f"**{data['raid']} {data['difficulty']}** 출발까지 10분 남았습니다!"
        )


async def close_recruitment_task(message_id):
    data = recruitments.get(message_id)
    if not data:
        return

    wait_seconds = (data["start_time"] - datetime.now(KST)).total_seconds()
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)

    data = recruitments.get(message_id)
    if not data or data.get("closed"):
        return

    await finalize_recruitment(message_id, data)


def cancel_recruitment_tasks(message_id):
    tasks = recruitment_task_handles.pop(message_id, [])
    for task in tasks:
        if not task.done():
            task.cancel()


def start_recruitment_tasks(message_id):
    data = recruitments.get(message_id)
    if not data or data.get("closed"):
        return
    cancel_recruitment_tasks(message_id)
    recruitment_task_handles[message_id] = [
        asyncio.create_task(reminder_task(message_id)),
        asyncio.create_task(close_recruitment_task(message_id)),
    ]

# =========================
# Date/time modal
# =========================
class DateTimeModal(discord.ui.Modal, title="출발 날짜/시간 입력"):
    date = discord.ui.TextInput(label="날짜", placeholder="예: 2026-09-05", required=True)
    time = discord.ui.TextInput(label="시간", placeholder="예: 21:00", required=True)

    def __init__(self, target_view, edit_message_id=None):
        super().__init__()
        self.target_view = target_view
        self.edit_message_id = edit_message_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            dt = datetime.strptime(
                f"{self.date.value} {self.time.value}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=KST)
        except ValueError:
            await interaction.response.send_message(
                "날짜/시간 형식이 올바르지 않습니다. 예: `2026-09-05`, `21:00`",
                ephemeral=True,
            )
            return

        if dt <= datetime.now(KST):
            await interaction.response.send_message("현재보다 이후 시간을 입력해주세요.", ephemeral=True)
            return

        if self.edit_message_id:
            data = recruitments.get(self.edit_message_id)
            if not data:
                await interaction.response.send_message("모집 정보를 찾을 수 없습니다.", ephemeral=True)
                return
            data["start_time"] = dt
            save_recruitment(self.edit_message_id, data)
            start_recruitment_tasks(self.edit_message_id)
            await update_recruit_message(self.edit_message_id)
            await update_weekly_schedule(data["guild_id"])
            await interaction.response.send_message(
                f"✅ 출발 시간을 `{dt.strftime('%Y-%m-%d %H:%M')}`으로 변경했습니다.",
                ephemeral=True,
            )
            return

        self.target_view.start_time = dt
        await interaction.response.send_message(
            f"✅ 출발 시간: `{dt.strftime('%Y-%m-%d %H:%M')}`\n이제 **모집 만들기**를 눌러주세요.",
            ephemeral=True,
        )

# =========================
# Verification / roles
# =========================
class VerifyModal(discord.ui.Modal, title="사뭇 길드원 인증 신청"):
    nickname = discord.ui.TextInput(label="닉네임", placeholder="예: 하도앵", required=True, max_length=30)
    main_class = discord.ui.TextInput(label="본캐 직업", placeholder="예: 바드", required=True, max_length=30)
    item_level = discord.ui.TextInput(label="템렙", placeholder="예: 1710", required=True, max_length=20)
    active_time = discord.ui.TextInput(label="주 활동 시간", placeholder="예: 평일 저녁 ~ 새벽", required=True, max_length=50)
    comment = discord.ui.TextInput(
        label="한마디",
        placeholder="예: 오래 재밌게 같이 하고 싶어요!",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user

        member_role_id = get_guild_value(guild.id, "member_role_id")
        newbie_role_id = get_guild_value(guild.id, "newbie_role_id")
        guest_role_id = get_guild_value(guild.id, "guest_role_id")
        intro_channel_id = get_guild_value(guild.id, "intro_channel_id")
        selfrole_channel_id = get_guild_value(guild.id, "selfrole_channel_id")

        member_role = guild.get_role(member_role_id) if member_role_id else None
        newbie_role = guild.get_role(newbie_role_id) if newbie_role_id else None
        guest_role = guild.get_role(guest_role_id) if guest_role_id else None
        intro_channel = guild.get_channel(intro_channel_id) if intro_channel_id else None
        selfrole_channel = guild.get_channel(selfrole_channel_id) if selfrole_channel_id else None

        if not member_role:
            await interaction.response.send_message(
                "길드원 역할이 설정되지 않았습니다. 관리자에게 문의해주세요.", ephemeral=True
            )
            return

        await member.add_roles(member_role)
        remove_roles = []
        if newbie_role and newbie_role in member.roles:
            remove_roles.append(newbie_role)
        if guest_role and guest_role in member.roles:
            remove_roles.append(guest_role)
        if remove_roles:
            await member.remove_roles(*remove_roles)

        embed = discord.Embed(title="✨ 새로운 길드원이 합류했습니다!", color=discord.Color.gold())
        embed.add_field(name="닉네임", value=self.nickname.value, inline=True)
        embed.add_field(name="본캐 직업", value=self.main_class.value, inline=True)
        embed.add_field(name="템렙", value=self.item_level.value, inline=True)
        embed.add_field(name="주 활동 시간", value=self.active_time.value, inline=False)
        embed.add_field(name="한마디", value=self.comment.value, inline=False)
        embed.set_footer(text="🎉 모두 따뜻하게 환영해주세요!")

        if intro_channel:
            await intro_channel.send(content=f"{member.mention} 님이 길드원 인증을 완료했습니다!", embed=embed)

        msg = "✅ 길드원 인증이 완료되었습니다!\n길드원 역할이 지급되었고, 신입소개 채널에 자기소개가 등록되었습니다."
        if selfrole_channel:
            msg += f"\n\n🎭 다음으로 {selfrole_channel.mention} 에서 본인에게 맞는 셀프 역할을 선택해주세요."
        await interaction.response.send_message(msg, ephemeral=True)


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🌱 길드원 인증", style=discord.ButtonStyle.success, custom_id="guild_member_verify_button")
    async def guild_member_verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerifyModal())

    @discord.ui.button(label="🎫 손님 입장", style=discord.ButtonStyle.secondary, custom_id="guest_join_button")
    async def guest_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user
        guest_role_id = get_guild_value(guild.id, "guest_role_id")
        newbie_role_id = get_guild_value(guild.id, "newbie_role_id")
        guest_role = guild.get_role(guest_role_id) if guest_role_id else None
        newbie_role = guild.get_role(newbie_role_id) if newbie_role_id else None

        if not guest_role:
            await interaction.response.send_message(
                "손님 역할이 설정되지 않았습니다. 관리자에게 문의해주세요.", ephemeral=True
            )
            return

        await member.add_roles(guest_role)
        if newbie_role and newbie_role in member.roles:
            await member.remove_roles(newbie_role)
        await interaction.response.send_message(
            f"🎫 손님 입장이 완료되었습니다!\n{guest_role.mention} 역할이 지급되었습니다.", ephemeral=True
        )


class SelfRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚔️ 딜러", style=discord.ButtonStyle.primary, custom_id="role_dealer")
    async def role_dealer(self, interaction, button):
        _, msg = await set_single_role(interaction.user, "⚔️ 딜러", POSITION_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🎵 서포터", style=discord.ButtonStyle.primary, custom_id="role_support")
    async def role_support(self, interaction, button):
        _, msg = await set_single_role(interaction.user, "🎵 서포터", POSITION_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🌙 밤반", style=discord.ButtonStyle.secondary, custom_id="role_night")
    async def role_night(self, interaction, button):
        _, msg = await toggle_role(interaction.user, "🌙 밤반")
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="☀️ 낮반", style=discord.ButtonStyle.secondary, custom_id="role_day")
    async def role_day(self, interaction, button):
        _, msg = await toggle_role(interaction.user, "☀️ 낮반")
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🌌 새벽반", style=discord.ButtonStyle.secondary, custom_id="role_dawn")
    async def role_dawn(self, interaction, button):
        _, msg = await toggle_role(interaction.user, "🌌 새벽반")
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🎤 음성 가능", style=discord.ButtonStyle.success, custom_id="role_voice")
    async def role_voice(self, interaction, button):
        _, msg = await set_single_role(interaction.user, "🎤 음성 가능", VOICE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🔇 듣코", style=discord.ButtonStyle.success, custom_id="role_listen")
    async def role_listen(self, interaction, button):
        _, msg = await set_single_role(interaction.user, "🔇 듣코", VOICE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🚫 음성·듣코 불가", style=discord.ButtonStyle.success, custom_id="role_no_voice")
    async def role_no_voice(self, interaction, button):
        _, msg = await set_single_role(interaction.user, "🚫 음성·듣코 불가", VOICE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🐣 트라이 선호", style=discord.ButtonStyle.danger, custom_id="role_try")
    async def role_try(self, interaction, button):
        _, msg = await set_single_role(interaction.user, "🐣 트라이 선호", STYLE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🔥 숙련 선호", style=discord.ButtonStyle.danger, custom_id="role_exp")
    async def role_exp(self, interaction, button):
        _, msg = await set_single_role(interaction.user, "🔥 숙련 선호", STYLE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="🤝 숙련도 상관없음", style=discord.ButtonStyle.danger, custom_id="role_any")
    async def role_any(self, interaction, button):
        _, msg = await set_single_role(interaction.user, "🤝 숙련도 상관없음", STYLE_ROLES)
        await interaction.response.send_message(msg, ephemeral=True)

# =========================
# Roster registration
# =========================
class RosterRegisterSelect(discord.ui.Select):
    def __init__(self, characters):
        self.characters = characters[:25]
        options = [
            discord.SelectOption(
                label=c["CharacterName"],
                description=f"{c.get('CharacterClassName', '직업없음')} / Lv.{get_item_level(c)}",
            )
            for c in self.characters
        ]
        super().__init__(placeholder="등록할 캐릭터 선택", min_values=1, max_values=len(options), options=options)

    async def callback(self, interaction: discord.Interaction):
        selected = [c for c in self.characters if c["CharacterName"] in self.values]
        save_roster(interaction.user.id, selected)
        await interaction.response.send_message("✅ 캐릭터 등록 완료!", ephemeral=True)


class RosterRegisterView(discord.ui.View):
    def __init__(self, characters):
        super().__init__(timeout=180)
        self.add_item(RosterRegisterSelect(characters))

# =========================
# Recruitment creation UI
# =========================
class RaidSelect(discord.ui.Select):
    def __init__(self, setup_view):
        self.setup_view = setup_view
        super().__init__(
            placeholder="1. 레이드를 선택해주세요",
            options=[discord.SelectOption(label=r, value=r) for r in RAIDS],
            row=0,
        )

    async def callback(self, interaction):
        self.setup_view.raid = self.values[0]
        await interaction.response.defer()


class DifficultySelect(discord.ui.Select):
    def __init__(self, setup_view):
        self.setup_view = setup_view
        super().__init__(
            placeholder="2. 난이도를 선택해주세요",
            options=[discord.SelectOption(label=d, value=d) for d in DIFFICULTIES],
            row=1,
        )

    async def callback(self, interaction):
        self.setup_view.difficulty = self.values[0]
        await interaction.response.defer()


class SkillSelect(discord.ui.Select):
    def __init__(self, setup_view):
        self.setup_view = setup_view
        super().__init__(
            placeholder="3. 숙련도를 선택해주세요",
            options=[discord.SelectOption(label=s, value=s) for s in SKILLS],
            row=2,
        )

    async def callback(self, interaction):
        self.setup_view.skill = self.values[0]
        await interaction.response.defer()


class RaidSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.raid = None
        self.difficulty = None
        self.skill = None
        self.party_size = None
        self.start_time = None
        self.add_item(RaidSelect(self))
        self.add_item(DifficultySelect(self))
        self.add_item(SkillSelect(self))

    @discord.ui.button(label="4인", style=discord.ButtonStyle.secondary, row=3)
    async def party_4(self, interaction, button):
        self.party_size = 4
        await interaction.response.send_message("✅ 모집 인원: 4인", ephemeral=True)

    @discord.ui.button(label="8인", style=discord.ButtonStyle.secondary, row=3)
    async def party_8(self, interaction, button):
        self.party_size = 8
        await interaction.response.send_message("✅ 모집 인원: 8인", ephemeral=True)

    @discord.ui.button(label="🕘 날짜/시간", style=discord.ButtonStyle.secondary, row=4)
    async def set_datetime(self, interaction, button):
        await interaction.response.send_modal(DateTimeModal(self))

    @discord.ui.button(label="✅ 모집 만들기", style=discord.ButtonStyle.primary, row=4)
    async def create_recruitment(self, interaction, button):
        if not all([self.raid, self.difficulty, self.skill, self.party_size, self.start_time]):
            await interaction.response.send_message(
                "레이드 / 난이도 / 숙련도 / 인원 / 날짜·시간을 모두 설정해주세요.", ephemeral=True
            )
            return

        channel_id = get_guild_value(interaction.guild.id, "recruit_channel_id")
        if not channel_id:
            await interaction.response.send_message("먼저 `/모집채널설정`을 해주세요.", ephemeral=True)
            return

        target_channel = interaction.guild.get_channel(channel_id)
        if not target_channel:
            await interaction.response.send_message("설정된 모집 채널을 찾을 수 없습니다.", ephemeral=True)
            return

        limits = PARTY_LIMITS[self.party_size]
        data = {
            "raid": self.raid,
            "difficulty": self.difficulty,
            "skill": self.skill,
            "party_size": self.party_size,
            "start_time": self.start_time,
            "dealer": [],
            "support": [],
            "max_dealer": limits["dealer"],
            "max_support": limits["support"],
            "creator_id": interaction.user.id,
            "creator_name": interaction.user.display_name,
            "channel_id": target_channel.id,
            "guild_id": interaction.guild.id,
            "closed": False,
        }

        msg = await target_channel.send(embed=make_recruit_embed(data))
        save_recruitment(msg.id, data)
        await msg.edit(view=RecruitView(msg.id))
        start_recruitment_tasks(msg.id)
        await update_weekly_schedule(interaction.guild.id)

        await interaction.response.send_message(
            f"✅ {target_channel.mention} 에 모집을 만들었습니다.", ephemeral=True
        )

# =========================
# Recruitment participation
# =========================
class JoinCharacterSelect(discord.ui.Select):
    def __init__(self, message_id, user_id):
        self.message_id = message_id
        self.user_id = user_id
        roster = load_roster(user_id)
        options = [
            discord.SelectOption(
                label=c["CharacterName"],
                description=f"{c.get('CharacterClassName', '직업없음')} / Lv.{get_item_level(c)}",
                value=c["CharacterName"],
            )
            for c in roster[:25]
        ]
        super().__init__(placeholder="참가할 캐릭터를 선택해주세요", options=options)

    async def callback(self, interaction):
        char_name = self.values[0]
        roster = load_roster(self.user_id)
        selected = next((c for c in roster if c["CharacterName"] == char_name), None)
        if not selected:
            await interaction.response.send_message("캐릭터 정보를 찾을 수 없습니다.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f"**{char_name}**으로 참가합니다. 포지션을 선택해주세요.",
            view=PositionSelectView(self.message_id, self.user_id, selected),
        )


class JoinCharacterView(discord.ui.View):
    def __init__(self, message_id, user_id):
        super().__init__(timeout=90)
        self.add_item(JoinCharacterSelect(message_id, user_id))


class PositionSelectView(discord.ui.View):
    def __init__(self, message_id, user_id, character):
        super().__init__(timeout=90)
        self.message_id = message_id
        self.user_id = user_id
        self.character = character

    async def apply_join(self, interaction, role_type):
        data = recruitments.get(self.message_id)
        if not data or recruitment_status(data) == "completed":
            await interaction.response.send_message("이미 종료된 모집입니다.", ephemeral=True)
            return

        already_joined = any(
            m["user_id"] == self.user_id for m in (data["dealer"] + data["support"])
        )
        if already_joined:
            await interaction.response.send_message(
                "이미 이 모집에 참가 중입니다. 변경하려면 먼저 `❌ 취소`를 눌러주세요.", ephemeral=True
            )
            return

        target = data[role_type]
        max_count = data["max_dealer"] if role_type == "dealer" else data["max_support"]
        if len(target) >= max_count:
            role_name = "딜러" if role_type == "dealer" else "서포터"
            await interaction.response.send_message(f"❌ 현재 {role_name} 자리가 모두 찼습니다.", ephemeral=True)
            return

        member_data = {
            "user_id": self.user_id,
            "character": self.character["CharacterName"],
            "class_name": self.character.get("CharacterClassName", "직업없음"),
            "item_level": get_item_level(self.character),
        }
        target.append(member_data)
        save_recruitment(self.message_id, data)
        await update_recruit_message(self.message_id)
        await update_weekly_schedule(data["guild_id"])
        await interaction.response.send_message(
            f"✅ **{member_data['character']}** 참가 신청이 완료되었습니다.", ephemeral=True
        )

    @discord.ui.button(label="🗡️ 딜러", style=discord.ButtonStyle.danger)
    async def dealer(self, interaction, button):
        await self.apply_join(interaction, "dealer")

    @discord.ui.button(label="🎵 서포터", style=discord.ButtonStyle.success)
    async def support(self, interaction, button):
        await self.apply_join(interaction, "support")


async def make_roster_list_embed(data):
    embed = discord.Embed(
        title=f"👥 {data['raid']} {data['difficulty']} 참가 명단",
        color=discord.Color.blurple(),
    )

    async def build_member_text(members):
        if not members:
            return "-"

        lines = []
        for member in members:
            profile = await fetch_character_profile(member["character"])
            combat_power = None
            if profile:
                combat_power = profile.get("CombatPower")

            lines.append(
                f"• **{member['character']}** · {member.get('class_name', '직업없음')}\n"
                f"  Lv.{member.get('item_level', '레벨없음')} · ⚔️ 전투력 **{format_combat_power(combat_power)}**"
                f" · <@{member['user_id']}>"
            )

        return "\n\n".join(lines)

    dealer_text = await build_member_text(data["dealer"])
    support_text = await build_member_text(data["support"])

    embed.add_field(
        name=f"🗡️ 딜러 {len(data['dealer'])}/{data['max_dealer']}",
        value=dealer_text,
        inline=False,
    )
    embed.add_field(
        name=f"🎵 서포터 {len(data['support'])}/{data['max_support']}",
        value=support_text,
        inline=False,
    )
    embed.set_footer(text="전투력은 명단을 열 때 최신 프로필에서 조회합니다.")
    return embed


class ManageView(discord.ui.View):
    def __init__(self, message_id):
        super().__init__(timeout=120)
        self.message_id = message_id

    async def check(self, interaction):
        data = recruitments.get(self.message_id)
        if not data:
            await interaction.response.send_message("모집 정보를 찾을 수 없습니다.", ephemeral=True)
            return None
        if not user_is_recruit_manager(interaction, data):
            await interaction.response.send_message("모집자 또는 관리자만 수정할 수 있습니다.", ephemeral=True)
            return None
        return data

    @discord.ui.button(label="🕘 시간 변경", style=discord.ButtonStyle.secondary)
    async def change_time(self, interaction, button):
        data = await self.check(interaction)
        if data:
            await interaction.response.send_modal(DateTimeModal(None, edit_message_id=self.message_id))

    @discord.ui.button(label="⚙️ 조건 변경", style=discord.ButtonStyle.primary)
    async def change_options(self, interaction, button):
        data = await self.check(interaction)
        if data:
            await interaction.response.send_message(
                "변경할 항목을 선택한 뒤 `✅ 변경 적용`을 눌러주세요.",
                view=RecruitEditView(self.message_id),
                ephemeral=True,
            )

    @discord.ui.button(label="✅ 모집 마감", style=discord.ButtonStyle.success)
    async def close_now(self, interaction, button):
        data = await self.check(interaction)
        if not data:
            return
        cancel_recruitment_tasks(self.message_id)
        await finalize_recruitment(self.message_id, data)
        await interaction.response.send_message(
            "✅ 모집을 마감했습니다. 기록 채널에 보관하고 모집 채널에서는 삭제했습니다.",
            ephemeral=True,
        )

    @discord.ui.button(label="🗑️ 모집 삭제", style=discord.ButtonStyle.danger)
    async def delete_now(self, interaction, button):
        data = await self.check(interaction)
        if not data:
            return
        channel = bot.get_channel(data["channel_id"])
        if channel:
            try:
                msg = await channel.fetch_message(self.message_id)
                await msg.delete()
            except discord.NotFound:
                pass
        cancel_recruitment_tasks(self.message_id)
        guild_id = data["guild_id"]
        delete_recruitment(self.message_id)
        await update_weekly_schedule(guild_id)
        await interaction.response.send_message("🗑️ 모집을 삭제했습니다.", ephemeral=True)


class EditRaidSelect(discord.ui.Select):
    def __init__(self, parent):
        self.parent_view = parent
        super().__init__(placeholder="레이드 변경 (선택)", options=[discord.SelectOption(label=r) for r in RAIDS], row=0)

    async def callback(self, interaction):
        self.parent_view.raid = self.values[0]
        await interaction.response.defer()


class EditDifficultySelect(discord.ui.Select):
    def __init__(self, parent):
        self.parent_view = parent
        super().__init__(placeholder="난이도 변경 (선택)", options=[discord.SelectOption(label=d) for d in DIFFICULTIES], row=1)

    async def callback(self, interaction):
        self.parent_view.difficulty = self.values[0]
        await interaction.response.defer()


class EditSkillSelect(discord.ui.Select):
    def __init__(self, parent):
        self.parent_view = parent
        super().__init__(placeholder="숙련도 변경 (선택)", options=[discord.SelectOption(label=s) for s in SKILLS], row=2)

    async def callback(self, interaction):
        self.parent_view.skill = self.values[0]
        await interaction.response.defer()


class RecruitEditView(discord.ui.View):
    def __init__(self, message_id):
        super().__init__(timeout=120)
        self.message_id = message_id
        self.raid = None
        self.difficulty = None
        self.skill = None
        self.party_size = None
        self.add_item(EditRaidSelect(self))
        self.add_item(EditDifficultySelect(self))
        self.add_item(EditSkillSelect(self))

    @discord.ui.button(label="4인", style=discord.ButtonStyle.secondary, row=3)
    async def set_4(self, interaction, button):
        self.party_size = 4
        await interaction.response.defer()

    @discord.ui.button(label="8인", style=discord.ButtonStyle.secondary, row=3)
    async def set_8(self, interaction, button):
        self.party_size = 8
        await interaction.response.defer()

    @discord.ui.button(label="✅ 변경 적용", style=discord.ButtonStyle.primary, row=4)
    async def apply(self, interaction, button):
        data = recruitments.get(self.message_id)
        if not data:
            await interaction.response.send_message("모집 정보를 찾을 수 없습니다.", ephemeral=True)
            return
        if not user_is_recruit_manager(interaction, data):
            await interaction.response.send_message("모집자 또는 관리자만 수정할 수 있습니다.", ephemeral=True)
            return

        if self.raid:
            data["raid"] = self.raid
        if self.difficulty:
            data["difficulty"] = self.difficulty
        if self.skill:
            data["skill"] = self.skill
        if self.party_size:
            limits = PARTY_LIMITS[self.party_size]
            if len(data["dealer"]) > limits["dealer"] or len(data["support"]) > limits["support"]:
                await interaction.response.send_message(
                    "현재 참가 인원이 변경하려는 인원 제한을 초과합니다. 먼저 참가 인원을 조정해주세요.", ephemeral=True
                )
                return
            data["party_size"] = self.party_size
            data["max_dealer"] = limits["dealer"]
            data["max_support"] = limits["support"]

        save_recruitment(self.message_id, data)
        await update_recruit_message(self.message_id)
        await update_weekly_schedule(data["guild_id"])
        await interaction.response.send_message("✅ 모집 정보를 변경했습니다.", ephemeral=True)


class RecruitView(discord.ui.View):
    def __init__(self, message_id):
        super().__init__(timeout=None)
        self.message_id = message_id

        self.join_button.custom_id = f"recruit_join:{message_id}"
        self.cancel_button.custom_id = f"recruit_cancel:{message_id}"
        self.list_button.custom_id = f"recruit_list:{message_id}"
        self.manage_button.custom_id = f"recruit_manage:{message_id}"

    @discord.ui.button(label="🙋 참가", style=discord.ButtonStyle.primary, custom_id="recruit_join")
    async def join_button(self, interaction, button):
        data = recruitments.get(self.message_id)
        if not data or recruitment_status(data) == "completed":
            await interaction.response.send_message("이미 종료된 모집입니다.", ephemeral=True)
            return
        if recruitment_is_full(data):
            await interaction.response.send_message("❌ 현재 모집 인원이 모두 찼습니다.", ephemeral=True)
            return
        if any(m["user_id"] == interaction.user.id for m in data["dealer"] + data["support"]):
            await interaction.response.send_message("이미 이 모집에 참가 중입니다.", ephemeral=True)
            return
        roster = load_roster(interaction.user.id)
        if not roster:
            await interaction.response.send_message("먼저 `/대표캐릭등록`을 해주세요.", ephemeral=True)
            return
        await interaction.response.send_message(
            "참가할 캐릭터를 선택해주세요.",
            view=JoinCharacterView(self.message_id, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(label="❌ 취소", style=discord.ButtonStyle.secondary, custom_id="recruit_cancel")
    async def cancel_button(self, interaction, button):
        data = recruitments.get(self.message_id)
        if not data or recruitment_status(data) == "completed":
            await interaction.response.send_message("이미 종료된 모집입니다.", ephemeral=True)
            return
        before = len(data["dealer"]) + len(data["support"])
        data["dealer"] = [m for m in data["dealer"] if m["user_id"] != interaction.user.id]
        data["support"] = [m for m in data["support"] if m["user_id"] != interaction.user.id]
        after = len(data["dealer"]) + len(data["support"])
        if before == after:
            await interaction.response.send_message("현재 이 모집에 참가하고 있지 않습니다.", ephemeral=True)
            return
        save_recruitment(self.message_id, data)
        await update_recruit_message(self.message_id)
        await update_weekly_schedule(data["guild_id"])
        await interaction.response.send_message("✅ 참가를 취소했습니다.", ephemeral=True)

    @discord.ui.button(label="👀 명단", style=discord.ButtonStyle.secondary, custom_id="recruit_list")
    async def list_button(self, interaction, button):
        data = recruitments.get(self.message_id)
        if not data:
            await interaction.response.send_message("모집 정보를 찾을 수 없습니다.", ephemeral=True)
            return

        # 최대 8명의 최신 전투력을 API에서 조회할 수 있어 응답을 먼저 defer합니다.
        await interaction.response.defer(ephemeral=True)
        embed = await make_roster_list_embed(data)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="⚙️ 모집 관리", style=discord.ButtonStyle.secondary, custom_id="recruit_manage")
    async def manage_button(self, interaction, button):
        data = recruitments.get(self.message_id)
        if not data:
            await interaction.response.send_message("모집 정보를 찾을 수 없습니다.", ephemeral=True)
            return
        if not user_is_recruit_manager(interaction, data):
            await interaction.response.send_message("모집자 또는 관리자만 수정할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"⚙️ **{data['raid']} {data['difficulty']} 모집 관리**",
            view=ManageView(self.message_id),
            ephemeral=True,
        )

# =========================
# Recruitment board panel
# =========================
def weekly_range(now):
    """
    로스트아크 주간 기준:
    수요일 00:00 ~ 다음 주 수요일 00:00 미만
    화면에는 수요일 ~ 화요일 날짜로 표시합니다.
    """
    now = now.astimezone(KST)
    days_since_wednesday = (now.weekday() - 2) % 7
    start_date = now.date() - timedelta(days=days_since_wednesday)
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=KST)
    end = start + timedelta(days=7)
    return start, end


def weekly_label(now=None):
    now = now or datetime.now(KST)
    start, _ = weekly_range(now)
    last_day = start + timedelta(days=6)
    return f"{start.strftime('%m/%d')} ~ {last_day.strftime('%m/%d')}"


def schedule_lines(items, limit=25):
    if not items:
        return "등록된 일정이 없습니다."

    lines = []
    weekday_ko = ["월", "화", "수", "목", "금", "토", "일"]

    for message_id, data in sorted(items, key=lambda x: x[1]["start_time"]):
        total = len(data["dealer"]) + len(data["support"])
        max_total = data["max_dealer"] + data["max_support"]
        dt = data["start_time"].astimezone(KST)
        day = weekday_ko[dt.weekday()]
        jump_url = (
            f"https://discord.com/channels/{data['guild_id']}/"
            f"{data['channel_id']}/{message_id}"
        )
        lines.append(
            f"{status_label(data)} **{dt.strftime('%m/%d')} ({day}) {dt.strftime('%H:%M')}**\n"
            f"└ [{data['raid']} · {data['difficulty']}]({jump_url})"
            f" · {data['skill']} · 👥 {total}/{max_total}"
        )

    if len(lines) > limit:
        lines = lines[:limit] + [f"… 외 {len(items) - limit}개"]

    return "\n\n".join(lines)


def get_weekly_items(guild_id):
    now = datetime.now(KST)
    start, end = weekly_range(now)
    return [
        (mid, data)
        for mid, data in recruitments.items()
        if data["guild_id"] == guild_id
        and start <= data["start_time"].astimezone(KST) < end
        and recruitment_status(data) != "completed"
    ]


def make_weekly_schedule_embed(guild_id):
    embed = discord.Embed(
        title="📅 이번 주 레이드 일정",
        description=(
            f"**{weekly_label()}**\n\n"
            f"{schedule_lines(get_weekly_items(guild_id))}\n\n"
            "⚔️ 레이드 모집 채널에서 새 모집을 만들어보세요."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="모집 생성 · 수정 · 참가 · 취소 시 자동 갱신됩니다.")
    return embed


async def update_weekly_schedule(guild_id):
    channel_id = get_guild_value(guild_id, "schedule_channel_id")
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    message_id = get_guild_value(guild_id, "schedule_message_id")
    embed = make_weekly_schedule_embed(guild_id)

    if message_id:
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
            return
        except (discord.NotFound, discord.Forbidden):
            pass

    message = await channel.send(embed=embed)
    set_guild_value(guild_id, "schedule_message_id", message.id)


@tasks.loop(minutes=10)
async def weekly_schedule_refresh_loop():
    cursor.execute(
        "SELECT guild_id FROM guild_settings WHERE schedule_channel_id IS NOT NULL"
    )
    guild_ids = [row[0] for row in cursor.fetchall()]
    for guild_id in guild_ids:
        try:
            await update_weekly_schedule(guild_id)
        except Exception as e:
            print(f"주간 일정 자동 갱신 실패 ({guild_id}): {e}")


@weekly_schedule_refresh_loop.before_loop
async def before_weekly_schedule_refresh_loop():
    await bot.wait_until_ready()


class RecruitmentBoardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ 새 모집 만들기", style=discord.ButtonStyle.primary, custom_id="board_create")
    async def create(self, interaction, button):
        embed = discord.Embed(
            title="➕ 새 레이드 모집",
            description=(
                "아래에서 **레이드 → 난이도 → 숙련도 → 인원 → 날짜/시간** 순서로 설정해주세요.\n\n"
                "모든 길드원이 자유롭게 모집을 만들 수 있습니다."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=RaidSetupView(), ephemeral=True)

    @discord.ui.button(label="📅 이번 주 일정", style=discord.ButtonStyle.secondary, custom_id="board_week")
    async def week(self, interaction, button):
        embed = make_weekly_schedule_embed(interaction.guild.id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="👤 내 일정", style=discord.ButtonStyle.secondary, custom_id="board_mine")
    async def mine(self, interaction, button):
        items = []
        for mid, data in recruitments.items():
            if data["guild_id"] != interaction.guild.id or recruitment_status(data) == "completed":
                continue
            if any(m["user_id"] == interaction.user.id for m in data["dealer"] + data["support"]):
                items.append((mid, data))
        embed = discord.Embed(title="👤 내 레이드 일정", description=schedule_lines(items), color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="📦 완료된 모집", style=discord.ButtonStyle.secondary, custom_id="board_done")
    async def done(self, interaction, button):
        items = [
            (mid, data)
            for mid, data in recruitments.items()
            if data["guild_id"] == interaction.guild.id and recruitment_status(data) == "completed"
        ]
        items = sorted(items, key=lambda x: x[1]["start_time"], reverse=True)[:15]
        embed = discord.Embed(title="📦 완료된 모집", description=schedule_lines(items), color=discord.Color.dark_gray())
        await interaction.response.send_message(embed=embed, ephemeral=True)


# =========================
# Suggestions
# =========================
def next_suggestion_number(guild_id):
    """
    서버별 건의 번호를 SQLite에 저장합니다.
    재시작/재배포 후에도 번호가 이어집니다.
    """
    cursor.execute(
        "INSERT OR IGNORE INTO suggestion_counters (guild_id, last_number) VALUES (?, 0)",
        (guild_id,),
    )
    cursor.execute(
        "UPDATE suggestion_counters SET last_number = last_number + 1 WHERE guild_id = ?",
        (guild_id,),
    )
    cursor.execute(
        "SELECT last_number FROM suggestion_counters WHERE guild_id = ?",
        (guild_id,),
    )
    number = cursor.fetchone()[0]
    conn.commit()
    return number


def save_suggestion(thread_id, guild_id, number, author_id, title, content, message_id=None):
    cursor.execute(
        """
        INSERT INTO suggestions (
            thread_id, guild_id, suggestion_number, author_id,
            title, content, status, message_id
        )
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            guild_id=excluded.guild_id,
            suggestion_number=excluded.suggestion_number,
            author_id=excluded.author_id,
            title=excluded.title,
            content=excluded.content,
            message_id=excluded.message_id
        """,
        (thread_id, guild_id, number, author_id, title, content, message_id),
    )
    conn.commit()


def set_suggestion_message_id(thread_id, message_id):
    cursor.execute(
        "UPDATE suggestions SET message_id = ? WHERE thread_id = ?",
        (message_id, thread_id),
    )
    conn.commit()


def mark_suggestion_completed(thread_id):
    cursor.execute(
        "UPDATE suggestions SET status = 'completed' WHERE thread_id = ?",
        (thread_id,),
    )
    conn.commit()


def get_suggestion(thread_id):
    cursor.execute(
        """
        SELECT guild_id, suggestion_number, author_id, title, content, status, message_id
        FROM suggestions
        WHERE thread_id = ?
        """,
        (thread_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    return {
        "guild_id": row[0],
        "number": row[1],
        "author_id": row[2],
        "title": row[3],
        "content": row[4],
        "status": row[5],
        "message_id": row[6],
    }


def get_open_suggestions():
    cursor.execute(
        """
        SELECT thread_id, message_id
        FROM suggestions
        WHERE status = 'open' AND message_id IS NOT NULL
        """
    )
    return cursor.fetchall()


def make_suggestion_embed(data, completed=False):
    status = "✅ 처리완료" if completed else "🟡 확인 대기"
    color = discord.Color.green() if completed else discord.Color.gold()

    embed = discord.Embed(
        title=f"💌 사뭇 건의사항 #{data['number']:03d}",
        color=color,
    )
    embed.add_field(name="작성자", value=f"<@{data['author_id']}>", inline=True)
    embed.add_field(name="상태", value=status, inline=True)
    embed.add_field(name="제목", value=data["title"], inline=False)
    embed.add_field(name="건의 내용", value=data["content"], inline=False)

    if completed:
        embed.set_footer(text="이 건의는 처리 완료되어 잠금·보관되었습니다.")
    else:
        embed.set_footer(text="작성자와 운영진만 확인할 수 있는 비공개 건의입니다.")

    return embed


class SuggestionCompleteView(discord.ui.View):
    def __init__(self, thread_id):
        super().__init__(timeout=None)
        self.thread_id = thread_id
        self.complete_button.custom_id = f"suggestion_complete:{thread_id}"

    @discord.ui.button(
        label="✅ 처리완료",
        style=discord.ButtonStyle.success,
        custom_id="suggestion_complete",
    )
    async def complete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = get_suggestion(self.thread_id)
        if not data:
            await interaction.response.send_message(
                "건의 정보를 찾을 수 없습니다.",
                ephemeral=True,
            )
            return

        if data["status"] == "completed":
            await interaction.response.send_message(
                "이미 처리 완료된 건의입니다.",
                ephemeral=True,
            )
            return

        staff_role_id = get_guild_value(interaction.guild.id, "suggestion_staff_role_id")
        staff_role = interaction.guild.get_role(staff_role_id) if staff_role_id else None

        is_staff = (
            interaction.user.guild_permissions.administrator
            or (staff_role is not None and staff_role in interaction.user.roles)
        )

        if not is_staff:
            await interaction.response.send_message(
                "운영진만 건의를 처리 완료할 수 있습니다.",
                ephemeral=True,
            )
            return

        mark_suggestion_completed(self.thread_id)
        completed_data = get_suggestion(self.thread_id)

        await interaction.response.edit_message(
            embed=make_suggestion_embed(completed_data, completed=True),
            view=None,
        )

        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            try:
                await thread.edit(locked=True, archived=True)
            except discord.Forbidden:
                print(
                    f"건의 스레드 잠금/보관 실패 #{completed_data['number']:03d}: "
                    "봇의 스레드 관리 권한을 확인해주세요."
                )


class SuggestionModal(discord.ui.Modal, title="💌 사뭇 건의사항 작성"):
    suggestion_title = discord.ui.TextInput(
        label="제목",
        placeholder="예: 레이드 운영 관련 건의",
        required=True,
        max_length=100,
    )
    suggestion_content = discord.ui.TextInput(
        label="내용",
        placeholder="건의 내용을 자유롭게 작성해주세요.",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=1500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel_id = get_guild_value(guild.id, "suggestion_channel_id")
        staff_role_id = get_guild_value(guild.id, "suggestion_staff_role_id")

        channel = guild.get_channel(channel_id) if channel_id else None
        staff_role = guild.get_role(staff_role_id) if staff_role_id else None

        if not channel or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "건의 채널이 설정되지 않았습니다. 관리자에게 문의해주세요.",
                ephemeral=True,
            )
            return

        if not staff_role:
            await interaction.response.send_message(
                "건의 운영진 역할이 설정되지 않았습니다. 관리자에게 문의해주세요.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        number = next_suggestion_number(guild.id)
        thread_name = f"건의-{number:03d}"

        try:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=1440,
                reason=f"사뭇 건의사항 #{number:03d}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "비공개 건의 스레드를 만들 수 없습니다.\n"
                "봇에 **비공개 스레드 만들기 / 스레드 관리** 권한이 있는지 확인해주세요.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            print(f"건의 스레드 생성 실패: {e}")
            await interaction.followup.send(
                "건의 스레드 생성 중 오류가 발생했습니다.",
                ephemeral=True,
            )
            return

        # 작성자 초대
        try:
            await thread.add_user(interaction.user)
        except discord.HTTPException as e:
            print(f"건의 작성자 스레드 초대 실패: {e}")

        # 운영진 역할 보유자를 개별 초대
        for member in staff_role.members:
            if member.bot:
                continue
            try:
                await thread.add_user(member)
            except discord.HTTPException as e:
                print(f"운영진 스레드 초대 실패 {member.id}: {e}")

        data = {
            "guild_id": guild.id,
            "number": number,
            "author_id": interaction.user.id,
            "title": str(self.suggestion_title.value),
            "content": str(self.suggestion_content.value),
            "status": "open",
            "message_id": None,
        }

        # 먼저 저장해 둬야 버튼 동작 시 바로 조회할 수 있습니다.
        save_suggestion(
            thread.id,
            guild.id,
            number,
            interaction.user.id,
            data["title"],
            data["content"],
        )

        message = await thread.send(
            content=f"{interaction.user.mention} {staff_role.mention}",
            embed=make_suggestion_embed(data),
            view=SuggestionCompleteView(thread.id),
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=True,
                everyone=False,
            ),
        )
        set_suggestion_message_id(thread.id, message.id)

        await interaction.followup.send(
            f"✅ 건의사항 **#{number:03d}**이 등록되었습니다.\n"
            f"{thread.mention} 에서 운영진과 대화하실 수 있습니다.",
            ephemeral=True,
        )


class SuggestionPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="💌 건의 작성하기",
        style=discord.ButtonStyle.primary,
        custom_id="suggestion_create",
    )
    async def create_suggestion(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = get_guild_value(interaction.guild.id, "suggestion_channel_id")
        staff_role_id = get_guild_value(interaction.guild.id, "suggestion_staff_role_id")

        if not channel_id:
            await interaction.response.send_message(
                "건의 채널이 아직 설정되지 않았습니다.",
                ephemeral=True,
            )
            return

        if not staff_role_id:
            await interaction.response.send_message(
                "건의 운영진 역할이 아직 설정되지 않았습니다.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(SuggestionModal())


def make_suggestion_panel_embed():
    embed = discord.Embed(
        title="💌 사뭇 건의함",
        description=(
            "길드 운영이나 디스코드 이용 중 건의하고 싶은 내용을 자유롭게 남겨주세요.\n\n"
            "**작성한 건의는 작성자와 운영진만 확인할 수 있습니다.**\n"
            "아래 버튼을 눌러 제목과 내용을 작성해주세요."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="건의사항은 접수 순서대로 #001, #002 … 번호가 자동 부여됩니다.")
    return embed


# =========================
# Slash commands
# =========================

@bot.tree.command(name="건의채널설정")
@app_commands.checks.has_permissions(administrator=True)
async def 건의채널설정(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_value(interaction.guild.id, "suggestion_channel_id", channel.id)
    set_guild_value(interaction.guild.id, "suggestion_panel_message_id", None)
    await interaction.response.send_message(
        f"✅ 건의 채널 설정 완료: {channel.mention}\n"
        "이 채널에는 길드원들이 볼 수 있는 건의 작성 패널을 두게 됩니다.",
        ephemeral=True,
    )


@bot.tree.command(name="건의운영진역할설정")
@app_commands.checks.has_permissions(administrator=True)
async def 건의운영진역할설정(interaction: discord.Interaction, role: discord.Role):
    set_guild_value(interaction.guild.id, "suggestion_staff_role_id", role.id)
    await interaction.response.send_message(
        f"✅ 건의 운영진 역할 설정 완료: {role.mention}\n"
        "이 역할을 가진 운영진이 비공개 건의를 열람하고 처리할 수 있습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="건의패널생성")
@app_commands.checks.has_permissions(administrator=True)
async def 건의패널생성(interaction: discord.Interaction):
    channel_id = get_guild_value(interaction.guild.id, "suggestion_channel_id")
    staff_role_id = get_guild_value(interaction.guild.id, "suggestion_staff_role_id")

    channel = interaction.guild.get_channel(channel_id) if channel_id else None
    staff_role = interaction.guild.get_role(staff_role_id) if staff_role_id else None

    if not channel or not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "먼저 `/건의채널설정`을 해주세요.",
            ephemeral=True,
        )
        return

    if not staff_role:
        await interaction.response.send_message(
            "먼저 `/건의운영진역할설정`을 해주세요.",
            ephemeral=True,
        )
        return

    old_message_id = get_guild_value(interaction.guild.id, "suggestion_panel_message_id")
    if old_message_id:
        try:
            old_message = await channel.fetch_message(old_message_id)
            await old_message.edit(
                embed=make_suggestion_panel_embed(),
                view=SuggestionPanelView(),
            )
            await interaction.response.send_message(
                f"✅ 기존 건의 패널을 갱신했습니다: {channel.mention}",
                ephemeral=True,
            )
            return
        except (discord.NotFound, discord.Forbidden):
            pass

    message = await channel.send(
        embed=make_suggestion_panel_embed(),
        view=SuggestionPanelView(),
    )
    set_guild_value(interaction.guild.id, "suggestion_panel_message_id", message.id)

    await interaction.response.send_message(
        f"✅ 건의 패널 생성 완료: {channel.mention}",
        ephemeral=True,
    )


@bot.tree.command(name="모집채널설정")
@app_commands.checks.has_permissions(administrator=True)
async def 모집채널설정(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_value(interaction.guild.id, "recruit_channel_id", channel.id)
    await interaction.response.send_message(f"모집 채널 설정 완료: {channel.mention}", ephemeral=True)


@bot.tree.command(name="일정채널설정")
@app_commands.checks.has_permissions(administrator=True)
async def 일정채널설정(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_value(interaction.guild.id, "schedule_channel_id", channel.id)
    set_guild_value(interaction.guild.id, "schedule_message_id", None)
    await update_weekly_schedule(interaction.guild.id)
    await interaction.response.send_message(
        f"✅ 이번 주 일정 채널 설정 완료: {channel.mention}\n"
        "수요일~화요일 기준으로 일정판이 자동 갱신됩니다.",
        ephemeral=True,
    )


@bot.tree.command(name="모집기록채널설정")
@app_commands.checks.has_permissions(administrator=True)
async def 모집기록채널설정(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_value(interaction.guild.id, "archive_channel_id", channel.id)
    await interaction.response.send_message(f"모집 기록 채널 설정 완료: {channel.mention}", ephemeral=True)


@bot.tree.command(name="모집패널생성")
@app_commands.checks.has_permissions(administrator=True)
async def 모집패널생성(interaction: discord.Interaction):
    channel_id = get_guild_value(interaction.guild.id, "recruit_channel_id")
    target_channel = interaction.guild.get_channel(channel_id) if channel_id else None
    if not target_channel:
        await interaction.response.send_message("먼저 `/모집채널설정`을 해주세요.", ephemeral=True)
        return

    embed = discord.Embed(
        title="⚔️ 사뭇 레이드 모집",
        description=(
            "같이 갈 레이드를 모집하거나 이번 주 일정을 확인해보세요.\n\n"
            "**모든 길드원이 자유롭게 모집을 만들 수 있습니다.**\n"
            "본인이 만든 모집은 직접 수정하거나 마감할 수 있습니다."
        ),
        color=discord.Color.blurple(),
    )
    await target_channel.send(embed=embed, view=RecruitmentBoardView())
    await interaction.response.send_message(f"✅ 모집 패널 생성 완료: {target_channel.mention}", ephemeral=True)


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


@bot.tree.command(name="손님역할설정")
@app_commands.checks.has_permissions(administrator=True)
async def 손님역할설정(interaction: discord.Interaction, role: discord.Role):
    set_guild_value(interaction.guild.id, "guest_role_id", role.id)
    await interaction.response.send_message(f"손님 역할 설정 완료: {role.mention}", ephemeral=True)


@bot.tree.command(name="인증패널생성")
@app_commands.checks.has_permissions(administrator=True)
async def 인증패널생성(interaction: discord.Interaction):
    channel_id = get_guild_value(interaction.guild.id, "verify_channel_id")
    target_channel = interaction.guild.get_channel(channel_id) if channel_id else interaction.channel
    embed = discord.Embed(
        title="🌱 사뭇 서버 입장 안내",
        description=(
            "아래 버튼을 선택해주세요.\n\n"
            "🌱 **길드원 인증**\n→ 길드 가입 및 내부 활동을 원하는 분\n\n"
            "🎫 **손님 입장**\n→ 외부 공대 / 지인 / 놀러오신 분\n\n"
            "※ 길드원 인증 시 작성한 소개는\n🌱｜신입소개 채널에 자동 업로드됩니다."
        ),
        color=discord.Color.green(),
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
        color=discord.Color.blurple(),
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
    names = "\n".join(
        f"{c['CharacterName']} / {c.get('CharacterClassName', '직업없음')} / Lv.{get_item_level(c)}"
        for c in roster[:25]
    )
    await interaction.response.send_message(names, ephemeral=True)


@bot.tree.command(name="캐릭터초기화")
async def 캐릭터초기화(interaction: discord.Interaction):
    clear_roster(interaction.user.id)
    await interaction.response.send_message("캐릭터 초기화 완료", ephemeral=True)


@bot.tree.command(name="모집")
async def 모집(interaction: discord.Interaction):
    embed = discord.Embed(
        title="➕ 새 레이드 모집",
        description=(
            "레이드 → 난이도 → 숙련도 → 인원 → 날짜/시간 순서로 설정해주세요.\n\n"
            "모든 길드원이 사용할 수 있습니다."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=RaidSetupView(), ephemeral=True)

# =========================
# Events
# =========================
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
    bot.add_view(RecruitmentBoardView())
    bot.add_view(SuggestionPanelView())

    for thread_id, message_id in get_open_suggestions():
        try:
            bot.add_view(
                SuggestionCompleteView(thread_id),
                message_id=message_id,
            )
        except Exception as e:
            print(f"건의 처리 버튼 persistent view 등록 실패 {thread_id}: {e}")

    recruitments.clear()
    recruitments.update(load_recruitments_from_db())

    for message_id, data in list(recruitments.items()):
        if recruitment_status(data) == "completed":
            try:
                await finalize_recruitment(message_id, data)
            except Exception as e:
                print(f"완료 모집 정리 실패 {message_id}: {e}")
            continue

        try:
            bot.add_view(RecruitView(message_id), message_id=message_id)
        except Exception as e:
            print(f"모집 persistent view 등록 실패 {message_id}: {e}")
        start_recruitment_tasks(message_id)

    cursor.execute(
        "SELECT guild_id FROM guild_settings WHERE schedule_channel_id IS NOT NULL"
    )
    for (guild_id,) in cursor.fetchall():
        try:
            await update_weekly_schedule(guild_id)
        except Exception as e:
            print(f"주간 일정 초기 갱신 실패 ({guild_id}): {e}")

    if not weekly_schedule_refresh_loop.is_running():
        weekly_schedule_refresh_loop.start()

    try:
        synced = await bot.tree.sync()
        print(f"슬래시 명령어 동기화 완료: {len(synced)}개")
    except Exception as e:
        print(e)

    print(f"{bot.user} 로그인 완료")


if not TOKEN:
    raise RuntimeError("TOKEN 환경변수가 설정되지 않았습니다.")

bot.run(TOKEN)

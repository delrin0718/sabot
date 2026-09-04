import os
import json
import asyncio
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands
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

    data["closed"] = True
    save_recruitment(message_id, data)
    await update_recruit_message(message_id)
    await archive_recruitment(data)
    await refresh_weekly_schedule(data["guild_id"])


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
# Weekly schedule board
# =========================
KOREAN_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def weekly_range(now):
    # 사뭇 레이드 일정 기준: 수요일 10:00 ~ 다음 주 화요일 05:00
    # 화요일 05:00 이후 ~ 수요일 10:00 이전은 다음 주 일정 시작 전 공백 시간으로 처리합니다.
    # Python weekday(): 월=0, 화=1, 수=2, ... 일=6
    days_since_wednesday = (now.weekday() - 2) % 7
    start = (now - timedelta(days=days_since_wednesday)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    if now < start:
        start -= timedelta(days=7)
    end = (start + timedelta(days=6)).replace(
        hour=5, minute=0, second=0, microsecond=0
    )
    return start, end


def get_weekly_recruitments(guild_id):
    now = datetime.now(KST)
    start, end = weekly_range(now)
    items = []
    for message_id, data in recruitments.items():
        if data["guild_id"] != guild_id:
            continue
        if not (start <= data["start_time"] < end):
            continue
        if recruitment_status(data) == "completed":
            continue
        items.append((message_id, data))
    return sorted(items, key=lambda x: x[1]["start_time"])


def make_weekly_schedule_embed(guild_id):
    now = datetime.now(KST)
    start, end = weekly_range(now)
    embed = discord.Embed(
        title="📅 이번 주 레이드 일정",
        description=f"**{start.strftime('%m/%d')} ~ {(end - timedelta(days=1)).strftime('%m/%d')}**",
        color=discord.Color.blurple(),
    )

    items = get_weekly_recruitments(guild_id)
    if not items:
        embed.add_field(
            name="등록된 일정이 없습니다",
            value="⚔️ 레이드 모집 채널에서 새 모집을 만들어보세요.",
            inline=False,
        )
        embed.set_footer(text="모집 생성 · 수정 · 참가 · 취소 시 자동 갱신됩니다.")
        return embed

    grouped = {}
    for message_id, data in items:
        day = data["start_time"].date()
        grouped.setdefault(day, []).append((message_id, data))

    for day in sorted(grouped):
        day_dt = grouped[day][0][1]["start_time"]
        lines = []
        for message_id, data in grouped[day]:
            total = len(data["dealer"]) + len(data["support"])
            max_total = data["max_dealer"] + data["max_support"]
            link = f"https://discord.com/channels/{data['guild_id']}/{data['channel_id']}/{message_id}"
            lines.append(
                f"**{data['start_time'].strftime('%H:%M')}**  {data['raid']} · {data['difficulty']}\n"
                f"{status_label(data)} · {data['skill']} · 👥 {total}/{max_total} · [모집글 바로가기]({link})"
            )
        weekday = KOREAN_WEEKDAYS[day_dt.weekday()]
        embed.add_field(
            name=f"{day_dt.strftime('%m월 %d일')} ({weekday})",
            value="\n\n".join(lines),
            inline=False,
        )

    embed.set_footer(text="모집 생성 · 수정 · 참가 · 취소 시 자동 갱신됩니다.")
    return embed


async def refresh_weekly_schedule(guild_id):
    channel_id = get_guild_value(guild_id, "schedule_channel_id")
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    embed = make_weekly_schedule_embed(guild_id)
    message_id = get_guild_value(guild_id, "schedule_message_id")

    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(embed=embed, content=None)
            return
        except (discord.NotFound, discord.Forbidden):
            pass

    msg = await channel.send(embed=embed)
    set_guild_value(guild_id, "schedule_message_id", msg.id)

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
            await refresh_weekly_schedule(data["guild_id"])
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
# Recruitment creation UI (step-by-step)
# =========================
def setup_embed(title, description):
    return discord.Embed(title=title, description=description, color=discord.Color.blurple())


class RaidStepSelect(discord.ui.Select):
    def __init__(self, state):
        self.state = state
        super().__init__(
            placeholder="레이드를 선택하세요",
            options=[discord.SelectOption(label=r, value=r) for r in RAIDS],
        )

    async def callback(self, interaction):
        self.state["raid"] = self.values[0]
        await interaction.response.edit_message(
            embed=setup_embed(
                "🎚️ 난이도 선택",
                f"⚔️ **{self.state['raid']}**\n\n난이도를 선택해 주세요."
            ),
            view=DifficultyStepView(self.state),
        )


class RaidStepView(discord.ui.View):
    def __init__(self, state=None):
        super().__init__(timeout=300)
        self.state = state or {}
        self.add_item(RaidStepSelect(self.state))


class DifficultyStepView(discord.ui.View):
    def __init__(self, state):
        super().__init__(timeout=300)
        self.state = state

    async def choose(self, interaction, value):
        self.state["difficulty"] = value
        await interaction.response.edit_message(
            embed=setup_embed(
                "🎯 숙련도 선택",
                f"⚔️ **{self.state['raid']} · {value}**\n\n모집 숙련도를 선택해 주세요."
            ),
            view=SkillStepView(self.state),
        )

    @discord.ui.button(label="노말", style=discord.ButtonStyle.secondary)
    async def normal(self, interaction, button):
        await self.choose(interaction, "노말")

    @discord.ui.button(label="하드", style=discord.ButtonStyle.secondary)
    async def hard(self, interaction, button):
        await self.choose(interaction, "하드")

    @discord.ui.button(label="나이트메어", style=discord.ButtonStyle.secondary)
    async def nightmare(self, interaction, button):
        await self.choose(interaction, "나이트메어")

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            embed=setup_embed("⚔️ 레이드 선택", "어떤 레이드를 모집하시겠어요?"),
            view=RaidStepView(self.state),
        )


class SkillStepView(discord.ui.View):
    def __init__(self, state):
        super().__init__(timeout=300)
        self.state = state

    async def choose(self, interaction, value):
        self.state["skill"] = value
        await interaction.response.edit_message(
            embed=setup_embed(
                "👥 모집 인원 선택",
                f"⚔️ **{self.state['raid']} · {self.state['difficulty']}**\n"
                f"🎯 **{value}**\n\n모집 인원을 선택해 주세요."
            ),
            view=PartyStepView(self.state),
        )

    @discord.ui.button(label="트라이", style=discord.ButtonStyle.secondary)
    async def try_btn(self, interaction, button):
        await self.choose(interaction, "트라이")

    @discord.ui.button(label="클경", style=discord.ButtonStyle.secondary)
    async def clear_btn(self, interaction, button):
        await self.choose(interaction, "클경")

    @discord.ui.button(label="반숙", style=discord.ButtonStyle.secondary)
    async def half_btn(self, interaction, button):
        await self.choose(interaction, "반숙")

    @discord.ui.button(label="숙련", style=discord.ButtonStyle.secondary)
    async def exp_btn(self, interaction, button):
        await self.choose(interaction, "숙련")

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            embed=setup_embed(
                "🎚️ 난이도 선택",
                f"⚔️ **{self.state['raid']}**\n\n난이도를 선택해 주세요."
            ),
            view=DifficultyStepView(self.state),
        )


class PartyStepView(discord.ui.View):
    def __init__(self, state):
        super().__init__(timeout=300)
        self.state = state

    async def choose(self, interaction, value):
        self.state["party_size"] = value
        await interaction.response.edit_message(
            embed=setup_embed(
                "🕘 출발 시간 설정",
                f"⚔️ **{self.state['raid']} · {self.state['difficulty']}**\n"
                f"🎯 **{self.state['skill']}**\n"
                f"👥 **{value}인**\n\n아래 버튼을 눌러 출발 날짜와 시간을 입력해 주세요."
            ),
            view=TimeStepView(self.state),
        )

    @discord.ui.button(label="4인", style=discord.ButtonStyle.secondary)
    async def four(self, interaction, button):
        await self.choose(interaction, 4)

    @discord.ui.button(label="8인", style=discord.ButtonStyle.secondary)
    async def eight(self, interaction, button):
        await self.choose(interaction, 8)

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            embed=setup_embed(
                "🎯 숙련도 선택",
                f"⚔️ **{self.state['raid']} · {self.state['difficulty']}**\n\n모집 숙련도를 선택해 주세요."
            ),
            view=SkillStepView(self.state),
        )


class CreateTimeModal(discord.ui.Modal, title="출발 시간 설정"):
    date = discord.ui.TextInput(label="날짜", placeholder="예: 2026-09-05", required=True)
    time = discord.ui.TextInput(label="시간", placeholder="예: 21:00", required=True)

    def __init__(self, state):
        super().__init__()
        self.state = state

    async def on_submit(self, interaction):
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

        self.state["start_time"] = dt
        await interaction.response.edit_message(
            embed=make_create_confirm_embed(self.state),
            view=CreateConfirmView(self.state),
        )


class TimeStepView(discord.ui.View):
    def __init__(self, state):
        super().__init__(timeout=300)
        self.state = state

    @discord.ui.button(label="🕘 시간 설정", style=discord.ButtonStyle.primary)
    async def time_btn(self, interaction, button):
        await interaction.response.send_modal(CreateTimeModal(self.state))

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            embed=setup_embed(
                "👥 모집 인원 선택",
                f"⚔️ **{self.state['raid']} · {self.state['difficulty']}**\n"
                f"🎯 **{self.state['skill']}**\n\n모집 인원을 선택해 주세요."
            ),
            view=PartyStepView(self.state),
        )


def make_create_confirm_embed(state):
    dt = state["start_time"]
    weekday = KOREAN_WEEKDAYS[dt.weekday()]
    return discord.Embed(
        title="⚔️ 모집 내용 확인",
        description=(
            f"**레이드**　{state['raid']}\n"
            f"**난이도**　{state['difficulty']}\n"
            f"**숙련도**　{state['skill']}\n"
            f"**출발**　{dt.strftime('%m/%d')} ({weekday}) {dt.strftime('%H:%M')}\n"
            f"**인원**　{state['party_size']}인\n\n"
            "이 내용으로 모집을 생성할까요?"
        ),
        color=discord.Color.blurple(),
    )


class CreateConfirmView(discord.ui.View):
    def __init__(self, state):
        super().__init__(timeout=300)
        self.state = state

    @discord.ui.button(label="✅ 모집 생성", style=discord.ButtonStyle.success)
    async def create_recruitment(self, interaction, button):
        channel_id = get_guild_value(interaction.guild.id, "recruit_channel_id")
        if not channel_id:
            await interaction.response.send_message("먼저 `/모집채널설정`을 해주세요.", ephemeral=True)
            return

        target_channel = interaction.guild.get_channel(channel_id)
        if not target_channel:
            await interaction.response.send_message("설정된 모집 채널을 찾을 수 없습니다.", ephemeral=True)
            return

        party_size = self.state["party_size"]
        limits = PARTY_LIMITS[party_size]
        data = {
            "raid": self.state["raid"],
            "difficulty": self.state["difficulty"],
            "skill": self.state["skill"],
            "party_size": party_size,
            "start_time": self.state["start_time"],
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
        await refresh_weekly_schedule(interaction.guild.id)

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ 모집 생성 완료",
                description=f"{target_channel.mention}에 모집을 만들었습니다.",
                color=discord.Color.green(),
            ),
            view=None,
        )

    @discord.ui.button(label="◀ 처음부터 수정", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction, button):
        await interaction.response.edit_message(
            embed=setup_embed("⚔️ 레이드 선택", "어떤 레이드를 모집하시겠어요?"),
            view=RaidStepView({}),
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
        await refresh_weekly_schedule(data["guild_id"])
        await interaction.response.send_message(
            f"✅ **{member_data['character']}** 참가 신청이 완료되었습니다.", ephemeral=True
        )

    @discord.ui.button(label="🗡️ 딜러", style=discord.ButtonStyle.danger)
    async def dealer(self, interaction, button):
        await self.apply_join(interaction, "dealer")

    @discord.ui.button(label="🎵 서포터", style=discord.ButtonStyle.success)
    async def support(self, interaction, button):
        await self.apply_join(interaction, "support")


def make_roster_list_embed(data):
    embed = discord.Embed(
        title=f"👥 {data['raid']} {data['difficulty']} 참가 명단",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name=f"🗡️ 딜러 {len(data['dealer'])}/{data['max_dealer']}",
        value=make_member_text(data["dealer"]),
        inline=False,
    )
    embed.add_field(
        name=f"🎵 서포터 {len(data['support'])}/{data['max_support']}",
        value=make_member_text(data["support"]),
        inline=False,
    )
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
        data["closed"] = True
        cancel_recruitment_tasks(self.message_id)
        save_recruitment(self.message_id, data)
        await update_recruit_message(self.message_id)
        await archive_recruitment(data)
        await refresh_weekly_schedule(data["guild_id"])
        await interaction.response.send_message("✅ 모집을 마감했습니다.", ephemeral=True)

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
        await refresh_weekly_schedule(guild_id)
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
        await refresh_weekly_schedule(data["guild_id"])
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
        await refresh_weekly_schedule(data["guild_id"])
        await interaction.response.send_message("✅ 참가를 취소했습니다.", ephemeral=True)

    @discord.ui.button(label="👀 명단", style=discord.ButtonStyle.secondary, custom_id="recruit_list")
    async def list_button(self, interaction, button):
        data = recruitments.get(self.message_id)
        if not data:
            await interaction.response.send_message("모집 정보를 찾을 수 없습니다.", ephemeral=True)
            return
        await interaction.response.send_message(embed=make_roster_list_embed(data), ephemeral=True)

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
def schedule_lines(items, limit=15):
    if not items:
        return "표시할 모집이 없습니다."
    lines = []
    for _, data in sorted(items, key=lambda x: x[1]["start_time"]):
        total = len(data["dealer"]) + len(data["support"])
        max_total = data["max_dealer"] + data["max_support"]
        lines.append(
            f"{status_label(data)} **{data['start_time'].strftime('%m/%d %H:%M')}** · "
            f"{data['raid']} {data['difficulty']} · {data['skill']} · 👥 {total}/{max_total} · <@{data['creator_id']}>"
        )
    if len(lines) > limit:
        lines = lines[:limit] + [f"… 외 {len(items) - limit}개"]
    return "\n".join(lines)


class RecruitmentBoardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ 새 모집 만들기", style=discord.ButtonStyle.primary, custom_id="board_create")
    async def create(self, interaction, button):
        embed = discord.Embed(
            title="⚔️ 레이드 선택",
            description="어떤 레이드를 모집하시겠어요?",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=RaidStepView(), ephemeral=True)

    @discord.ui.button(label="📅 이번 주 일정", style=discord.ButtonStyle.secondary, custom_id="board_week")
    async def week(self, interaction, button):
        now = datetime.now(KST)
        start, end = weekly_range(now)
        items = [
            (mid, data)
            for mid, data in recruitments.items()
            if data["guild_id"] == interaction.guild.id
            and start <= data["start_time"] < end
            and recruitment_status(data) != "completed"
        ]
        embed = discord.Embed(
            title="📅 이번 주 레이드 일정",
            description=schedule_lines(items),
            color=discord.Color.blurple(),
        )
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
# Slash commands
# =========================
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
    await refresh_weekly_schedule(interaction.guild.id)
    await interaction.response.send_message(
        f"이번 주 일정 채널 설정 완료: {channel.mention}\n일정판 메시지도 생성했습니다.",
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
        title="⚔️ 레이드 선택",
        description="어떤 레이드를 모집하시겠어요?",
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=RaidStepView(), ephemeral=True)

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

    recruitments.clear()
    recruitments.update(load_recruitments_from_db())

    for message_id, data in list(recruitments.items()):
        if recruitment_status(data) == "completed" and not data.get("closed"):
            data["closed"] = True
            save_recruitment(message_id, data)

        if not data.get("closed"):
            try:
                bot.add_view(RecruitView(message_id), message_id=message_id)
            except Exception as e:
                print(f"모집 persistent view 등록 실패 {message_id}: {e}")
            start_recruitment_tasks(message_id)

    for guild in bot.guilds:
        try:
            await refresh_weekly_schedule(guild.id)
        except Exception as e:
            print(f"일정판 갱신 실패 {guild.id}: {e}")

    try:
        synced = await bot.tree.sync()
        print(f"슬래시 명령어 동기화 완료: {len(synced)}개")
    except Exception as e:
        print(e)

    print(f"{bot.user} 로그인 완료")


if not TOKEN:
    raise RuntimeError("TOKEN 환경변수가 설정되지 않았습니다.")

bot.run(TOKEN)

import discord
import asyncio
import json
import os
from openai import OpenAI
from discord import app_commands

# -------------------------
# Env
# -------------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

focus_channel_raw = os.getenv("FOCUS_CHANNEL_ID")
FOCUS_CHANNEL_ID = int(focus_channel_raw) if focus_channel_raw else None

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")

# -------------------------
# Pricing
# -------------------------

COST_PER_1M_INPUT = 0.150
COST_PER_1M_OUTPUT = 0.600

MONTHLY_BUDGET_USD = 1.00
WARNING_THRESHOLD = 0.80

# -------------------------
# Cost persistence
# -------------------------

COST_FILE = "cost.json"

if os.path.exists(COST_FILE):
    with open(COST_FILE, "r", encoding="utf-8") as f:
        cost_data = json.load(f)
else:
    cost_data = {
        "total_cost": 0.0,
        "requests": 0,
        "tokens_used": 0,
    }

total_cost_usd = float(cost_data.get("total_cost", 0.0))
total_requests = int(cost_data.get("requests", 0))
total_tokens = int(cost_data.get("tokens_used", 0))


def save_cost():
    with open(COST_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "total_cost": total_cost_usd,
                "requests": total_requests,
                "tokens_used": total_tokens,
            },
            f,
            indent=2,
        )


# -------------------------
# Memory config
# -------------------------

MEMORY_FILE = "memory.json"
MAX_HISTORY = 15

if os.path.exists(MEMORY_FILE):
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        memory = json.load(f)
else:
    memory = {}


def save_memory():
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2, ensure_ascii=False)


# -------------------------
# User profiles
# -------------------------

PROFILE_FILE = "profiles.json"

if os.path.exists(PROFILE_FILE):
    with open(PROFILE_FILE, "r", encoding="utf-8") as f:
        profiles = json.load(f)
else:
    profiles = {"users": {}}


def save_profiles():
    with open(PROFILE_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)


def get_profile(user_id: int):
    uid = str(user_id)

    if uid not in profiles["users"]:
        profiles["users"][uid] = {"facts": []}

    return profiles["users"][uid]


# -------------------------
# Keywords that trigger memory learning
# -------------------------

MEMORY_KEYWORDS = [
    "i like",
    "i love",
    "i play",
    "my favorite",
    "i work",
    "i study",
    "i prefer",
    "i enjoy",
]

# -------------------------

ai = OpenAI(api_key=OPENAI_API_KEY)


class MyClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Global sync so the command can exist in servers + user installs.
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} global app commands")


intents = discord.Intents.default()
intents.message_content = True

client = MyClient(intents=intents)
tree = client.tree

# -------------------------


def calculate_cost(usage):
    input_cost = (usage.prompt_tokens / 1_000_000) * COST_PER_1M_INPUT
    output_cost = (usage.completion_tokens / 1_000_000) * COST_PER_1M_OUTPUT
    return input_cost + output_cost


# -------------------------
# Auto memory extraction
# -------------------------

async def extract_user_facts(user_id, text):
    lowered = text.lower()

    if not any(k in lowered for k in MEMORY_KEYWORDS):
        return

    response = await asyncio.to_thread(
        ai.chat.completions.create,
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Extract permanent facts about the user from the message. "
                    'Return a JSON array of strings. Example: ["User likes rhythm games"]. '
                    "If nothing useful, return []."
                ),
            },
            {"role": "user", "content": text},
        ],
        max_tokens=50,
    )

    try:
        content = response.choices[0].message.content or "[]"
        facts = json.loads(content)
        if not isinstance(facts, list):
            return
    except Exception:
        return

    if not facts:
        return

    profile = get_profile(user_id)

    for fact in facts:
        if isinstance(fact, str) and fact not in profile["facts"]:
            profile["facts"].append(fact)

    profile["facts"] = profile["facts"][-5:]
    save_profiles()


# -------------------------
# GPT response
# -------------------------

async def ask_gpt(channel_id, user_id, prompt):
    history = memory.get(str(channel_id), [])
    profile = get_profile(user_id)

    profile_lines = "\n".join(f"- {fact}" for fact in profile["facts"])
    profile_text = "Known user facts:\n" + (profile_lines if profile_lines else "- None yet")

    messages = [
        {
            "role": "system",
            "content": (
                "You are Lappland, a sarcastic but mostly helpful Discord bot. "
                "You answer questions clearly but often include humor, memes, or jokes."
            ),
        },
        {"role": "system", "content": profile_text},
        *history,
        {"role": "user", "content": prompt},
    ]

    response = await asyncio.to_thread(
        ai.chat.completions.create,
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=125,
    )

    reply = response.choices[0].message.content or "..."
    cost = calculate_cost(response.usage)
    return reply, cost, response.usage


# -------------------------

def maybe_append_warning(text):
    used_pct = total_cost_usd / MONTHLY_BUDGET_USD if MONTHLY_BUDGET_USD > 0 else 0
    remaining = MONTHLY_BUDGET_USD - total_cost_usd

    if used_pct >= WARNING_THRESHOLD:
        text += (
            f"\n\n⚠️ Budget warning — "
            f"${total_cost_usd:.4f} / ${MONTHLY_BUDGET_USD:.2f} used "
            f"(${remaining:.4f} left)"
        )

    return text


# -------------------------
# Shared response handler
# -------------------------

async def run_bot_response(channel_id, user_id, prompt, send_initial, edit_message):
    global total_cost_usd, total_requests, total_tokens

    if total_cost_usd >= MONTHLY_BUDGET_USD:
        await send_initial("❌ OpenAI budget exhausted. Message @Rain798377")
        return

    await extract_user_facts(user_id, prompt)

    msg = await send_initial("...")

    reply, cost, usage = await ask_gpt(channel_id, user_id, prompt)

    # Fake streaming via edits
    partial = ""
    last_update = asyncio.get_running_loop().time()

    for word in reply.split():
        partial += word + " "
        now = asyncio.get_running_loop().time()

        if now - last_update > 0.5:
            shown = maybe_append_warning(partial.strip()[:1900])
            await edit_message(msg, shown)
            last_update = now

    final_text = maybe_append_warning(reply[:1900])
    await edit_message(msg, final_text)

    # Save history after successful response
    history = memory.get(str(channel_id), [])
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": reply})
    memory[str(channel_id)] = history[-MAX_HISTORY:]
    save_memory()

    total_cost_usd = round(total_cost_usd + cost, 6)
    total_requests += 1
    total_tokens += int(getattr(usage, "total_tokens", 0))
    save_cost()

    print(
        f"[cost] +${cost:.6f} | total ${total_cost_usd:.6f} | "
        f"req {total_requests} | tokens {total_tokens}"
    )


# -------------------------

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    print(f"Loaded memory for {len(memory)} channels")
    print(f"Current spending: ${total_cost_usd:.6f}")


# -------------------------
# Slash / app command
# -------------------------

@tree.command(name="Lappland", description="Ask Lappland something")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(prompt="What do you want to ask?")
async def lappland_command(interaction: discord.Interaction, prompt: str):
    print("slash command fired")
    await interaction.response.defer()

    channel_id = interaction.channel_id or interaction.user.id

    async def send_initial(text):
        return await interaction.followup.send(text)

    async def edit_message(msg, text):
        await msg.edit(content=text)

    try:
        await run_bot_response(
            channel_id=channel_id,
            user_id=interaction.user.id,
            prompt=prompt,
            send_initial=send_initial,
            edit_message=edit_message,
        )
    except Exception as e:
        print(f"Slash command error: {type(e).__name__}: {e}")
        try:
            await interaction.followup.send("GPT error. @Rain798377")
        except Exception as inner_e:
            print(f"Followup send error: {type(inner_e).__name__}: {inner_e}")

@tree.context_menu(name="Ask Lappland")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ask_lappland_on_message(
    interaction: discord.Interaction,
    message: discord.Message
):
    await interaction.response.defer()

    replied_text = (message.content or "").strip()
    replied_author = message.author.display_name

    if replied_text:
        prompt = (
            f"{replied_author} said:\n"
            f"\"{replied_text}\"\n\n"
            f"User request:\nRespond to that message."
        )
    else:
        prompt = (
            f"{replied_author} sent a message with no text content.\n\n"
            f"User request:\nRespond to that message."
        )

    channel_id = interaction.channel_id or interaction.user.id

    async def send_initial(text):
        return await interaction.followup.send(text)

    async def edit_message(msg, text):
        await msg.edit(content=text)

    try:
        await run_bot_response(
            channel_id=channel_id,
            user_id=interaction.user.id,
            prompt=prompt,
            send_initial=send_initial,
            edit_message=edit_message,
        )
    except Exception as e:
        print(f"Context menu error: {e}")
        try:
            await interaction.followup.send("GPT error. @Rain798377")
        except Exception:
            pass

# -------------------------


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    prompt = None

    # Allow plain text DMs to the bot
    if isinstance(message.channel, discord.DMChannel):
        prompt = message.content.strip()

    # Focus channel in a server
    elif FOCUS_CHANNEL_ID is not None and message.channel.id == FOCUS_CHANNEL_ID:
        prompt = message.content.strip()

    # Mention trigger in a server / group channel
    elif client.user and client.user in message.mentions:
        prompt = message.content
        prompt = prompt.replace(f"<@{client.user.id}>", "")
        prompt = prompt.replace(f"<@!{client.user.id}>", "").strip()

    if not prompt:
        return

    if message.reference:
        try:
            replied_message = await message.channel.fetch_message(message.reference.message_id)

            replied_text = replied_message.content.strip()
            replied_author = replied_message.author.display_name

            if replied_text:
                prompt = (
                    f"{replied_author} said:\n"
                    f"\"{replied_text}\"\n\n"
                    f"User request:\n{prompt}"
                )
        except Exception as e:
            print(f"Failed to fetch replied message: {e}")

    async with message.channel.typing():
        try:
            async def send_initial(text):
                return await message.channel.send(text)

            async def edit_message(msg, text):
                await msg.edit(content=text)

            await run_bot_response(
                channel_id=message.channel.id,
                user_id=message.author.id,
                prompt=prompt,
                send_initial=send_initial,
                edit_message=edit_message,
            )

        except Exception as e:
            print(f"Message handler error: {e}")
            await message.channel.send("GPT error. @Rain798377")


# -------------------------

client.run(DISCORD_TOKEN)
import discord
import asyncio
import json
import os
from openai import OpenAI
from discord import app_commands

FOCUS_CHANNEL_ID = int(os.getenv("FOCUS_CHANNEL_ID"))
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

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
    with open(COST_FILE, "r") as f:
        cost_data = json.load(f)
else:
    cost_data = {
        "total_cost": 0.0,
        "requests": 0,
        "tokens_used": 0
    }

total_cost_usd = cost_data["total_cost"]
total_requests = cost_data["requests"]
total_tokens = cost_data["tokens_used"]


def save_cost():
    with open(COST_FILE, "w") as f:
        json.dump({
            "total_cost": total_cost_usd,
            "requests": total_requests,
            "tokens_used": total_tokens
        }, f, indent=2)

# -------------------------
# Memory config
# -------------------------

MEMORY_FILE = "memory.json"
MAX_HISTORY = 15

if os.path.exists(MEMORY_FILE):
    with open(MEMORY_FILE, "r", encoding="utf8") as f:
        memory = json.load(f)
else:
    memory = {}

def save_memory():
    with open(MEMORY_FILE, "w", encoding="utf8") as f:
        json.dump(memory, f, indent=2)

# -------------------------
# User profiles
# -------------------------

PROFILE_FILE = "profiles.json"

if os.path.exists(PROFILE_FILE):
    with open(PROFILE_FILE, "r") as f:
        profiles = json.load(f)
else:
    profiles = {"users": {}}

def save_profiles():
    with open(PROFILE_FILE, "w") as f:
        json.dump(profiles, f, indent=2)

def get_profile(user_id):
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
    "i enjoy"
]

# -------------------------

ai = OpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# create app command tree
tree = app_commands.CommandTree(client)

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
                "content":
                "Extract permanent facts about the user from the message. "
                "Return JSON list. Example: [\"User likes rhythm games\"]. "
                "If nothing useful return []."
            },
            {"role": "user", "content": text}
        ],
        max_tokens=50
    )

    try:
        facts = json.loads(response.choices[0].message.content)
    except:
        return

    if not facts:
        return

    profile = get_profile(user_id)

    for fact in facts:
        if fact not in profile["facts"]:
            profile["facts"].append(fact)

    profile["facts"] = profile["facts"][-5:]
    save_profiles()

# -------------------------
# GPT STREAMING
# -------------------------

async def ask_gpt_stream(channel_id, user_id, prompt):
    history = memory.get(str(channel_id), [])
    profile = get_profile(user_id)

    profile_text = "Known user facts:\n"
    for fact in profile["facts"]:
        profile_text += f"- {fact}\n"

    messages = [
        {
            "role": "system",
            "content":
            "You are Lappland, a sarcastic but mostly helpful Discord bot. "
            "You answer questions clearly but often include humor, memes, or jokes."
        },
        {"role": "system", "content": profile_text}
    ] + history + [
        {"role": "user", "content": prompt}
    ]

    response = await asyncio.to_thread(
        ai.chat.completions.create,
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=125
    )

    reply = response.choices[0].message.content
    cost = calculate_cost(response.usage)

    partial = ""
    last_update = asyncio.get_event_loop().time()

    for word in reply.split():
        partial += word + " "
        now = asyncio.get_event_loop().time()

        if now - last_update > 0.5:
            yield partial.strip(), None
            last_update = now

    yield reply, cost

    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": reply})

    history = history[-MAX_HISTORY:]
    memory[str(channel_id)] = history
    save_memory()

# -------------------------

def maybe_append_warning(text):
    used_pct = total_cost_usd / MONTHLY_BUDGET_USD
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
    global total_cost_usd

    if total_cost_usd >= MONTHLY_BUDGET_USD:
        await send_initial("❌ OpenAI budget exhausted. Message @Rain798377")
        return

    await extract_user_facts(user_id, prompt)

    cost = None
    msg = await send_initial("...")

    async for partial, c in ask_gpt_stream(channel_id, user_id, prompt):
        if len(partial) > 1900:
            partial = partial[:1900]

        await edit_message(msg, maybe_append_warning(partial))

        if c is not None:
            cost = c

    if cost is not None:
        total_cost_usd = round(total_cost_usd + cost, 6)
        save_cost()
        print(f"[cost] +${cost:.6f} | total ${total_cost_usd:.6f}")

# -------------------------

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    print(f"Loaded memory for {len(memory)} channels")
    print(f"Current spending: ${total_cost_usd:.6f}")

    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} app commands")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

# -------------------------
# Slash command / app command
# -------------------------

@tree.command(name="lappland", description="Ask Lappland something")
@app_commands.describe(prompt="What do you want to ask?")
async def lappland_command(interaction: discord.Interaction, prompt: str):
    async def send_initial(text):
        await interaction.response.send_message(text)
        return await interaction.original_response()

    async def edit_message(msg, text):
        await msg.edit(content=text)

    try:
        await run_bot_response(
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            prompt=prompt,
            send_initial=send_initial,
            edit_message=edit_message
        )
    except Exception as e:
        print(e)
        if interaction.response.is_done():
            await interaction.followup.send("GPT error. @Rain798377")
        else:
            await interaction.response.send_message("GPT error. @Rain798377")

# -------------------------

@client.event
async def on_message(message):
    if message.author.bot:
        return

    prompt = None

    if message.channel.id == FOCUS_CHANNEL_ID:
        prompt = message.content

    elif client.user in message.mentions:
        prompt = message.content
        prompt = prompt.replace(f"<@{client.user.id}>", "")
        prompt = prompt.replace(f"<@!{client.user.id}>", "").strip()

    if not prompt:
        return

    if message.reference:
        try:
            replied_message = await message.channel.fetch_message(
                message.reference.message_id
            )

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
                edit_message=edit_message
            )

        except Exception as e:
            print(e)
            await message.channel.send("GPT error. @Rain798377")

# -------------------------

client.run(DISCORD_TOKEN)
import discord
import asyncio
import json
import os
from openai import OpenAI
import os

FOCUS_CHANNEL_ID = int(os.getenv("FOCUS_CHANNEL_ID"))

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

ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

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

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    print(f"Loaded memory for {len(memory)} channels")
    print(f"Current spending: ${total_cost_usd:.6f}")

# -------------------------

@client.event
async def on_message(message):
    global total_cost_usd

    if message.author.bot:
        return

    if total_cost_usd >= MONTHLY_BUDGET_USD:
        await message.channel.send("❌ OpenAI budget exhausted. Message @Rain798377")
        return

    prompt = None

    if message.channel.id == int(os.getenv("FOCUS_CHANNEL_ID")):
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

    await extract_user_facts(message.author.id, prompt)

    async with message.channel.typing():
        try:
            msg = await message.channel.send("...")

            cost = None

            async for partial, c in ask_gpt_stream(
                message.channel.id,
                message.author.id,
                prompt
            ):
                if len(partial) > 1900:
                    break

                await msg.edit(content=maybe_append_warning(partial))

                if c is not None:
                    cost = c

            if cost is not None:
                total_cost_usd = round(total_cost_usd + cost, 6)
                save_cost()

                print(f"[cost] +${cost:.6f} | total ${total_cost_usd:.6f}")

        except Exception as e:
            print(e)
            await message.channel.send("GPT error. @Rain798377")

# -------------------------

client.run(os.getenv("DISCORD_TOKEN"))
# Setup guide

End-to-end instructions to install Mythic, Epoch, and Chronos and get a working callback.

If something fails, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Step 1 — What you need

1. **Mythic host** — Ubuntu 22.04/24.04 (local VM, cloud VM, or WSL2). **4 GB RAM minimum** (8 GB recommended), 2+ vCPU, 20+ GB disk.
2. **Agent host** — Any machine with Python 3.8+ (can be the same as the Mythic host for a self-test).
3. **GCP project** — Google Cloud project with billing enabled (Calendar API has a free tier; labs use very little quota).
4. **Browser** — Access to Mythic UI on port **7443** (`https://<mythic-host>:7443`).

---

## Step 2 — Google Calendar and service account

### 2.1 Create or select a GCP project

1. Open [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project or select an existing lab project

### 2.2 Enable the Calendar API

1. **APIs & Services → Library**
2. Search **Google Calendar API** → **Enable**

### 2.3 Create a service account and download JSON key

1. **APIs & Services → Credentials → Create credentials → Service account**
2. Name it (e.g. `epoch-chronos-lab`)
3. Open the service account → **Keys → Add key → JSON**
4. Save the file privately (e.g. `~/lab-credentials.json`). **Never commit it.**

Note the service account email:

```text
your-sa@YOUR_PROJECT.iam.gserviceaccount.com
```

**If key creation fails** with `iam.disableServiceAccountKeyCreation`:

1. **IAM & Admin → Organization Policies** (or project policy)
2. Find **Disable service account key creation**
3. Set to **Not enforced** for your lab project, or request an org exception
4. Retry **Add key → JSON**

### 2.4 Create and share a calendar

1. Open [Google Calendar](https://calendar.google.com/) as a normal Google user
2. **Settings → Add calendar** — create a lab-only calendar
3. **Share with specific people** → add the **service account email**
4. Permission: **Make changes to events**

### 2.5 Copy the full calendar ID

In that calendar’s settings, find **Calendar ID**. It looks like:

```text
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx@group.calendar.google.com
```

Paste this exact string into Epoch later. Use the **full ID** from Google Calendar settings on both Epoch and the built Chronos agent.

---

## Step 3 — Install Mythic

On the Mythic host:

```bash
sudo apt update
sudo apt install -y ca-certificates curl git build-essential

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in so the `docker` group applies, then:

```bash
cd ~
git clone https://github.com/its-a-feature/Mythic.git
cd Mythic
sudo make
sudo ./mythic-cli install
sudo ./mythic-cli start
sudo ./mythic-cli status
```

Open `https://<mythic-host>:7443` and log in.

**VM vs cloud:** On a local VM, use bridged networking or port-forward **7443** to the guest. On a cloud VM, open **7443** (and **22** for SSH) in the security group / firewall.

---

## Step 4 — Get this repository

On the Mythic host:

```bash
cd ~
git clone https://github.com/0xNirvana/mythic-epoch-chronos.git
export REPO=~/mythic-epoch-chronos
```

Or download the repo as a ZIP and extract it — set `REPO` to that path.

SSH clone:

```bash
git clone git@github.com:0xNirvana/mythic-epoch-chronos.git
export REPO=~/mythic-epoch-chronos
```

---

## Step 5 — Install Epoch and Chronos

There are **two different `config.json` files** in this project:

| Path | Purpose |
|------|---------|
| `$REPO/install/epoch/config.json` | Mythic **install wrapper** (tells mythic-cli what to package) |
| `~/Mythic/InstalledServices/epoch/c2_code/config.json` | **Runtime server config** for `server.py` — created later in Step 7 |

From your Mythic directory:

```bash
cd ~/Mythic
bash "$REPO/scripts/sync_install_trees.sh"

sudo ./mythic-cli install folder "$REPO/install/epoch"
sudo ./mythic-cli install folder "$REPO/install/chronos"

sudo ./mythic-cli status
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'epoch|chronos|NAME'
```

**Do not** point `mythic-cli` at `$REPO/epoch` or `$REPO/chronos` directly — those folders lack the install wrapper.

In Mythic UI → **Installed Services**:

- Under **C2**, you should see **epoch**
- Under **Payload Types**, you should see **chronos**

After code updates, re-run `sync_install_trees.sh` and both `mythic-cli install folder` commands.

---

## Step 6 — Configure Epoch

1. Mythic UI → **Installed Services → C2 → epoch**
2. Create or edit instance **`default`**
3. Set parameters:

| Parameter | Value |
|-----------|--------|
| `calendar_id` | Full shared calendar ID from Step 2.5 |
| `credentials_file` | Upload the service account JSON |
| `poll_interval` | `10`–`15` recommended for labs |
| `callback_jitter` | `20` |
| `AESPSK` | `aes256_hmac` |
| `debug` | `true` while learning |

4. Click **Save** (disk icon)

**Important:** Save stores settings in Mythic’s database only. It does **not** write `c2_code/config.json`. Do not start the profile yet.

---

## Step 7 — Build a Chronos payload

Building a payload runs Epoch **config check**, which writes runtime files:

- `~/Mythic/InstalledServices/epoch/c2_code/config.json`
- `~/Mythic/InstalledServices/epoch/c2_code/credentials.json`

1. Mythic UI → **Create Payload**
2. Payload type: **chronos**
3. C2 profile: **epoch** (instance `default`)
4. Build parameters: `version` **3.10**, `debug` **on**, `output_type` **script**
5. Build and download the `.py` file

Verify the runtime config exists and is valid JSON:

```bash
cat ~/Mythic/InstalledServices/epoch/c2_code/config.json
```

You should see JSON with your `calendar_id`. The file must not be empty (0 bytes).

If config check fails, re-upload a fresh credentials JSON in Step 6 and rebuild.

---

## Step 8 — Start Epoch

1. **Installed Services → C2 → epoch** → **Start**
2. If you changed `calendar_id` or credentials after a previous start: **Stop → Start** to reload config

Confirm the server picked up your calendar (do not rely on `docker logs epoch` alone — RabbitMQ noise is normal):

```bash
tail -20 ~/Mythic/InstalledServices/epoch/c2_code/server.log
```

Look for `Using calendar: xxx@group.calendar.google.com` — not a stale or missing calendar ID.

---

## Step 9 — Run the agent

On the target (WSL, Linux, or macOS):

```bash
python3 -m pip install --user \
  google-api-python-client google-auth google-auth-oauthlib \
  google-auth-httplib2 pycryptodome

python3 ./your_chronos_payload.py
```

**Wait 30 seconds to 4 minutes** for the first callback. Calendar API visibility lag is normal.

Success: a **Chronos callback** appears in Mythic. Task **`whoami`** first — one command at a time until the callback is stable.

**Cold start vs resume:** The agent stores its callback ID in `~/.chronos_<payload8>`. Delete that file (or build a new payload) for a brand-new callback.

---

## Step 10 — Expect this

- **Latency** — Tens of seconds per task round trip; not TCP-beacon speed.
- **First check-in** — Can take up to ~4 minutes in worst-case Calendar lag.
- **Files** — Download/upload practical max ~500 KB, single-shot.
- **Scale** — One shared calendar supports roughly 10–20 agents before quota gets tight.
- **Config changes** — After changing Epoch parameters: **Stop → Start** Epoch, then **rebuild** Chronos so the agent embeds the new calendar ID and credentials.

---

## Optional: event HMAC

Generate a shared secret:

```bash
python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
```

Set the same value in Epoch `event_hmac_key` before building the agent.

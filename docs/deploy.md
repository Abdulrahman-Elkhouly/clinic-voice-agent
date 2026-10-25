# Deploying the agent to a server

The agent (`agent/agent.py`) runs 24/7 on a Linux server (Oracle Cloud, Always Free). It only makes outgoing
connections (LiveKit Cloud, Soniox, Gemini, OpenAI, Voyage, Qdrant), so the server needs no open ports except SSH.
The web page (`backend/server.py`) stays on the laptop for now: it talks to the deployed agent through LiveKit.

- **Code** reaches the server through GitHub (private repo): push on the laptop, pull on the server.
- **Keys** (`.env`) are copied straight to the server, never through GitHub.
- **The clinic database** (`data/clinic.db`) is created on the server once and kept there (the server's disk keeps
  it across restarts and updates).

The same steps work on WSL (Ubuntu on the laptop), which is how they were rehearsed.

---

## 1. Create the server (Oracle Cloud)
1. cloud.oracle.com, "Start for free". **Home region: Germany Central (Frankfurt)**, next to the Qdrant cluster.
   It can't be changed later.
2. Compute, Instances, Create instance:
   - Image: **Ubuntu** (22.04 or newer; the setup installs Python 3.10 itself).
   - Shape: Ampere **VM.Standard.A1.Flex**, **2 OCPUs, 12 GB** (Always Free allows up to 4 and 24).
   - SSH keys: "Generate a key pair for me", **download the private key** and keep it safe.
   - "Out of capacity": try again later, or another availability domain. Not the 1 GB Micro shape (too small).
3. Note the instance's **public IP address** once it's Running.

## 2. Log in from the laptop (PowerShell)
Put the key somewhere safe, for example `C:\Users\<you>\.ssh\oracle.key`, then:
```powershell
icacls $HOME\.ssh\oracle.key /inheritance:r /grant:r "$($env:USERNAME):R"   # ssh refuses keys others can read
ssh -i $HOME\.ssh\oracle.key ubuntu@<public-ip>
```
Everything below runs **on the server** (the prompt shows `ubuntu@...`), unless it says "on the laptop".

## 3. Install the tools
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install git
curl -LsSf https://astral.sh/uv/install.sh | sh      # uv: installs Python 3.10 and the packages
source ~/.local/bin/env
```

## 4. Get the code from GitHub
The repo is private, so the server gets its own read-only key ("deploy key"):
```bash
ssh-keygen -t ed25519 -f ~/.ssh/github -N ""
cat ~/.ssh/github.pub
```
On GitHub: the repo, Settings, Deploy keys, Add deploy key, paste it, **leave "Allow write access" off**. Then:
```bash
printf 'Host github.com\n  IdentityFile ~/.ssh/github\n' >> ~/.ssh/config
git clone git@github.com:Abdulrahman-Elkhouly/clinic-voice-agent.git ~/clinic-voice-agent
cd ~/clinic-voice-agent
```

## 5. Python and packages
```bash
uv venv --python 3.10 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## 6. Keys
**On the laptop** (PowerShell, in the project folder):
```powershell
scp -i $HOME\.ssh\oracle.key .env ubuntu@<public-ip>:~/clinic-voice-agent/.env
```
On the server, so only you can read it:
```bash
chmod 600 ~/clinic-voice-agent/.env
```

## 7. Database and first run
```bash
.venv/bin/python agent/seed_db.py --reset        # once: doctors, schedules, fake patients
.venv/bin/python agent/check_schedules.py        # the knowledge base and the database agree
.venv/bin/python agent/agent.py download-files   # models the agent needs, fetched once
.venv/bin/python agent/agent.py start            # test run: wait for "registered worker", then Ctrl+C
```
Never run `seed_db.py --reset` again on the server once it's in use: it deletes every booking.

## 8. Run it 24/7
**Stop the agent on the laptop first** (otherwise both answer calls).
```bash
sudo cp deploy/clinic-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now clinic-agent      # starts now and at every boot
systemctl status clinic-agent                 # should say "active (running)"
```
Then on the laptop: `python backend\server.py`, open http://localhost:8000, and call.

---

## Every day
| What | Command (on the server) |
|---|---|
| Live log | `journalctl -u clinic-agent -f` (Ctrl+C to stop watching; the agent keeps running) |
| Last hour of log | `journalctl -u clinic-agent --since "1 hour ago"` |
| Restart | `sudo systemctl restart clinic-agent` |
| Stop / start | `sudo systemctl stop clinic-agent` / `sudo systemctl start clinic-agent` |

## Updating
On the laptop: commit and push. Then on the server:
```bash
cd ~/clinic-voice-agent
git pull
uv pip install --python .venv/bin/python -r requirements.txt   # only if requirements.txt changed
sudo systemctl restart clinic-agent       # waits for calls in progress to finish (up to 10 minutes)
```
After editing `knowledge_base/`, run `index_kb.py` once (from the laptop or the server; it's the same cloud index).
After changing a fixed line or the voice, re-record it on the laptop (`prerecord_voice.py`), commit `assets/`, pull.

## Backing up the database
```bash
sqlite3 ~/clinic-voice-agent/data/clinic.db ".backup '/home/ubuntu/clinic-backup.db'"   # sudo apt install sqlite3
```
On the laptop, to keep a copy: `scp -i $HOME\.ssh\oracle.key ubuntu@<public-ip>:clinic-backup.db .`
(Fake patients only for now; with real data, backups must follow the privacy rules.)

# Deploying TopHat (free GCP VM + auto-deploy on push)

Hosts the dashboard 24/7 at `https://your-domain`, and redeploys automatically on
every push to `master` via [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml).

**Model:** a single always-on VM running the app under `systemd`, with Caddy in
front for automatic HTTPS. State lives in `/opt/tophat/data` (the session key,
encrypted API keys, account states, login users) and is **never touched by
deploys** (`git reset --hard` leaves untracked/gitignored files alone).

The script [`setup.sh`](setup.sh) does all the on-box work. Everything below is the
part tied to *your* accounts.

---

## 0. Commit these files first

The VM clones the repo to run `setup.sh`, so push them to `master`:

```bash
git add deploy/ .github/
git commit -m "Add deploy scripts"
git push origin master
```

---

## 1. Create the free VM (GCP)

1. Console → create/select a project, enable billing (needs a card for identity;
   the free tier doesn't charge within limits).
2. **Compute Engine → VM instances → Create instance:**
   - **Region:** `us-east1` (low latency to TopstepX/CME; free-tier eligible).
   - **Machine type:** `e2-micro` (the always-free shape — 1 per month in
     us-east1/us-west1/us-central1).
   - **Boot disk:** Ubuntu 24.04 LTS, 30 GB standard (free tier covers 30 GB).
   - **Firewall:** check **Allow HTTP traffic** and **Allow HTTPS traffic**.
   - Create.
3. Recommended: **VPC network → IP addresses → reserve** the instance's external
   IP as static, so it survives reboots (free while attached to a running VM).

## 2. Point a domain at it

Caddy needs a real domain to issue HTTPS (Let's Encrypt won't certify a bare IP).

- Have a domain? Add an **A record** → the VM's external IP.
- No domain? Free option: create a subdomain at <https://www.duckdns.org> (e.g.
  `yourname.duckdns.org`) pointing at the IP.

## 3. Run the bootstrap (one command)

SSH into the VM (the **SSH** button in the GCP console works), then:

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/<you>/<repo>.git /opt/tophat
sudo chown -R "$USER":"$USER" /opt/tophat
sudo DOMAIN=yourname.duckdns.org REPO=https://github.com/<you>/<repo>.git \
     bash /opt/tophat/deploy/setup.sh
```

> Private repo? Clone with a [GitHub token](https://docs.github.com/authentication)
> or a deploy key first; the rest is identical.

## 4. Add your credentials, restart

```bash
sudo nano /opt/tophat/.env       # set PROJECTX_USERNAME/API_KEY + TOPHAT_ADMIN_*
sudo systemctl restart tophat
```

Visit **`https://your-domain`**, log in with the admin email/password from `.env`.
Your `.env` ProjectX key auto-imports into the encrypted store on first live start;
add more keys in **Settings → API keys**.

---

## 5. Wire up auto-deploy (GitHub secrets)

The workflow SSHes in and runs `git pull` + `systemctl restart tophat`. Give it a key:

```bash
# on your laptop:
ssh-keygen -t ed25519 -f tophat_deploy -N ""
# copy tophat_deploy.pub into the VM:
ssh-copy-id -i tophat_deploy.pub <you>@<VM-external-IP>
#   (or paste tophat_deploy.pub into the VM's ~/.ssh/authorized_keys)
```

In **GitHub → repo → Settings → Secrets and variables → Actions**, add:

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | VM external IP (or your domain) |
| `DEPLOY_USER` | your VM username (`echo $USER` on the VM) |
| `DEPLOY_SSH_KEY` | full contents of the **private** `tophat_deploy` file |
| `DEPLOY_PORT` | `22` (optional) |

Done. **Push to `master` → GitHub Actions deploys → live in ~30s.** Watch runs in
the repo's **Actions** tab.

---

## Operating it

```bash
systemctl status tophat        # is it running?
journalctl -u tophat -f        # live logs
sudo systemctl restart tophat  # manual restart
```

- **Arming automation:** `auto_execute` defaults OFF (safe). Turn it on in Settings
  only after you've validated on a practice account.
- **Backups:** the whole of `/opt/tophat/data` is your state — snapshot it, or copy
  `data/.secret` + `data/credentials.json` somewhere safe (losing `.secret` makes
  stored API keys unrecoverable).
- **Reliability note:** a free 1 GB VM is fine technically, but this trades real
  money — if uptime matters, a ~$5/mo box (Fly.io/Render) or a larger VM is cheap
  insurance. The setup is identical (it's just a Linux box).

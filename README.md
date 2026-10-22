# Immich Google Photos Importer

A small web page that copies a **Google Photos shared album** into [Immich](https://immich.app). Paste the album's share link (from your phone is fine) and every photo and video is downloaded in original quality, uploaded to Immich and put in an Immich album with the same name.

It's made for the "everyone dumps their trip photos into one shared Google album" situation: no Google Takeout, no CLI, no selecting photos one by one.

## Features

- **Paste a link, get an album.** Works with `photos.app.goo.gl/...` and `photos.google.com/share/...` links.
- **Original quality.** Full-resolution photos and original videos, with their original capture dates.
- **"Taken by" credits.** Each photo's Immich description is set to `Taken by <name>` using the contributor shown in the shared album. Existing descriptions are never overwritten.
- **Re-sync when people add more.** Imported albums are remembered, so a later sync only downloads the new photos and adds them to the same Immich album. It also fills in missing "Taken by" credits on photos imported earlier.
- **Album grid like Immich's.** Saved albums show their Immich cover photo; tap one to open it in Immich. There's a one-tap **Sync all** button, plus per-album Sync and Forget.
- **Looks like Immich.** Same colors, light/dark mode and icons as the Immich web app, and it works well on a phone.
- **Preview** an album's contents without importing anything.
- **No duplicates.** Immich de-duplicates by checksum, so re-importing is safe.
- Python standard library only; no dependencies.

## Install

You need Docker and a running Immich server.

### 1. Create an Immich API key

In Immich: **Account Settings → API Keys → New API Key**. Give it these permissions (or "All"):

`asset.upload`, `asset.read`, `asset.update`, `asset.view`, `album.create`, `album.read`, `albumAsset.create`

(`asset.view` is only used to show album cover thumbnails.)

Photos are imported into the library of the user who owns the key.

### 2a. Docker Compose

```sh
git clone https://github.com/CKraft11/immich-google-importer.git
cd immich-google-importer
cp .env.example .env
nano .env              # set IMMICH_URL and IMMICH_API_KEY
docker compose up -d
```

### 2b. Portainer

**Stacks → Add stack → Repository**, repository URL `https://github.com/CKraft11/immich-google-importer`, compose path `docker-compose.yml`. Add the environment variables below, then deploy.

Or use the **Web editor**: paste `docker-compose.yml`, add the variables and deploy. The compose file builds the image straight from GitHub, so no other files are needed.

### 3. Open it

Go to `http://<your-server>:8098`, paste a shared-album link and tap **Import**.

On a phone, bookmark the page or add it to your home screen. You can also open it as `http://<your-server>:8098/?url=<album link>` to pre-fill the form.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `IMMICH_URL` | yes | | Address the container uses to reach Immich, e.g. `http://192.168.1.10:2283` or `http://immich_server:2283` if on the same Docker network. |
| `IMMICH_API_KEY` | yes | | Immich API key (see above). |
| `IMMICH_PUBLIC_URL` | no | `IMMICH_URL` | Immich address your browser uses, for the "open album" links. |
| `WORKERS` | no | `4` | Number of photos downloaded/uploaded in parallel. |
| `PORT` | no | `8098` | Host port for the web page (compose file). |

Import history is stored in `./data/state.json`. Delete an album's entry (or use **Forget** in the UI) to import it from scratch.

### Same Docker network as Immich

To reach Immich by container name instead of IP, attach the importer to Immich's network:

```yaml
services:
  immich-google-importer:
    networks: [immich_default]
networks:
  immich_default:
    external: true
```

and set `IMMICH_URL=http://immich_server:2283`.

## Updating

```sh
docker compose build --pull && docker compose up -d
```

In Portainer, use **Pull and redeploy** (repository stacks) or redeploy with "Re-pull image and redeploy".

## Caveats

- **Unofficial.** Google's Photos API no longer gives third-party apps access to shared albums, so this reads the album the same way the photos.google.com web page does. If Google changes that page, imports will break until this is updated.
- **Only works with albums shared by link.** The album must be viewable by anyone with the link.
- **No login on the web page.** Anyone who can reach the port can start imports into the API key owner's library. Keep it on your LAN/VPN or put it behind a reverse proxy with authentication. Don't expose it to the internet.
- Only import albums you have permission to copy.
